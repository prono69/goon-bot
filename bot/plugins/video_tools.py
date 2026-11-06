import io
import math
import time
import uuid
import random
import shutil
import asyncio
import logging
import contextlib
from functools import lru_cache
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont
from aiohttp import web

from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputRichMessage,
    InputRichMessageMedia,
    InputMediaPhoto,
)
from pyrogram.errors import FloodWait, MessageNotModified

logger = logging.getLogger(__name__)

# Temporary directory for generated output files
TEMP_DIR = Path("./downloads/video_tools")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# State storage for user sessions
VIDEO_SESSIONS: Dict[str, Dict[str, Any]] = {}

# Active stream jobs: { job_token: { "client": client, "message": message, "file_size": int, "mime_type": str } }
STREAM_JOBS: Dict[str, Dict[str, Any]] = {}

# Active users to prevent concurrent spam
ACTIVE_USERS: set = set()

# Strong references to background tasks so they are not garbage collected mid-run
BACKGROUND_TASKS: set = set()

# Internal local HTTP server state
HTTP_SERVER_RUNNER: Optional[web.AppRunner] = None
HTTP_SERVER_PORT = 19485

# Size of each slice written to the HTTP socket (Telegram still delivers 1 MB chunks)
SEND_CHUNK = 64 * 1024


# ==================== GENERIC HELPERS ====================

async def run_proc(cmd: List[str], timeout: float) -> Tuple[int, bytes, bytes]:
    """
    Run a subprocess, capture stdout/stderr, and ALWAYS kill it on timeout or cancellation.
    Raises asyncio.TimeoutError on timeout. stdin is closed so ffmpeg never waits on it.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    return proc.returncode, out, err


async def safe_edit(msg: Optional[Message], text: str) -> None:
    """Edit a message, ignoring 'not modified' and any other edit failure."""
    if msg is None:
        return
    try:
        await msg.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.debug(f"edit_text failed: {e}")


async def safe_delete(msg: Optional[Message]) -> None:
    if msg is None:
        return
    with contextlib.suppress(Exception):
        await msg.delete()


async def safe_answer(callback: CallbackQuery, text: Optional[str] = None, show_alert: bool = False) -> None:
    """Answer a callback query without letting a stale/invalid query abort the handler."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception as e:
        logger.debug(f"callback.answer failed: {e}")


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


# ==================== LOCAL HTTP RANGE STREAMER ====================

def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """
    Parse a single HTTP Range header.
    Returns (start, end) inclusive for a valid range.
    Returns None when the header is absent/malformed/multi-range (serve the full body with 200).
    Raises ValueError when the range is syntactically valid but unsatisfiable (416).
    Supports open-ended (bytes=500-) and suffix (bytes=-500) ranges.
    """
    if not header or not header.startswith("bytes=") or "," in header:
        return None

    start_s, sep, end_s = header[len("bytes="):].strip().partition("-")
    if not sep:
        return None

    try:
        if start_s == "":
            suffix_len = int(end_s)
            if suffix_len <= 0:
                return None
            start = max(0, size - suffix_len)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None

    if start < 0 or start >= size or start > end:
        raise ValueError("unsatisfiable range")

    return start, min(end, size - 1)


