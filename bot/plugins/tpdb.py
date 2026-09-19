import asyncio
import html
import math
import time
from typing import Any, Dict, List, Optional

import aiohttp
from bot.config import PORNDB_API_TOKEN
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

# Configuration
API_URL = "https://api.theporndb.net/scenes"
API_TOKEN = PORNDB_API_TOKEN
RESULTS_PER_PAGE = 5
REQUEST_TIMEOUT = 15
CACHE_TTL = 30 * 60
MAX_CACHE_SIZE = 100
DEFAULT_POSTER = "https://via.placeholder.com/400x600?text=No+Image"

# INCREASED CAPTION LIMIT for performer bios
SCENE_CAPTION_LIMIT = 1024
PERFORMER_CAPTION_LIMIT = 1024  # Much higher limit for performer cards

USER_SEARCH_CACHE: Dict[Any, Dict[str, Any]] = {}


def get_cache_key(message_or_callback) -> tuple:
    chat = message_or_callback.chat
    user = message_or_callback.from_user
    chat_id = chat.id if chat else 0
    user_id = user.id if user else 0
    return chat_id, user_id


def cleanup_cache() -> None:
    now = time.time()
    expired = [
        key
        for key, value in USER_SEARCH_CACHE.items()
        if now - value.get("created_at", 0) > CACHE_TTL
    ]
    for key in expired:
        USER_SEARCH_CACHE.pop(key, None)

    if len(USER_SEARCH_CACHE) > MAX_CACHE_SIZE:
        oldest = sorted(
            USER_SEARCH_CACHE.items(), key=lambda item: item[1].get("created_at", 0)
        )
        remove_count = len(USER_SEARCH_CACHE) - MAX_CACHE_SIZE
        for key, _ in oldest[:remove_count]:
            USER_SEARCH_CACHE.pop(key, None)


def save_cache(cache_key: tuple, data: Dict[str, Any]) -> None:
    cleanup_cache()
    USER_SEARCH_CACHE[cache_key] = {**data, "created_at": time.time()}


def get_cache(cache_key: tuple) -> Optional[Dict[str, Any]]:
    cache = USER_SEARCH_CACHE.get(cache_key)
    if not cache:
        return None
    if time.time() - cache.get("created_at", 0) > CACHE_TTL:
        USER_SEARCH_CACHE.pop(cache_key, None)
        return None
    return cache


def clean_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(x) for x in value if x)
    value = str(value).strip()
    if not value:
        return None
    if value.lower() in {"n/a", "none", "null", "unknown"}:
        return None
    return value


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def truncate_text(text: str, max_length: int) -> str:
    text = clean_value(text) or ""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def build_clean_caption(
    title: str, 
    details: Dict[str, Any], 
    max_length: int = 1024, 
    special_field: str = "📖 Description"
) -> str:
    """
    FIXED: Special handling for descriptions/bios to ensure they always display
    """
    title = clean_value(title) or "Untitled"
    lines = [f"<b>🎬 {escape(title)}</b>", ""]

    # Extract the special field (description/bio) separately
    special_field_value = details.get(special_field)
    special_field_value = clean_value(special_field_value)

    # Build other fields first (non-description)
    for key, value in details.items():
        if key == special_field:  # Skip, we'll add this last
            continue
            
        value = clean_value(value)
        if not value:
            continue
        value = truncate_text(value, 450)
        
        line = f"<b>{escape(key)}:</b> <code>{escape(value)}</code>"
        
        candidate = "\n".join(lines + [line])

        if len(candidate) <= max_length:
            lines.append(line)
        else:
            remaining = max_length - len("\n".join(lines)) - 1
            if remaining > 20:
                shortened = truncate_text(value, max(10, remaining - len(key) - 10))
                line = f"<b>{escape(key)}:</b> <code>{escape(shortened)}</code>"
                candidate = "\n".join(lines + [line])
                if len(candidate) <= max_length:
                    lines.append(line)

    # ADD DESCRIPTION/BIO LAST - This ensures it's always included if space permits
    if special_field_value:
        desc_line = f"<b>{escape(special_field)}:</b> \n<i>{escape(special_field_value)}</i>"
        candidate = "\n".join(lines + [desc_line])
        
        if len(candidate) <= max_length:
            lines.append(desc_line)
        else:
            # Even if we're over limit, try to include a truncated version of the bio
            remaining = max_length - len("\n".join(lines)) - 1
            if remaining > 50:  # Only if we have meaningful space left
                shortened = truncate_text(special_field_value, max(30, remaining - len(special_field) - 10))
                desc_line = f"<b>{escape(special_field)}:</b> <i>{escape(shortened)}</i>"
                lines.append(desc_line)

    return "\n".join(lines).strip()


