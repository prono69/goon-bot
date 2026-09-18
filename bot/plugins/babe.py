import json
import re
from urllib.parse import quote, urljoin
import aiohttp
from bs4 import BeautifulSoup
from html_telegraph_poster import TelegraphPoster
from pyrogram import Client, filters
from pyrogram.errors import BadRequest, WebpageCurlFailed
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

BABEPEDIA_BASE = "https://www.babepedia.com"


def safe_urljoin(base: str, path: str) -> str:
    """Safely encodes URL paths without breaking URL structure."""
    if not path:
        return ""
    joined = urljoin(base, path)
    return quote(joined, safe="/:?=&")


def get_bio_field(soup: BeautifulSoup, label: str) -> str:
    label_span = soup.find(
        lambda tag: tag.name == "span" and label.lower() in tag.text.lower()
    )
    if label_span:
        sibling = label_span.find_next_sibling("span")
        if sibling:
            text = sibling.get_text(" ", strip=True)
            text = re.sub(r"show conversions.*", "", text, flags=re.IGNORECASE)
            text = re.sub(r",([^\s])", r", \1", text)
            return text.strip()
    return "N/A"


def create_telegraph_page(title: str, img_urls: list[str]) -> str | None:
    """Posts full-res gallery images to Telegraph using html_telegraph_poster."""
    try:
        t = TelegraphPoster(use_api=True, telegraph_api_url="https://api.graph.org")
        t.create_api_token("BabepediaBot")

        html_content = f"<p>{title} Photos</p>"
        for img_url in img_urls:
            html_content += f'<img src="{img_url}"/><br/>'

        page = t.post(
            title=f"{title} Gallery",
            author="Babepedia",
            text=html_content
        )
        return page.get("url")
    except Exception:
        return None


async def search_and_scrape_babepedia(
    session: aiohttp.ClientSession, query: str
) -> tuple[dict | None, list[list[InlineKeyboardButton]]]:
    search_url = f"{BABEPEDIA_BASE}/ajax-search.php?term={quote(query)}"
    async with session.get(search_url, headers=HEADERS) as resp:
        if resp.status != 200:
            return None, []
        search_results = await resp.json()

    if not search_results:
        return None, []

    best_match = search_results[0]["value"].replace(" ", "_")
    profile_url = f"{BABEPEDIA_BASE}/babe/{best_match}"

    async with session.get(profile_url, headers=HEADERS) as resp:
        if resp.status != 200:
            return None, []
        html_text = await resp.text()

    soup = BeautifulSoup(html_text, "html.parser")
    data = {"url": profile_url}

    # Clean Name
    h1_name = soup.find("h1", id="babename")
    data["name"] = h1_name.get_text(strip=True) if h1_name else query.title()

    # Clean AKA
    h2_aka = soup.find("h2", id="aka")
    if h2_aka:
        raw_aka = h2_aka.get_text(" ", strip=True)
        data["aka"] = re.sub(r"^(AKA|Also known as):\s*", "", raw_aka, flags=re.IGNORECASE)
    else:
        data["aka"] = "N/A"

    # Rating & Votes Extraction
    rating_str = "N/A"
    votes_str = "N/A"

    rating_elem = soup.select_one(".rating-global")
    if rating_elem:
        text = rating_elem.get_text(" ", strip=True)
        score_match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", text)
        votes_match = re.search(r"([\d,]+)\s*votes?", text, re.IGNORECASE)

        if score_match:
            rating_str = score_match.group(1)
        if votes_match:
            votes_str = f"{votes_match.group(1)} votes"

    if rating_str == "N/A":
        json_ld_scripts = soup.find_all("script", type="application/ld+json")
        for script in json_ld_scripts:
            if script.string:
                try:
                    ld_data = json.loads(script.string)
                    if isinstance(ld_data, dict):
                        agg = ld_data.get("aggregateRating") or ld_data
                        if "ratingValue" in agg:
                            rating_str = str(agg.get("ratingValue"))
                        if "reviewCount" in agg or "ratingCount" in agg:
                            votes_count = agg.get("reviewCount") or agg.get("ratingCount")
                            votes_str = f"{votes_count} votes"
                except json.JSONDecodeError:
                    continue

    data["rating"] = rating_str
    data["votes"] = votes_str

    # Scraping Bio Fields
    data["age"] = get_bio_field(soup, "Age")
    data["born"] = get_bio_field(soup, "Born")
    data["years_active"] = get_bio_field(soup, "Years active")
    data["birthplace"] = get_bio_field(soup, "Birthplace")
    data["nationality"] = get_bio_field(soup, "Nationality")
    data["ethnicity"] = get_bio_field(soup, "Ethnicity")
    data["professions"] = get_bio_field(soup, "Professions")
    data["sexuality"] = get_bio_field(soup, "Sexuality")
    data["hair_color"] = get_bio_field(soup, "Hair color")
    data["eye_color"] = get_bio_field(soup, "Eye color")
    data["height"] = get_bio_field(soup, "Height")
    data["weight"] = get_bio_field(soup, "Weight")
    data["body_type"] = get_bio_field(soup, "Body type")
    data["measurements"] = get_bio_field(soup, "Measurements")
    data["bra_size"] = get_bio_field(soup, "Bra/cup size")
    data["boobs"] = get_bio_field(soup, "Boobs")
    data["tattoos"] = get_bio_field(soup, "Tattoos")
    data["piercings"] = get_bio_field(soup, "Piercings")

    data["solo"] = get_bio_field(soup, "Solo")
    data["girl_girl"] = get_bio_field(soup, "Girl/girl")
    data["boy_girl"] = get_bio_field(soup, "Boy/girl")
    data["special"] = get_bio_field(soup, "Special")

    # High quality profile photo extraction
    hq_img = soup.find("div", id="profbox2")
    if hq_img and (img_link := hq_img.find("a", class_="img")):
        data["photo"] = safe_urljoin(BABEPEDIA_BASE, img_link.get("href", ""))
    else:
        main_img = soup.find("img", id="bioimg")
        data["photo"] = safe_urljoin(BABEPEDIA_BASE, main_img.get("src", "")) if main_img else None

    # Full-Res Gallery Image Extraction
    gallery_imgs = []
    img_elements = soup.select("#profbox2 a.img img, .useruploads2 a.img img")

    for img_tag in img_elements:
        src = img_tag.get("src", "") or img_tag.get("data-src", "")
        if not src:
            continue

        # Convert thumbnail paths to full-resolution direct image URLs
        full_img_src = src.replace("/thumbs/", "/").replace("/thumb_", "/")
        full_url = safe_urljoin(BABEPEDIA_BASE, full_img_src)

        if full_url not in gallery_imgs:
            gallery_imgs.append(full_url)

    # Fallback to main profile picture if no gallery exists
    if not gallery_imgs and data.get("photo"):
        gallery_imgs.append(data["photo"])

    # Create Telegraph page
    telegraph_url = None
    if gallery_imgs:
        telegraph_url = create_telegraph_page(data["name"], gallery_imgs)

    gallery_btn_url = telegraph_url if telegraph_url else profile_url

    # Detect Platform Names for Social Buttons
    proxy_map = {
        "/onlyfans/": ("OnlyFans", "https://onlyfans.com/"),
        "/fansly/": ("Fansly", "https://fansly.com/"),
        "/instagram/": ("Instagram", "https://instagram.com/"),
        "/twitter/": ("X", "https://x.com/"),
        "/tiktok/": ("TikTok", "https://tiktok.com/"),
        "/imdb/": ("IMDb", "https://imdb.com/"),
        "/iafd/": ("IAFD", "https://iafd.com/"),
    }

    social_links = soup.select("#socialicons a[href]")
    row_buttons = []

    for a_tag in social_links:
        href = a_tag.get("href", "")
        title = a_tag.get_text(strip=True) or a_tag.get("title", "")

        final_url = href
        for prefix, (name, target) in proxy_map.items():
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
            if not title:
                title = "Official"

        if not final_url.startswith("http"):
            final_url = urljoin(BABEPEDIA_BASE, final_url)

        row_buttons.append(InlineKeyboardButton(f"{title} ↗", url=final_url))

    keyboard = [[InlineKeyboardButton("Gallery/Bio ↗", url=gallery_btn_url)]]
    for i in range(0, len(row_buttons), 2):
        keyboard.append(row_buttons[i : i + 2])

    return data, keyboard


