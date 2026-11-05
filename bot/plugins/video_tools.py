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

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from pyrogram.errors import FloodWait, MessageNotModified

logger = logging.getLogger(__name__)

# Temporary directory for generated files
TEMP_DIR = Path("./downloads/video_tools")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# State storage for user sessions
# Format: { f"{chat_id}_{message_id}": { ...data... } }
VIDEO_SESSIONS: Dict[str, Dict[str, Any]] = {}

# Keep track of active tasks per user to avoid spamming
ACTIVE_USERS: set = set()


def get_video_info_from_message(message: Message) -> Optional[Dict[str, Any]]:
    """Check if message has a video or a video-like document."""
    video = message.video
    if video:
        return {
            "type": "video",
            "file_id": video.file_id,
            "file_name": video.file_name or f"video_{video.file_unique_id}.mp4",
            "file_size": video.file_size or 0,
            "duration": video.duration or 0,
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
                "duration": 0,  # Documents might not have duration metadata
                "width": 1280,
                "height": 720,
                "mime_type": mime or "video/mp4",
            }
    return None


async def probe_duration_with_ffprobe(pipe_reader: asyncio.StreamReader) -> float:
    """Read duration from ffprobe using streaming input."""
    # ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 -i pipe:0
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "-i", "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # We can't pass the StreamReader directly to stdin without piping tasks
        return 0.0
    except Exception:
        return 0.0


async def pipe_stream_to_ffmpeg(stream_gen, stdin_writer: asyncio.StreamWriter):
    """Pipes chunks from client.stream_media() into ffmpeg stdin."""
    try:
        async for chunk in stream_gen:
            if stdin_writer.is_closing():
                break
            stdin_writer.write(chunk)
            await stdin_writer.drain()
    except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:
        logger.warning(f"Error while feeding stream to ffmpeg stdin: {e}")
    finally:
        try:
            stdin_writer.close()
            await stdin_writer.wait_closed()
        except Exception:
            pass


async def get_stream_duration(client: Client, message: Message, fallback_duration: int = 0) -> float:
    """Obtain duration from video metadata or by reading the container header with ffprobe."""
    if fallback_duration > 0:
        return float(fallback_duration)

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "-i", "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stream_gen = client.stream_media(message, limit=10) # stream first few chunks (e.g. 10 * 1MB)
        pipe_task = asyncio.create_task(pipe_stream_to_ffmpeg(stream_gen, proc.stdin))
        stdout, _ = await proc.communicate()
        await pipe_task
        dur_str = stdout.decode().strip()
        if dur_str:
            return float(dur_str)
    except Exception as e:
        logger.warning(f"Could not probe duration via ffprobe: {e}")

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

    # Standardize image size for each cell
    cell_w, cell_h = 480, 270  # 16:9 ratio thumbnail
    margin = 12
    header_height = 40 if title else 0

    total_w = cols * cell_w + (cols + 1) * margin
    total_h = rows * cell_h + (rows + 1) * margin + header_height

    # Create dark-themed collage canvas
    collage = Image.new("RGB", (total_w, total_h), color=(20, 24, 30))

    idx = 0
    for r in range(rows):
        for c in range(cols):
            if idx >= num_images:
                break
            img = images[idx]
            # Resize keeping aspect ratio, crop to fill cell
            img_ratio = img.width / img.height
            target_ratio = cell_w / cell_h

            if img_ratio > target_ratio:
                # Wider: scale by height then crop width
                new_h = cell_h
                new_w = int(img.width * (cell_h / img.height))
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                crop_x = (new_w - cell_w) // 2
                cropped = resized.crop((crop_x, 0, crop_x + cell_w, cell_h))
            else:
                # Taller: scale by width then crop height
                new_w = cell_w
                new_h = int(img.height * (cell_w / img.width))
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                crop_y = (new_h - cell_h) // 2
                cropped = resized.crop((0, crop_y, cell_w, crop_y + cell_h))

            x = margin + c * (cell_w + margin)
            y = header_height + margin + r * (cell_h + margin)
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

    dur = video_info["duration"]
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

    await message.reply_text(caption, reply_markup=buttons, quote=True)


