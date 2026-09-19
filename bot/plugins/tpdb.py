import asyncio
import html
import time
from typing import Any, Dict, List, Optional

import aiohttp
from bot.config import PORNDB_API_TOKEN
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# Configuration
API_URL = "https://api.theporndb.net/scenes"
API_TOKEN = PORNDB_API_TOKEN
PER_PAGE = 5
REQUEST_TIMEOUT = 15
CACHE_TTL = 30 * 60
MAX_CACHE_SIZE = 100

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


def build_clean_caption(title: str, details: Dict[str, Any], max_length: int = 1024) -> str:
    title = clean_value(title) or "Untitled"
    lines = [f"<b>🎬 {escape(title)}</b>", ""]

    for key, value in details.items():
        value = clean_value(value)
        if not value:
            continue
        value = truncate_text(value, 450)
        line = f"<b>{escape(key)}:</b> {escape(value)}"
        candidate = "\n".join(lines + [line])

        if len(candidate) <= max_length:
            lines.append(line)
        else:
            remaining = max_length - len("\n".join(lines)) - 1
            if remaining > 20:
                shortened = truncate_text(value, max(10, remaining - len(key) - 10))
                line = f"<b>{escape(key)}:</b> {escape(shortened)}"
                candidate = "\n".join(lines + [line])
                if len(candidate) <= max_length:
                    lines.append(line)

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


def get_scene_from_cache(cache: Dict[str, Any], scene_id: str) -> Optional[Dict[str, Any]]:
    scenes = cache.get("scenes", [])
    for scene in scenes:
        if str(scene.get("id")) == str(scene_id):
            return scene
    return None


def get_performer_from_scene(
    scene: Dict[str, Any], performer_id: str
) -> Optional[Dict[str, Any]]:
    for performer in scene.get("performers") or []:
        if str(performer.get("id")) == str(performer_id):
            return performer
    return None


async def fetch_scenes(query: str, page: int = 1) -> Optional[Dict[str, Any]]:
    if not API_TOKEN:
        raise RuntimeError("PORNDB_API_TOKEN environment variable is not configured.")

    headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}
    params = {"q": query, "per_page": PER_PAGE, "page": page}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(API_URL, params=params) as response:
                if response.status != 200:
                    return None
                return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