@Client.on_message(filters.command("babe"))
async def babe_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/babe <performer name>`")
        return

    query = " ".join(message.command[1:])
    status_msg = await message.reply_text(f"🔍 Searching Babepedia for **{query}**...")

    async with aiohttp.ClientSession() as session:
        data, keyboard = await search_and_scrape_babepedia(session, query)

    if not data:
        await status_msg.edit_text(f"❌ No results found for **{query}**.")
        return

    caption = (
        f"**{data['name']}**\n"
        f"**Also known as:** {data['aka']}\n"
        f"**Rating:** ⭐ {data['rating']}/10 ({data['votes']})\n\n"
        f"**Age::** {data['age']}\n"
        f"**Born::** {data['born']}\n"
        f"**Years active::** {data['years_active']}\n"
        f"**Birthplace::** {data['birthplace']}\n"
        f"**Nationality::** {data['nationality']}\n"
        f"**Ethnicity::** {data['ethnicity']}\n"
        f"**Professions::** {data['professions']}\n"
        f"**Sexuality::** {data['sexuality']}\n"
        f"**Hair color::** {data['hair_color']}\n"
        f"**Eye color::** {data['eye_color']}\n"
        f"**Height::** {data['height']}\n"
        f"**Weight::** {data['weight']}\n"
        f"**Body type::** {data['body_type']}\n"
        f"**Measurements::** {data['measurements']}\n"
        f"**Bra/cup size::** {data['bra_size']}\n"
        f"**Boobs::** {data['boobs']}\n"
        f"**Tattoos::** {data['tattoos']}\n\n"
        f"**Piercings::** {data['piercings']}\n"
        f"**Solo::** {data['solo']}\n"
        f"**Girl/girl::** {data['girl_girl']}\n"
        f"**Boy/girl::** {data['boy_girl']}\n"
        f"**Special::** {data['special']}"
    )

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await status_msg.delete()

    # Fallback wrapper in case Telegram fails to curl the image URL
    if data.get("photo"):
        try:
            await message.reply_photo(
                photo=data["photo"],
                caption=caption,
                reply_markup=reply_markup,
            )
            return
        except (WebpageCurlFailed, BadRequest):
            pass

    # Fallback to text message if photo reply fails
    await message.reply_text(
        text=caption,
        reply_markup=reply_markup,
    )
