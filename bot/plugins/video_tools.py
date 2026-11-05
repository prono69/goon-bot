import os
import io
import math
import time
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from PIL import Image
from aiohttp import web

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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

# Internal local HTTP server state
HTTP_SERVER_RUNNER: Optional[web.AppRunner] = None
HTTP_SERVER_PORT = 19485


# ==================== LOCAL HTTP RANGE STREAMER ====================

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

    range_header = request.headers.get("Range")
    from_bytes = 0
    until_bytes = file_size - 1

    if range_header:
        # Format: bytes=start-end or bytes=start-
        try:
            ranges = range_header.replace("bytes=", "").split("-")
            from_bytes = int(ranges[0])
            if ranges[1]:
                until_bytes = int(ranges[1])
        except Exception:
            from_bytes = 0

    from_bytes = max(0, min(from_bytes, file_size - 1))
    until_bytes = max(from_bytes, min(until_bytes, file_size - 1))
    req_length = (until_bytes - from_bytes) + 1

    headers = {
        "Content-Type": mime_type,
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Length": str(req_length),
    }

    status_code = 206 if range_header else 200
    response = web.StreamResponse(status=status_code, headers=headers)
    await response.prepare(request)

    if request.method == "HEAD":
        return response

    # Stream requested byte range from Telegram MTProto
    try:
        # In Pyrogram / Kurigram, offset is given in 1MB chunks (or chunk offset)
        # stream_media(message, offset=..., limit=...)
        # offset is the chunk index (1 chunk = 1MB = 1048576 bytes)
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
                await response.write(chunk)
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


# ==================== HELPERS ====================

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


async def probe_stream_duration(stream_url: str, fallback_duration: int = 0) -> float:
    """Reads accurate duration from the local stream URL using ffprobe."""
    if fallback_duration > 0:
        return float(fallback_duration)

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            stream_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        dur_str = stdout.decode().strip()
        if dur_str:
            return float(dur_str)
    except Exception as e:
        logger.warning(f"Could not probe duration from {stream_url}: {e}")

    return 0.0


def create_collage(image_paths: List[Path], output_path: Path, title: str = "") -> Path:
    """Create a high-quality beautiful collage/grid of screenshots using PIL."""
    if not image_paths:
        raise ValueError("No images to collage.")

    images = [Image.open(p).convert("RGB") for p in image_paths]
    num_images = len(images)

    # Determine grid layout (cols, rows)
    if num_images == 1:
        cols, rows = 1, 1
    elif num_images == 2:
        cols, rows = 2, 1
    elif num_images <= 4:
        cols, rows = 2, 2
    elif num_images <= 6:
        cols, rows = 3, 2
    elif num_images <= 9:
        cols, rows = 3, 3
    else:  # 10
        cols, rows = 5, 2

    cell_w, cell_h = 480, 270  # 16:9 ratio thumbnail
    margin = 12

    total_w = cols * cell_w + (cols + 1) * margin
    total_h = rows * cell_h + (rows + 1) * margin

    collage = Image.new("RGB", (total_w, total_h), color=(20, 24, 30))

    idx = 0
    for r in range(rows):
        for c in range(cols):
            if idx >= num_images:
                break
            img = images[idx]
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

            x = margin + c * (cell_w + margin)
            y = margin + r * (cell_h + margin)
            collage.paste(cropped, (x, y))
            idx += 1

    for img in images:
        img.close()

    collage.save(output_path, format="JPEG", quality=90, optimize=True)
    return output_path


# ==================== HANDLERS ====================

@Client.on_message(filters.video | filters.document)
async def video_receive_handler(client: Client, message: Message):
    """Detects video or video documents and prompts user with action buttons."""
    video_info = get_video_info_from_message(message)
    if not video_info:
        return

    session_key = f"{message.chat.id}_{message.id}"
    VIDEO_SESSIONS[session_key] = {
        "chat_id": message.chat.id,
        "message_id": message.id,
        "video_info": video_info,
        "created_at": time.time(),
    }

    # Clean up old sessions (> 1 hour)
    now = time.time()
    for k in list(VIDEO_SESSIONS.keys()):
        if now - VIDEO_SESSIONS[k]["created_at"] > 3600:
            VIDEO_SESSIONS.pop(k, None)

    dur = int(video_info.get("duration") or 0)
    dur_text = f"{dur // 60}:{dur % 60:02d}" if dur > 0 else "Unknown"
    size_mb = video_info["file_size"] / (1024 * 1024)

    caption = (
        f"🎬 **Video Detected!**\n"
        f"📁 **Name:** `{video_info['file_name']}`\n"
        f"📦 **Size:** `{size_mb:.1f} MB`\n"
        f"⏱ **Duration:** `{dur_text}`\n\n"
        f"Select what you'd like to do:"
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎞 Generate 30s Sample", callback_data=f"vtool:sample:{session_key}"),
            InlineKeyboardButton("📸 Generate Screenshots", callback_data=f"vtool:shots:{session_key}"),
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data=f"vtool:close:{session_key}")
        ]
    ])

    await message.reply_text(caption, reply_markup=buttons)


