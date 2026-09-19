import hashlib
import json
import time
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.utils.javdatabase import get_movie_details, search_movies


# ============================================================
# RESULT CACHE (stores search results temporarily)
# ============================================================

_RESULT_CACHE = {}
_CACHE_TIMEOUT = 3600  # 1 hour


def _cache_key(user_id: int, query: str) -> str:
    """Generate a cache key from user ID and query."""
    return hashlib.md5(f"{user_id}:{query}".encode()).hexdigest()


def _cache_results(user_id: int, query: str, results: list) -> str:
    """Cache results and return cache key."""
    key = _cache_key(user_id, query)
    _RESULT_CACHE[key] = {
        "results": results,
        "timestamp": time.time(),
    }
    return key


def _get_cached_results(key: str) -> list:
    """Retrieve cached results if not expired."""
    if key not in _RESULT_CACHE:
        return None
    
    cache_entry = _RESULT_CACHE[key]
    if time.time() - cache_entry["timestamp"] > _CACHE_TIMEOUT:
        del _RESULT_CACHE[key]
        return None
    
    return cache_entry["results"]


def _get_result_from_cache(cache_key: str, index: int):
    """Get a specific result from cache by index."""
    results = _get_cached_results(cache_key)
    if results and 0 <= index < len(results):
        return results[index]
    return None


# ============================================================
# FORMATTING
# ============================================================

def _make_button_rows(buttons, per_row=2):
    """Arrange buttons into rows."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _format_movie(movie: dict) -> str:
    """Format movie data into Telegram caption."""
    
    fields = {
        "🆔 DVD ID": movie.get("dvd_id") or "N/A",
        "🔖 Content ID": movie.get("content_id") or "N/A",
        "📅 Release Date": movie.get("release_date") or "N/A",
        "⏱ Runtime": movie.get("runtime") or "N/A",
        "🏢 Studio": movie.get("studio") or "N/A",
        "🎬 Director": movie.get("director") or "N/A",
        "📚 Series": movie.get("series") or "N/A",
    }
    
    text = f"<b>🎬 {movie.get('title') or 'Unknown'}</b>\n\n"
    text += "\n".join(f"<b>{k}:</b> <code>{v}</code>" if "ID" in k else f"<b>{k}:</b> {v}" 
                      for k, v in fields.items())
    
    genres = movie.get("genres") or []
    if genres:
        text += f"\n\n<b>🏷 Genres:</b> {', '.join(genres)}"
    
    actresses = movie.get("actresses") or []
    if actresses:
        text += f"\n\n<b>👤 Actresses:</b> {', '.join(actresses)}"
    
    plot = movie.get("plot")
    if plot:
        if len(plot) > 1500:
            plot = plot[:1500].rstrip() + "..."
        text += f"\n\n<b>📝 Plot:</b>\n{plot}"
    
    return text


# ============================================================
# COMMAND: /jav
# ============================================================

@Client.on_message(filters.command("jav"))
async def jav_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text(
            "❌ <b>Usage:</b>\n\n"
            "<code>/jav VNDS-5242</code>\n\n"
            "You can also use a JAVDatabase URL."
        )
        return

    query = " ".join(message.command[1:]).strip()
    status = await message.reply_text("🔎 <b>Searching JAVDatabase...</b>")

    try:
        results = search_movies(query)
    except Exception as exc:
        await status.edit_text(
            f"❌ <b>Search failed.</b>\n\n<code>{type(exc).__name__}: {exc}</code>"
        )
        return

    if not results:
        await status.edit_text(f"❌ <b>No results found.</b>\n\nQuery: <code>{query}</code>")
        return

    # Single result: show details directly
    if len(results) == 1:
        await _show_movie_details(client, status, results[0])
        return

    # Multiple results: show selection buttons
    await _show_results_list(client, status, message, results, query)


async def _show_movie_details(client: Client, status: Message, result: dict):
    """Fetch and display movie details."""
    await status.edit_text("📖 <b>Fetching movie details...</b>")
    
    try:
        movie = get_movie_details(result["link"])
    except Exception:
        movie = None

    if not movie:
        await status.edit_text("❌ <b>Unable to fetch movie details.</b>")
        return

    caption = _format_movie(movie)
    keyboard = None
    
    if movie.get("link"):
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🌐 JAVDatabase", url=movie["link"])]]
        )

    poster = movie.get("poster_url")
    if poster:
        try:
            await status.delete()
            await client.send_photo(
                chat_id=status.chat.id,
                photo=poster,
                caption=caption,
                reply_to_message_id=status.reply_to_message_id,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    await status.edit_text(caption, reply_markup=keyboard, disable_web_page_preview=True)


async def _show_results_list(client: Client, status: Message, message: Message, results: list, query: str):
    """Display search results as buttons."""
    cache_key = _cache_results(message.from_user.id, query, results)
    buttons = []

    for index, result in enumerate(results[:10], start=1):
        code = result.get("code") or "Unknown"
        buttons.append(
            InlineKeyboardButton(
                f"{index}. {code}",
                callback_data=f"javopen:{cache_key}:{index - 1}",  # Compact callback data
            )
        )

    keyboard = InlineKeyboardMarkup(_make_button_rows(buttons, per_row=2))
    text = f"🔎 <b>Found {len(results)} results</b>\n\nSelect a movie:"

    await status.edit_text(text, reply_markup=keyboard)


# ============================================================
# CALLBACK: OPEN MOVIE
# ============================================================

@Client.on_callback_query(filters.regex(r"^javopen:"))
async def jav_open_callback(client: Client, callback_query):
    try:
        _, cache_key, index = callback_query.data.split(":")
        index = int(index)
    except (ValueError, IndexError):
        await callback_query.answer("❌ Invalid callback data", show_alert=True)
        return

    await callback_query.answer("Fetching movie details...")

    result = _get_result_from_cache(cache_key, index)
    if not result:
        await callback_query.message.reply_text("❌ Cache expired. Please search again.")
        return

    try:
        movie = get_movie_details(result["link"])
    except Exception:
        movie = None

    if not movie:
        await callback_query.message.reply_text("❌ Unable to fetch movie details.")
        return

    caption = _format_movie(movie)
    keyboard = None
    
    if movie.get("link"):
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🌐 JAVDatabase", url=movie["link"])]]
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