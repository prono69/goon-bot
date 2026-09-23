import asyncio
import json
import re
from typing import Optional
from urllib.parse import quote, urljoin

import aiohttp
from bot import logger
from bs4 import BeautifulSoup
from html_telegraph_poster import TelegraphPoster
from pyrogram import Client, filters
from pyrogram.errors import BadRequest, WebpageCurlFailed
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQuery,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

BABEPEDIA_BASE = "https://www.babepedia.com"
_telegraph_poster = None

BIO_FIELDS = [
    "Age", "Born", "Years active", "Birthplace", "Nationality", "Ethnicity",
    "Professions", "Sexuality", "Hair color", "Eye color", "Height", "Weight",
    "Body type", "Measurements", "Bra/cup size", "Boobs", "Tattoos", "Piercings",
    "Solo", "Girl/girl", "Boy/girl", "Special"
]

SOCIAL_PROXY_MAP = {
    "/onlyfans/": ("OnlyFans", "https://onlyfans.com/"),
    "/fansly/": ("Fansly", "https://fansly.com/"),
    "/instagram/": ("Instagram", "https://instagram.com/"),
    "/twitter/": ("X", "https://x.com/"),
    "/tiktok/": ("TikTok", "https://tiktok.com/"),
    "/imdb/": ("IMDb", "https://imdb.com/"),
    "/iafd/": ("IAFD", "https://iafd.com/"),
}


def safe_urljoin(base: str, path: str) -> str:
    """Safely join URLs without double-encoding."""
    return urljoin(base, path) if path else ""


def get_telegraph_poster():
    """Create and cache Telegraph poster instance."""
    global _telegraph_poster
    if _telegraph_poster is None:
        _telegraph_poster = TelegraphPoster(use_api=True, telegraph_api_url="https://api.graph.org")
        _telegraph_poster.create_api_token("BabepediaBot")
    return _telegraph_poster


def get_bio_field(soup: BeautifulSoup, label: str) -> str:
    """Extract biography field from Babepedia."""
    label_span = soup.find(
        lambda tag: tag.name == "span" and label.lower() in tag.get_text(" ", strip=True).lower()
    )
    
    if label_span and (sibling := label_span.find_next_sibling("span")):
        text = sibling.get_text(" ", strip=True)
        text = re.sub(r"show conversions.*", "", text, flags=re.IGNORECASE)
        text = re.sub(r",([^\s])", r", \1", text)
        return text.strip()
    return "N/A"


def create_telegraph_page(title: str, img_urls: list[str]) -> Optional[str]:
    """Upload gallery images to Telegraph and return page URL."""
    if not img_urls:
        return None
    
    try:
        telegraph = get_telegraph_poster()
        html_content = f"<p>{title} Photos</p>" + "".join(f'<img src="{url}"/><br/>' for url in img_urls)
        page = telegraph.post(title=f"{title} Gallery", author="NekoDrive", text=html_content)
        return page.get("url")
    except Exception as e:
        logger.error(f"[Babepedia] Telegraph error: {e}")
        return None


def build_caption(data: dict) -> str:
    """Helper function to create formatted text/caption from scraped performer data."""
    bio_text = "\n".join(
        f"**{field}:** `{data.get(field.lower().replace(' ', '_'), 'N/A')}`"
        + ("\n" if field == "Tattoos" else "")
        for field in BIO_FIELDS
    )
    return (
        f"**{data['name']}**\n"
        f"__Also known as:__ `{data['aka']}`\n"
        f"**Rating:** ⭐ `{data['rating']}/10 ({data['votes']})`\n\n"
        f"{bio_text}"
    )


