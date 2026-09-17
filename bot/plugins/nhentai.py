import asyncio
import re
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
from bot import logger
from bot.utils.nhentai_dl import cleanup_dir_and_files, create_cbz_archive, make_progress_bar
from bot.config import NHENTAI_API_KEY
from pyrogram import Client, filters, enums
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# Constants
API_URL = "https://nhentai.net/api/v2/galleries/{}"
SEARCH_URL = "https://nhentai.net/api/v2/search"
COVER_URL_TEMPLATE = "https://t1.nhentai.net/{}"
GALLERY_URL_TEMPLATE = "https://nhentai.net/g/{}/"

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Key {NHENTAI_API_KEY}",
    "User-Agent": "NHentaiBot/1.0 (https://github.com/prono69)",
}

# Configuration constants
SEARCH_RESULT_LIMIT = 6
TAG_DISPLAY_LIMIT = 12
API_TIMEOUT_SECONDS = 15
CACHE_EXPIRY_SECONDS = 3600  # 1 hour

# Response cache to avoid duplicate API calls
_gallery_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def extract_gallery_id(text: str) -> Optional[str]:
    """
    Extract gallery ID from text or URL.
    
    Args:
        text: Raw user input (ID, URL, or search query)
    
    Returns:
        Gallery ID as string, or None if not found/invalid
    """
    text = text.strip()
    
    # Direct numeric ID
    if text.isdigit():
        gallery_id = text
        if len(gallery_id) > 0:  # Ensure it's not empty
            return gallery_id
        return None

    # URL pattern: nhentai.net/g/123456
    match = re.search(
        r"https?://(?:www\.)?nhentai\.net/g/(\d+)/?",
        text,
        re.IGNORECASE,
    )
    
    if match:
        return match.group(1)
    
    return None


def format_upload_date(timestamp: int) -> str:
    """
    Format Unix timestamp to human-readable date.
    
    Args:
        timestamp: Unix timestamp
    
    Returns:
        Formatted date string or "Unknown" on error
    """
    try:
        return datetime.fromtimestamp(timestamp).strftime("%d %B %Y")
    except (ValueError, OSError, OverflowError):
        logger.warning(f"Failed to format timestamp: {timestamp}")
        return "Unknown"


def get_tag_names(tags: List[Dict[str, Any]], tag_type: Optional[str] = None) -> List[str]:
    """
    Extract tag names, optionally filtered by type.
    
    Args:
        tags: List of tag dictionaries from API
        tag_type: Optional filter (e.g., "artist", "group", "tag")
    
    Returns:
        List of tag name strings
    """
    if not tags:
        return []
    
    result = []
    for tag in tags:
        # If filtering by type, skip mismatches
        if tag_type and tag.get("type") != tag_type:
            continue
        
        name = tag.get("name")
        if name:
            result.append(name)
    
    return result


def _clear_expired_cache() -> None:
    """Remove expired entries from gallery cache."""
    now = datetime.now()
    expired_keys = [
        key for key, (_, timestamp) in _gallery_cache.items()
        if (now - timestamp).total_seconds() > CACHE_EXPIRY_SECONDS
    ]
    for key in expired_keys:
        del _gallery_cache[key]
        logger.debug(f"Cleared expired cache entry: {key}")


def clean_gallery_title(title: str) -> str:
    """Clean nHentai gallery title for display by removing metadata, tags, and language markers."""
    if not title:
        return "Unknown"

    # 1. Strip leading bracketed tags: [Artist], [Circle (Artist)], (Event)
    title = re.sub(
        r"^(\s*(\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}))*\s*", "", title
    )

    # 2. Strip trailing bracketed tags: [English], [Digital], (C99), {Decensored}
    title = re.sub(
        r"(\s*(\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}))*\s*$", "", title
    )

    # 3. Strip trailing language translations after pipe (e.g. "| English")
    title = re.sub(
        r"\s*\|\s*(English|Japanese|Chinese|Korean|Russian|French|German|Spanish|Translated)\b.*$",
        "",
        title,
        flags=re.IGNORECASE,
    )

    # 4. Collapse extra whitespace
    title = re.sub(r"\s+", " ", title).strip()

    return title if title else "Unknown"