@Client.on_callback_query(filters.regex(r"^vtool:"))
async def video_tool_callback(client: Client, callback: CallbackQuery):
    """Handles button interactions for sample generation and screenshot collage."""
    data = callback.data.split(":")
    action = data[1]
    session_key = f"{data[2]}:{data[3]}" if len(data) > 3 and not data[2].startswith("-") and "_" not in data[2] else data[2]
    # For safety, reconstruct session_key from parts:
    # "vtool:action:chat_id_msg_id" or "vtool:shots_num:chat_id_msg_id:count"
    
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
        dur = video_info["duration"]
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
    """Streams video via pyrogram, trims 30 seconds via ffmpeg without downloading full file, and sends result."""
    user_id = callback.from_user.id
    status_msg = await callback.edit_message_text("⚡ **Streaming video to generate 30s sample...**")
    chat_id = session["chat_id"]
    msg_id = session["message_id"]

    sample_output = TEMP_DIR / f"sample_{user_id}_{int(time.time())}.mp4"
    sample_thumb = TEMP_DIR / f"thumb_{user_id}_{int(time.time())}.jpg"

    try:
        # Retrieve target original message
        target_message = await client.get_messages(chat_id, msg_id)
        if not target_message:
            await status_msg.edit_text("❌ Could not find original message.")
            return

        # Figure out duration and start offset
        duration = await get_stream_duration(client, target_message, session["video_info"].get("duration", 0))
        
        # Decide starting point: if video is >= 60s, take 30s from the middle or 10s in
        start_time = 0
        if duration > 45:
            start_time = min(30, int(duration / 3))

        # Launch ffmpeg process reading from pipe:0 and writing to sample_output
        # -ss start_time before or after -i pipe:0
        # For pipe input, -ss before -i seeks by decoding/skipping or after -i trims stream.
        # -t 30 gives a 30s cut.
        # We re-encode fast with ultrafast or copy if possible. Fast re-encode is safest across codecs.
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-i", "pipe:0",
            "-ss", str(start_time),
            "-t", "30",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "26",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(sample_output),
        ]

        logger.info(f"Running ffmpeg for 30s sample: {' '.join(ffmpeg_cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Stream media from Pyrogram into ffmpeg stdin
        stream_gen = client.stream_media(target_message)
        pipe_task = asyncio.create_task(pipe_stream_to_ffmpeg(stream_gen, proc.stdin))

        # Wait for ffmpeg to complete
        _, stderr_data = await proc.communicate()
        await pipe_task

        if not sample_output.exists() or sample_output.stat().st_size == 0:
            err_msg = stderr_data.decode(errors="replace")[-300:]
            logger.error(f"FFmpeg sample generation failed: {err_msg}")
            await status_msg.edit_text(f"❌ **Failed to generate sample video.**\n`{err_msg}`")
            return

        # Generate thumbnail for the sample video
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

        # Send sample video to user
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

    except Exception as e:
        logger.exception("Error during sample video processing:")
        await status_msg.edit_text(f"❌ **Error generating sample:** `{e}`")
    finally:
        ACTIVE_USERS.discard(user_id)
        if sample_output.exists():
            sample_output.unlink(missing_ok=True)
        if sample_thumb.exists():
            sample_thumb.unlink(missing_ok=True)


async def process_screenshot_collage(client: Client, callback: CallbackQuery, session: Dict[str, Any], count: int):
    """Streams video via pyrogram, extracts requested number of frames, builds a collage and sends it."""
    user_id = callback.from_user.id
    count = max(2, min(count, 10))
    status_msg = await callback.edit_message_text(f"⚡ **Streaming video to capture {count} screenshots...**")
    chat_id = session["chat_id"]
    msg_id = session["message_id"]

    work_dir = TEMP_DIR / f"shots_{user_id}_{int(time.time())}"
    work_dir.mkdir(parents=True, exist_ok=True)
    collage_path = work_dir / "collage.jpg"

    try:
        target_message = await client.get_messages(chat_id, msg_id)
        if not target_message:
            await status_msg.edit_text("❌ Could not find original message.")
            return

        # Figure out duration
        duration = await get_stream_duration(client, target_message, session["video_info"].get("duration", 0))
        if duration <= 0:
            duration = 120.0  # fallback assumption 2 minutes

        # Calculate evenly spaced intervals across the video
        # E.g. for duration 100 and count 5 -> intervals at 10, 30, 50, 70, 90
        step = duration / (count + 1)
        timestamps = [step * (i + 1) for i in range(count)]

        # Extract frames using ffmpeg fps or select filter
        # Using select filter: select='eq(n\,...)' or extracting via fps
        # To make it fastest over pipe without seeking issues, we can extract `count` frames evenly:
        # ffmpeg -i pipe:0 -vf "thumbnail,scale=640:-1" -vframes count
        # Or even better: -vf "fps=count/duration" or select expression:
        # A very robust way for piped streams is to sample 1 frame every (duration / count) seconds:
        fps_rate = count / max(duration, 1.0)
        out_pattern = str(work_dir / "frame_%02d.jpg")

        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-i", "pipe:0",
            "-vf", f"fps={fps_rate:.4f},scale=640:-1",
            "-vframes", str(count),
            "-q:v", "3",
            out_pattern,
        ]

        logger.info(f"Running ffmpeg for screenshots: {' '.join(ffmpeg_cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stream_gen = client.stream_media(target_message)
        pipe_task = asyncio.create_task(pipe_stream_to_ffmpeg(stream_gen, proc.stdin))

        _, stderr_data = await proc.communicate()
        await pipe_task

        frames = sorted(list(work_dir.glob("frame_*.jpg")))
        if not frames:
            err_msg = stderr_data.decode(errors="replace")[-300:]
            logger.error(f"FFmpeg screenshot extraction failed: {err_msg}")
            await status_msg.edit_text(f"❌ **Failed to extract frames.**\n`{err_msg}`")
            return

        # Trim frames to requested count
        frames = frames[:count]

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

    except Exception as e:
        logger.exception("Error during screenshot collage processing:")
        await status_msg.edit_text(f"❌ **Error generating screenshots:** `{e}`")
    finally:
        ACTIVE_USERS.discard(user_id)
        shutil.rmtree(work_dir, ignore_errors=True)