@Client.on_message(filters.command(["search", "pdb"]) & filters.text)
async def search_scenes(client: Client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("<b>Usage:</b> <code>/search &lt;query&gt;</code>")
        return

    query = parts[1].strip()
    status = await message.reply_text("🔎 <i>Searching records...</i>")

    try:
        data = await fetch_scenes(query)
        if not data or not data.get("data"):
            await status.edit_text("❌ No results found. Try a different search.")
            return

        scenes = data["data"]
        cache_key = get_cache_key(message)
        cache_data = {"scenes": scenes, "page": 1, "query": query}
        save_cache(cache_key, cache_data)

        await display_search_results(client, message, status, cache_key, scenes, 1)
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")


async def display_search_results(
    client: Client,
    message: Message,
    status: Message,
    cache_key: tuple,
    scenes: List[Dict[str, Any]],
    page: int,
):
    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    page_scenes = scenes[start:end]

    if not page_scenes:
        await status.edit_text("❌ No results on this page.")
        return

    cache = get_cache(cache_key)
    if not cache:
        await status.edit_text("❌ Session expired.")
        return

    first_scene = page_scenes[0]
    title = clean_value(first_scene.get("title")) or "Unknown Scene"
    poster = (
        first_scene.get("poster")
        or first_scene.get("image")
        or "https://via.placeholder.com/400x600?text=No+Image"
    )
    duration_str = format_duration(first_scene.get("duration"))
    release_date = clean_value(first_scene.get("release_date"))
    director = clean_value(first_scene.get("director"))
    site = first_scene.get("site") or {}
    site_name = (
        clean_value(site.get("name"))
        if isinstance(site, dict)
        else clean_value(site)
    )

    studios = first_scene.get("studios") or []
    studio_names = (
        ", ".join([clean_value(s.get("name")) or str(s) for s in studios if s])
        if studios
        else None
    )

    categories = first_scene.get("categories") or []
    category_names = (
        ", ".join([clean_value(c.get("name")) or str(c) for c in categories if c])
        if categories
        else None
    )

    metadata = {
        "📺 Site": site_name,
        "🎞️ Duration": duration_str,
        "📅 Release Date": release_date,
        "👤 Director": director,
        "🏢 Studio": studio_names,
        "🎬 Categories": category_names,
    }

    caption = build_clean_caption(title, metadata)
    buttons = []
    next_button_text = (
        "Next 📍" if page < len(scenes) // PER_PAGE + 1 else "Last Page"
    )

    buttons.append([InlineKeyboardButton(next_button_text, callback_data=f"page:{page+1}")])
    buttons.append([InlineKeyboardButton("📋 Scene Details", callback_data=f"show_sc:{first_scene.get('id')}")])
    buttons.append([InlineKeyboardButton("⛔ Close", callback_data="noop")])

    try:
        await status.delete()
    except Exception:
        pass

    await client.send_photo(
        chat_id=message.chat.id,
        photo=poster,
        caption=caption,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^page:(\d+)$"))
async def paginate_results(client: Client, callback: CallbackQuery):
    await callback.answer()
    page = int(callback.data.split(":")[1])
    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    scenes = cache.get("scenes", [])
    total_pages = (len(scenes) + PER_PAGE - 1) // PER_PAGE

    if page < 1 or page > total_pages:
        await callback.answer("Invalid page.", show_alert=True)
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    temp_status = await client.send_message(
        chat_id=callback.message.chat.id, text="⏳ <i>Loading...</i>"
    )
    await display_search_results(
        client, callback.message, temp_status, cache_key, scenes, page
    )


@Client.on_callback_query(filters.regex(r"^show_sc:(.+)$"))
async def show_scene_details(client: Client, callback: CallbackQuery):
    await callback.answer()
    scene_id = callback.data.split(":", 1)[1]
    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    target_scene = get_scene_from_cache(cache, scene_id)
    if not target_scene:
        await callback.answer("Scene data is no longer available.", show_alert=True)
        return

    poster = (
        target_scene.get("poster")
        or target_scene.get("image")
        or "https://via.placeholder.com/400x600?text=No+Image"
    )
    duration_str = format_duration(target_scene.get("duration"))
    release_date = clean_value(target_scene.get("release_date"))
    director = clean_value(target_scene.get("director"))
    site = target_scene.get("site") or {}
    site_name = (
        clean_value(site.get("name"))
        if isinstance(site, dict)
        else clean_value(site)
    )

    studios = target_scene.get("studios") or []
    studio_names = (
        ", ".join([clean_value(s.get("name")) or str(s) for s in studios if s])
        if studios
        else None
    )

    categories = target_scene.get("categories") or []
    category_names = (
        ", ".join([clean_value(c.get("name")) or str(c) for c in categories if c])
        if categories
        else None
    )

    tags = target_scene.get("tags") or []
    tag_names = (
        ", ".join([clean_value(t.get("name")) or str(t) for t in tags if t])
        if tags
        else None
    )

    plot = clean_value(target_scene.get("plot"))

    metadata = {
        "📺 Site": site_name,
        "🎞️ Duration": duration_str,
        "📅 Release Date": release_date,
        "👤 Director": director,
        "🏢 Studio": studio_names,
        "🎬 Categories": category_names,
        "🏷️ Tags": tag_names,
        "📖 Plot": plot,
    }

    caption = build_clean_caption(target_scene.get("title", "Scene Details"), metadata)
    buttons = []

    scene_url = clean_value(target_scene.get("url"))
    if scene_url:
        buttons.append([InlineKeyboardButton("🔗 Open Scene Web Page", url=scene_url)])

    performers = target_scene.get("performers") or []
    if performers:
        buttons.append(
            [InlineKeyboardButton("🎭 Details About Performers", callback_data=f"list_perf:{scene_id}")]
        )

    current_page = cache.get("page", 1)
    buttons.append([InlineKeyboardButton("⬅️ Back to Results", callback_data=f"page:{current_page}")])

    try:
        await callback.message.delete()
    except Exception:
        pass

    await client.send_photo(
        chat_id=callback.message.chat.id,
        photo=poster,
        caption=caption,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^list_perf:(.+)$"))
async def list_performers(client: Client, callback: CallbackQuery):
    await callback.answer()
    scene_id = callback.data.split(":", 1)[1]
    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    scene = get_scene_from_cache(cache, scene_id)
    if not scene:
        await callback.answer("Scene data is no longer available.", show_alert=True)
        return

    performers = scene.get("performers") or []
    if not performers:
        await callback.answer("No performers listed for this item.", show_alert=True)
        return

    performer_buttons = []
    for performer in performers:
        performer_name = clean_value(performer.get("name")) or "Unknown Performer"
        performer_id = performer.get("id")
        if not performer_id:
            continue
        performer_buttons.append(
            InlineKeyboardButton(
                f"👤 {truncate_text(performer_name, 30)}",
                callback_data=f"show_perf:{scene_id}:{performer_id}",
            )
        )

    buttons = make_button_rows(performer_buttons, per_row=2)
    buttons.append([InlineKeyboardButton("⬅️ Back to Scene Details", callback_data=f"show_sc:{scene_id}")])

    try:
        await callback.message.delete()
    except Exception:
        pass

    await client.send_message(
        chat_id=callback.message.chat.id,
        text="<b>🎭 Select a Performer to view full profile:</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^show_perf:(.+):(.+)$"))
async def display_performer(client: Client, callback: CallbackQuery):
    await callback.answer()
    _, scene_id, performer_id = callback.data.split(":", 2)
    cache_key = get_cache_key(callback)
    cache = get_cache(cache_key)

    if not cache:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    scene = get_scene_from_cache(cache, scene_id)
    if not scene:
        await callback.answer("Scene data is no longer available.", show_alert=True)
        return

    target_performer = get_performer_from_scene(scene, performer_id)
    if not target_performer:
        await callback.answer("Could not load performer information.", show_alert=True)
        return

    parent_data = target_performer.get("parent") or {}
    extras = {**(parent_data.get("extras") or {}), **(target_performer.get("extra") or {})}

    photo = (
        target_performer.get("image")
        or target_performer.get("thumbnail")
        or target_performer.get("face")
        or parent_data.get("image")
        or parent_data.get("thumbnail")
        or parent_data.get("face")
    )

    if not photo:
        await callback.answer("No performer photo available.", show_alert=True)
        return

    bio_text = target_performer.get("bio") or parent_data.get("bio")

    performer_details = {
        "⚧ Gender": extras.get("gender"),
        "🎂 Birthday": extras.get("birthday"),
        "📍 Birthplace": extras.get("birthplace"),
        "🌐 Nationality": extras.get("nationality"),
        "📏 Height": extras.get("height"),
        "⚖️ Weight": extras.get("weight"),
        "📐 Measurements": extras.get("measurements"),
        "☕ Cup Size": extras.get("cupsize"),
        "👁 Eye Color": extras.get("eye_colour") or extras.get("eye_color"),
        "💇 Hair Color": extras.get("haircolor") or extras.get("hair_colour"),
        "✨ Astrology": extras.get("astrology"),
        "📖 Biography": bio_text,
    }

    performer_name = (
        clean_value(target_performer.get("name"))
        or clean_value(parent_data.get("name"))
        or "Performer Info"
    )

    caption = build_clean_caption(performer_name, performer_details)
    buttons = [[InlineKeyboardButton("⬅️ Back to Performers List", callback_data=f"list_perf:{scene_id}")]]

    try:
        await callback.message.delete()
    except Exception:
        pass

    await client.send_photo(
        chat_id=callback.message.chat.id,
        photo=photo,
        caption=caption,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^noop$"))
async def noop_callback(client: Client, callback: CallbackQuery):
    await callback.answer()