# ============================================================================
# API FUNCTIONS
# ============================================================================

async def fetch_gallery_data(gallery_id: str) -> Tuple[Optional[Dict[str, Any]], int]:
    """
    Fetch gallery data from nhentai API with caching.
    
    Args:
        gallery_id: nhentai gallery ID
    
    Returns:
        Tuple of (data dict or None, HTTP status code)
    """
    # Check cache first
    _clear_expired_cache()
    if gallery_id in _gallery_cache:
        data, _ = _gallery_cache[gallery_id]
        logger.debug(f"Cache hit for gallery {gallery_id}")
        return data, 200

    url = API_URL.format(gallery_id)
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=HEADERS) as response:
                if response.status == 200:
                    data = await response.json()
                    # Cache the result
                    _gallery_cache[gallery_id] = (data, datetime.now())
                    logger.info(f"Fetched gallery {gallery_id} from API")
                    return data, 200
                elif response.status == 404:
                    logger.warning(f"Gallery {gallery_id} not found (404)")
                    return None, 404
                else:
                    logger.error(f"API error for gallery {gallery_id}: {response.status}")
                    return None, response.status
                    
    except asyncio.TimeoutError:
        logger.error(f"API timeout for gallery {gallery_id}")
        return None, 504  # Gateway Timeout
    except aiohttp.ClientConnectorError as e:
        logger.error(f"Connection error for gallery {gallery_id}: {e}")
        return None, 503  # Service Unavailable
    except aiohttp.ClientError as e:
        logger.error(f"HTTP client error for gallery {gallery_id}: {e}")
        return None, 500
    except Exception as e:
        logger.error(f"Unexpected error fetching gallery {gallery_id}: {e}")
        return None, 500


async def search_galleries(query: str, page: int = 1) -> Tuple[Optional[List[Dict]], int]:
    """
    Search galleries by query.
    
    Args:
        query: Search query string
        page: Page number (default 1)
    
    Returns:
        Tuple of (results list or None, HTTP status code)
    """
    params = {"query": query, "sort": "popular", "page": page}
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(SEARCH_URL, headers=HEADERS, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("result", [])
                    logger.info(f"Search for '{query}' returned {len(results)} results")
                    return results, 200
                else:
                    logger.error(f"Search API error: {response.status}")
                    return None, response.status
                    
    except asyncio.TimeoutError:
        logger.error(f"Search timeout for query: {query}")
        return None, 504
    except aiohttp.ClientConnectorError as e:
        logger.error(f"Search connection error: {e}")
        return None, 503
    except aiohttp.ClientError as e:
        logger.error(f"Search HTTP error: {e}")
        return None, 500
    except Exception as e:
        logger.error(f"Unexpected search error: {e}")
        return None, 500


# ============================================================================
# MESSAGE FORMATTING & SENDING
# ============================================================================

def _build_gallery_caption(data: Dict[str, Any], gallery_id: str) -> str:
    """
    Build formatted caption for gallery information.
    
    Args:
        data: Gallery data from API
        gallery_id: Gallery ID for fallback
    
    Returns:
        Formatted HTML caption string
    """
    title_data = data.get("title", {})
    title = (
        title_data.get("pretty")
        or title_data.get("english")
        or title_data.get("japanese")
        or "Unknown"
    )

    tags = data.get("tags", [])
    artists = get_tag_names(tags, "artist")
    groups = get_tag_names(tags, "group")
    languages = get_tag_names(tags, "language")
    categories = get_tag_names(tags, "category")
    parodies = get_tag_names(tags, "parody")
    regular_tags = get_tag_names(tags, "tag")[:TAG_DISPLAY_LIMIT]

    pages = data.get("num_pages", 0)
    favorites = data.get("num_favorites", 0)
    upload_date = format_upload_date(data.get("upload_date", 0))

    caption = (
        "📚 <b>Gallery Information</b>\n\n"
        f"🆔 <b>ID:</b> <code>{data.get('id', gallery_id)}</code>\n"
        f"📖 <b>Title:</b> `{title}`\n\n"
        f"👤 <b>Artist:</b> `{', '.join(artists) if artists else 'Unknown'}`\n"
        f"👥 <b>Group:</b> `{', '.join(groups) if groups else 'Unknown'}`\n"
        f"🌐 <b>Language:</b> `{', '.join(languages) if languages else 'Unknown'}`\n"
        f"📂 <b>Category:</b> `{', '.join(categories) if categories else 'Unknown'}`\n"
        f"🎭 <b>Parody:</b> `{', '.join(parodies) if parodies else 'Original'}`\n"
        f"📄 <b>Pages:</b> `{pages}`\n"
        f"❤️ <b>Favorites:</b> `{favorites:,}`\n"
        f"📅 <b>Uploaded:</b> `{upload_date}`\n\n"
        f"🏷 <b>Tags:</b>\n"
        f"{' • '.join(regular_tags) if regular_tags else 'None'}"
    )

    return caption


def _build_gallery_keyboard(gallery_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for gallery info."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔗 Open Gallery",
                    url=GALLERY_URL_TEMPLATE.format(gallery_id),
                    style=enums.ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    "📥 Download CBZ", callback_data=f"nhdl_{gallery_id}",
                    style=enums.ButtonStyle.PRIMARY
                ),
            ]
        ]
    )


