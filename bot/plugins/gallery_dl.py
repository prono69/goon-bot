import os
import re
import json
import time
import shutil
import zipfile
import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse
from typing import List, Dict, Any, Tuple

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
)
from pyrogram.errors import FloodWait, MediaEmpty, WebpageCurlFailed

logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
DOWNLOAD_BASE_DIR = Path("./downloads/gallery_dl")
COOKIES_FILE = Path("./gdl-cookies.txt")
CONFIG_FILE = Path("./gallery-dl.conf")

MAX_CONCURRENT_JOBS = 2          # Maximum simultaneous download jobs
MAX_FILES_PER_JOB = 1000         # Safety cap to prevent disk exhaustion
TG_MAX_FILE_SIZE = 2000 * 1024 * 1024  # Standard 2GB limit

DOWNLOAD_BASE_DIR.mkdir(parents=True, exist_ok=True)
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# In-memory store for pending button selections
PENDING_JOBS: Dict[str, Dict[str, Any]] = {}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
GIF_EXTS = {".gif"}

URL_REGEX = re.compile(r"https?://\S+")
PIXIV_REGEX = re.compile(r"https?://(?:www\.)?pixiv\.net/\S+", re.IGNORECASE)



# ==================== HELPER UTILITIES ====================

def human_bytes(size: float) -> str:
    """Format bytes into readable strings."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


def sanitize_filename(name: str) -> str:
    """Remove filesystem-illegal characters and trim whitespace."""
    clean = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return clean if clean else "Gallery"


def extract_custom_title(metadata: dict, url: str) -> str:
    """
    Extracts name prioritizing:
    1. title (category)
    2. model_name (category)
    3. url_slug (category)
    """
    category = metadata.get("category", "").strip()

    # 1. Search for title (top-level or inside album dict)
    title = metadata.get("title")
    if not title and isinstance(metadata.get("album"), dict):
        title = metadata["album"].get("title")

    if title and isinstance(title, str) and title.strip():
        name = f"{title.strip()} ({category})" if category else title.strip()
        return sanitize_filename(name)

    # 2. Search for model_name
    model_name = metadata.get("model_name")
    if model_name and isinstance(model_name, str) and model_name.strip():
        name = f"{model_name.strip()} ({category})" if category else model_name.strip()
        return sanitize_filename(name)

    # 3. Fallback to URL endpoint path
    parsed = urlparse(url)
    slug = parsed.path.strip("/").split("/")[-1]
    if slug:
        name = f"{slug} ({category})" if category else slug
        return sanitize_filename(name)

    return "Gallery_Download"


async def progress_bar(current: int, total: int, status_msg: Message, start_time: float, prefix: str = "Uploading"):
    """Throttled progress update for Telegram uploads."""
    now = time.time()
    diff = now - start_time
    if round(diff % 4.00) == 0 or current == total:
        percentage = current * 100 / total if total else 0
        speed = current / diff if diff > 0 else 0
        filled = int(percentage / 10)
        bar = "█" * filled + "░" * (10 - filled)
        text = (
            f"**{prefix}...**\n"
            f"`[{bar}]` {percentage:.1f}%\n"
            f"**Size:** {human_bytes(current)} / {human_bytes(total)}\n"
            f"**Speed:** {human_bytes(speed)}/s"
        )
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass


async def safe_tg_call(coro, *args, **kwargs):
    """Executes a Pyrogram call while safely resolving FloodWait errors."""
    while True:
        try:
            return await coro(*args, **kwargs)
        except FloodWait as e:
            logger.warning(f"FloodWait hit. Sleeping for {e.value}s...")
            await asyncio.sleep(e.value + 1)


def create_zip_sync(source_dir: Path, output_zip: Path) -> Path:
    """Synchronously creates a ZIP archive excluding metadata JSON files."""
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(source_dir):
            for file in files:
                if file.endswith(".json"):
                    continue
                file_path = Path(root) / file
                arcname = file_path.relative_to(source_dir)
                zf.write(file_path, arcname)
    return output_zip


async def probe_gallery_fast(url: str, range_opt: str = None) -> Tuple[int, int, str, str]:
    """
    Simulates extraction via gallery-dl -j to discover file count,
    cumulative size (if available), and dynamic gallery name.
    Returns: (file_count, total_bytes, gallery_name, raw_error)
    """
    cmd = [
        "gallery-dl",
        "--simulate",
        "-j",
        "--range", range_opt if range_opt else f"1-{MAX_FILES_PER_JOB}",
    ]

    if COOKIES_FILE.exists():
        cmd.extend(["--cookies", str(COOKIES_FILE)])
    if CONFIG_FILE.exists():
        cmd.extend(["--config", str(CONFIG_FILE)])

    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_bytes, stderr_bytes = await proc.communicate()
    raw_output = stdout_bytes.decode("utf-8", errors="ignore").strip()
    raw_error = stderr_bytes.decode("utf-8", errors="ignore").strip()

    file_count = 0
    total_bytes = 0
    extracted_meta = {}
    known_album_count = 0

    if not raw_output:
        fallback_title = sanitize_filename(urlparse(url).path.strip("/").split("/")[-1] or "Gallery")
        return 0, 0, fallback_title, raw_error

    records = []
    try:
        parsed = json.loads(raw_output)
        if isinstance(parsed, list):
            records = parsed
        elif isinstance(parsed, dict):
            records = [parsed]
    except Exception:
        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                records.append(item)
            except Exception:
                continue

    for data in records:
        if isinstance(data, list) and len(data) >= 2:
            msg_type = data[0]
            candidate = data[-1] if isinstance(data[-1], dict) else (data[1] if isinstance(data[1], dict) else None)

            if isinstance(candidate, dict):
                if not extracted_meta:
                    extracted_meta.update(candidate)
                elif not ("title" in extracted_meta or "model_name" in extracted_meta):
                    extracted_meta.update(candidate)

                cnt = candidate.get("count")
                if isinstance(cnt, int) and cnt > known_album_count:
                    known_album_count = cnt

            if msg_type in (2, 3, 6):
                if msg_type == 2 and isinstance(data[1], dict):
                    pass
                else:
                    file_count += 1

                if isinstance(candidate, dict):
                    size = candidate.get("filesize") or candidate.get("file_size")
                    if isinstance(size, (int, float)):
                        total_bytes += int(size)

        elif isinstance(data, dict):
            file_count += 1
            if not extracted_meta:
                extracted_meta = data
            size = data.get("filesize") or data.get("file_size")
            if isinstance(size, (int, float)):
                total_bytes += int(size)

    if known_album_count > 0:
        file_count = known_album_count

    gallery_name = extract_custom_title(extracted_meta, url)
    return file_count, total_bytes, gallery_name, raw_error


async def run_gallery_dl(url: str, dest_dir: Path, status_msg: Message, range_opt: str = None) -> Tuple[int, str]:
    """Downloads gallery items and edits the Telegram message as items arrive."""
    cmd = [
        "gallery-dl",
        "--dest", str(dest_dir),
        "--write-info-json",
        "--no-mtime",
        "--range", range_opt if range_opt else f"1-{MAX_FILES_PER_JOB}",
    ]

    if COOKIES_FILE.exists():
        cmd.extend(["--cookies", str(COOKIES_FILE)])
    if CONFIG_FILE.exists():
        cmd.extend(["--config", str(CONFIG_FILE)])

    cmd.append(url)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    downloaded_count = 0
    last_edit = time.time()
    stderr_lines = []

    while True:
        line = await process.stdout.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="ignore").strip()
        if decoded:
            downloaded_count += 1
            now = time.time()
            if now - last_edit >= 3.5:
                try:
                    await status_msg.edit_text(
                        f"📥 **Downloading Gallery...**\n"
                        f"**Downloaded items:** `{downloaded_count}`\n"
                        f"**Latest:** `{Path(decoded).name}`"
                    )
                    last_edit = now
                except Exception:
                    pass

    stderr_data = await process.stderr.read()
    if stderr_data:
        stderr_lines.append(stderr_data.decode("utf-8", errors="ignore"))

    return_code = await process.wait()
    return return_code, "\n".join(stderr_lines)


# ==================== COMMAND HANDLERS ====================

@Client.on_message(
    (
        filters.command(["gdl", "dl"])
        | (filters.text & filters.regex(PIXIV_REGEX))
    )
    & (filters.private | filters.group)
)
async def gdl_command_handler(client: Client, message: Message):
    """Entry point: /gdl <url>, /dl <url>, or direct Pixiv URL."""
    text = message.text or ""
    
    # First priority: check if message contains a Pixiv URL
    pixiv_urls = PIXIV_REGEX.findall(text)
    if pixiv_urls:
        urls = pixiv_urls
    else:
        # Fallback to general URL extraction if triggered via /gdl
        urls = URL_REGEX.findall(text)

    # Check replied message if command was sent as a reply
    if not urls and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        urls = PIXIV_REGEX.findall(reply_text) or URL_REGEX.findall(reply_text)

    # If it was triggered via /gdl without any link, show usage
    if not urls:
        return await message.reply_text(
            "❌ **Please provide a valid URL!**\n\n"
            "**Usage:**\n"
            "• Send any Pixiv link directly into the chat.\n"
            "• `/gdl https://example.com/gallery`\n"
            "• `/gdl -r 1-15 https://example.com/gallery` *(Download items 1 to 15)*\n"
            "• Reply `/gdl` to a message containing a URL.\n\n"
            "**Cookies:**\n"
            "• Reply `/setcookies` to a `cookies.txt` file."
        )

    url = urls[0]
    range_match = re.search(r"-r\s+(\d+-\d+|\d+)", text)
    range_opt = range_match.group(1) if range_match else None

    probing_msg = await message.reply_text("🔎 **Inspecting gallery link...**")

    file_count, total_bytes, gallery_name, raw_error = await probe_gallery_fast(url, range_opt)

    if file_count == 0:
        err_msg = f"\n\n**Details:**\n`{raw_error[-350:]}`" if raw_error else ""
        return await probing_msg.edit_text(
            f"❌ **Could not find any downloadable media!**\n"
            f"The link might be private, require login (`/setcookies`), or is not supported by gallery-dl.{err_msg}"
        )

    session_id = f"{message.chat.id}_{message.id}"
    PENDING_JOBS[session_id] = {
        "url": url,
        "user_id": message.from_user.id if message.from_user else 0,
        "range_opt": range_opt,
        "gallery_name": gallery_name,
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼️ Normal (Albums)", callback_data=f"gdl_norm|{session_id}"),
            InlineKeyboardButton("🗜️ Upload as ZIP", callback_data=f"gdl_zip|{session_id}"),
        ],
        [
            InlineKeyboardButton("📄 Uncompressed (Docs)", callback_data=f"gdl_doc|{session_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"gdl_cancel|{session_id}"),
        ]
    ])

    size_display = human_bytes(total_bytes) if total_bytes > 0 else "Dynamic (Calculated upon download)"
    range_display = f"\n🎯 **Range:** `{range_opt}`" if range_opt else ""

    await probing_msg.edit_text(
        f"🏷️ **Title:** `{gallery_name}`\n"
        f"🔗 **URL:** `{url}`{range_display}\n"
        f"📁 **Total Items:** `{file_count}`\n"
        f"📦 **Estimated Size:** `{size_display}`\n\n"
        "How would you like to receive this gallery?",
        reply_markup=keyboard,
    )



@Client.on_message(filters.command("setcookies") & filters.reply)
async def set_cookies_handler(client: Client, message: Message):
    """Save a replied cookies.txt file for authenticated sites."""
    doc = message.reply_to_message.document
    if not doc or not doc.file_name.endswith(".txt"):
        return await message.reply_text("❌ Please reply to a valid Netscape format `cookies.txt` file.")

    await message.reply_to_message.download(file_name=str(COOKIES_FILE.resolve()))
    await message.reply_text(f"✅ `{COOKIES_FILE.name}` saved. It will now be applied to future downloads!")


# ==================== CALLBACK QUERY DISPATCHER ====================

@Client.on_callback_query(filters.regex(r"^gdl_(norm|zip|doc|cancel)\|"))
async def gdl_callback_handler(client: Client, query: CallbackQuery):
    action, session_id = query.data.split("|", 1)
    job = PENDING_JOBS.get(session_id)

    if not job:
        return await query.answer("⚠️ Session has expired. Please send the link again.", show_alert=True)

    if query.from_user.id != job["user_id"]:
        return await query.answer("⛔ Only the user who sent the command can make a selection!", show_alert=True)

    if action == "gdl_cancel":
        PENDING_JOBS.pop(session_id, None)
        return await query.message.edit_text("❌ **Task Cancelled.**")

    PENDING_JOBS.pop(session_id, None)
    url = job["url"]
    range_opt = job["range_opt"]
    preset_name = job["gallery_name"]

    status_msg = await query.message.edit_text("⏳ **Queued in scheduler...**")
    job_dir = DOWNLOAD_BASE_DIR / session_id

    async with job_semaphore:
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            await status_msg.edit_text(f"🔍 **Downloading files...**\n🏷️ `{preset_name}`")

            ret_code, stderr_out = await run_gallery_dl(url, job_dir, status_msg, range_opt)

            all_files: List[Path] = sorted(
                [p for p in job_dir.rglob("*") if p.is_file() and not p.name.endswith(".json")]
            )

            if not all_files:
                err_snippet = stderr_out.strip()[-400:] if stderr_out else "No media downloaded."
                return await status_msg.edit_text(
                    f"❌ **Download Failed!**\n\n`{err_snippet}`"
                )

            json_files = list(job_dir.rglob("*.json"))
            meta = {}
            if json_files:
                try:
                    with open(json_files[0], "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass

            gallery_name = extract_custom_title(meta, url) if meta else preset_name
            total_files = len(all_files)

            # -------------------- MODE 1: ZIP ARCHIVE --------------------
            if action == "gdl_zip":
                await status_msg.edit_text(f"🗜️ **Packing {total_files} files into `{gallery_name}.zip`...**")
                zip_path = DOWNLOAD_BASE_DIR / f"{gallery_name}.zip"

                await asyncio.to_thread(create_zip_sync, job_dir, zip_path)

                zip_size = zip_path.stat().st_size
                if zip_size > TG_MAX_FILE_SIZE:
                    if zip_path.exists():
                        zip_path.unlink()
                    return await status_msg.edit_text(
                        f"❌ **Archive is too large ({human_bytes(zip_size)})** for Telegram (Max 2GB)!"
                    )

                start_t = time.time()
                await safe_tg_call(
                    client.send_document,
                    chat_id=query.message.chat.id,
                    document=str(zip_path),
                    file_name=f"{gallery_name}.zip",
                    caption=f"📦 **{gallery_name}**\n📁 Files: `{total_files}`\n📦 Size: `{human_bytes(zip_size)}`\n🔗 [Source URL]({url})",
                    reply_to_message_id=query.message.reply_to_message_id,
                    progress=progress_bar,
                    progress_args=(status_msg, start_t, "Uploading ZIP"),
                )
                if zip_path.exists():
                    zip_path.unlink()

            # -------------------- MODE 2: UNCOMPRESSED DOCUMENTS --------------------
            elif action == "gdl_doc":
                await status_msg.edit_text(f"📤 **Uploading {total_files} files as raw documents...**")
                total_batches = (total_files + 9) // 10

                for i in range(0, total_files, 10):
                    batch = all_files[i : i + 10]
                    batch_num = (i // 10) + 1

                    part_info = f" `[{batch_num}/{total_batches}]`" if total_batches > 1 else ""
                    batch_caption = f"🏷️ **{gallery_name}**{part_info}\n🔗 [Source URL]({url})"

                    if len(batch) == 1:
                        await safe_tg_call(
                            client.send_document,
                            chat_id=query.message.chat.id,
                            document=str(batch[0]),
                            caption=batch_caption,
                            reply_to_message_id=query.message.reply_to_message_id,
                        )
                    else:
                        last_idx = len(batch) - 1
                        media_group = [
                            InputMediaDocument(
                                media=str(f),
                                # Attach caption strictly to the bottom item of this batch
                                caption=batch_caption if idx == last_idx else ""
                            )
                            for idx, f in enumerate(batch)
                        ]
                        await safe_tg_call(
                            client.send_media_group,
                            chat_id=query.message.chat.id,
                            media=media_group,
                            reply_to_message_id=query.message.reply_to_message_id,
                        )
                    await asyncio.sleep(1.5)

            # -------------------- MODE 3: NORMAL MEDIA / ALBUMS --------------------
            elif action == "gdl_norm":
                await status_msg.edit_text(f"📤 **Uploading {total_files} media items...**")

                album_items = []
                singles = []

                for file_path in all_files:
                    ext = file_path.suffix.lower()
                    f_size = file_path.stat().st_size

                    # Telegram has a 10MB limit for compressed send_photo
                    if ext in IMAGE_EXTS and f_size <= 10 * 1024 * 1024:
                        album_items.append(("photo", file_path))
                    elif ext in VIDEO_EXTS and f_size <= TG_MAX_FILE_SIZE:
                        album_items.append(("video", file_path))
                    else:
                        singles.append(file_path)

                total_album_batches = (len(album_items) + 9) // 10

                # Send albums in chunks of 10
                for i in range(0, len(album_items), 10):
                    chunk = album_items[i : i + 10]
                    batch_num = (i // 10) + 1

                    part_info = f" `[{batch_num}/{total_album_batches}]`" if total_album_batches > 1 else ""
                    batch_caption = f"🏷️ **{gallery_name}**{part_info}\n🔗 [Source URL]({url})"

                    await status_msg.edit_text(
                        f"📤 **Uploading media ({min(i + 10, len(album_items))}/{len(album_items)})...**"
                    )

                    if len(chunk) == 1:
                        m_type, f_path = chunk[0]
                        if m_type == "photo":
                            await safe_tg_call(
                                client.send_photo,
                                chat_id=query.message.chat.id,
                                photo=str(f_path),
                                caption=batch_caption,
                                reply_to_message_id=query.message.reply_to_message_id,
                            )
                        else:
                            await safe_tg_call(
                                client.send_video,
                                chat_id=query.message.chat.id,
                                video=str(f_path),
                                caption=batch_caption,
                                reply_to_message_id=query.message.reply_to_message_id,
                            )
                    else:
                        last_idx = len(chunk) - 1
                        media_group = []
                        for idx, (m_type, f_path) in enumerate(chunk):
                            # Put caption on the LAST item of EVERY chunk so it stays anchored at the bottom
                            cap = batch_caption if idx == last_idx else ""
                            if m_type == "photo":
                                media_group.append(InputMediaPhoto(media=str(f_path), caption=cap))
                            else:
                                media_group.append(InputMediaVideo(media=str(f_path), caption=cap))

                        try:
                            await safe_tg_call(
                                client.send_media_group,
                                chat_id=query.message.chat.id,
                                media=media_group,
                                reply_to_message_id=query.message.reply_to_message_id,
                            )
                        except (MediaEmpty, WebpageCurlFailed):
                            for idx_f, (_, f_path) in enumerate(chunk):
                                await safe_tg_call(
                                    client.send_document,
                                    chat_id=query.message.chat.id,
                                    document=str(f_path),
                                    caption=batch_caption if idx_f == len(chunk) - 1 else None,
                                )
                    await asyncio.sleep(2)

                # Standalone single items (GIFs or oversized >10MB images)
                for idx, f_path in enumerate(singles):
                    single_caption = f"🏷️ **{gallery_name}**\n🔗 [Source URL]({url})"
                    if f_path.suffix.lower() in GIF_EXTS:
                        await safe_tg_call(
                            client.send_animation,
                            chat_id=query.message.chat.id,
                            animation=str(f_path),
                            caption=single_caption,
                            reply_to_message_id=query.message.reply_to_message_id,
                        )
                    else:
                        await safe_tg_call(
                            client.send_document,
                            chat_id=query.message.chat.id,
                            document=str(f_path),
                            caption=single_caption,
                            reply_to_message_id=query.message.reply_to_message_id,
                        )
                    await asyncio.sleep(1.5)

            await status_msg.delete()

        except Exception as e:
            logger.exception("Error during gallery task execution:")
            await status_msg.edit_text(f"❌ **An error occurred:**\n`{str(e)}`")

        finally:
            if job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
