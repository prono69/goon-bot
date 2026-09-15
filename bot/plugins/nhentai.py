import re
from datetime import datetime

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


API_URL = "https://nhentai.net/api/v2/galleries/{}"


def extract_gallery_id(text: str):
    """
    Extract gallery ID from:
      /nh 644225
      /nh https://nhentai.net/g/644225/
      /nh https://nhentai.net/g/644225
    """

    text = text.strip()

    # Direct numeric ID
    if text.isdigit():
        return text

    # nhentai gallery URL
    match = re.search(
        r"https?://(?:www\.)?nhentai\.net/g/(\d+)/?",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return None


def format_upload_date(timestamp):
    """Convert Unix timestamp to a readable date."""

    try:
        return datetime.fromtimestamp(timestamp).strftime("%d %B %Y")
    except Exception:
        return "Unknown"


def get_tag_names(tags, tag_type=None):
    """Return tag names, optionally filtered by type."""

    if not tags:
        return []

    result = []

    for tag in tags:
        if tag_type and tag.get("type") != tag_type:
            continue

        name = tag.get("name")

        if name:
            result.append(name)

    return result


@Client.on_message(filters.command("nh"))
async def nhentai_info(client, message):

    # /nh <id or URL>
    if len(message.command) < 2:
        await message.reply_text(
            "<b>Usage:</b>\n\n"
            "<code>/nh 644225</code>\n"
            "<code>/nh https://nhentai.net/g/644225/</code>"
        )
        return

    query = message.text.split(maxsplit=1)[1].strip()

    gallery_id = extract_gallery_id(query)

    if not gallery_id:
        await message.reply_text(
            "❌ <b>Invalid gallery ID or URL.</b>\n\n"
            "Send an nhentai gallery ID or a gallery URL."
        )
        return

    status = await message.reply_text(
        "🔎 <i>Fetching gallery information...</i>"
    )

    try:
        url = API_URL.format(gallery_id)

        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        }

        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:

                if response.status == 404:
                    await status.edit_text(
                        f"❌ <b>Gallery not found.</b>\n\n"
                        f"🆔 ID: <code>{gallery_id}</code>"
                    )
                    return

                if response.status != 200:
                    await status.edit_text(
                        "❌ <b>API request failed.</b>\n\n"
                        f"HTTP Status: <code>{response.status}</code>"
                    )
                    return

                data = await response.json()

        # -----------------------------
        # Basic information
        # -----------------------------

        title_data = data.get("title", {})

        title = (
            title_data.get("pretty")
            or title_data.get("english")
            or title_data.get("japanese")
            or "Unknown"
        )

        # -----------------------------
        # Tags
        # -----------------------------

        tags = data.get("tags", [])

        artists = get_tag_names(tags, "artist")
        groups = get_tag_names(tags, "group")
        languages = get_tag_names(tags, "language")

        # Useful broad categories
        categories = get_tag_names(tags, "category")
        parodies = get_tag_names(tags, "parody")

        # Keep regular tags separate.
        regular_tags = get_tag_names(tags, "tag")

        # Limit the number of displayed tags so the message
        # doesn't become unnecessarily huge.
        regular_tags = regular_tags[:12]

        # -----------------------------
        # Other information
        # -----------------------------

        pages = data.get("num_pages", 0)
        favorites = data.get("num_favorites", 0)

        upload_date = format_upload_date(
            data.get("upload_date")
        )

        artist_text = ", ".join(artists) if artists else "Unknown"
        group_text = ", ".join(groups) if groups else "Unknown"
        language_text = ", ".join(languages) if languages else "Unknown"

        category_text = (
            ", ".join(categories)
            if categories
            else "Unknown"
        )

        parody_text = (
            ", ".join(parodies)
            if parodies
            else "Original"
        )

        tags_text = (
            " • ".join(regular_tags)
            if regular_tags
            else "None"
        )

        # -----------------------------
        # Response
        # -----------------------------

        caption = (
            "📚 <b>Gallery Information</b>\n\n"

            f"🆔 <b>ID:</b> <code>{data.get('id', gallery_id)}</code>\n"
            f"📖 <b>Title:</b> {title}\n\n"

            f"👤 <b>Artist:</b> {artist_text}\n"
            f"👥 <b>Group:</b> {group_text}\n"
            f"🌐 <b>Language:</b> {language_text}\n"
            f"📂 <b>Category:</b> {category_text}\n"
            f"🎭 <b>Parody:</b> {parody_text}\n"
            f"📄 <b>Pages:</b> {pages}\n"
            f"❤️ <b>Favorites:</b> {favorites:,}\n"
            f"📅 <b>Uploaded:</b> {upload_date}\n\n"

            f"🏷 <b>Tags:</b>\n"
            f"{tags_text}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔗 Open Gallery",
                        url=f"https://nhentai.net/g/{gallery_id}/"
                    )
                ]
            ]
        )

        # -----------------------------
        # Cover
        # -----------------------------

        cover = data.get("cover", {})
        cover_path = cover.get("path")

        # The API provides the gallery storage path.
        # We intentionally don't download/distribute gallery pages.
        if cover_path:
            cover_url = f"https://t1.nhentai.net/{cover_path}"

            try:
                await status.delete()

                await message.reply_photo(
                    photo=cover_url,
                    caption=caption,
                    reply_markup=keyboard,
                )

                return

            except Exception:
                # If the cover can't be sent, fall back to text.
                pass

        await status.edit_text(
            caption,
            reply_markup=keyboard,
        )

    except aiohttp.ClientError:
        await status.edit_text(
            "❌ <b>Could not connect to the API.</b>\n\n"
            "Please try again later."
        )

    except Exception as e:
        print(f"nh plugin error: {e}")

        await status.edit_text(
            "❌ <b>Something went wrong while fetching "
            "the gallery information.</b>"
        )