async def send_gallery_info(
    message_or_query: Any,
    data: Dict[str, Any],
    gallery_id: str,
) -> bool:
    """
    Send gallery information as a message with photo or text.
    
    Args:
        message_or_query: Message or CallbackQuery object
        data: Gallery data from API
        gallery_id: Gallery ID
    
    Returns:
        True if successful, False otherwise
    """
    caption = _build_gallery_caption(data, gallery_id)
    keyboard = _build_gallery_keyboard(gallery_id)

    cover = data.get("cover", {})
    cover_path = cover.get("path")
    
    target_msg = (
        message_or_query.message
        if isinstance(message_or_query, CallbackQuery)
        else message_or_query
    )

    # Try to send with photo first
    if cover_path:
        cover_url = COVER_URL_TEMPLATE.format(cover_path)
        try:
            if isinstance(message_or_query, CallbackQuery):
                await target_msg.delete()
                await target_msg.reply_photo(
                    photo=cover_url,
                    caption=caption,
                    reply_markup=keyboard,
                )
            else:
                await target_msg.reply_photo(
                    photo=cover_url,
                    caption=caption,
                    reply_markup=keyboard,
                )
            logger.info(f"Sent gallery {gallery_id} with photo")
            return True
        except aiohttp.ClientError as e:
            logger.warning(f"Failed to fetch cover photo for {gallery_id}: {e}")
        except Exception as e:
            logger.warning(f"Failed to send photo for {gallery_id}: {e}")

    # Fallback to text-only
    try:
        if isinstance(message_or_query, CallbackQuery):
            await target_msg.edit_text(caption, reply_markup=keyboard)
        else:
            await target_msg.reply_text(caption, reply_markup=keyboard)
        logger.info(f"Sent gallery {gallery_id} as text (photo unavailable)")
        return True
    except Exception as e:
        logger.error(f"Failed to send gallery {gallery_id} as text: {e}")
        return False


async def send_search_results(
    status: Message,
    results: List[Dict[str, Any]],
    query: str,
) -> bool:
    """
    Send search results as inline buttons with cleaned titles.
    
    Args:
        status: Status message to edit
        results: List of gallery results
        query: Original search query
    
    Returns:
        True if successful, False otherwise
    """
    if not results:
        try:
            await status.edit_text("❌ <b>No galleries found matching your query.</b>")
            return False
        except Exception as e:
            logger.error(f"Failed to edit status for empty results: {e}")
            return False

    buttons = []
    for item in results[:SEARCH_RESULT_LIMIT]:
        raw_title = item.get("english_title") or f"Gallery {item.get('id')}"
        # Clean the title by removing artist credits, tags, and metadata
        cleaned_title = clean_gallery_title(raw_title)
        
        # Truncate cleaned title for button display (max 40 chars for better UX)
        btn_title = f"🔍 {cleaned_title[:40] + '...' if len(cleaned_title) > 40 else cleaned_title}"
        item_id = item.get("id")

        buttons.append(
            [InlineKeyboardButton(text=btn_title, callback_data=f"nhget_{item_id}", style=enums.ButtonStyle.PRIMARY)]
        )

    reply_markup = InlineKeyboardMarkup(buttons)
    
    try:
        await status.edit_text(
            f"🔍 <b>Search results for:</b> <i>{query}</i>\nSelect a gallery below:",
            reply_markup=reply_markup,
        )
        logger.info(f"Sent {len(buttons)} search results for query: {query}")
        return True
    except Exception as e:
        logger.error(f"Failed to send search results: {e}")
        return False


