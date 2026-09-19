from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.utils.javdatabase import (
    get_movie_details,
    search_movies,
)


# ============================================================
# BUTTON HELPER
# ============================================================

def make_button_rows(
    buttons,
    per_row=2,
):
    """Put buttons into rows."""

    return [
        buttons[i:i + per_row]
        for i in range(
            0,
            len(buttons),
            per_row,
        )
    ]


# ============================================================
# FORMAT MOVIE
# ============================================================

def format_movie(movie):
    """Create the Telegram caption."""

    title = movie.get("title") or "Unknown"

    dvd_id = movie.get("dvd_id") or "N/A"
    content_id = movie.get("content_id") or "N/A"
    release_date = movie.get("release_date") or "N/A"
    runtime = movie.get("runtime") or "N/A"
    studio = movie.get("studio") or "N/A"
    director = movie.get("director") or "N/A"
    series = movie.get("series") or "N/A"

    genres = movie.get("genres") or []
    actresses = movie.get("actresses") or []

    text = (
        f"<b>🎬 {title}</b>\n\n"
        f"<b>🆔 DVD ID:</b> <code>{dvd_id}</code>\n"
        f"<b>🔖 Content ID:</b> <code>{content_id}</code>\n"
        f"<b>📅 Release Date:</b> {release_date}\n"
        f"<b>⏱ Runtime:</b> {runtime}\n"
        f"<b>🏢 Studio:</b> {studio}\n"
        f"<b>🎬 Director:</b> {director}\n"
        f"<b>📚 Series:</b> {series}\n"
    )

    if genres:
        text += (
            "\n<b>🏷 Genres:</b> "
            + ", ".join(genres)
        )

    if actresses:
        text += (
            "\n\n<b>👤 Actresses:</b> "
            + ", ".join(actresses)
        )

    plot = movie.get("plot")

    if plot:
        # Keep Telegram messages manageable.
        if len(plot) > 1500:
            plot = plot[:1500].rstrip() + "..."

        text += (
            f"\n\n<b>📝 Plot:</b>\n{plot}"
        )

    return text


# ============================================================
# /JAV
# ============================================================

@Client.on_message(filters.command("jav"))
async def jav_handler(
    client: Client,
    message: Message,
):
    # --------------------------------------------------------
    # Check query
    # --------------------------------------------------------

    if len(message.command) < 2:
        await message.reply_text(
            "❌ <b>Usage:</b>\n\n"
            "<code>/jav VNDS-5242</code>\n\n"
            "You can also use a JAVDatabase URL."
        )
        return

    query = " ".join(
        message.command[1:]
    ).strip()

    status = await message.reply_text(
        "🔎 <b>Searching JAVDatabase...</b>"
    )

    # --------------------------------------------------------
    # Search
    # --------------------------------------------------------

    try:
        results = search_movies(query)

    except Exception as exc:
        await status.edit_text(
            "❌ <b>Search failed.</b>\n\n"
            f"<code>{type(exc).__name__}: {exc}</code>"
        )
        return

    if not results:
        await status.edit_text(
            "❌ <b>No results found.</b>\n\n"
            f"Query: <code>{query}</code>"
        )
        return

    # --------------------------------------------------------
    # If only one result, directly show details
    # --------------------------------------------------------

    if len(results) == 1:
        result = results[0]

        await status.edit_text(
            "📖 <b>Fetching movie details...</b>"
        )

        movie = get_movie_details(
            result["link"]
        )

        if not movie:
            await status.edit_text(
                "❌ <b>Unable to fetch movie details.</b>"
            )
            return

        caption = format_movie(movie)

        buttons = []

        if movie.get("link"):
            buttons.append(
                InlineKeyboardButton(
                    "🌐 JAVDatabase",
                    url=movie["link"],
                )
            )

        keyboard = (
            InlineKeyboardMarkup(
                make_button_rows(
                    buttons,
                    per_row=2,
                )
            )
            if buttons
            else None
        )

        poster = movie.get("poster_url")

        if poster:
            try:
                await status.delete()

                await client.send_photo(
                    chat_id=message.chat.id,
                    photo=poster,
                    caption=caption,
                    reply_to_message_id=message.id,
                    reply_markup=keyboard,
                )

                return

            except Exception:
                # Poster may be inaccessible to Telegram.
                pass

        await status.edit_text(
            caption,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        return

    # --------------------------------------------------------
    # Multiple results
    # --------------------------------------------------------

    buttons = []

    for index, result in enumerate(
        results[:10],
        start=1,
    ):
        code = result.get("code") or "Unknown"
        title = result.get("title") or code

        # Keep button text short.
        if len(title) > 35:
            title = title[:32] + "..."

        buttons.append(
            InlineKeyboardButton(
                f"{index}. {code} — {title}",
                callback_data=f"javresult:{index - 1}",
            )
        )

    # Store results on the message object isn't reliable
    # across workers, so use encoded callback data containing
    # the actual URL.
    #
    # Rebuild the buttons with URL callbacks instead.
    buttons = []

    for index, result in enumerate(
        results[:10],
        start=1,
    ):
        code = result.get("code") or "Unknown"

        buttons.append(
            InlineKeyboardButton(
                f"{index}. {code}",
                callback_data=(
                    f"javopen:{result['link']}"
                ),
            )
        )

    keyboard = InlineKeyboardMarkup(
        make_button_rows(
            buttons,
            per_row=2,
        )
    )

    text = (
        f"🔎 <b>Found {len(results)} results</b>\n\n"
        "Select a movie:"
    )

    await status.edit_text(
        text,
        reply_markup=keyboard,
    )


# ============================================================
# CALLBACK — OPEN MOVIE
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^javopen:")
)
async def jav_open_callback(
    client,
    callback_query,
):
    url = callback_query.data[
        len("javopen:"):
    ]

    await callback_query.answer(
        "Fetching movie details..."
    )

    try:
        movie = get_movie_details(url)

    except Exception:
        movie = {}

    if not movie:
        await callback_query.message.reply_text(
            "❌ Unable to fetch movie details."
        )
        return

    caption = format_movie(movie)

    buttons = []

    if movie.get("link"):
        buttons.append(
            InlineKeyboardButton(
                "🌐 JAVDatabase",
                url=movie["link"],
            )
        )

    keyboard = (
        InlineKeyboardMarkup(
            make_button_rows(
                buttons,
                per_row=2,
            )
        )
        if buttons
        else None
    )

    poster = movie.get("poster_url")

    if poster:
        try:
            await callback_query.message.reply_photo(
                photo=poster,
                caption=caption,
                reply_markup=keyboard,
            )
            return

        except Exception:
            pass

    await callback_query.message.reply_text(
        caption,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )