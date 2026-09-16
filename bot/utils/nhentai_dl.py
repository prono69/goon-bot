import asyncio
import os
import random
import shutil
import zipfile
from typing import Any, Callable, List, Optional, Tuple

import aiohttp
from bot import logger

HEADERS = {
    "User-Agent": "NHentaiBot/1.0 (https://github.com/prono69)",
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
    """Download the cover/thumbnail image to use as Telegram document thumbnail."""
    cover_obj = gallery_data.get("cover") or gallery_data.get("thumbnail")
    if not cover_obj or not cover_obj.get("path"):
        return None

    rel_path = cover_obj.get("path")
    ext = rel_path.split(".")[-1] if "." in rel_path else "jpg"
    thumb_path = os.path.join(temp_dir, f"thumb.{ext}")

    semaphore = asyncio.Semaphore(1)
    success = await download_file_with_fallback(
        session, THUMB_SERVERS, rel_path, thumb_path, semaphore
    )

    return thumb_path if success else None


async def create_cbz_archive(
    gallery_data: dict,
    progress_callback: Optional[Callable[[int, int, str], Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Download gallery pages using CDN server fallbacks and build a CBZ file.

    Returns:
        Tuple of (cbz_file_path, thumbnail_file_path)
    """
    gallery_id = str(gallery_data.get("id"))
    pages = gallery_data.get("pages", [])

    if not pages:
        logger.error(f"No pages found for gallery {gallery_id}")
        return None, None

    total_pages = len(pages)
    temp_dir = f"/tmp/nh_{gallery_id}"
    cbz_path = f"/tmp/{gallery_id}.cbz"

    os.makedirs(temp_dir, exist_ok=True)
    semaphore = asyncio.Semaphore(5)  # Limit concurrent downloads to 5

    completed_count = 0

    async with aiohttp.ClientSession() as session:
        # Fetch thumbnail concurrently
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

            # Shuffle image servers for load balancing
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
        f for f in sorted(os.listdir(temp_dir)) if not f.startswith("thumb.")
    ]

    if not downloaded_files:
        logger.error(f"No pages downloaded for gallery {gallery_id}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, None

    # Compress into CBZ
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