# ============================================================================
# MESSAGE HANDLERS
# ============================================================================

@Client.on_message(filters.command("nh"))
async def nhentai_handler(client: Client, message: Message) -> None:
    """Handle /nh command for direct ID/URL or text search."""
    if len(message.command) < 2:
        await message.reply_text(
            "<b>Usage:</b>\n\n"
            "• Direct Gallery: <code>/nh 644225</code>\n"
            "• Gallery URL: <code>/nh https://nhentai.net/g/123456/</code>\n"
            "• Search Text: <code>/nh Milf</code>"
        )
        return

    query = message.text.split(maxsplit=1)[1].strip()
    gallery_id = extract_gallery_id(query)

    # ========== DIRECT ID / URL MATCH ==========
    if gallery_id:
        status = await message.reply_text("🔎 <i>Fetching gallery information...</i>")
        try:
            data, status_code = await fetch_gallery_data(gallery_id)
            
            if status_code == 404:
                await status.edit_text(
                    f"❌ <b>Gallery not found.</b>\n🆔 ID: <code>{gallery_id}</code>"
                )
                logger.info(f"Gallery {gallery_id} not found")
                return
            elif status_code == 504:
                await status.edit_text(
                    "⏱️ <b>Request timed out.</b> The API is slow. Try again in a moment."
                )
                return
            elif status_code == 503:
                await status.edit_text(
                    "🌐 <b>API temporarily unavailable.</b> Try again later."
                )
                return
            elif status_code != 200:
                await status.edit_text(
                    f"❌ <b>API request failed.</b>\n"
                    f"Status: <code>{status_code}</code>"
                )
                return

            await status.delete()
            await send_gallery_info(message, data, gallery_id)
            
        except Exception as e:
            logger.error(f"Error in nhentai_handler (ID lookup): {e}")
            try:
                await status.edit_text(
                    "❌ <b>Something went wrong while fetching gallery information.</b>"
                )
            except:
                pass
        return

    # ========== TEXT SEARCH MATCH ==========
    status = await message.reply_text(f"🔎 <i>Searching for:</i> <b>{query}</b>...")
    
    try:
        results, status_code = await search_galleries(query, page=1)
        
        if status_code == 504:
            await status.edit_text(
                "⏱️ <b>Search timed out.</b> The API is slow. Try again in a moment."
            )
            return
        elif status_code == 503:
            await status.edit_text(
                "🌐 <b>API temporarily unavailable.</b> Try again later."
            )
            return
        elif status_code != 200:
            await status.edit_text(
                f"❌ <b>Search failed.</b>\n"
                f"Status: <code>{status_code}</code>"
            )
            return

        await send_search_results(status, results, query)
        
    except Exception as e:
        logger.error(f"Error in nhentai_handler (search): {e}")
        try:
            await status.edit_text(
                "❌ <b>Something went wrong while searching.</b>"
            )
        except:
            pass


