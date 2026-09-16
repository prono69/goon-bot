import asyncio
import os
import random
import shutil
import zipfile
from typing import Any, Callable, List, Optional, Tuple

import aiohttp
from PIL import Image
from bot import logger

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

IMAGE_SERVERS = [
    "https://i1.nhentai.net",
    "https://i2.nhentai.net",
    "https://i3.nhentai.net",
    "https://i4.nhentai.net",
]

THUMB_SERVERS = [
    "https://t1.nhentai.net",
    "https://t2.nhentai.net",
    "https://t3.nhentai.net",
    "https://t4.nhentai.net",
]


async def download_file_with_fallback(
    session: aiohttp.ClientSession,
    servers: List[str],
    relative_path: str,
    save_path: str,
    semaphore: asyncio.Semaphore,
) -> bool:
    """Try downloading from servers in sequence until one succeeds."""
    async with semaphore:
        for base_url in servers:
            full_url = f"{base_url.rstrip('/')}/{relative_path.lstrip('/')}"
            try:
                async with session.get(full_url, headers=HEADERS) as response:
                    if response.status == 200:
                        content = await response.read()
                        with open(save_path, "wb") as f:
                            f.write(content)
                        return True
            except Exception as e:
                logger.warning(f"Failed to fetch {full_url}: {e}")
                continue
        return False


async def download_thumbnail(
    session: aiohttp.ClientSession,
    gallery_data: dict,
    temp_dir: str,
) -> Optional[str]:
    """Download cover image and convert to JPEG format for Telegram."""
    cover_obj = gallery_data.get("cover") or gallery_data.get("thumbnail")
    if not cover_obj or not cover_obj.get("path"):
        return None

    rel_path = cover_obj.get("path")
    raw_thumb_path = os.path.join(temp_dir, "raw_thumb")
    final_thumb_path = os.path.join(temp_dir, "thumb.jpg")

    semaphore = asyncio.Semaphore(1)
    success = await download_file_with_fallback(
        session, THUMB_SERVERS, rel_path, raw_thumb_path, semaphore
    )

    if not success or not os.path.exists(raw_thumb_path):
        return None

    # Convert thumbnail to JPEG format (required by Telegram API)
    try:
        with Image.open(raw_thumb_path) as img:
            img = img.convert("RGB")
            img.save(final_thumb_path, "JPEG")
        
        if os.path.exists(raw_thumb_path):
            os.remove(raw_thumb_path)
            
        return final_thumb_path
    except Exception as e:
        logger.error(f"Failed to process thumbnail for Telegram: {e}")
        return None


async def create_cbz_archive(
    gallery_data: dict,
    progress_callback: Optional[Callable[[int, int, str], Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Download gallery pages using CDN server fallbacks and build a CBZ file."""
    gallery_id = str(gallery_data.get("id"))
    pages = gallery_data.get("pages", [])

    if not pages:
        logger.error(f"No pages found for gallery {gallery_id}")
        return None, None

    total_pages = len(pages)
    temp_dir = f"/tmp/nh_{gallery_id}"
    cbz_path = f"/tmp/{gallery_id}.cbz"

    os.makedirs(temp_dir, exist_ok=True)
    semaphore = asyncio.Semaphore(5)

    completed_count = 0

    async with aiohttp.ClientSession() as session:
        # Fetch and format thumbnail concurrently
        thumb_task = asyncio.create_task(
            download_thumbnail(session, gallery_data, temp_dir)
        )

        tasks = []
        for idx, page_info in enumerate(pages, start=1):
            rel_path = page_info.get("path")
            if not rel_path:
                continue

            ext = rel_path.split(".")[-1] if "." in rel_path else "jpg"
            file_name = f"{idx:03d}.{ext}"
            save_path = os.path.join(temp_dir, file_name)

            servers = IMAGE_SERVERS.copy()
            random.shuffle(servers)

            async def task_wrapper(r_path=rel_path, s_path=save_path):
                nonlocal completed_count
                res = await download_file_with_fallback(
                    session, servers, r_path, s_path, semaphore
                )
                if res:
                    completed_count += 1
                    if progress_callback:
                        await progress_callback(
                            completed_count, total_pages, "downloading"
                        )
                return res

            tasks.append(task_wrapper())

        await asyncio.gather(*tasks)
        thumb_path = await thumb_task

    downloaded_files = [
        f for f in sorted(os.listdir(temp_dir)) if not f.startswith("thumb") and not f.startswith("raw_thumb")
    ]

    if not downloaded_files:
        logger.error(f"No pages downloaded for gallery {gallery_id}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, None

    try:
        with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as cbz:
            for file in downloaded_files:
                file_path = os.path.join(temp_dir, file)
                cbz.write(file_path, arcname=file)

        return cbz_path, thumb_path

    except Exception as e:
        logger.error(f"CBZ packaging error for {gallery_id}: {e}")
        return None, None


def cleanup_dir_and_files(*paths: Optional[str]) -> None:
    """Clean up files or directories used during processing."""
    for path in paths:
        if path and os.path.exists(path):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except Exception as e:
                logger.error(f"Error cleaning up path {path}: {e}")