def make_button_rows(
    buttons: List[InlineKeyboardButton], per_row: int = 2
) -> List[List[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def format_duration(duration: Any) -> Optional[str]:
    if duration is None:
        return None
    try:
        seconds = int(float(duration))
    except (TypeError, ValueError):
        return None

    if seconds <= 0:
        return None

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def get_scene_from_cache(cache: Dict[str, Any], scene_index: int) -> Optional[Dict[str, Any]]:
    scenes = cache.get("all_scenes", [])
    if 0 <= scene_index < len(scenes):
        return scenes[scene_index]
    return None


def get_performer_by_index(
    scene: Dict[str, Any], performer_index: int
) -> Optional[Dict[str, Any]]:
    performers = scene.get("performers") or []
    if 0 <= performer_index < len(performers):
        return performers[performer_index]
    return None


def build_grid_payload(cache: Dict[str, Any], grid_page: int = 1) -> tuple:
    scenes = cache.get("all_scenes", [])
    total_scenes = len(scenes)
    total_pages = math.ceil(total_scenes / RESULTS_PER_PAGE) or 1
    grid_page = max(1, min(grid_page, total_pages))

    cache["grid_page"] = grid_page

    start_idx = (grid_page - 1) * RESULTS_PER_PAGE
    end_idx = min(start_idx + RESULTS_PER_PAGE, total_scenes)
    page_scenes = scenes[start_idx:end_idx]

    scene_buttons = []
    for offset, scene in enumerate(page_scenes):
        actual_index = start_idx + offset
        title = clean_value(scene.get("title")) or "Untitled"
        button_text = truncate_text(title, 38)
        scene_buttons.append(
            InlineKeyboardButton(
                f"🎬 {button_text}",
                callback_data=f"view_scene:{actual_index}",
            )
        )

    buttons = make_button_rows(scene_buttons, per_row=1)

    if total_pages > 1:
        nav_row = []
        if grid_page > 1:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"grid_page:{grid_page - 1}"))
        else:
            nav_row.append(InlineKeyboardButton("⛔", callback_data="noop"))

        nav_row.append(InlineKeyboardButton(f"Page {grid_page}/{total_pages}", callback_data="noop"))

        if grid_page < total_pages:
            nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"grid_page:{grid_page + 1}"))
        else:
            nav_row.append(InlineKeyboardButton("⛔", callback_data="noop"))

        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("❌ Close", callback_data="close_menu")])

    caption = f"""
<b>🔍 Search Results</b>
Query: <code>{escape(cache.get('query', 'Unknown'))}</code>
Found: <b>{cache.get('total_results', total_scenes)}</b> total results

Page <b>{grid_page}</b> of <b>{total_pages}</b> (Showing {start_idx + 1}-{end_idx} of {total_scenes})
Click any title below to view full details 👇
"""
    return caption.strip(), InlineKeyboardMarkup(buttons)


async def fetch_scenes(query: str, page: int = 1) -> Optional[Dict[str, Any]]:
    if not API_TOKEN:
        raise RuntimeError("PORNDB_API_TOKEN environment variable is not configured.")

    headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}
    params = {"q": query, "per_page": 25, "page": page}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(API_URL, params=params) as response:
                if response.status != 200:
                    return None
                return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


