from html import escape
import io
import os

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import logger
from bot.config import OWNER_ID

LOG_FILE = "log.txt"
LINES_TO_SHOW = 50


@Client.on_message(filters.command(["logs", "log"]) & filters.user(OWNER_ID))
async def send_logs(client: Client, message: Message):
    """Sends log.txt as a document with an inline button preview."""
    status_message = await message.reply_text("<code>Fetching logs...</code>")
    reply_to = message.reply_to_message or message

    if not os.path.exists(LOG_FILE):
        await status_message.edit_text("No <code>log.txt</code> file found on disk.")
        return

    try:
        with open(LOG_FILE, "rb") as f:
            file_bytes = f.read()
    except Exception as e:
        logger.error(f"Failed to read log file: {e}")
        await status_message.edit_text(f"<code>Failed to read log file: {e}</code>")
        return

    if not file_bytes.strip():
        await status_message.edit_text("<code>log.txt is empty.</code>")
        return

    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📄 Display Logs", callback_data="show_logs")]]
    )

    with io.BytesIO(file_bytes) as out_file:
        out_file.name = "log.txt"
        await reply_to.reply_document(
            document=out_file,
            caption="<b>Bot log file.</b>",
            reply_markup=buttons,
            #quote=True,
        )

    await status_message.delete()


@Client.on_callback_query(filters.regex("^show_logs$"))
async def show_logs_callback(client: Client, callback_query: CallbackQuery):
    """Callback query to display the last N lines inside an expandable blockquote."""
    # Ensure only owners can trigger the inline preview
    user_id = callback_query.from_user.id
    if isinstance(OWNER_ID, (list, set, tuple)) and user_id not in OWNER_ID:
        await callback_query.answer("⚠️ You are not authorized to view logs.", show_alert=True)
        return
    elif isinstance(OWNER_ID, int) and user_id != OWNER_ID:
        await callback_query.answer("⚠️ You are not authorized to view logs.", show_alert=True)
        return

    if not os.path.exists(LOG_FILE):
        await callback_query.answer("log.txt not found on disk.", show_alert=True)
        return

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-LINES_TO_SHOW:]
    except Exception as e:
        logger.error(f"Failed to read log file: {e}")
        await callback_query.answer("Failed to read log file.", show_alert=True)
        return

    if not lines:
        await callback_query.answer("log.txt is empty.", show_alert=True)
        return

    # Keep chronological sequence or reverse depending on preference
    log_text = escape("".join(reversed(lines)))

    formatted_text = (
        f"<b>Showing Last {len(lines)} Lines from log.txt:</b>\n\n"
        f"----------<b>START LOG</b>----------\n\n"
        f"<blockquote expandable>{log_text}</blockquote>\n"
        f"----------<b>END LOG</b>----------"
    )

    if len(formatted_text) > 4096:
        formatted_text = formatted_text[:4000] + "\n\n<i>... truncated, download full file above.</i>"

    await callback_query.answer()
    await callback_query.message.reply_text(formatted_text)
