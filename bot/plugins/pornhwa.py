import asyncio
import aiohttp
import html
import re

from bot.config import PORNHWADB_API_KEY
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


# ─────────────────────────────────────────────────────────────
# API Configuration
# ─────────────────────────────────────────────────────────────

API_BASE = "https://pornhwadb.com/api/v1"

API_HEADERS = {
    "X-API-Key": PORNHWADB_API_KEY
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

MAX_RESULTS = 7
MAX_EXTERNAL_LINKS = 3


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def esc(text) -> str:
    """Escape text for Telegram HTML formatting."""

    if text is None:
        return ""

    return html.escape(str(text))


def clean_query(text: str) -> tuple[str, str]:
    """
    Extract the search query and type flag.

    Supported:

        /search title
        /search title --pornhwa
        /search title --character
        /search title --all
    """

    text = text.strip()

    search_type = "pornhwa"

    if re.search(r"\s--character(?:\s|$)", text, re.I):
        search_type = "character"

        text = re.sub(
            r"\s--character(?:\s|$)",
            " ",
            text,
            flags=re.I,
        )

    elif re.search(r"\s--all(?:\s|$)", text, re.I):
        search_type = "all"

        text = re.sub(
            r"\s--all(?:\s|$)",
            " ",
            text,
            flags=re.I,
        )

    elif re.search(r"\s--pornhwa(?:\s|$)", text, re.I):
        search_type = "pornhwa"

        text = re.sub(
            r"\s--pornhwa(?:\s|$)",
            " ",
            text,
            flags=re.I,
        )

    return text.strip(), search_type


def get_type_label(search_type: str) -> str:

    return {
        "pornhwa": "📚 Pornhwa",
        "character": "👤 Character",
        "all": "🔎 All",
    }.get(search_type, "🔎 Search")


def format_list(items: list) -> str:
    """Format a simple list as Telegram bullets."""

    return "\n".join(
        f"• {esc(item)}"
        for item in items
        if item
    )


# ─────────────────────────────────────────────────────────────
# API Request
# ─────────────────────────────────────────────────────────────

async def api_get(endpoint: str, params: dict | None = None):
    """
    Perform an authenticated GET request.

    Returns:
        (json_data, status)
    """

    try:
        async with aiohttp.ClientSession(
            headers=API_HEADERS,
            timeout=REQUEST_TIMEOUT,
        ) as session:

            async with session.get(
                f"{API_BASE}{endpoint}",
                params=params,
            ) as response:

                if response.status != 200:
                    return None, response.status

                return await response.json(), response.status

    except asyncio.TimeoutError:
        return None, "timeout"

    except aiohttp.ClientError:
        return None, "connection"

    except Exception:
        return None, "unknown"


# ─────────────────────────────────────────────────────────────
# Search Result Buttons
# ─────────────────────────────────────────────────────────────

def build_search_buttons(
    data: dict,
    search_type: str,
) -> list[list[InlineKeyboardButton]]:

    buttons = []

    if search_type == "pornhwa":

        for item in data.get("pornhwa", [])[:MAX_RESULTS]:

            title = item.get("title") or "Unknown Title"
            slug = item.get("slug")

            if not slug:
                continue

            buttons.append([
                InlineKeyboardButton(
                    f"📖 {title}",
                    callback_data=f"phd|{slug}",
                )
            ])

    elif search_type == "character":

        for item in data.get("characters", [])[:MAX_RESULTS]:

            name = item.get("name") or "Unknown Character"
            character_id = item.get("id")

            if not character_id:
                continue

            buttons.append([
                InlineKeyboardButton(
                    f"👤 {name}",
                    callback_data=f"phc|{character_id}",
                )
            ])

    else:
        # type=all returns separate pornhwa and character arrays.
        # Combine them and cap the final result count at 7.

        results = []

        for item in data.get("pornhwa", []):

            title = item.get("title") or "Unknown Title"
            slug = item.get("slug")

            if slug:
                results.append(
                    (
                        f"📖 {title}",
                        f"phd|{slug}",
                    )
                )

        for item in data.get("characters", []):

            name = item.get("name") or "Unknown Character"
            character_id = item.get("id")

            if character_id:
                results.append(
                    (
                        f"👤 {name}",
                        f"phc|{character_id}",
                    )
                )

        for label, callback in results[:MAX_RESULTS]:

            buttons.append([
                InlineKeyboardButton(
                    label,
                    callback_data=callback,
                )
            ])

    return buttons


# ─────────────────────────────────────────────────────────────
# /search
# ─────────────────────────────────────────────────────────────

@Client.on_message(filters.command("search"))
async def search_cmd(client: Client, message: Message):

    if len(message.command) < 2:

        await message.reply_text(
            "🔎 <b>Usage:</b>\n\n"
            "<code>/search query</code>\n\n"
            "<b>Flags:</b>\n"
            "• <code>--pornhwa</code> — Search pornhwa\n"
            "• <code>--character</code> — Search characters\n"
            "• <code>--all</code> — Search everything\n\n"
            "<b>Examples:</b>\n"
            "<code>/search solo leveling</code>\n"
            "<code>/search john --character</code>\n"
            "<code>/search hero --all</code>"
        )

        return

    raw_query = message.text.split(None, 1)[1]

    query, search_type = clean_query(raw_query)

    if not query:

        await message.reply_text(
            "❌ <b>Please provide a search query.</b>"
        )

        return

    if len(query) > 200:

        await message.reply_text(
            "❌ <b>Search query is too long.</b>\n"
            "Maximum length is 200 characters."
        )

        return

    msg = await message.reply_text(
        "🔎 <b>Searching...</b>"
    )

    data, status = await api_get(
        "/search",
        params={
            "q": query,
            "type": search_type,
            "page": 1,
            "limit": MAX_RESULTS,
        },
    )

    if data is None:

        if status in (401, 403):

            text = (
                "❌ <b>API authentication failed.</b>"
            )

        elif status == "timeout":

            text = (
                "⏱️ <b>API request timed out.</b>"
            )

        elif status == "connection":

            text = (
                "🌐 <b>Could not connect to PornhwaDB.</b>"
            )

        else:

            text = (
                "❌ <b>Something went wrong while searching.</b>"
            )

        await msg.edit_text(text)

        return

    response_data = data.get("data", {})

    buttons = build_search_buttons(
        response_data,
        search_type,
    )

    if not buttons:

        await msg.edit_text(
            f"🔎 <b>No results found.</b>\n\n"
            f"<b>Query:</b> <code>{esc(query)}</code>\n"
            f"<b>Type:</b> {get_type_label(search_type)}"
        )

        return

    await msg.edit_text(
        f"🔎 <b>Search Results</b>\n\n"
        f"<b>Query:</b> <code>{esc(query)}</code>\n"
        f"<b>Type:</b> {get_type_label(search_type)}\n\n"
        f"👇 <b>Select an item:</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─────────────────────────────────────────────────────────────
# External Link Buttons
# ─────────────────────────────────────────────────────────────

def build_external_link_buttons(
    external_links: list,
) -> list[list[InlineKeyboardButton]]:

    buttons = []

    valid_links = []

    for link in external_links:

        if not isinstance(link, dict):
            continue

        site_name = link.get("siteName")
        url = link.get("url")

        if not site_name or not url:
            continue

        valid_links.append(
            (site_name, url)
        )

    # Only show the first 3 valid external links.
    for site_name, url in valid_links[:MAX_EXTERNAL_LINKS]:

        buttons.append([
            InlineKeyboardButton(
                f"🌐 {site_name}",
                url=url,
            )
        ])

    return buttons


# ─────────────────────────────────────────────────────────────
# Pornhwa Details
# ─────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^phd\|"))
async def pornhwa_details(
    client: Client,
    query: CallbackQuery,
):

    try:

        slug = query.data.split("|", 1)[1]

    except (IndexError, AttributeError):

        await query.answer(
            "❌ Invalid item.",
            show_alert=True,
        )

        return

    await query.answer(
        "📖 Loading details..."
    )

    data, status = await api_get(
        f"/pornhwa/{slug}"
    )

    if data is None:

        if status == 404:

            text = "❌ <b>Item not found.</b>"

        elif status in (401, 403):

            text = (
                "❌ <b>API authentication failed.</b>"
            )

        elif status == "timeout":

            text = (
                "⏱️ <b>Request timed out.</b>"
            )

        elif status == "connection":

            text = (
                "🌐 <b>Could not connect to PornhwaDB.</b>"
            )

        else:

            text = (
                "❌ <b>Failed to fetch details.</b>"
            )

        await query.message.edit_text(text)

        return

    item = data.get("data", {})

    title = item.get("title") or "Unknown Title"

    description = (
        item.get("description")
        or "No description available."
    )

    status_text = (
        item.get("status")
        or "Unknown"
    )

    orientation = item.get("orientation")

    artists = item.get("artists") or []
    authors = item.get("authors") or []
    genres = item.get("genreTags") or []

    alternative_titles = (
        item.get("alternativeTitles")
        or []
    )

    rating = item.get("averageRating")
    total_ratings = item.get("totalRatings") or 0

    chapter_count = item.get("chapterCount")
    total_chapters = item.get("totalChapters")

    release_year = item.get("releaseYear")
    release_month = item.get("releaseMonth")

    end_year = item.get("endYear")
    end_month = item.get("endMonth")

    characters = item.get("characters") or []

    external_links = (
        item.get("externalLinks")
        or []
    )

    # ────────────────────────────────────────────────────────
    # Basic Information
    # ────────────────────────────────────────────────────────

    lines = [
        f"📖 <b>{esc(title)}</b>",
        "",
        f"📝 <b>Description:</b>\n{esc(description)}",
        "",
        f"📊 <b>Status:</b> {esc(status_text)}",
    ]

    if orientation:

        lines.append(
            f"🔄 <b>Orientation:</b> "
            f"{esc(orientation)}"
        )

    if rating is not None:

        lines.append(
            f"⭐ <b>Rating:</b> "
            f"{esc(rating)} "
            f"({esc(total_ratings)} ratings)"
        )

    if chapter_count is not None:

        if total_chapters:

            chapter_text = (
                f"{chapter_count}/{total_chapters}"
            )

        else:

            chapter_text = str(chapter_count)

        lines.append(
            f"📚 <b>Chapters:</b> "
            f"{esc(chapter_text)}"
        )

    # ────────────────────────────────────────────────────────
    # Release Information
    # ────────────────────────────────────────────────────────

    if release_year:

        release = str(release_year)

        if release_month:

            release += (
                f"-{int(release_month):02d}"
            )

        lines.append(
            f"📅 <b>Released:</b> "
            f"{esc(release)}"
        )

    if end_year:

        ended = str(end_year)

        if end_month:

            ended += (
                f"-{int(end_month):02d}"
            )

        lines.append(
            f"🏁 <b>Ended:</b> "
            f"{esc(ended)}"
        )

    # ────────────────────────────────────────────────────────
    # Artists / Authors / Genres
    # ────────────────────────────────────────────────────────

    if artists:

        lines.append(
            f"🎨 <b>Artists:</b> "
            f"{esc(', '.join(map(str, artists)))}"
        )

    if authors:

        lines.append(
            f"✍️ <b>Authors:</b> "
            f"{esc(', '.join(map(str, authors)))}"
        )

    if genres:

        lines.append(
            f"🏷️ <b>Genres:</b> "
            f"{esc(', '.join(map(str, genres)))}"
        )

    # ────────────────────────────────────────────────────────
    # Alternative Titles
    # ────────────────────────────────────────────────────────

    if alternative_titles:

        lines.append(
            "🔤 <b>Alternative Titles:</b>\n"
            + format_list(
                alternative_titles
            )
        )

    # ────────────────────────────────────────────────────────
    # Characters
    # ────────────────────────────────────────────────────────

    if characters:

        character_names = [
            character.get("name")
            for character in characters
            if character.get("name")
        ]

        if character_names:

            lines.append(
                "👥 <b>Characters:</b>\n"
                + format_list(character_names)
            )

    lines.append("")

    lines.append(
        f"🆔 <b>ID:</b> "
        f"<code>{esc(item.get('id', 'N/A'))}</code>"
    )

    text = "\n".join(lines)

    # ────────────────────────────────────────────────────────
    # Buttons
    # ────────────────────────────────────────────────────────

    buttons = build_external_link_buttons(
        external_links
    )

    buttons.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="phback",
        )
    ])

    keyboard = InlineKeyboardMarkup(buttons)

    cover = item.get("coverImage")

    try:

        if cover:

            await query.message.delete()

            await client.send_photo(
                chat_id=query.message.chat.id,
                photo=cover,
                caption=text,
                reply_markup=keyboard,
            )

        else:

            await query.message.edit_text(
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

    except Exception:

        try:

            await query.message.edit_text(
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        except Exception:

            pass


# ─────────────────────────────────────────────────────────────
# Character Details
# ─────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^phc\|"))
async def character_details(
    client: Client,
    query: CallbackQuery,
):

    try:

        character_id = int(
            query.data.split("|", 1)[1]
        )

    except (IndexError, ValueError, AttributeError):

        await query.answer(
            "❌ Invalid character.",
            show_alert=True,
        )

        return

    await query.answer(
        "👤 Loading character..."
    )

    data, status = await api_get(
        f"/characters/{character_id}"
    )

    if data is None:

        if status == 404:

            text = (
                "❌ <b>Character not found.</b>"
            )

        elif status in (401, 403):

            text = (
                "❌ <b>API authentication failed.</b>"
            )

        elif status == "timeout":

            text = (
                "⏱️ <b>Request timed out.</b>"
            )

        elif status == "connection":

            text = (
                "🌐 <b>Could not connect to PornhwaDB.</b>"
            )

        else:

            text = (
                "❌ <b>Failed to fetch character details.</b>"
            )

        await query.message.edit_text(text)

        return

    item = data.get("data", {})

    name = (
        item.get("name")
        or "Unknown Character"
    )

    image = item.get("image")

    role = item.get("role")

    tags = item.get("tags") or []

    alternative_names = (
        item.get("alternativeNames")
        or []
    )

    description = item.get("description")

    pornhwa = item.get("pornhwa") or {}

    pornhwa_id = pornhwa.get("id")
    pornhwa_title = pornhwa.get("title")
    pornhwa_slug = pornhwa.get("slug")

    # ────────────────────────────────────────────────────────
    # Character Information
    # ────────────────────────────────────────────────────────

    lines = [
        f"👤 <b>{esc(name)}</b>",
    ]

    if description:

        lines.extend([
            "",
            f"📝 <b>Description:</b>\n"
            f"{esc(description)}",
        ])

    if role:

        lines.append(
            f"🎭 <b>Role:</b> "
            f"{esc(role)}"
        )

    if tags:

        lines.append(
            "🏷️ <b>Tags:</b>\n"
            + format_list(tags)
        )

    if alternative_names:

        lines.append(
            "🔤 <b>Alternative Names:</b>\n"
            + format_list(alternative_names)
        )

    if pornhwa_title:

        lines.extend([
            "",
            f"📖 <b>Pornhwa:</b> "
            f"{esc(pornhwa_title)}",
        ])

    if pornhwa_id is not None:

        lines.append(
            f"🆔 <b>Pornhwa ID:</b> "
            f"<code>{esc(pornhwa_id)}</code>"
        )

    lines.append(
        f"👤 <b>Character ID:</b> "
        f"<code>{esc(item.get('id', 'N/A'))}</code>"
    )

    text = "\n".join(lines)

    # ────────────────────────────────────────────────────────
    # Character Buttons
    # ────────────────────────────────────────────────────────

    buttons = []

    if pornhwa_slug:

        buttons.append([
            InlineKeyboardButton(
                f"📖 {pornhwa_title or 'View Pornhwa'}",
                callback_data=f"phd|{pornhwa_slug}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="phback",
        )
    ])

    keyboard = InlineKeyboardMarkup(buttons)

    # ────────────────────────────────────────────────────────
    # Send Character Image
    # ────────────────────────────────────────────────────────

    try:

        if image:

            await query.message.delete()

            await client.send_photo(
                chat_id=query.message.chat.id,
                photo=image,
                caption=text,
                reply_markup=keyboard,
            )

        else:

            await query.message.edit_text(
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

    except Exception:

        try:

            await query.message.edit_text(
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        except Exception:

            pass


# ─────────────────────────────────────────────────────────────
# Back Button
# ─────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^phback$"))
async def back_callback(
    client: Client,
    query: CallbackQuery,
):

    await query.answer()

    await query.message.edit_text(
        "🔎 <b>Search session ended.</b>\n\n"
        "Use the search command again:\n\n"
        "<code>/search your query</code>"
    )