@Client.on_callback_query(filters.regex(r"^nhget_(\d+)$"))
async def nhentai_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Handle callback queries from search result buttons."""
    gallery_id = callback_query.data.split("_")[1]
    
    try:
        await callback_query.answer("Fetching gallery details...")

        data, status_code = await fetch_gallery_data(gallery_id)
        
        if status_code != 200:
            error_msgs = {
                404: "Gallery not found.",
                504: "Request timed out. Try again.",
                503: "API temporarily unavailable.",
            }
            msg = error_msgs.get(status_code, f"Error (HTTP {status_code})")
            await callback_query.message.reply_text(f"❌ <b>{msg}</b>")
            logger.warning(f"Callback error for gallery {gallery_id}: {status_code}")
            return

        await send_gallery_info(callback_query, data, gallery_id)
        
    except Exception as e:
        logger.error(f"Error in nhentai_callback: {e}")
        try:
            await callback_query.message.reply_text(
                "❌ <b>An error occurred while loading details.</b>"
            )
        except:
            pass


@Client.on_callback_query(filters.regex(r"^nhdl_(\d+)$"))
async def nhentai_download_callback(
    client: Client, callback_query: CallbackQuery
) -> None:
    """Handle CBZ download request with strict FloodWait prevention."""
    gallery_id = callback_query.data.split("_")[1]

    await callback_query.answer("⏳ Starting download...", show_alert=False)
    status_msg = await callback_query.message.reply_text(
        "🚀 <b>Initializing download...</b>"
    )

    data, status_code = await fetch_gallery_data(gallery_id)
    if status_code != 200 or not data:
        return await status_msg.edit_text(
            "❌ <b>Failed to fetch gallery details.</b>"
        )

    last_update_time = [0.0]  # Store timestamp for throttling edits

    async def update_progress(current: int, total: int, phase: str):
        now = time.time()
        # Strictly throttle edits to every 5 seconds to prevent FloodWait
        if (now - last_update_time[0]) >= 5.0 or current == total:
            last_update_time[0] = now
            bar = make_progress_bar(current, total)
            try:
                if phase == "downloading":
                    await status_msg.edit_text(
                        f"📥 <b>Downloading Pages...</b>\n\n"
                        f"<code>{bar}</code>\n"
                        f"<b>Progress:</b> <code>{current}/{total}</code> pages"
                    )
            except Exception:
                pass

    temp_dir = f"/tmp/nh_{gallery_id}"
    cbz_path = None
    thumb_path = None

    try:
        # 1. Download pages & package CBZ
        cbz_path, thumb_path = await create_cbz_archive(
            data, progress_callback=update_progress
        )

        if not cbz_path or not os.path.exists(cbz_path):
            return await status_msg.edit_text(
                "❌ <b>Failed to process pages or generate CBZ.</b>"
            )

        # 2. Update status to Uploading
        await status_msg.edit_text("📤 <b>Uploading CBZ to Telegram...</b>")

        # 3. Extract title, total pages, and tags for caption
        title_obj = data.get("title", {})
        raw_name = (
            title_obj.get("pretty")
            or title_obj.get("english")
            or title_obj.get("japanese")
            or f"Gallery {gallery_id}"
        )
        name = clean_gallery_title(raw_name)

        total_pages = data.get("num_pages", len(data.get("pages", [])))

        # Format tags as hashtags: #big_breasts #anal #milf
        raw_tags = get_tag_names(data.get("tags", []), "tag")[:12]
        formatted_tags = " ".join(
            [f"#{t.lower().replace(' ', '_').replace('-', '_')}" for t in raw_tags]
        )

        caption = (
            f"📖 <b>{name}</b>\n\n"
            f"📊 <b>Format:</b> CBZ ({total_pages} Pages)\n"
            f"🔗 <b>Source:</b> nHentai `#{gallery_id}`\n\n"
            f"🏷 {formatted_tags if formatted_tags else '#nHentai'}"
        )

        # 4. Send CBZ document with thumbnail & caption
        await callback_query.message.reply_document(
            document=cbz_path,
            thumb=thumb_path if (thumb_path and os.path.exists(thumb_path)) else None,
            file_name=f"[{gallery_id}] {name[:35]}.cbz",
            caption=caption,
        )

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Error in nhentai_download_callback: {e}")
        await status_msg.edit_text(
            "❌ <b>An unexpected error occurred during download/upload.</b>"
        )
    finally:
        # Cleanup temp directory and generated CBZ archive
        cleanup_dir_and_files(temp_dir, cbz_path)