async def handle_stream(request: web.Request) -> web.StreamResponse:
    """Handles HTTP GET and HEAD requests with Range header support for ffmpeg seeking."""
    job_token = request.match_info.get("token")
    job = STREAM_JOBS.get(job_token)
    if not job:
        return web.Response(status=404, text="Stream not found")

    client: Client = job["client"]
    message: Message = job["message"]
    file_size: int = job["file_size"]
    mime_type: str = job.get("mime_type", "video/mp4")

    if file_size <= 0:
        return web.Response(status=404, text="Unknown file size")

    try:
        byte_range = parse_range(request.headers.get("Range"), file_size)
    except ValueError:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})

    if byte_range:
        from_bytes, until_bytes = byte_range
        status_code = 206
    else:
        from_bytes, until_bytes = 0, file_size - 1
        status_code = 200

    req_length = (until_bytes - from_bytes) + 1

    headers = {
        "Content-Type": mime_type,
        "Accept-Ranges": "bytes",
        "Content-Length": str(req_length),
    }
    # Content-Range is only valid on 206 responses
    if status_code == 206:
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"

    response = web.StreamResponse(status=status_code, headers=headers)
    await response.prepare(request)

    if request.method == "HEAD":
        return response

    # Stream requested byte range from Telegram MTProto
    try:
        chunk_size = 1024 * 1024
        start_chunk = from_bytes // chunk_size
        end_chunk = until_bytes // chunk_size
        limit_chunks = (end_chunk - start_chunk) + 1

        bytes_to_skip = from_bytes % chunk_size
        bytes_left = req_length

        async for chunk in client.stream_media(message, offset=start_chunk, limit=limit_chunks):
            if bytes_to_skip > 0:
                if len(chunk) <= bytes_to_skip:
                    bytes_to_skip -= len(chunk)
                    continue
                chunk = chunk[bytes_to_skip:]
                bytes_to_skip = 0

            if len(chunk) > bytes_left:
                chunk = chunk[:bytes_left]

            if chunk:
                # Write in small slices so ffmpeg gets data sooner and memory copies stay small
                view = memoryview(chunk)
                for i in range(0, len(view), SEND_CHUNK):
                    await response.write(view[i:i + SEND_CHUNK])
                bytes_left -= len(chunk)

            if bytes_left <= 0:
                break

    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        logger.warning(f"Streaming error for job {job_token}: {e}")

    return response


async def ensure_http_server():
    """Starts the internal localhost HTTP streaming server if not already running."""
    global HTTP_SERVER_RUNNER
    if HTTP_SERVER_RUNNER is not None:
        return

    app = web.Application()
    app.router.add_get("/stream/{token}", handle_stream)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HTTP_SERVER_PORT)
    await site.start()
    HTTP_SERVER_RUNNER = runner
    logger.info(f"Internal Video Stream Server listening on http://127.0.0.1:{HTTP_SERVER_PORT}")


# ==================== VIDEO / IMAGE HELPERS ====================

def format_timestamp(seconds: float) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    sec = max(0, int(seconds))
    hrs = sec // 3600
    mins = (sec % 3600) // 60
    rem_sec = sec % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{rem_sec:02d}"
    return f"{mins:02d}:{rem_sec:02d}"


def get_video_info_from_message(message: Message) -> Optional[Dict[str, Any]]:
    """Check if message has a video or a video-like document."""
    video = message.video
    if video:
        return {
            "type": "video",
            "file_id": video.file_id,
            "file_name": video.file_name or f"video_{video.file_unique_id}.mp4",
            "file_size": video.file_size or 0,
            "duration": int(video.duration or 0),
            "width": video.width or 1280,
            "height": video.height or 720,
            "mime_type": video.mime_type or "video/mp4",
        }

    doc = message.document
    if doc:
        mime = (doc.mime_type or "").lower()
        name = (doc.file_name or "").lower()
        video_exts = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v")
        if mime.startswith("video/") or name.endswith(video_exts):
            return {
                "type": "document",
                "file_id": doc.file_id,
                "file_name": doc.file_name or f"video_{doc.file_unique_id}.mp4",
                "file_size": doc.file_size or 0,
                "duration": 0,
                "width": 1280,
                "height": 720,
                "mime_type": mime or "video/mp4",
            }
    return None

async def _video_or_reply_filter(_, __, message: Message) -> bool:
    if message.from_user and (message.from_user.is_self or message.from_user.is_bot):
        return False

    # 1. Private chat: Automatic trigger on video upload
    if message.chat.type.name == "PRIVATE":
        return get_video_info_from_message(message) is not None

    # 2. Group/Supergroup/Channel: Trigger only on /ss command in reply to a video
    text = (message.text or message.caption or "").strip()
    if text.startswith(("/", "!", ".")):
        cmd = text[1:].split()[0].split("@")[0].lower()
        if cmd in ("ss", "shots") and message.reply_to_message:
            return get_video_info_from_message(message.reply_to_message) is not None

    return False

unified_video_filter = filters.create(_video_or_reply_filter, "UnifiedVideoFilter")


async def probe_duration(target: str) -> float:
    """Reads duration (seconds) from a file path or URL using ffprobe. Returns 0.0 on failure."""
    try:
        rc, out, _ = await run_proc(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                target,
            ],
            timeout=15,
        )
        text = out.decode(errors="replace").strip()
        if rc == 0 and text:
            return float(text)
    except Exception as e:
        logger.warning(f"Could not probe duration from {target}: {e}")
    return 0.0


