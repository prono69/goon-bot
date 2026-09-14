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


API_BASE = "https://pornhwadb.com/api/v1"
API_HEADERS = {
    "X-API-Key": PORNHWADB_API_KEY
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def clean_query(text: str) -> tuple[str, str]:
    """
    Extract search query and search type from command arguments.

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
        text = re.sub(r"\s--character(?:\s|$)", " ", text, flags=re.I)

    elif re.search(r"\s--all(?:\s|$)", text, re.I):
        search_type = "all"
        text = re.sub(r"\s--all(?:\s|$)", " ", text, flags=re.I)

    elif re.search(r"\s--pornhwa(?:\s|$)", text, re.I):
        search_type = "pornhwa"
        text = re.sub(r"\s--pornhwa(?:\s|$)", " ", text, flags=re.I)

    return text.strip(), search_type


def esc(text) -> str:
    """Escape text for Telegram HTML formatting."""
    if text is None:
        return ""

    return html.escape(str(text))


def get_type_label(search_type: str) -> str:
    return {
        "pornhwa": "📚 Pornhwa",
        "character": "👤 Character",
        "all": "🔎 All",
    }.get(search_type, "🔎 Search")


def result_buttons(
    data: dict,
    search_type: str,
    page: int,
    limit: int,
) -> list[list[InlineKeyboardButton]]:

    buttons = []

    if search_type == "pornhwa":
        items = data.get("pornhwa", [])

        for item in items:
            title = item.get("title") or "Unknown Title"
            slug = item.get("slug")

            if not slug:
                continue

            buttons.append([
                InlineKeyboardButton(
                    f"📖 {title}",
                    callback_data=f"phd|{slug}"
                )
            ])

    elif search_type == "character":
        items = data.get("characters", [])

        for item in items:
            name = item.get("name") or "Unknown Character"

            # Character endpoint was not provided.
            # We therefore store the parent pornhwa slug so
            # clicking the character can show its parent details.
            slug = item.get("pornhwaSlug")

            if not slug:
                continue

            buttons.append([
                InlineKeyboardButton(
                    f"👤 {name}",
                    callback_data=f"phd|{slug}"
                )
            ])

    else:
        # type=all returns both lists.
        for item in data.get("pornhwa", []):
            title = item.get("title") or "Unknown Title"
            slug = item.get("slug")

            if slug:
                buttons.append([
                    InlineKeyboardButton(
                        f"📖 {title}",
                        callback_data=f"phd|{slug}"
                    )
                ])

        for item in data.get("characters", []):
            name = item.get("name") or "Unknown Character"
            slug = item.get("pornhwaSlug")

            if slug:
                buttons.append([
                    InlineKeyboardButton(
                        f"👤 {name}",
                        callback_data=f"phd|{slug}"
                    )
                ])

    return buttons


def pagination_buttons(
    pagination: dict,
    search_type: str,
    page: int,
    limit: int,
) -> list[list[InlineKeyboardButton]]:

    if search_type == "all":
        # Use pornhwa pagination as the primary page indicator.
        # If it has no results, use character pagination.
        page_data = pagination.get("pornhwa", {})

        if not page_data.get("total"):
            page_data = pagination.get("characters", {})

    else:
        key = "pornhwa" if search_type == "pornhwa" else "characters"
        page_data = pagination.get(key, {})

    current_page = page_data.get("page", page)
    total_pages = page_data.get("totalPages", 1)
    has_more = page_data.get("hasMore", False)

    buttons = []

    navigation = []

    if current_page > 1:
        navigation.append(
            InlineKeyboardButton(
                "◀️ Previous",
                callback_data=f"phs|{search_type}|{page - 1}|{limit}"
            )
        )

    navigation.append(
        InlineKeyboardButton(
            f"📄 {current_page}/{total_pages}",
            callback_data="phnoop"
        )
    )

    if has_more or current_page < total_pages:
        navigation.append(
            InlineKeyboardButton(
                "Next ▶️",
                callback_data=f"phs|{search_type}|{page + 1}|{limit}"
            )
        )

    if navigation:
        buttons.append(navigation)

    return buttons


async def api_get(endpoint: str, params: dict | None = None):
    """Perform an authenticated GET request to PornhwaDB."""

    try:
        async with aiohttp.ClientSession(
            headers=API_HEADERS,
            timeout=REQUEST_TIMEOUT
        ) as session:

            async with session.get(
                f"{API_BASE}{endpoint}",
                params=params
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
# Search
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
            "<b>Example:</b>\n"
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
            "limit": 20,
        }
    )

    if data is None:
        if status == 401 or status == 403:
            text = "❌ <b>API authentication failed.</b>"
        elif status == "timeout":
            text = "⏱️ <b>API request timed out.</b>"
        elif status == "connection":
            text = "🌐 <b>Could not connect to PornhwaDB.</b>"
        else:
            text = "❌ <b>Something went wrong while searching.</b>"

        await msg.edit_text(text)
        return

    response_data = data.get("data", {})
    pagination = data.get("pagination", {})

    buttons = result_buttons(
        response_data,
        search_type,
        1,
        20,
    )

    buttons += pagination_buttons(
        pagination,
        search_type,
        1,
        20,
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
# Search Pagination
# ─────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^phs\|"))
async def search_pagination(client: Client, query: CallbackQuery):

    try:
        _, search_type, page, limit = query.data.split("|")

        page = int(page)
        limit = int(limit)

    except (ValueError, AttributeError):
        await query.answer(
            "❌ Invalid pagination data.",
            show_alert=True
        )
        return

    if page < 1 or page > 100:
        await query.answer(
            "❌ Invalid page.",
            show_alert=True
        )
        return

    # Retrieve the original search query from the message.
    message_text = query.message.text or ""

    match = re.search(
        r"<b>Query:</b>\s*<code>(.*?)</code>",
        message_text
    )

    if not match:
        await query.answer(
            "❌ Search session expired.",
            show_alert=True
        )
        return

    search_query = html.unescape(match.group(1))

    await query.answer("🔎 Loading...")

    data, status = await api_get(
        "/search",
        params={
            "q": search_query,
            "type": search_type,
            "page": page,
            "limit": limit,
        }
    )

    if data is None:
        await query.answer(
            "❌ Failed to load this page.",
            show_alert=True
        )
        return

    response_data = data.get("data", {})
    pagination = data.get("pagination", {})

    buttons = result_buttons(
        response_data,
        search_type,
        page,
        limit,
    )

    buttons += pagination_buttons(
        pagination,
        search_type,
        page,
        limit,
    )

    if not buttons:
        await query.answer(
            "❌ No results on this page.",
            show_alert=True
        )
        return

    await query.message.edit_text(
        f"🔎 <b>Search Results</b>\n\n"
        f"<b>Query:</b> <code>{esc(search_query)}</code>\n"
        f"<b>Type:</b> {get_type_label(search_type)}\n\n"
        f"👇 <b>Select an item:</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─────────────────────────────────────────────────────────────
# Details
# ─────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^phd\|"))
async def pornhwa_details(client: Client, query: CallbackQuery):

    try:
        slug = query.data.split("|", 1)[1]

    except (IndexError, AttributeError):
        await query.answer(
            "❌ Invalid item.",
            show_alert=True
        )
        return

    await query.answer("📖 Loading details...")

    data, status = await api_get(
        f"/pornhwa/{slug}"
    )

    if data is None:
        if status == 404:
            text = "❌ <b>Item not found.</b>"
        elif status == 401 or status == 403:
            text = "❌ <b>API authentication failed.</b>"
        elif status == "timeout":
            text = "⏱️ <b>Request timed out.</b>"
        else:
            text = "❌ <b>Failed to fetch details.</b>"

        await query.message.edit_text(text)
        return

    item = data.get("data", {})

    title = item.get("title") or "Unknown Title"
    description = item.get("description") or "No description available."

    status_text = item.get("status") or "Unknown"
    orientation = item.get("orientation")

    artists = item.get("artists") or []
    authors = item.get("authors") or []
    genres = item.get("genreTags") or []
    alternative_titles = item.get("alternativeTitles") or []

    rating = item.get("averageRating")
    total_ratings = item.get("totalRatings", 0)

    chapter_count = item.get("chapterCount")
    total_chapters = item.get("totalChapters")

    release_year = item.get("releaseYear")
    release_month = item.get("releaseMonth")

    end_year = item.get("endYear")
    end_month = item.get("endMonth")

    characters = item.get("characters") or []
    external_links = item.get("externalLinks") or []

    lines = [
        f"📖 <b>{esc(title)}</b>",
        "",
        f"📝 <b>Description:</b>\n{esc(description)}",
        "",
        f"📊 <b>Status:</b> {esc(status_text)}",
    ]

    if orientation:
        lines.append(
            f"🔄 <b>Orientation:</b> {esc(orientation)}"
        )

    if rating is not None:
        lines.append(
            f"⭐ <b>Rating:</b> {esc(rating)}"
            f" ({esc(total_ratings)} ratings)"
        )

    if chapter_count is not None:
        if total_chapters:
            chapter_text = f"{chapter_count}/{total_chapters}"
        else:
            chapter_text = str(chapter_count)

        lines.append(
            f"📚 <b>Chapters:</b> {esc(chapter_text)}"
        )

    if release_year:
        release = str(release_year)

        if release_month:
            release += f"-{release_month:02d}"

        lines.append(
            f"📅 <b>Released:</b> {esc(release)}"
        )

    if end_year:
        ended = str(end_year)

        if end_month:
            ended += f"-{end_month:02d}"

        lines.append(
            f"🏁 <b>Ended:</b> {esc(ended)}"
        )

    if artists:
        lines.append(
            f"🎨 <b>Artists:</b> {esc(', '.join(map(str, artists)))}"
        )

    if authors:
        lines.append(
            f"✍️ <b>Authors:</b> {esc(', '.join(map(str, authors)))}"
        )

    if genres:
        lines.append(
            f"🏷️ <b>Genres:</b> {esc(', '.join(map(str, genres)))}"
        )

    if alternative_titles:
        lines.append(
            "🔤 <b>Alternative Titles:</b>\n"
            + "\n".join(
                f"• {esc(title)}"
                for title in alternative_titles
            )
        )

    if characters:
        character_names = [
            character.get("name")
            for character in characters
            if character.get("name")
        ]

        if character_names:
            lines.append(
                "👥 <b>Characters:</b>\n"
                + "\n".join(
                    f"• {esc(name)}"
                    for name in character_names
                )
            )

    if external_links:
        lines.append("")
        lines.append("🔗 <b>External Links:</b>")

        for link in external_links:
            site = link.get("siteName") or "Link"
            url = link.get("url")

            if url:
                lines.append(
                    f'• <a href="{esc(url)}">{esc(site)}</a>'
                )

    lines.append("")
    lines.append(
        f"🆔 <b>ID:</b> <code>{esc(item.get('id', 'N/A'))}</code>"
    )

    text = "\n".join(lines)

    back_button = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔙 Back to Search",
                callback_data="phback"
            )
        ]
    ])

    cover = item.get("coverImage")

    try:
        if cover:
            await query.message.delete()

            await client.send_photo(
                chat_id=query.message.chat.id,
                photo=cover,
                caption=text,
                reply_markup=back_button,
            )
        else:
            await query.message.edit_text(
                text,
                reply_markup=back_button,
                disable_web_page_preview=True,
            )

    except Exception:
        # If sending the cover fails, fall back to a normal text message.
        try:
            await query.message.edit_text(
                text,
                reply_markup=back_button,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# No-op Pagination Button
# ─────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^phnoop$"))
async def noop_callback(client: Client, query: CallbackQuery):
    await query.answer()


# ─────────────────────────────────────────────────────────────
# Back Button
# ─────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^phback$"))
async def back_to_search(client: Client, query: CallbackQuery):

    await query.answer("🔙 Going back...")

    # Since Telegram callback messages don't retain arbitrary state,
    # the search result itself is reconstructed from the current message
    # when possible.
    await query.message.edit_text(
        "🔎 <b>Search session ended.</b>\n\n"
        "Please run the search command again.\n\n"
        "<code>/search your query</code>"
    )