@Client.on_message(filters.command("pdb") & filters.text)
async def search_scenes(client: Client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("<b>Usage:</b> <code>/search &lt;query&gt;</code>")
        return

    query = parts[1].strip()
    status = await message.reply_text("🔎 <i>Searching records...</i>")

    try:
        data = await fetch_scenes(query, page=1)
        if not data or not data.get("data"):
            await status.edit_text("❌ No results found. Try a different search.")
            return

        scenes = data["data"]
        total_results = data.get("meta", {}).get("total", len(scenes))

        cache_key = get_cache_key(message)
        cache_data = {
            "all_scenes": scenes,
            "query": query,
            "total_results": total_results,
            "grid_page": 1,
        }
        save_cache(cache_key, cache_data)

        text, reply_markup = build_grid_payload(cache_data, grid_page=1)
        await status.edit_text(text=text, reply_markup=reply_markup)
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")


@Client.on_callback_query(filters.regex(r"^grid_page:(\d+)$"))
async def paginate_grid(client: Client, callback: CallbackQuery):
    grid_page = int(callback.data.split(":")[1])
    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    text, reply_markup = build_grid_payload(cache, grid_page=grid_page)

    try:
        await callback.message.edit_text(text=text, reply_markup=reply_markup)
        await callback.answer()
    except Exception:
        await callback.answer("Already on this page.")


@Client.on_callback_query(filters.regex(r"^view_scene:(\d+)$"))
async def view_scene_details(client: Client, callback: CallbackQuery):
    await callback.answer()
    scene_index = int(callback.data.split(":")[1])
    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    scene = get_scene_from_cache(cache, scene_index)
    if not scene:
        await callback.answer("Scene data is no longer available.", show_alert=True)
        return

    poster = (
        scene.get("image")
        or scene.get("poster")
        or DEFAULT_POSTER
    )

    duration_str = format_duration(scene.get("duration"))
    release_date = clean_value(scene.get("date") or scene.get("release_date"))
    director = clean_value(scene.get("director"))
    site = scene.get("site") or {}
    site_name = (
        clean_value(site.get("name"))
        if isinstance(site, dict)
        else clean_value(site)
    )

    studios = scene.get("studios") or []
    studio_names = (
        ", ".join([clean_value(s.get("name")) or str(s) for s in studios if s])
        if studios
        else None
    )

    tags = scene.get("tags") or []
    tag_names = (
        ", ".join([clean_value(t.get("name")) or str(t) for t in tags if t])
        if tags
        else None
    )

    performers = scene.get("performers") or []
    performer_names = (
        ", ".join([clean_value(p.get("name")) or str(p) for p in performers if p])
        if performers
        else None
    )

    # FIXED: Use 'description' field (API provides 'description', not 'plot')
    plot = clean_value(scene.get("description"))

    metadata = {
        "📺 Site": site_name,
        "🎞️ Duration": duration_str,
        "📅 Release Date": release_date,
        "👤 Director": director,
        "🏢 Studio": studio_names,
        "🎭 Performers": performer_names,
        "🏷️ Tags": tag_names,
        "📖 Description": plot,
    }

    caption = build_clean_caption(
        scene.get("title", "Scene Details"), 
        metadata,
        max_length=SCENE_CAPTION_LIMIT
    )
    
    buttons = []

    scene_url = clean_value(scene.get("url"))
    if scene_url:
        buttons.append([InlineKeyboardButton("🔗 Open Scene Web Page", url=scene_url)])

    if performers:
        buttons.append(
            [InlineKeyboardButton("🎭 View Performers", callback_data=f"list_perf:{scene_index}")]
        )

    buttons.append([InlineKeyboardButton("❌ Close Card", callback_data="close_menu")])

    try:
        await client.send_photo(
            chat_id=callback.message.chat.id,
            photo=poster,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        await client.send_photo(
            chat_id=callback.message.chat.id,
            photo=DEFAULT_POSTER,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


@Client.on_callback_query(filters.regex(r"^list_perf:(\d+)$"))
async def list_performers(client: Client, callback: CallbackQuery):
    await callback.answer("Fetching Randis")
    scene_index = int(callback.data.split(":")[1])
    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    scene = get_scene_from_cache(cache, scene_index)
    if not scene:
        await callback.answer("Scene data is no longer available.", show_alert=True)
        return

    performers = scene.get("performers") or []
    if not performers:
        await callback.answer("No performers listed for this item.", show_alert=True)
        return

    # QoL IMPROVEMENT: Single Performer Shortcut
    # If there is only 1 performer, route directly to display_performer
    if len(performers) == 1:
        # Update callback data dynamically to point to performer index 0
        callback.data = f"show_perf:{scene_index}:0"
        await display_performer(client, callback)
        return

    # Otherwise, show the selection grid for multiple performers
    performer_buttons = []
    for performer_index, performer in enumerate(performers):
        performer_name = clean_value(performer.get("name")) or "Unknown Performer"
        performer_buttons.append(
            InlineKeyboardButton(
                f"👤 {truncate_text(performer_name, 30)}",
                callback_data=f"show_perf:{scene_index}:{performer_index}",
            )
        )

    buttons = make_button_rows(performer_buttons, per_row=2)
    buttons.append([InlineKeyboardButton("⬅️ Back to Scene", callback_data=f"back_to_scene:{scene_index}")])

    await client.send_message(
        chat_id=callback.message.chat.id,
        text="<b>🎭 Select a Performer to view full profile:</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
    )



@Client.on_callback_query(filters.regex(r"^show_perf:(\d+):(\d+)$"))
async def display_performer(client: Client, callback: CallbackQuery):
    await callback.answer()
    scene_index = int(callback.data.split(":")[1])
    performer_index = int(callback.data.split(":")[2])

    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    scene = get_scene_from_cache(cache, scene_index)
    if not scene:
        await callback.answer("Scene data is no longer available.", show_alert=True)
        return

    target_performer = get_performer_by_index(scene, performer_index)
    if not target_performer:
        await callback.answer("Could not load performer information.", show_alert=True)
        return

    # FIXED: Properly handle bio retrieval with fallback chain
    parent_data = target_performer.get("parent") or {}
    
    # Try multiple bio locations
    bio_text = (
        clean_value(target_performer.get("bio"))
        or clean_value(parent_data.get("bio"))
    )

    parent_extras = parent_data.get("extras") or parent_data.get("extra") or {}
    performer_extra = target_performer.get("extra") or {}

    extras = {**performer_extra, **parent_extras}

    photo = (
        target_performer.get("image")
        or target_performer.get("thumbnail")
        or target_performer.get("face")
        or parent_data.get("image")
        or parent_data.get("thumbnail")
        or parent_data.get("face")
        or DEFAULT_POSTER
    )

    def get_extra(key1, key2=None):
        value = extras.get(key1)
        if not value and key2:
            value = extras.get(key2)
        return value

    performer_details = {
        "⚧ Gender": get_extra("gender"),
        "⭐ Rating": clean_value(parent_data.get("rating")),
        "🎂 Birthday": get_extra("birthday"),
        "📍 Birthplace": get_extra("birthplace"),
        "🌐 Nationality": get_extra("nationality"),
        "📏 Height": get_extra("height"),
        "⚖️ Weight": get_extra("weight"),
        "📐 Measurements": get_extra("measurements"),
        "☕ Cup Size": get_extra("cupsize"),
        "👁 Eye Color": get_extra("eye_colour", "eye_color"),
        "💇 Hair Color": get_extra("haircolor", "hair_colour"),
        "✨ Astrology": get_extra("astrology"),
        "💪 Tattoos": get_extra("tattoos"),
        "📌 Piercings": get_extra("piercings"),
        "✂️ Fake Boobs": get_extra("fakeboobs"),
        "👶 Ethnicity": get_extra("ethnicity"),
        "📖 Description": bio_text,  # Bio is now guaranteed to be retrieved correctly
    }

    performer_name = (
        clean_value(target_performer.get("name"))
        or clean_value(parent_data.get("name"))
        or "Performer Info"
    )

    # FIXED: Use higher caption limit for performers to ensure bios display
    caption = build_clean_caption(
        performer_name, 
        performer_details,
        max_length=PERFORMER_CAPTION_LIMIT
    )
    
    # Platforms to skip
    SKIP_PLATFORMS = {"IAFD", "DATA18", "Indexxx", "StashDB", "Wikidata"}

    buttons = []

    # Add social/profile links (2 per row)
    links = parent_data.get("extras", {}).get("links", {})
    if links:
        link_buttons = []
        for platform, url in links.items():
            if platform in SKIP_PLATFORMS:
                continue
            
            if url:
                link_buttons.append(
                    InlineKeyboardButton(f"🔗 {platform}", url=url)
                )
        
        if link_buttons:
            buttons.extend(make_button_rows(link_buttons, per_row=2))

    buttons.append([InlineKeyboardButton("⬅️ Back to Performers", callback_data=f"list_perf:{scene_index}")])
    await callback.answer("Sending performer's details")

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        await client.send_photo(
            chat_id=callback.message.chat.id,
            photo=photo,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        await client.send_photo(
            chat_id=callback.message.chat.id,
            photo=DEFAULT_POSTER,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


@Client.on_callback_query(filters.regex(r"^back_to_scene:(\d+)$"))
async def back_to_scene(client: Client, callback: CallbackQuery):
    await callback.answer()
    scene_index = int(callback.data.split(":")[1])
    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    scene = get_scene_from_cache(cache, scene_index)
    if not scene:
        await callback.answer("Scene data is no longer available.", show_alert=True)
        return

    poster = scene.get("poster") or scene.get("image") or DEFAULT_POSTER
    duration_str = format_duration(scene.get("duration"))
    release_date = clean_value(scene.get("date") or scene.get("release_date"))
    director = clean_value(scene.get("director"))
    site = scene.get("site") or {}
    site_name = clean_value(site.get("name")) if isinstance(site, dict) else clean_value(site)

    studios = scene.get("studios") or []
    studio_names = ", ".join([clean_value(s.get("name")) or str(s) for s in studios if s]) if studios else None

    tags = scene.get("tags") or []
    tag_names = ", ".join([clean_value(t.get("name")) or str(t) for t in tags if t]) if tags else None

    performers = scene.get("performers") or []
    performer_names = ", ".join([clean_value(p.get("name")) or str(p) for p in performers if p]) if performers else None

    plot = clean_value(scene.get("description"))

    metadata = {
        "📺 Site": site_name,
        "🎞️ Duration": duration_str,
        "📅 Release Date": release_date,
        "👤 Director": director,
        "🏢 Studio": studio_names,
        "🎭 Performers": performer_names,
        "🏷️ Tags": tag_names,
        "📖 Description": plot,
    }

    caption = build_clean_caption(
        scene.get("title", "Scene Details"),
        metadata,
        max_length=SCENE_CAPTION_LIMIT
    )
    
    buttons = []

    scene_url = clean_value(scene.get("url"))
    if scene_url:
        buttons.append([InlineKeyboardButton("🔗 Open Scene Web Page", url=scene_url)])

    if performers:
        buttons.append(
            [InlineKeyboardButton("🎭 View Performers", callback_data=f"list_perf:{scene_index}")]
        )

    buttons.append([InlineKeyboardButton("❌ Close Card", callback_data="close_menu")])

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        await client.send_photo(
            chat_id=callback.message.chat.id,
            photo=poster,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        await client.send_photo(
            chat_id=callback.message.chat.id,
            photo=DEFAULT_POSTER,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


@Client.on_callback_query(filters.regex(r"^close_menu$"))
async def close_menu(client: Client, callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^noop$"))
async def noop_callback(client: Client, callback: CallbackQuery):
    await callback.answer()