async def probe_stream_duration(stream_url: str, fallback_duration: int = 0) -> float:
    """Uses the known duration if present, otherwise probes the local stream URL."""
    if fallback_duration > 0:
        return float(fallback_duration)
    return await probe_duration(stream_url)


async def sample_is_valid(path: Path, expected_duration: float) -> bool:
    """Sanity check for a produced sample: exists, non-empty, and not badly truncated."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    dur = await probe_duration(str(path))
    return dur >= max(0.5, expected_duration * 0.7)


@lru_cache(maxsize=32)
def _get_font(size: int):
    """Scalable font: TrueType if available, else Pillow's sized default, else the tiny bitmap default."""
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def add_timestamp_badge(image: Image.Image, timestamp_text: str) -> Image.Image:
    """
    Returns a NEW RGB image with a semi-transparent timestamp badge in the bottom-right.
    The badge is alpha-composited (true transparency) and its font/padding scale with image width.
    """
    base = image.convert("RGBA")

    font_size = max(14, int(base.width * 0.026))
    font = _get_font(font_size)

    measure = ImageDraw.Draw(base)
    bbox = measure.textbbox((0, 0), timestamp_text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad_x = int(font_size * 0.6)
    pad_y = int(font_size * 0.35)
    margin = int(font_size * 0.8)

    x2 = base.width - margin
    y2 = base.height - margin
    x1 = max(0, x2 - (text_w + pad_x * 2))
    y1 = max(0, y2 - (text_h + pad_y * 2))
    badge_w = max(1, x2 - x1)
    badge_h = max(1, y2 - y1)

    badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(badge)
    bdraw.rounded_rectangle(
        [0, 0, badge_w - 1, badge_h - 1],
        radius=max(3, int(font_size * 0.3)),
        fill=(15, 15, 15, 200),
    )
    # Subtract the bbox origin so the glyphs sit exactly inside the padding
    bdraw.text((pad_x - bbox[0], pad_y - bbox[1]), timestamp_text, fill=(255, 255, 255, 255), font=font)

    base.alpha_composite(badge, dest=(x1, y1))
    return base.convert("RGB")


def create_collage(image_data_list: List[Tuple[Image.Image, str]], output_path: Path) -> Path:
    """
    Create a clean, dynamically balanced collage from in-memory frames.
    Takes UNSTAMPED PIL images; the badge is applied once, after cropping, so it stays in-frame.
    CPU-bound: call via asyncio.to_thread.
    """
    if not image_data_list:
        raise ValueError("No images to collage.")

    num_images = len(image_data_list)

    row_distribution_map = {
        1: [1],
        2: [2],
        3: [3],
        4: [2, 2],
        5: [3, 2],
        6: [3, 3],
        7: [4, 3],
        8: [4, 4],
        9: [3, 3, 3],
        10: [5, 5],
    }

    row_counts = row_distribution_map.get(num_images)
    if not row_counts:
        cols = math.ceil(math.sqrt(num_images))
        rows = math.ceil(num_images / cols)
        row_counts = [cols] * (rows - 1) + [num_images - (cols * (rows - 1))]

    max_cols = max(row_counts)
    spacing = 10

    base_canvas_width = 1920
    cell_w = (base_canvas_width - (max_cols + 1) * spacing) // max_cols
    cell_h = int(cell_w * 9 / 16)  # 16:9 aspect ratio

    total_rows = len(row_counts)
    canvas_w = max_cols * cell_w + (max_cols + 1) * spacing
    canvas_h = total_rows * cell_h + (total_rows + 1) * spacing

    collage = Image.new("RGB", (canvas_w, canvas_h), color=(18, 20, 24))

    img_idx = 0
    y = spacing
    for row_items in row_counts:
        row_total_w = row_items * cell_w + (row_items - 1) * spacing
        start_x = (canvas_w - row_total_w) // 2

        x = start_x
        for _ in range(row_items):
            if img_idx >= num_images:
                break
            img, ts_text = image_data_list[img_idx]

            img_ratio = img.width / img.height
            target_ratio = cell_w / cell_h

            if img_ratio > target_ratio:
                new_h = cell_h
                new_w = int(img.width * (cell_h / img.height))
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                crop_x = (new_w - cell_w) // 2
                cropped = resized.crop((crop_x, 0, crop_x + cell_w, cell_h))
            else:
                new_w = cell_w
                new_h = int(img.height * (cell_w / img.width))
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                crop_y = (new_h - cell_h) // 2
                cropped = resized.crop((0, crop_y, cell_w, crop_y + cell_h))

            # Badge applied to the cropped cell so it is never cut off
            cropped = add_timestamp_badge(cropped, ts_text)

            collage.paste(cropped, (x, y))
            x += cell_w + spacing
            img_idx += 1

        y += cell_h + spacing

    collage.save(output_path, format="JPEG", quality=92, optimize=True)
    return output_path


def build_screenshot_assets(
    raw_frames: List[Tuple[bytes, str]],
    work_dir: Path,
    collage_path: Path,
) -> Tuple[Path, List[Path]]:
    """
    Decode frames once, build the collage, then stamp full-size frames once for the slideshow.
    All Pillow work happens here so it can run in a single asyncio.to_thread call.
    """
    frames: List[Tuple[Image.Image, str]] = []
    for data, ts in raw_frames:
        with Image.open(io.BytesIO(data)) as im:
            frames.append((im.convert("RGB"), ts))

    create_collage(frames, collage_path)

    slide_paths: List[Path] = []
    for i, (im, ts) in enumerate(frames):
        stamped = add_timestamp_badge(im, ts)  # returns a new image; originals stay unstamped
        slide_path = work_dir / f"slide_{i:02d}.jpg"
        stamped.save(slide_path, format="JPEG", quality=92)
        slide_paths.append(slide_path)

    return collage_path, slide_paths


# ==================== HANDLERS ====================

def build_main_menu(session_key: str, video_info: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    dur = int(video_info.get("duration") or 0)
    dur_text = format_timestamp(dur) if dur > 0 else "Unknown"
    size_mb = video_info["file_size"] / (1024 * 1024)

    caption = (
        f"🎬 **Video Detected!**\n"
        f"📁 **Name:** `{video_info['file_name']}`\n"
        f"📦 **Size:** `{size_mb:.1f} MB`\n"
        f"⏱ **Duration:** `{dur_text}`\n\n"
        f"Select the number of screenshots or generate a sample:"
    )

    buttons = InlineKeyboardMarkup([
        # 2 3
        [
            InlineKeyboardButton("2", callback_data=f"vtool:shots_run:{session_key}:2", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton("3", callback_data=f"vtool:shots_run:{session_key}:3", style=ButtonStyle.PRIMARY),
        ],
        # 4 5
        [
            InlineKeyboardButton("4", callback_data=f"vtool:shots_run:{session_key}:4", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton("5", callback_data=f"vtool:shots_run:{session_key}:5", style=ButtonStyle.PRIMARY),
        ],
        # 6 7
        [
            InlineKeyboardButton("6", callback_data=f"vtool:shots_run:{session_key}:6", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton("7", callback_data=f"vtool:shots_run:{session_key}:7", style=ButtonStyle.PRIMARY),
        ],
        # 8 9
        [
            InlineKeyboardButton("8", callback_data=f"vtool:shots_run:{session_key}:8", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton("9", callback_data=f"vtool:shots_run:{session_key}:9", style=ButtonStyle.PRIMARY),
        ],
        # 10
        [
            InlineKeyboardButton("10", callback_data=f"vtool:shots_run:{session_key}:10", style=ButtonStyle.PRIMARY),
        ],
        # Get sample video (30s)
        [
            InlineKeyboardButton(
                "🎞 Get sample video (30s)",
                callback_data=f"vtool:sample:{session_key}",
                style=ButtonStyle.SUCCESS,
            ),
        ],
        # Close
        [
            InlineKeyboardButton(
                "❌ Close",
                callback_data=f"vtool:close:{session_key}",
                style=ButtonStyle.DANGER,
            ),
        ],
    ])
    return caption, buttons



# Private chats only, never our own or other bots' messages, and only real video files.
# To also allow groups, replace filters.private with filters.private | filters.group.
@Client.on_message(unified_video_filter)
async def unified_video_handler(client: Client, message: Message):
    """Handles both auto-detection in private chats and /ss command replies in groups."""
    target_msg = message.reply_to_message if message.reply_to_message else message
    video_info = get_video_info_from_message(target_msg)
    if not video_info:
        return

    session_key = f"{target_msg.chat.id}_{target_msg.id}"
    VIDEO_SESSIONS[session_key] = {
        "chat_id": target_msg.chat.id,
        "message_id": target_msg.id,
        "video_info": video_info,
        "created_at": time.time(),
    }

    # Clean up old sessions (> 1 hour)
    now = time.time()
    for k in list(VIDEO_SESSIONS.keys()):
        if now - VIDEO_SESSIONS[k]["created_at"] > 3600:
            VIDEO_SESSIONS.pop(k, None)

    caption, buttons = build_main_menu(session_key, video_info)
    await message.reply_text(caption, reply_markup=buttons)


@Client.on_callback_query(filters.regex(r"^vtool:"))
async def video_tool_callback(client: Client, callback: CallbackQuery):
    """Handles button interactions directly for sample generation and screenshot counts."""
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await safe_answer(callback, "⚠️ Invalid request.", show_alert=True)
        return

    action = parts[1]
    session_key = parts[2]

    if action == "close":
        VIDEO_SESSIONS.pop(session_key, None)
        await safe_answer(callback, "Closed.")
        await safe_delete(callback.message)
        return

    if action not in ("sample", "shots_run"):
        await safe_answer(callback, "⚠️ Unknown action.", show_alert=True)
        return

    user_id = callback.from_user.id
    if user_id in ACTIVE_USERS:
        await safe_answer(callback, "⏳ You already have an active processing task. Please wait.", show_alert=True)
        return

    session = VIDEO_SESSIONS.get(session_key)
    if not session:
        await safe_answer(callback, "⚠️ Session expired. Please re-send the video.", show_alert=True)
        return

    if action == "sample":
        ACTIVE_USERS.add(user_id)
        await safe_answer(callback, "Starting sample video generation...")
        spawn(process_sample_video(client, callback, session))

    elif action == "shots_run":
        try:
            count = int(parts[3])
        except (IndexError, ValueError):
            await safe_answer(callback, "⚠️ Invalid request.", show_alert=True)
            return

        ACTIVE_USERS.add(user_id)
        await safe_answer(callback, f"Generating {count} screenshots...")
        spawn(process_screenshot_collage(client, callback, session, count))



# ==================== STREAM PROCESSING TASKS ====================

async def process_sample_video(client: Client, callback: CallbackQuery, session: Dict[str, Any]):
    """Streams video via local HTTP range server, trims 30 seconds via ffmpeg without full download, and sends result."""
    user_id = callback.from_user.id
    status_msg = callback.message
    chat_id = session["chat_id"]
    msg_id = session["message_id"]

    uid = uuid.uuid4().hex
    sample_output = TEMP_DIR / f"sample_{uid}.mp4"
    sample_thumb = TEMP_DIR / f"thumb_{uid}.jpg"
    job_token = f"sample_{uid}"

    try:
        # Everything that can raise lives inside the try so ACTIVE_USERS is always released
        await safe_edit(status_msg, "⚡ **Preparing stream for 30s sample...**")
        await ensure_http_server()

        target_message = await client.get_messages(chat_id, msg_id)
        if not target_message or target_message.empty:
            await safe_edit(status_msg, "❌ Could not find original message.")
            return

        STREAM_JOBS[job_token] = {
            "client": client,
            "message": target_message,
            "file_size": session["video_info"]["file_size"],
            "mime_type": session["video_info"].get("mime_type", "video/mp4"),
        }

        stream_url = f"http://127.0.0.1:{HTTP_SERVER_PORT}/stream/{job_token}"
        await safe_edit(status_msg, "⚡ **Streaming video to generate 30s sample...**")

        duration = await probe_stream_duration(stream_url, session["video_info"].get("duration", 0))
        sample_duration = 30

        if duration > sample_duration + 10:
            min_start = max(5.0, duration * 0.05)
            max_start = max(min_start, (duration * 0.95) - sample_duration)
            start_time = int(random.uniform(min_start, max_start))
        elif duration > sample_duration:
            start_time = int(random.uniform(0, duration - sample_duration))
        else:
            start_time = 0

        end_time = min(start_time + sample_duration, int(duration)) if duration > 0 else start_time + sample_duration
        actual_sample_dur = max(1, end_time - start_time)
        timestamp_range = f"{format_timestamp(start_time)} - {format_timestamp(end_time)}"

        common_in = [
            "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
            "-ss", str(start_time),
            "-i", stream_url,
            "-t", str(actual_sample_dur),
            # First video + optional first audio only; drops subtitles/data/attachments that break mp4 muxing
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        ]

        fast_cmd = common_in + [
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(sample_output),
        ]

        logger.info(f"Trying fast stream copy: {' '.join(fast_cmd)}")
        fast_ok = False
        try:
            rc, _, fast_err = await run_proc(fast_cmd, timeout=45)  # killed on timeout by run_proc
            # A zero exit and a file on disk is not enough: verify the output really has the expected length
            fast_ok = rc == 0 and await sample_is_valid(sample_output, actual_sample_dur)
            if not fast_ok:
                logger.info(f"Fast copy output rejected (rc={rc}): {fast_err.decode(errors='replace')[-200:]}")
        except asyncio.TimeoutError:
            logger.info("Fast copy timed out; falling back to re-encode")

        if not fast_ok:
            sample_output.unlink(missing_ok=True)
            logger.info("Falling back to ultrafast re-encode")
            reencode_cmd = common_in + [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "fastdecode",
                "-threads", "4",
                "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(sample_output),
            ]
            rc, _, stderr_data = await run_proc(reencode_cmd, timeout=120)

            if rc != 0 or not sample_output.exists() or sample_output.stat().st_size == 0:
                err_msg = stderr_data.decode(errors="replace")[-300:]
                logger.error(f"FFmpeg sample generation failed: {err_msg}")
                await safe_edit(status_msg, f"❌ **Failed to generate sample video.**\n`{err_msg}`")
                return

        # Thumbnail is optional; failure here must not block the upload
        thumb_ss = "1" if actual_sample_dur > 2 else "0"
        with contextlib.suppress(Exception):
            await run_proc(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
                    "-ss", thumb_ss,
                    "-i", str(sample_output),
                    "-vframes", "1",
                    "-vf", "scale=320:-2",
                    str(sample_thumb),
                ],
                timeout=20,
            )

        await safe_edit(status_msg, "📤 **Uploading 30-second sample video...**")
        caption = (
            f"🎞 **30-second Sample Video ({timestamp_range})**\n"
            f"📁 `{session['video_info']['file_name']}`"
        )
        await client.send_video(
            chat_id=chat_id,
            video=str(sample_output),
            caption=caption,
            thumb=str(sample_thumb) if sample_thumb.exists() else None,
            duration=actual_sample_dur,
            reply_to_message_id=msg_id,
        )
        await safe_delete(status_msg)

    except asyncio.TimeoutError:
        await safe_edit(status_msg, "❌ **Timeout:** Video generation took too long.")
    except Exception as e:
        logger.exception("Error during sample video processing:")
        await safe_edit(status_msg, f"❌ **Error generating sample:** `{e}`")
    finally:
        ACTIVE_USERS.discard(user_id)
        STREAM_JOBS.pop(job_token, None)
        sample_output.unlink(missing_ok=True)
        sample_thumb.unlink(missing_ok=True)


async def extract_single_frame(stream_url: str, target_sec: float) -> Optional[bytes]:
    """
    Grabs one frame as JPEG bytes via stdout (no temp file).
    -noaccurate_seek snaps to the nearest keyframe instead of decoding up to the exact
    timestamp, which is much faster over a network stream. Probe limits stop ffmpeg
    from reading a lot of data before it starts. run_proc kills ffmpeg on timeout.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
        "-noaccurate_seek",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-ss", f"{target_sec:.2f}",
        "-i", stream_url,
        "-an", "-sn",
        "-vframes", "1",
        "-q:v", "3",
        "-f", "image2pipe",
        "-c:v", "mjpeg",
        "pipe:1",
    ]
    try:
        rc, out, _ = await run_proc(cmd, timeout=35)
        return out if rc == 0 and out else None
    except Exception:
        return None


async def process_screenshot_collage(client: Client, callback: CallbackQuery, session: Dict[str, Any], count: int):
    """Streams video, captures frames, stamps timestamps, builds collage, and sends rich slideshow."""
    user_id = callback.from_user.id
    count = max(2, min(count, 10))
    status_msg = callback.message
    chat_id = session["chat_id"]
    msg_id = session["message_id"]
    file_name = session["video_info"]["file_name"]

    uid = uuid.uuid4().hex
    work_dir = TEMP_DIR / f"shots_{uid}"
    collage_path = work_dir / "collage.jpg"
    job_token = f"shots_{uid}"

    try:
        # Everything that can raise lives inside the try so ACTIVE_USERS is always released
        work_dir.mkdir(parents=True, exist_ok=True)
        await safe_edit(status_msg, f"⚡ **Preparing stream to capture {count} screenshots...**")
        await ensure_http_server()

        target_message = await client.get_messages(chat_id, msg_id)
        if not target_message or target_message.empty:
            await safe_edit(status_msg, "❌ Could not find original message.")
            return

        STREAM_JOBS[job_token] = {
            "client": client,
            "message": target_message,
            "file_size": session["video_info"]["file_size"],
            "mime_type": session["video_info"].get("mime_type", "video/mp4"),
        }

        stream_url = f"http://127.0.0.1:{HTTP_SERVER_PORT}/stream/{job_token}"
        await safe_edit(status_msg, f"⚡ **Streaming video to capture {count} screenshots in parallel...**")

        duration = await probe_stream_duration(stream_url, session["video_info"].get("duration", 0))
        if duration <= 0:
            duration = 120.0

        start_bound = duration * 0.02
        usable_span = duration * 0.96
        timestamps = [start_bound + (i * usable_span / max(1, count - 1)) if count > 1 else duration * 0.5 for i in range(count)]

        sem = asyncio.Semaphore(4)

        async def worker(idx: int, target_sec: float):
            async with sem:
                data = await extract_single_frame(stream_url, target_sec)
                return idx, data, target_sec

        results = await asyncio.gather(*(worker(i, t) for i, t in enumerate(timestamps)))

        # Frames stay in memory as JPEG bytes; nothing is written to disk yet
        raw_frames: List[Tuple[bytes, str]] = [
            (data, format_timestamp(target_sec))
            for idx, data, target_sec in sorted(results, key=lambda r: r[0])
            if data
        ]

        if not raw_frames:
            await safe_edit(status_msg, "❌ **Failed to extract frames from video stream.**")
            return

        # 1. Build collage + stamped slideshow frames off the event loop
        await safe_edit(status_msg, "🎨 **Creating collage with timestamps...**")
        collage_file, slide_paths = await asyncio.to_thread(
            build_screenshot_assets, raw_frames, work_dir, collage_path
        )

        await safe_edit(status_msg, "📤 **Uploading collage...**")
        caption = (
            f"📸 **Screenshots Collage ({len(raw_frames)} frames)**\n"
            f"📁 `{file_name}`"
        )
        await client.send_photo(
            chat_id=chat_id,
            photo=str(collage_file),
            caption=caption,
            reply_to_message_id=msg_id,
        )

        # 2. Build and Send Rich Message Slideshow
        await safe_edit(status_msg, "🎞 **Creating rich slideshow message...**")

        media_list = [
            InputRichMessageMedia(
                id=str(i),
                media=InputMediaPhoto(media=str(slide_path))
            )
            for i, slide_path in enumerate(slide_paths)
        ]

        img_tags = "\n".join([f'<img src="tg://photo?id={i}"/>' for i in range(len(slide_paths))])

        rich_html = (
            f"<details><summary><b>🖼️ SCREENSHOTS</b></summary>\n"
            f"<tg-slideshow>\n"
            f"{img_tags}\n"
            f"<figcaption><b>Screenshots</b><br>{file_name}</figcaption>\n"
            f"</tg-slideshow>\n"
            f"</details>"
        )

        await client.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(
                html=rich_html,
                media=media_list
            )
        )

        await safe_delete(status_msg)

    except asyncio.TimeoutError:
        await safe_edit(status_msg, "❌ **Timeout:** Screenshot capture took too long.")
    except Exception as e:
        logger.exception("Error during screenshot processing:")
        await safe_edit(status_msg, f"❌ **Error generating screenshots:** `{e}`")
    finally:
        ACTIVE_USERS.discard(user_id)
        STREAM_JOBS.pop(job_token, None)
        shutil.rmtree(work_dir, ignore_errors=True)