@Client.on_callback_query(filters.regex(r"^vtool:"))
async def video_tool_callback(client: Client, callback: CallbackQuery):
    """Handles button interactions for sample generation and screenshot collage."""
    parts = callback.data.split(":")
    action = parts[1]

    if action == "close":
        await callback.message.delete()
        session_key = parts[2]
        VIDEO_SESSIONS.pop(session_key, None)
        await callback.answer("Closed.")
        return

    if action == "shots":
        session_key = parts[2]
        session = VIDEO_SESSIONS.get(session_key)
        if not session:
            await callback.answer("⚠️ Session expired or invalid. Please re-send the video.", show_alert=True)
            return

        # Show selection for screenshot counts 2 to 10
        buttons = []
        row1 = [InlineKeyboardButton(str(i), callback_data=f"vtool:shots_run:{session_key}:{i}") for i in range(2, 6)]
        row2 = [InlineKeyboardButton(str(i), callback_data=f"vtool:shots_run:{session_key}:{i}") for i in range(6, 11)]
        buttons.append(row1)
        buttons.append(row2)
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"vtool:back:{session_key}")])

        await callback.edit_message_text(
            "📸 **Select number of screenshots (Max: 10):**\n"
            "The bot will extract evenly distributed frames and arrange them into a collage.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        await callback.answer()
        return

    if action == "back":
        session_key = parts[2]
        session = VIDEO_SESSIONS.get(session_key)
        if not session:
            await callback.answer("⚠️ Session expired.", show_alert=True)
            return

        video_info = session["video_info"]
        dur = int(video_info.get("duration") or 0)
        dur_text = f"{dur // 60}:{dur % 60:02d}" if dur > 0 else "Unknown"
        size_mb = video_info["file_size"] / (1024 * 1024)

        caption = (
            f"🎬 **Video Detected!**\n"
            f"📁 **Name:** `{video_info['file_name']}`\n"
            f"📦 **Size:** `{size_mb:.1f} MB`\n"
            f"⏱ **Duration:** `{dur_text}`\n\n"
            f"Select what you'd like to do:"
        )

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎞 Generate 30s Sample", callback_data=f"vtool:sample:{session_key}"),
                InlineKeyboardButton("📸 Generate Screenshots", callback_data=f"vtool:shots:{session_key}"),
            ],
            [
                InlineKeyboardButton("❌ Close", callback_data=f"vtool:close:{session_key}")
            ]
        ])

        await callback.edit_message_text(caption, reply_markup=buttons)
        await callback.answer()
        return

    user_id = callback.from_user.id
    if user_id in ACTIVE_USERS:
        await callback.answer("⏳ You already have an active processing task. Please wait.", show_alert=True)
        return

    if action == "sample":
        session_key = parts[2]
        session = VIDEO_SESSIONS.get(session_key)
        if not session:
            await callback.answer("⚠️ Session expired. Please re-send the video.", show_alert=True)
            return

        ACTIVE_USERS.add(user_id)
        await callback.answer("Starting sample video generation...")
        asyncio.create_task(process_sample_video(client, callback, session))

    elif action == "shots_run":
        session_key = parts[2]
        count = int(parts[3])
        session = VIDEO_SESSIONS.get(session_key)
        if not session:
            await callback.answer("⚠️ Session expired. Please re-send the video.", show_alert=True)
            return

        ACTIVE_USERS.add(user_id)
        await callback.answer(f"Generating {count} screenshots collage...")
        asyncio.create_task(process_screenshot_collage(client, callback, session, count))


# ==================== STREAM PROCESSING TASKS ====================

