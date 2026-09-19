import os
import sys
from dotenv import load_dotenv

# Load variables from .env
load_dotenv()

# ─────────────────────────────────────────────────────────────
# Required Credentials
# ─────────────────────────────────────────────────────────────

API_ID_RAW = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

if not all([API_ID_RAW, API_HASH, BOT_TOKEN, MONGO_URI]):
    print(
        "ERROR: Missing required variables: "
        "API_ID, API_HASH, BOT_TOKEN, or MONGO_URI"
    )
    sys.exit(1)

try:
    API_ID = int(API_ID_RAW)
except ValueError:
    print("ERROR: API_ID must be a valid integer.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
# Optional Configuration
# ─────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
OWNER_ID = [
    int(x)
    for x in os.getenv("OWNER_ID", "").split()
    if x.isdigit()
]

DB_NAME = os.getenv("DB_NAME", "goon_bot")
PICS = [
    'https://files.catbox.moe/726u1k.jpg',
    'https://files.catbox.moe/z2kfgd.jpg',
    'https://files.catbox.moe/ifzunf.jpg',
    'https://files.catbox.moe/7h3tsr.jpg',
    'https://files.catbox.moe/60q49h.jpg',
    'https://files.catbox.moe/5jbuo6.jpg',
    'https://files.catbox.moe/iimrtt.jpg',
    'https://files.catbox.moe/8ts63v.jpg',
    'https://files.catbox.moe/37x9fi.jpg',
    'https://files.catbox.moe/2fdad8.jpg',
    'https://files.catbox.moe/65k3bi.jpg',
    'https://files.catbox.moe/lxmf58.jpg',
    'https://files.catbox.moe/kfvxbx.jpg',
]

START_TXT = """<b>🪶 Kᴏɴɪᴄʜɪᴡᴀ, {}! 🪶</b>

<b>✨ Welcome aboard ✨</b>

<b>🤖 I'm your friendly bot, ready to assist you anytime.</b>
<b>⚡ Fast, reliable, and always online.</b>

"""
PORNHWADB_API_KEY = os.getenv("PORNHWADB_API_KEY", "pwdb")
NHENTAI_API_KEY = os.getenv("NHENTAI_API_KEY", "put_api_key_here")
PORNDB_API_TOKEN = os.getenv("PORNDB_API_TOKEN", "tpdb")
