<div align="center">

# 🔞 Goon Bot

**An all-in-one Telegram bot for adult content discovery, metadata search, and gallery downloading.**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-2.x-2AABAB?style=for-the-badge&logo=telegram&logoColor=white)](https://docs.pyrogram.org/)
[![MongoDB](https://img.shields.io/badge/MongoDB-Async-47A248?style=for-the-badge&logo=mongodb&logoColor=white)](https://www.mongodb.com/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![Plugins](https://img.shields.io/badge/Plugins-9-blueviolet?style=for-the-badge)](#-plugin-architecture)

</div>

---

## 📖 Table of Contents

- [✨ Features](#-features)
- [🎬 Command Reference](#-command-reference)
- [🧩 Plugin Architecture](#-plugin-architecture)
- [🗂️ Project Structure](#️-project-structure)
- [⚙️ Configuration](#️-configuration)
- [🚀 Installation](#-installation)
- [🐳 Docker Deployment](#-docker-deployment)
- [🧪 Local Development](#-local-development)
- [🗄️ Database Layer](#️-database-layer)
- [🔐 Security & Owner Controls](#-security--owner-controls)
- [🧰 Tech Stack](#-tech-stack)
- [🤝 Contributing](#-contributing)
- [⚠️ Disclaimer](#️-disclaimer)

---

## ✨ Features

Goon Bot is a modular, plugin-driven Telegram bot built on [Pyrogram](https://docs.pyrogram.org/) with asynchronous MongoDB persistence and a polished, inline-keyboard-first UX.

| | Feature | Description |
|---|---------|-------------|
| 🎬 | **JAVDatabase search** | Search movies by code or title, with full metadata, poster art, cast, genres, runtime, and plot. |
| 🎥 | **ThePornDB search** | Paginated scene grid, detailed scene cards, and full performer profiles with bios and imagery. |
| 📚 | **PornhwaDB search** | Search pornhwa titles and characters, with external links and per-item detail pages. |
| 📖 | **nHentai galleries** | Look up galleries by ID/URL or free-text search, then download them as a packaged `.cbz` archive. |
| 👰 | **Babepedia profiles** | Scrape performer profiles — age, measurements, ratings, social links — plus a Telegraph-hosted photo gallery. |
| 🖼️ | **gallery-dl integration** | Download galleries from Pixiv and 100+ supported sites. Choose albums, ZIP archive, or raw documents. |
| 🛠️ | **Owner shell & eval** | Run shell commands or arbitrary Python inside the running bot, restricted to configured owners. |
| 📊 | **Live log viewer** | Stream `log.txt` inline in Telegram with a one-tap expandable preview. |
| 🔄 | **Hot restart** | `git pull` + process re-exec straight from Telegram, with an automatic success confirmation on boot. |

---

## 🎬 Command Reference

### 🌍 Public Commands

| Command | Aliases | Description |
|---------|---------|-------------|
| `/start` | — | Welcome card with a random banner image. |
| `/ping` | — | Round-trip latency check in milliseconds. |
| `/babe <name>` | — | Search Babepedia for a performer profile and gallery. |
| `/jav <query>` | — | Search JAVDatabase by DVD code, title, or full URL. |
| `/pdb <query>` | — | Search ThePornDB scenes. Supports `-p`/`--parse` and auto-parse. |
| `/ph <query>` | `/hwa` | Search PornhwaDB. Flags: `-pornhwa`, `-char`, `-all`. |
| `/nh <query>` | — | nHentai gallery lookup by ID, URL, or text search. |
| `/gdl <url>` | `/dl` | Download a gallery via gallery-dl. Also accepts a bare Pixiv link. |
| `/setcookies` | — | Reply to a Netscape-format `cookies.txt` to unlock private galleries. |

### 🔒 Owner-Only Commands

| Command | Aliases | Description |
|---------|---------|-------------|
| `/eval <code>` | `/val` | Execute arbitrary async Python in the bot process. |
| `/bash <cmd>` | `/sh` | Execute a shell command (`ll` → `ls -l`, `sysinfo` → `neofetch`). |
| `/logs` | `/log` | Download `log.txt` and preview the last 50 lines inline. |
| `/restart` | — | `git pull` the latest code and re-exec the bot process. |

### 💡 Usage Examples

```
/pdb Brooke Bailey
/pdb -p Bang.Bros.2023-01-01.Brooke.Bailey.XXX
/pdb Bang.Bros.2023-01-01.Brooke.Bailey          ← auto-detected parse string

/ph Love Quest
/ph Jeong In-Ha -char
/ph Love Quest -all

/nh 644225
/nh https://nhentai.net/g/123456/
/nh Milf

/jav VNDS-5242
/babe Mia Malkova

/gdl https://www.pixiv.net/en/artworks/12345678
/gdl -r 1-15 https://example.com/gallery        ← download items 1–15 only
```

---

## 🧩 Plugin Architecture

Every feature lives in a self-contained module under `bot/plugins/`. Pyrogram discovers them automatically through the `plugins={"root": "bot/plugins"}` argument passed to the `Client`, so adding a new plugin requires **zero changes to the core**.

```
bot/plugins/
├── babe.py         → Babepedia profile scraper + inline mode + Telegraph gallery
├── commands.py     → /start, /ping, /restart
├── eval.py         → Owner-only Python & shell execution
├── gallery_dl.py   → gallery-dl downloader with albums / ZIP / documents
├── jav.py          → JAVDatabase search and detail viewer
├── logs.py         → Log file delivery and inline preview
├── nhentai.py      → nHentai search, gallery info, CBZ packaging
├── pornhwa.py      → PornhwaDB search, details, character pages
└── tpdb.py         → ThePornDB scene grid and performer profiles
```

**Plugin conventions:**

- Handlers are registered with the `@Client.on_message(...)` / `@Client.on_callback_query(...)` decorators.
- Async throughout — no blocking I/O on the event loop.
- Long output is truncated to Telegram's 4096-character limit and shipped as a `.txt` document instead.
- `FloodWait` is caught and slept off automatically rather than crashing the handler.
- Progress messages are throttled (3–5 s intervals) to avoid Telegram rate limits.

---

## 🗂️ Project Structure

```
goon-bot/
├── bot/
│   ├── __init__.py            # Exports setup_logger & logger
│   ├── __main__.py            # Entry point, signal handling, graceful shutdown
│   ├── config.py              # .env loading + validation
│   ├── logger.py              # Rotating log file + stdout handlers
│   ├── database/
│   │   └── db.py              # AsyncMongoManager (generic upsert/query helper)
│   ├── plugins/               # All feature modules (auto-loaded)
│   └── utils/
│       ├── javdatabase.py     # JAVDatabase scraping, NFO generation, downloads
│       └── nhentai_dl.py      # nHentai page fetch, CBZ archive builder
├── .gitignore
├── .env.example               # Copy to .env and fill in your credentials
├── Dockerfile                 # Multi-stage build (builder → slim runtime)
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## ⚙️ Configuration

All configuration is read from a `.env` file in the project root via `python-dotenv`.

### Required Variables

The bot refuses to start if any of these are missing — `bot/config.py` prints a clear error and exits.

| Variable | Description | Example |
|----------|-------------|---------|
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) — must be an integer. | `1234567` |
| `API_HASH` | Telegram API hash. | `0123456789abcdef...` |
| `BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather). | `123456:ABC-DEF...` |
| `MONGO_URI` | MongoDB connection string. | `mongodb://localhost:27017` |

### Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OWNER_ID` | *(empty)* | Space-separated Telegram user IDs granted owner privileges (eval, bash, logs, restart). |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `RESTART_CHAT_ID` | *(auto)* | Set internally by `/restart` so the bot can confirm the restart on boot. |
| `DB_NAME` | `goon_bot` | MongoDB database name. |
| `PORNHWADB_API_KEY` | `pwdb` | PornhwaDB API key. |
| `PORNDB_API_TOKEN` | `tpdb` | ThePornDB bearer token. |
| `NHENTAI_API_KEY` | *(placeholder)* | nHentai API key. |

### 📄 Example `.env`

```env
API_ID=1234567
API_HASH=0123456789abcdef0123456789abcdef
BOT_TOKEN=1234567:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
MONGO_URI=mongodb://localhost:27017

OWNER_ID=123456789 987654321
LOG_LEVEL=INFO
DB_NAME=goon_bot

PORNHWADB_API_KEY=pwdb
PORNDB_API_TOKEN=tpdb
NHENTAI_API_KEY=your_key_here
```

> 🔐 Never commit your `.env` — it is already excluded by `.gitignore`.

---

## 🚀 Installation

### Prerequisites

- Python **3.12+**
- A running **MongoDB** instance (local or Atlas)
- Telegram credentials from [my.telegram.org](https://my.telegram.org)

### Step-by-step

```bash
# 1. Clone the repository
git clone https://github.com/prono69/goon-bot.git
cd goon-bot

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure the environment
cp .env.example .env              # then edit .env with your credentials
nano .env

# 5. Launch the bot
python -m bot
```

On successful boot you should see:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Starting Telegram bot...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bot started successfully!
Username : @YourBot
Bot ID   : 1234567890
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 🐳 Docker Deployment

The included `Dockerfile` uses a **multi-stage build** — dependencies are compiled in a builder stage and only the final slim runtime image ships.

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f

# Stop
docker compose down
```

`docker-compose.yml` mounts the project directory into `/app` so `log.txt`, the Pyrogram session file, and downloaded galleries stay visible on the host — and so the `/restart` command's `git pull` works in place.

> The image installs `git` and configures `safe.directory /app` specifically to support the in-bot `/restart` workflow.

---

## 🧪 Local Development

```bash
# Run with verbose logging
LOG_LEVEL=DEBUG python -m bot

# Syntax-check everything
python -m compileall bot

# Format / lint (if you use ruff)
ruff check bot
ruff format bot
```

### 🔄 Hot Reload

With the project directory bind-mounted (as `docker-compose.yml` does), an owner can simply send:

```
/restart
```

The bot runs `git pull` inside `/app`, re-executes itself via `os.execl`, and edits the original message to **"✅ Bot restarted successfully!"** once it is back online.

---

## 🗄️ Database Layer

`bot/database/db.py` provides a single shared `AsyncMongoManager` instance with a **generic, collection-agnostic** API:

```python
from bot.database.db import mongo

await mongo.add_item("lastfm_tracks", {"track_id": "abc"}, {"track_id": "abc"})
await mongo.item_exists("lastfm_tracks", {"track_id": "abc"})
await mongo.get_item("lastfm_tracks", {"track_id": "abc"})
await mongo.get_items("lastfm_tracks", limit=10, sort_field="updated_at")
await mongo.delete_item("lastfm_tracks", {"track_id": "abc"})
```

- `add_item` performs an **upsert** when a `unique_query` is supplied, otherwise a plain insert.
- `created_at` / `updated_at` timestamps are managed automatically (UTC-aware).
- All `PyMongoError` exceptions are caught and logged — a database hiccup never kills a handler.

---

## 🔐 Security & Owner Controls

Privilege-sensitive commands are gated with `filters.user(OWNER_ID)`:

- `/eval` — arbitrary Python execution in the bot process
- `/bash` — arbitrary shell execution (120 s timeout, process killed on expiry)
- `/logs` — read access to the bot's log file
- `/restart` — pulls and re-execs the running process

> ⚠️ **Only set `OWNER_ID` to accounts you fully trust.** Anyone listed there can execute arbitrary code on your host. Leave it empty if you do not need these features.

Additional hardening already in place:

- `OWNER_ID` parsing accepts only numeric IDs (`.isdigit()` filter).
- Output longer than Telegram's limit is sent as a file rather than being truncated into uselessness.
- `FloodWait` exceptions are handled with an automatic sleep-and-retry.
- Gallery jobs are capped by a semaphore (2 concurrent) and a per-job file limit (1000 items).
- Uploads are capped at Telegram's 2 GB file limit.

---

## 🧰 Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Telegram API | [Pyrogram](https://docs.pyrogram.org/) (`kurigram[fast]`) + [TgCrypto](https://github.com/pyrogram/tgcrypto) |
| Database | MongoDB via `pymongo` async driver (`AsyncMongoClient`) |
| HTTP | `aiohttp` (async scraping), `niquests` (blocking JAVDatabase scraper) |
| Scraping | `BeautifulSoup4`, `lxml_html_clean`, regex-based parsers |
| Archives | `zipfile` (ZIP/CBZ), `Pillow` (thumbnails) |
| Gallery DL | `gallery-dl` (100+ site extractors) |
| Publishing | `html-telegraph-poster` (Babepedia galleries) |
| Config | `python-dotenv` |
| Ops | Docker (multi-stage), `docker-compose` |

---

## 🤝 Contributing

Contributions are welcome! 🎀

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/awesome-plugin`
3. Commit your changes: `git commit -m "Add awesome plugin"`
4. Push to your fork: `git push origin feature/awesome-plugin`
5. Open a Pull Request

### Adding a new plugin

Create `bot/plugins/my_plugin.py` — it is auto-loaded, no registration needed:

```python
from pyrogram import Client, filters
from pyrogram.types import Message


@Client.on_message(filters.command("hello"))
async def hello(client: Client, message: Message):
    await message.reply_text("Hello! 🐱")
```

Keep it async, throttle progress edits, and handle `FloodWait`.

---

## ⚠️ Disclaimer

This project is provided for **educational and personal use only**. It is not affiliated with, endorsed by, or sponsored by any of the content providers it queries.

- Users are responsible for complying with local laws and with the terms of service of the websites and APIs they access through this bot.
- No content is hosted or redistributed by this project — the bot only acts as a search and download interface.
- The maintainers accept no liability for how the software is used.

---

<div align="center">

**Made with 🐾 and a lot of patience by the Goon Bot community**

[![Fork](https://img.shields.io/github/forks/prono69/goon-bot?style=social)](https://github.com/prono69/goon-bot/fork)
[![Stars](https://img.shields.io/github/stars/prono69/goon-bot?style=social)](https://github.com/prono69/goon-bot/stargazers)

</div>