async def process_sample_video(client: Client, callback: CallbackQuery, session: Dict[str, Any]):
    """Streams video via local HTTP range server, trims 30 seconds via ffmpeg without full download, and sends result."""
    user_id = callback.from_user.id
    status_msg = await callback.edit_message_text("⚡ **Preparing stream for 30s sample...**")
    chat_id = session["chat_id"]
    msg_id = session["message_id"]

    sample_output = TEMP_DIR / f"sample_{user_id}_{int(time.time())}.mp4"
    sample_thumb = TEMP_DIR / f"thumb_{user_id}_{int(time.time())}.jpg"
    job_token = f"sample_{user_id}_{int(time.time())}"

    try:
        await ensure_http_server()
        target_message = await client.get_messages(chat_id, msg_id)
        if not target_message:
            await status_msg.edit_text("❌ Could not find original message.")
            return

        # Register streaming job
        STREAM_JOBS[job_token] = {
            "client": client,
            "message": target_message,
            "file_size": session["video_info"]["file_size"],
            "mime_type": session["video_info"].get("mime_type", "video/mp4"),
        }

        stream_url = f"http://127.0.0.1:{HTTP_SERVER_PORT}/stream/{job_token}"
        await status_msg.edit_text("⚡ **Streaming video to generate 30s sample...**")

        # Determine start time (seek point)
        duration = await probe_stream_duration(stream_url, session["video_info"].get("duration", 0))
        start_time = 0
        if duration > 45:
            start_time = min(30, int(duration / 3))

        # FFmpeg reads directly from local HTTP URL with seeking support
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-ss", str(start_time),
            "-i", stream_url,
            "-t", "30",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "26",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(sample_output),
        ]

        logger.info(f"Running ffmpeg sample command: {' '.join(ffmpeg_cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=180)

        if not sample_output.exists() or sample_output.stat().st_size == 0:
            err_msg = stderr_data.decode(errors="replace")[-300:]
            logger.error(f"FFmpeg sample generation failed: {err_msg}")
            await status_msg.edit_text(f"❌ **Failed to generate sample video.**\n`{err_msg}`")
            return

        # Generate thumbnail for the sample
        thumb_cmd = [
            "ffmpeg",
            "-y",
            "-i", str(sample_output),
            "-ss", "00:00:01",
            "-vframes", "1",
            str(sample_thumb)
        ]
        t_proc = await asyncio.create_subprocess_exec(*thumb_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await t_proc.wait()

        # Upload generated sample
        await status_msg.edit_text("📤 **Uploading 30-second sample video...**")
        await client.send_video(
            chat_id=chat_id,
            video=str(sample_output),
            caption=f"🎞 **30-second Sample Video**\n📁 `{session['video_info']['file_name']}`",
            thumb=str(sample_thumb) if sample_thumb.exists() else None,
            duration=30,
            reply_to_message_id=msg_id,
        )
        await status_msg.delete()

    except asyncio.TimeoutError:
        await status_msg.edit_text("❌ **Timeout:** Video generation took too long.")
    except Exception as e:
        logger.exception("Error during sample video processing:")
        await status_msg.edit_text(f"❌ **Error generating sample:** `{e}`")
    finally:
        ACTIVE_USERS.discard(user_id)
        STREAM_JOBS.pop(job_token, None)
        if sample_output.exists():
            sample_output.unlink(missing_ok=True)
        if sample_thumb.exists():
            sample_thumb.unlink(missing_ok=True)


async def process_screenshot_collage(client: Client, callback: CallbackQuery, session: Dict[str, Any], count: int):
    """Streams video via local HTTP range server, captures screenshots, and creates a collage."""
    user_id = callback.from_user.id
    count = max(2, min(count, 10))
    status_msg = await callback.edit_message_text(f"⚡ **Preparing stream to capture {count} screenshots...**")
    chat_id = session["chat_id"]
    msg_id = session["message_id"]

    work_dir = TEMP_DIR / f"shots_{user_id}_{int(time.time())}"
    work_dir.mkdir(parents=True, exist_ok=True)
    collage_path = work_dir / "collage.jpg"
    job_token = f"shots_{user_id}_{int(time.time())}"

    try:
        await ensure_http_server()
        target_message = await client.get_messages(chat_id, msg_id)
        if not target_message:
            await status_msg.edit_text("❌ Could not find original message.")
            return

        # Register streaming job
        STREAM_JOBS[job_token] = {
            "client": client,
            "message": target_message,
            "file_size": session["video_info"]["file_size"],
            "mime_type": session["video_info"].get("mime_type", "video/mp4"),
        }

        stream_url = f"http://127.0.0.1:{HTTP_SERVER_PORT}/stream/{job_token}"
        await status_msg.edit_text(f"⚡ **Streaming video to capture {count} screenshots...**")

        # Determine duration
        duration = await probe_stream_duration(stream_url, session["video_info"].get("duration", 0))
        if duration <= 0:
            duration = 120.0

        # Extract frames evenly spaced across duration
        step = duration / (count + 1)
        frames: List[Path] = []

        # Seek and extract each frame individually using HTTP range seeking
        for i in range(count):
            target_sec = step * (i + 1)
            frame_file = work_dir / f"frame_{i:02d}.jpg"
            cmd = [
                "ffmpeg",
                "-y",
                "-ss", f"{target_sec:.2f}",
                "-i", stream_url,
                "-vframes", "1",
                "-q:v", "3",
                str(frame_file)
            ]
            f_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(f_proc.wait(), timeout=30)
            if frame_file.exists() and frame_file.stat().st_size > 0:
                frames.append(frame_file)

        if not frames:
            await status_msg.edit_text("❌ **Failed to extract frames from video stream.**")
            return

        await status_msg.edit_text("🎨 **Creating collage...**")
        collage_file = create_collage(frames, collage_path, title=session["video_info"]["file_name"])

        await status_msg.edit_text("📤 **Uploading collage...**")
        caption = (
            f"📸 **Screenshots Collage ({len(frames)} frames)**\n"
            f"📁 `{session['video_info']['file_name']}`"
        )
        await client.send_photo(
            chat_id=chat_id,
            photo=str(collage_file),
            caption=caption,
            reply_to_message_id=msg_id,
        )
        await status_msg.delete()

    except asyncio.TimeoutError:
        await status_msg.edit_text("❌ **Timeout:** Screenshot capture took too long.")
    except Exception as e:
        logger.exception("Error during screenshot collage processing:")
        await status_msg.edit_text(f"❌ **Error generating screenshots:** `{e}`")
    finally:
        ACTIVE_USERS.discard(user_id)
        STREAM_JOBS.pop(job_token, None)
        shutil.rmtree(work_dir, ignore_errors=True)