async def search_and_scrape_babepedia(
    session: aiohttp.ClientSession, query: str
) -> tuple[Optional[dict], list[list[InlineKeyboardButton]]]:
    
    try:
        async with session.get(f"{BABEPEDIA_BASE}/ajax-search.php?term={quote(query)}", headers=HEADERS) as resp:
            if resp.status != 200:
                logger.error(f"[Babepedia] Search HTTP status: {resp.status}")
                return None, []
            search_results = await resp.json()
    except Exception as e:
        logger.error(f"[Babepedia] Search error: {e}")
        return None, []

    if not search_results:
        return None, []

    try:
        best_match = search_results[0]["value"].replace(" ", "_")
    except (KeyError, IndexError, TypeError):
        return None, []

    profile_url = f"{BABEPEDIA_BASE}/babe/{best_match}"

    try:
        async with session.get(profile_url, headers=HEADERS) as resp:
            if resp.status != 200:
                logger.error(f"[Babepedia] Profile HTTP status: {resp.status}")
                return None, []
            html_text = await resp.text()
    except Exception as e:
        logger.error(f"[Babepedia] Profile request error: {e}")
        return None, []

    soup = BeautifulSoup(html_text, "html.parser")

    h1_name = soup.find("h1", id="babename")
    if not h1_name or not (name := h1_name.get_text(" ", strip=True)):
        return None, []

    data = {"url": profile_url, "name": name}

    h2_aka = soup.find("h2", id="aka")
    data["aka"] = re.sub(r"^(AKA|Also known as):\s*", "", h2_aka.get_text(" ", strip=True), flags=re.IGNORECASE) if h2_aka else "N/A"

    rating_str, votes_str = "N/A", "N/A"
    
    if rating_elem := soup.select_one(".rating-global"):
        text = rating_elem.get_text(" ", strip=True)
        if score_match := re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", text):
            rating_str = score_match.group(1)
        if votes_match := re.search(r"([\d,]+)\s*votes?", text, re.IGNORECASE):
            votes_str = f"{votes_match.group(1)} votes"

    if rating_str == "N/A":
        for script in soup.find_all("script", type="application/ld+json"):
            if script.string:
                try:
                    ld_data = json.loads(script.string)
                    agg = ld_data.get("aggregateRating") or ld_data
                    if "ratingValue" in agg:
                        rating_str = str(agg.get("ratingValue"))
                    if "reviewCount" in agg or "ratingCount" in agg:
                        votes_count = agg.get("reviewCount") or agg.get("ratingCount")
                        votes_str = f"{votes_count} votes"
                except (json.JSONDecodeError, TypeError):
                    continue

    data["rating"] = rating_str
    data["votes"] = votes_str

    for field in BIO_FIELDS:
        key = field.lower().replace(" ", "_")
        data[key] = get_bio_field(soup, field)

    data["photo"] = None
    if hq_img := soup.find("div", id="profbox2"):
        if img_link := hq_img.find("a", class_="img"):
            if href := img_link.get("href", ""):
                data["photo"] = safe_urljoin(BABEPEDIA_BASE, href)

    if not data["photo"]:
        if main_img := soup.find("img", id="bioimg"):
            if src := (main_img.get("src") or main_img.get("data-src") or ""):
                data["photo"] = safe_urljoin(BABEPEDIA_BASE, src)

    gallery_imgs = []
    for a_tag in soup.select("#profbox2 a.img, .useruploads2 a.img"):
        if href := a_tag.get("href", ""):
            full_url = safe_urljoin(BABEPEDIA_BASE, href)
            if full_url and full_url not in gallery_imgs:
                gallery_imgs.append(full_url)

    if not gallery_imgs and data.get("photo"):
        gallery_imgs.append(data["photo"])

    logger.info(f"[Babepedia] Found {len(gallery_imgs)} gallery images for {data['name']}")

    telegraph_url = create_telegraph_page(data["name"], gallery_imgs) if gallery_imgs else None
    gallery_btn_url = telegraph_url or profile_url

    row_buttons = []
    for a_tag in soup.select("#socialicons a[href]"):
        href = a_tag.get("href", "")
        title = a_tag.get_text(strip=True) or a_tag.get("title", "")
        final_url = href

        for prefix, (name, target) in SOCIAL_PROXY_MAP.items():
            if prefix in href:
                username = href.split(prefix)[-1]
                final_url = f"{target}{username}"
                title = name
                break

        if not title:
            classes = " ".join(a_tag.get("class", []))
            for key in ["instagram", "twitter", "onlyfans", "fansly", "tiktok", "imdb", "iafd"]:
                if key in classes.lower() or key in href.lower():
                    title = key.capitalize()
                    break

        title = title or "Official"
        final_url = final_url if final_url.startswith("http") else urljoin(BABEPEDIA_BASE, final_url)
        row_buttons.append(InlineKeyboardButton(f"{title} ↗", url=final_url))

    keyboard = [[InlineKeyboardButton("Gallery/Bio ↗", url=gallery_btn_url)]]
    for i in range(0, len(row_buttons), 2):
        keyboard.append(row_buttons[i:i + 2])

    return data, keyboard


@Client.on_message(filters.command("babe"))
async def babe_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/babe <performer name>`")
        return

    query = " ".join(message.command[1:])
    status_msg = await message.reply_text(f"🔍 Searching Babepedia for **{query}**...")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            data, keyboard = await search_and_scrape_babepedia(session, query)
    except Exception as e:
        logger.error(f"[Babepedia] Handler error: {e}")
        await status_msg.edit_text("❌ An error occurred while fetching the profile.")
        return

    if not data:
        await status_msg.edit_text(f"❌ No results found for **{query}**.")
        return

    caption = build_caption(data)
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    try:
        await status_msg.delete()
    except Exception:
        pass

    if data.get("photo"):
        try:
            logger.info(f"[Babepedia] Sending photo: {data['photo']}")
            await message.reply_photo(photo=data["photo"], caption=caption, reply_markup=reply_markup)
            return
        except (WebpageCurlFailed, BadRequest) as e:
            logger.error(f"[Babepedia] Telegram could not load image: {e}")
        except Exception as e:
            logger.error(f"[Babepedia] Photo send error: {e}")

    await message.reply_text(text=caption, reply_markup=reply_markup)


@Client.on_inline_query()
async def babe_inline_handler(client: Client, inline_query: InlineQuery):
    query = inline_query.query.strip()
    
    # Strip optional prefix command if typed as `@bot babe <name>`
    if query.lower().startswith("babe "):
        query = query[5:].strip()

    if not query:
        await inline_query.answer(
            results=[],
            switch_pm_text="Type a performer's name to search...",
            switch_pm_parameter="start"
        )
        return

    try:
        # Enforce a tight timeout so inline queries don't hang and expire (Telegram limits inline queries to ~10s)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            data, keyboard = await asyncio.wait_for(
                search_and_scrape_babepedia(session, query), timeout=8.5
            )
    except Exception as e:
        logger.error(f"[Babepedia] Inline search error: {e}")
        data, keyboard = None, []

    if not data:
        await inline_query.answer(
            results=[],
            cache_time=1
        )
        return

    caption = build_caption(data)
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    # Include an image link preview at top if photo exists
    if data.get("photo"):
        caption = f"[\u200b]({data['photo']}){caption}"

    results = [
        InlineQueryResultArticle(
            title=data["name"],
            description=f"AKA: {data['aka']} | Rating: {data['rating']}/10",
            thumb_url=data.get("photo"),
            input_message_content=InputTextMessageContent(
                message_text=caption,
                link_preview_options=LinkPreviewOptions(is_disabled=False, show_above_text=True)
            ),
            reply_markup=reply_markup
        )
    ]

    await inline_query.answer(results=results, cache_time=300)
