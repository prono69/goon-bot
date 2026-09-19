import re
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

import niquests

BASE_URL = "https://www.javdatabase.com"
REQUEST_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 20

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts", ".webm"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL + "/",
}

# Known metadata labels on JAVDatabase used as hard boundary stops
FIELD_MARKERS = [
    "DVD ID",
    "Content ID",
    "Release Date",
    "Released",
    "Runtime",
    "Studio",
    "Director",
    "JAV Series",
    "Series",
    "Genre(s)",
    "Idol(s)/Actress(es)",
    "Idol",
]

# ============================================================
# COMPILED REGEX PATTERNS (for static patterns only)
# ============================================================

_PATTERN_TAG_REMOVAL = re.compile(r"<[^>]+>")
_PATTERN_BREAK_TAGS = re.compile(r"<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>", re.IGNORECASE)
_PATTERN_WHITESPACE = re.compile(r"\s+")
_PATTERN_LINKS = re.compile(r"<a[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_PATTERN_HEADING = re.compile(r"(?is)<h[1-6][^>]*>[^<]*About[^<]*JAV Movie[^<]*</h[1-6]>")
_PATTERN_MOVIE_CODE = re.compile(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b")
_PATTERN_H1 = re.compile(r"(?is)<h1[^>]*>(.*?)</h1>")
_PATTERN_CARD = re.compile(r'(?is)<div[^>]+class="[^"]*\bcard\b[^"]*\bborderlesscard\b[^"]*"[^>]*>(.*?)</div>')
_PATTERN_PCARD = re.compile(r'(?is)<p[^>]+class="[^"]*\bpcard\b[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>')
_PATTERN_MTAUTO = re.compile(r'(?is)<(?:div|p|span)[^>]+class="[^"]*\bmt-auto\b[^"]*"[^>]*>(.*?)</(?:div|p|span)>')
_PATTERN_BTN = re.compile(r'(?is)<span[^>]+class="[^"]*\bbtn(?:-primary)?\b[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>')
_PATTERN_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_PATTERN_POSTER_CONTAINER = re.compile(r'(?is)<div[^>]+id="poster-container"[^>]*>(.*?)</div>')
_PATTERN_OG_IMAGE = re.compile(r'(?is)<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']')
_PATTERN_POSTER_DIV = re.compile(r'(?is)<div[^>]+class="[^"]*\bposter\b[^"]*"[^>]*>.*?<img[^>]+src=["\']([^"\']+)["\']')
_PATTERN_ALT_IMG = re.compile(r'(?is)<img[^>]+alt=["\'][^"\']*JAV Movie Cover[^"\']*["\'][^>]+src=["\']([^"\']+)["\']')
_PATTERN_ANCHORS = re.compile(r'(?is)<a([^>]*data-image-src="[^"]+"[^>]*)>(.*?)</a>')
_PATTERN_UNWANTED_CONTENT = [
    re.compile(r"\(No Ratings Yet\).*", re.IGNORECASE),
    re.compile(r"No Ratings Yet.*", re.IGNORECASE),
    re.compile(r"Loading\.{0,3}.*", re.IGNORECASE),
    re.compile(r"JAV Database only provides official, legitimate & legal links.*", re.IGNORECASE),
    re.compile(r"\b[1-5]\s*Stars?.*", re.IGNORECASE),
]

# Build stopping boundary out of all known metadata labels
_FIELD_BOUNDARY_PATTERN = "|".join([re.escape(m) for m in FIELD_MARKERS])


# ============================================================
# TEXT / HTML HELPERS
# ============================================================

def _clean_html_text(fragment: str) -> str:
    """Convert a small HTML fragment into clean text."""
    if not fragment:
        return ""
    text = _PATTERN_TAG_REMOVAL.sub(" ", fragment)
    text = unescape(text)
    return _PATTERN_WHITESPACE.sub(" ", text).strip()


def _html_to_text(html: str) -> str:
    """Convert HTML into reasonably structured plain text."""
    if not html:
        return ""

    html = _PATTERN_BREAK_TAGS.sub("\n", html)
    text = unescape(_PATTERN_TAG_REMOVAL.sub(" ", html))

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _url_join(path: str) -> str:
    """Helper to join BASE_URL with a path and unescape."""
    return urljoin(BASE_URL, unescape(path))


def _labeled_links(html: str, label: str) -> List[str]:
    """Extract links belonging to a labeled <p>/<div>/<li>."""
    pattern = re.compile(
        rf"(?is)<(?:p|div|li)[^>]*>.*?<b[^>]*>\s*{re.escape(label)}\s*:?\s*</b>(.*?)</(?:p|div|li)>"
    )
    values = []

    for match in pattern.finditer(html):
        for link in _PATTERN_LINKS.finditer(match.group(1)):
            value = _clean_html_text(link.group(1))
            if value and value not in values:
                values.append(value)

    return values


def _labeled_single(html: str, label: str) -> Optional[str]:
    """
    Extract a single value from:
    <p><b>Label: </b>Value</p> OR <b>Label:</b> Value <b>Next Label:</b>
    """
    pattern = re.compile(
        rf"(?is)<b[^>]*>\s*{re.escape(label)}\s*:?\s*</b>\s*(.*?)(?=<b[^>]*>|<(?:/p|/div|/li|br)>|$)"
    )
    match = pattern.search(html)

    if not match:
        return None

    value = match.group(1)
    value = re.sub(r"<a[^>]*>(.*?)</a>", r"\1", value, flags=re.IGNORECASE | re.DOTALL)
    value = _clean_html_text(value)

    # If the value contains another field label, it's invalid
    if value and any(marker + ":" in value or marker in value for marker in ("Genre(s)", "Studio", "DVD ID", "Content ID") if marker != label):
        return None

    return value or None


def _extract_line(page_text: str, labels, max_words: int = 20) -> Optional[str]:
    """Fallback field extractor with lookahead stopping at adjacent labels."""
    for label in labels:
        pattern = re.compile(
            rf"{re.escape(label)}\s*[:\-–]?\s*(.*?)(?=(?:{_FIELD_BOUNDARY_PATTERN})\s*[:\-–]?|\n|$)",
            re.IGNORECASE,
        )
        match = pattern.search(page_text)

        if match:
            value = match.group(1).strip()
            value = re.sub(r"^[:\-–\s]+|[:\-–\s]+$", "", value)

            if value:
                words = value.split()
                if words:
                    return " ".join(words[:max_words])
    return None


def _extract_about(html: str) -> Optional[str]:
    """Extract the About JAV Movie section."""
    heading = _PATTERN_HEADING.search(html)
    section = html[heading.end():] if heading else None

    if not section:
        fallback = re.search(r"(?is)About[^<]*JAV Movie(.*)", html)
        if not fallback:
            return None
        section = fallback.group(1)

    section = re.split(r"(?is)<h[1-6][^>]*>", section, maxsplit=1)[0]
    text = _html_to_text(section)

    for pattern in _PATTERN_UNWANTED_CONTENT:
        text = pattern.sub("", text)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines) or None


def format_runtime(runtime_str: Optional[str]) -> Optional[str]:
    """
    Convert a runtime string like '160 min.' or '160' into '2 hr 40 min'.
    If runtime is under an hour, returns '45 min'.
    """
    if not runtime_str:
        return None

    # Extract digits from the string
    match = re.search(r"\d+", runtime_str)
    if not match:
        return runtime_str

    total_minutes = int(match.group(0))
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours > 0 and minutes > 0:
        return f"{hours} hr {minutes} min"
    elif hours > 0:
        return f"{hours} hr"
    else:
        return f"{minutes} min"


# ============================================================
# HTTP
# ============================================================

def fetch_html(url: str) -> Optional[str]:
    """Fetch HTML from JAVDatabase."""
    try:
        response = niquests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        return response.text or None
    except Exception as exc:
        print(f"[JAVDatabase] Request failed: {url} | {type(exc).__name__}: {exc}")
        return None


# ============================================================
# URL / CODE HELPERS
# ============================================================

def extract_movie_code(value: str) -> Optional[str]:
    """Extract a movie code from either: VNDS-5242 or https://www.javdatabase.com/movies/VNDS-5242/"""
    value = value.strip()
    if not value:
        return None

    match = re.search(r"javdatabase\.com/movies/([^/?#]+)/?", value, re.IGNORECASE)
    if match:
        return match.group(1).strip("/")

    code_match = _PATTERN_MOVIE_CODE.search(value)
    return code_match.group(0) if code_match else value


def build_movie_url(code: str) -> str:
    """Build a JAVDatabase movie URL."""
    code = extract_movie_code(code) or code.strip()
    return f"{BASE_URL}/movies/{code.lower().strip('/')}/"


# ============================================================
# SEARCH
# ============================================================

def search_movies(query: str) -> List[Dict[str, Optional[str]]]:
    """Search JAVDatabase."""
    query = query.strip()
    if not query:
        return []

    results: List[Dict[str, Optional[str]]] = []

    # --------------------------------------------------------
    # 1. NORMAL SEARCH
    # --------------------------------------------------------
    try:
        response = niquests.get(
            BASE_URL + "/",
            params={"post_type": "movies,uncensored", "s": query},
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        html = response.text or ""

        for card_match in _PATTERN_CARD.finditer(html):
            block = card_match.group(1)
            code_match = _PATTERN_PCARD.search(block)

            if not code_match:
                continue

            link = _url_join(code_match.group(1))
            code = _clean_html_text(code_match.group(2))

            title_block = _PATTERN_MTAUTO.search(block)
            title = None
            if title_block:
                title_link = _PATTERN_LINKS.search(title_block.group(1))
                if title_link:
                    title = _clean_html_text(title_link.group(1))

            text = _clean_html_text(block)
            date_match = _PATTERN_DATE.search(text)
            studio_match = _PATTERN_BTN.search(block)

            results.append({
                "code": code,
                "title": title or code,
                "link": link,
                "date": date_match.group(1) if date_match else None,
                "studio": _clean_html_text(studio_match.group(1)) if studio_match else None,
            })

    except Exception as exc:
        print(f"[JAVDatabase] Search failed: {type(exc).__name__}: {exc}")

    if results:
        return results

    # --------------------------------------------------------
    # 2. DIRECT CODE FALLBACK
    # --------------------------------------------------------
    code = extract_movie_code(query)
    if not code:
        return []

    fallback_url = build_movie_url(code)
    fallback_html = fetch_html(fallback_url)
    if not fallback_html:
        return []

    metadata = parse_movie_metadata(fallback_html)
    if not any((metadata.get("Title"), metadata.get("DVD ID"), metadata.get("Content ID"))):
        return []

    results.append({
        "code": metadata.get("DVD ID") or code.upper(),
        "title": metadata.get("Title") or code.upper(),
        "link": fallback_url,
        "date": metadata.get("Release Date"),
        "studio": metadata.get("Studio"),
    })

    return results


# ============================================================
# MOVIE METADATA
# ============================================================

def parse_movie_metadata(html: str) -> Dict[str, Any]:
    """Extract movie metadata from a JAVDatabase movie page."""
    page_text = _html_to_text(html)

    title_match = _PATTERN_H1.search(html)
    title = _clean_html_text(title_match.group(1)) if title_match else None

    def field(label: str, fallbacks, max_words: int) -> Optional[str]:
        return _labeled_single(html, label) or _extract_line(page_text, fallbacks, max_words)

    genres = _labeled_links(html, "Genre(s)") or []
    actresses = _labeled_links(html, "Idol(s)/Actress(es)") or _labeled_links(html, "Idol") or []

    director = field("Director", ("Director",), 8)
    if director and any(marker in director for marker in ("Genre", "Studio", "Series", "DVD ID")):
        director = None

    series = _labeled_single(html, "JAV Series") or _labeled_single(html, "Series") or _extract_line(page_text, ("JAV Series", "Series"), 8)
    if series and any(marker in series for marker in ("DVD ID", "Content ID", "Genre")):
        series = None

    return {
        "Title": title,
        "DVD ID": field("DVD ID", ("DVD ID", "DVD"), 4),
        "Content ID": field("Content ID", ("Content ID",), 4),
        "Release Date": field("Release Date", ("Release Date", "Released"), 4),
        "Runtime": format_runtime(field("Runtime", ("Runtime",), 8)),
        "Studio": field("Studio", ("Studio",), 8),
        "Director": director,
        "Series": series,
        "Plot": _extract_about(html),
        "Genres": sorted(set(genres), key=str.casefold),
        "Actresses": sorted(set(actresses), key=str.casefold),
    }


# ============================================================
# POSTER / PREVIEWS
# ============================================================

def parse_poster_url(html: str) -> Optional[str]:
    """Extract the movie poster URL."""
    container = _PATTERN_POSTER_CONTAINER.search(html)

    if container:
        image = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', container.group(1), re.IGNORECASE)
        if image:
            return _url_join(image.group(1))

    for pattern in [_PATTERN_POSTER_DIV, _PATTERN_ALT_IMG, _PATTERN_OG_IMAGE]:
        image = pattern.search(html)
        if image:
            return _url_join(image.group(1))

    return None


def parse_preview_images(html: str) -> List[Dict[str, Optional[str]]]:
    """Extract gallery preview/full-size images."""
    images = []

    for attributes, inner_html in _PATTERN_ANCHORS.findall(html):
        preview = re.search(r'data-image-src=["\']([^"\']+)["\']', attributes, re.IGNORECASE)
        full = re.search(r'data-image-href=["\']([^"\']+)["\']', attributes, re.IGNORECASE)
        image = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', inner_html, re.IGNORECASE)

        item = {
            "preview": _url_join(preview.group(1)) if preview else None,
            "full": _url_join(full.group(1)) if full else None,
            "img": _url_join(image.group(1)) if image else None,
        }

        if item["preview"] or item["full"]:
            images.append(item)

    return images


# ============================================================
# COMPLETE MOVIE DETAILS
# ============================================================

def get_movie_details(page_url: str) -> Dict[str, Any]:
    """Fetch complete movie details."""
    html = fetch_html(page_url)
    if not html:
        return {}

    metadata = parse_movie_metadata(html)
    poster_url = parse_poster_url(html)
    previews = parse_preview_images(html)

    preview_urls = []
    for image in previews:
        url = image.get("full") or image.get("preview")
        if url and url not in preview_urls:
            preview_urls.append(url)

    title = metadata.get("Title") or "movie"
    genres = metadata.get("Genres") or []
    actresses = metadata.get("Actresses") or []

    nfo_xml = build_nfo_xml(metadata, title, genres, actresses, poster_url, [])

    return {
        "link": page_url,
        "title": title,
        "dvd_id": metadata.get("DVD ID"),
        "content_id": metadata.get("Content ID"),
        "release_date": metadata.get("Release Date"),
        "runtime": metadata.get("Runtime"),
        "studio": metadata.get("Studio"),
        "director": metadata.get("Director"),
        "series": metadata.get("Series"),
        "plot": metadata.get("Plot"),
        "genres": genres,
        "actresses": actresses,
        "poster_url": poster_url,
        "preview_urls": preview_urls,
        "nfo_xml": nfo_xml,
    }


# ============================================================
# NFO
# ============================================================

def _add_element(parent: Element, tag: str, text: Optional[str] = None, attrib: Dict = None) -> Element:
    """Helper to add an element with optional text."""
    element = SubElement(parent, tag, attrib or {})
    if text:
        element.text = text
    return element


def build_nfo_xml(
    metadata: Dict[str, Any],
    title: str,
    genres: List[str],
    actresses: List[str],
    poster_url: Optional[str],
    local_fanart: List[str],
) -> str:
    """Generate Kodi-compatible NFO XML."""
    movie = Element("movie")

    release_date = metadata.get("Release Date")
    year = release_date[:4] if release_date and len(release_date) >= 4 and release_date[:4].isdigit() else None

    if title:
        for tag_name in ("title", "originaltitle", "sorttitle", "localtitle"):
            _add_element(movie, tag_name, title)

    if year:
        _add_element(movie, "year", year)
    if release_date:
        _add_element(movie, "releasedate", release_date)

    runtime_match = re.search(r"\d+", metadata.get("Runtime") or "")
    if runtime_match:
        _add_element(movie, "runtime", runtime_match.group(0))

    for field_name, tag_name in [("Plot", "plot"), ("Studio", "studio"), ("Director", "director"), ("Series", "set")]:
        value = metadata.get(field_name)
        if value:
            _add_element(movie, tag_name, value)

    for genre in genres:
        _add_element(movie, "genre", genre)

    for actress in actresses:
        actor = _add_element(movie, "actor")
        _add_element(actor, "name", actress)

    for id_type, value in [("dvdid", metadata.get("DVD ID")), ("contentid", metadata.get("Content ID"))]:
        if value:
            _add_element(movie, "uniqueid", value, {"type": id_type})

    if poster_url:
        _add_element(movie, "thumb", poster_url)

    if local_fanart:
        fanart = _add_element(movie, "fanart")
        for filename in local_fanart:
            _add_element(fanart, "thumb", filename)

    raw_xml = tostring(movie, encoding="utf-8")
    return minidom.parseString(raw_xml).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


# ============================================================
# DOWNLOAD
# ============================================================

def download_file_bytes(url: str) -> Optional[bytes]:
    """Download an asset into memory."""
    try:
        response = niquests.get(url, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        return response.content
    except Exception as exc:
        print(f"[JAVDatabase] Download failed: {type(exc).__name__}: {exc}")
        return None


# ============================================================
# FILENAME
# ============================================================

def safe_filename(name: str) -> str:
    """Create a filesystem-safe filename."""
    name = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', "-", name.strip())
    name = re.sub(r"\s+", "_", name)
    return name.strip("._-")[:200]
