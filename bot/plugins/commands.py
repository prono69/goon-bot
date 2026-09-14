import sys
import os
import asyncio
import random
from bot.config import PICS, START_TXT, OWNER_ID
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

START_BTN = InlineKeyboardMarkup(
    [[InlineKeyboardButton("📢 Uᴘᴅᴀᴛᴇ Cʜᴀɴɴᴇʟ", url="https://t.me/Neko_Drive")]]
)

@Client.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    user = message.from_user
    mention = user.mention if user else "there"

    await message.reply_photo(
        photo=random.choice(PICS),
        caption=START_TXT.format(mention),
        reply_markup=START_BTN,
    )
    
    
@Client.on_message(filters.command("restart") & filters.user(OWNER_ID))
async def restart_cmd(client: Client, message: Message):
    msg = await message.reply("🔄 **Pulling latest updates...**")

    process = await asyncio.create_subprocess_shell(
        "git pull",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/app",
    )
    stdout, stderr = await process.communicate()
    output = (stdout or stderr).decode().strip() or "No output"

    await msg.edit(f"📦 **Git pull result:**\n`{output}`")
    await asyncio.sleep(1)
    await msg.edit("♻️ **Restarting bot...**")

    os.execl(sys.executable, sys.executable, "-m", "bot")    