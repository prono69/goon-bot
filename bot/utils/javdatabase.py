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

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".wmv",
    ".mov",
    ".flv",
    ".ts",
    ".webm",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL + "/",
}


# ============================================================
# TEXT / HTML HELPERS
# ============================================================

def _clean_html_text(fragment: str) -> str:
    """Convert a small HTML fragment into clean text."""
    if not fragment:
        return ""

    text = re.sub(r"<[^>]+>", " ", fragment)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _html_to_text(html: str) -> str:
    """Convert HTML into reasonably structured plain text."""
    if not html:
        return ""

    html = re.sub(
        r"<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>",
        "\n",
        html,
        flags=re.IGNORECASE,
    )

    text = unescape(re.sub(r"<[^>]+>", " ", html))

    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)

    return "\n".join(lines)


def _labeled_links(html: str, label: str) -> List[str]:
    """
    Extract links belonging to a labeled <p>/<div>/<li>.

    Supports labels such as:
        Genre(s)
        Idol(s)/Actress(es)
    """

    pattern = re.compile(
        rf"(?is)"
        rf"<(?:p|div|li)[^>]*>"
        rf".*?<b[^>]*>\s*{re.escape(label)}\s*:?\s*</b>"
        rf"(.*?)"
        rf"</(?:p|div|li)>"
    )

    values = []

    for match in pattern.finditer(html):
        links = re.findall(
            r'<a[^>]*>(.*?)</a>',
            match.group(1),
            flags=re.IGNORECASE | re.DOTALL,
        )

        for link in links:
            value = _clean_html_text(link)

            if value and value not in values:
                values.append(value)

    return values


def _labeled_single(
    html: str,
    label: str,
) -> Optional[str]:
    """
    Extract a single value from:

    <p><b>Label: </b>Value</p>
    """

    pattern = re.compile(
        rf"(?is)"
        rf"<(?:p|div|li)[^>]*>"
        rf"\s*<b[^>]*>\s*{re.escape(label)}\s*:?\s*</b>"
        rf"\s*(.*?)"
        rf"</(?:p|div|li)>"
    )

    match = pattern.search(html)

    if not match:
        return None

    value = match.group(1)

    # Remove links while keeping their text.
    value = re.sub(
        r"<a[^>]*>(.*?)</a>",
        r"\1",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )

    value = _clean_html_text(value)

    return value or None


def _extract_line(
    page_text: str,
    labels,
    max_words: int = 20,
) -> Optional[str]:
    """Fallback field extractor for plain text."""

    for label in labels:
        pattern = re.compile(
            rf"{re.escape(label)}\s*[:\-–]?\s*(.*?)\s*(?:\n|$)",
            re.IGNORECASE,
        )

        match = pattern.search(page_text)

        if match:
            value = match.group(1).strip()

            if not value:
                continue

            words = value.split()

            return " ".join(words[:max_words])

    return None


def _extract_about(html: str) -> Optional[str]:
    """Extract the About JAV Movie section."""

    heading = re.search(
        r"(?is)"
        r"<h[1-6][^>]*>"
        r"[^<]*About[^<]*JAV Movie[^<]*"
        r"</h[1-6]>",
        html,
    )

    if heading:
        section = html[heading.end():]
    else:
        fallback = re.search(
            r"(?is)About[^<]*JAV Movie(.*)",
            html,
        )

        if not fallback:
            return None

        section = fallback.group(1)

    # Stop at the next heading.
    section = re.split(
        r"(?is)<h[1-6][^>]*>",
        section,
        maxsplit=1,
    )[0]

    text = _html_to_text(section)

    # Remove common unwanted content.
    for marker in (
        r"\(No Ratings Yet\).*",
        r"No Ratings Yet.*",
        r"Loading\.{0,3}.*",
        r"JAV Database only provides official, legitimate & legal links.*",
    ):
        text = re.sub(marker, "", text, flags=re.IGNORECASE)

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    return "\n".join(lines) or None


# ============================================================
# HTTP
# ============================================================

def fetch_html(url: str) -> Optional[str]:
    """Fetch HTML from JAVDatabase."""

    try:
        response = niquests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        response.raise_for_status()

        html = response.text or ""

        if not html:
            return None

        return html

    except Exception as exc:
        print(
            f"[JAVDatabase] Request failed: "
            f"{url} | {type(exc).__name__}: {exc}"
        )
        return None


# ============================================================
# URL / CODE HELPERS
# ============================================================

def extract_movie_code(value: str) -> Optional[str]:
    """
    Extract a movie code from either:

        VNDS-5242

    or:

        https://www.javdatabase.com/movies/VNDS-5242/
    """

    value = value.strip()

    if not value:
        return None

    match = re.search(
        r"javdatabase\.com/movies/([^/?#]+)/?",
        value,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1).strip("/")

    # If it looks like a normal movie code.
    code_match = re.search(
        r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b",
        value,
    )

    if code_match:
        return code_match.group(0)

    return value


def build_movie_url(code: str) -> str:
    """Build a JAVDatabase movie URL."""

    code = extract_movie_code(code) or code.strip()

    return f"{BASE_URL}/movies/{code.lower().strip('/')}/"


# ============================================================
# SEARCH
# ============================================================

def search_movies(
    query: str,
) -> List[Dict[str, Optional[str]]]:
    """
    Search JAVDatabase.

    First:
        https://www.javdatabase.com/?post_type=movies,uncensored&s=QUERY

    If nothing is found:
        /movies/{code}/
    """

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
            params={
                "post_type": "movies,uncensored",
                "s": query,
            },
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        response.raise_for_status()

        html = response.text or ""

        # Current JAVDatabase movie search cards.
        card_pattern = re.compile(
            r'(?is)'
            r'<div[^>]+class="[^"]*'
            r'\bcard\b[^"]*\bborderlesscard\b[^"]*"'
            r'[^>]*>'
            r'(.*?)'
            r'</div>'
        )

        for card_match in card_pattern.finditer(html):
            block = card_match.group(1)

            code_match = re.search(
                r'(?is)'
                r'<p[^>]+class="[^"]*\bpcard\b[^"]*"[^>]*>'
                r'.*?'
                r'<a[^>]+href="([^"]+)"[^>]*>'
                r'(.*?)'
                r'</a>',
                block,
            )

            if not code_match:
                continue

            link = urljoin(
                BASE_URL,
                unescape(code_match.group(1)),
            )

            code = _clean_html_text(code_match.group(2))

            title = None

            title_block = re.search(
                r'(?is)'
                r'<(?:div|p|span)[^>]+class="[^"]*\bmt-auto\b[^"]*"'
                r'[^>]*>'
                r'(.*?)'
                r'</(?:div|p|span)>',
                block,
            )

            if title_block:
                title_link = re.search(
                    r"(?is)<a[^>]*>(.*?)</a>",
                    title_block.group(1),
                )

                if title_link:
                    title = _clean_html_text(
                        title_link.group(1)
                    )

            text = _clean_html_text(block)

            date_match = re.search(
                r"\b(\d{4}-\d{2}-\d{2})\b",
                text,
            )

            studio_match = re.search(
                r'(?is)'
                r'<span[^>]+class="[^"]*\bbtn(?:-primary)?\b[^"]*"'
                r'[^>]*>'
                r'.*?<a[^>]*>(.*?)</a>',
                block,
            )

            results.append(
                {
                    "code": code,
                    "title": title or code,
                    "link": link,
                    "date": (
                        date_match.group(1)
                        if date_match
                        else None
                    ),
                    "studio": (
                        _clean_html_text(
                            studio_match.group(1)
                        )
                        if studio_match
                        else None
                    ),
                }
            )

    except Exception as exc:
        print(
            f"[JAVDatabase] Search failed: "
            f"{type(exc).__name__}: {exc}"
        )

    # If normal search worked, return it.
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

    # A valid movie page should have at least one of these.
    if not any(
        (
            metadata.get("Title"),
            metadata.get("DVD ID"),
            metadata.get("Content ID"),
        )
    ):
        return []

    results.append(
        {
            "code": metadata.get("DVD ID") or code.upper(),
            "title": metadata.get("Title") or code.upper(),
            "link": fallback_url,
            "date": metadata.get("Release Date"),
            "studio": metadata.get("Studio"),
        }
    )

    return results


# ============================================================
# MOVIE METADATA
# ============================================================

def parse_movie_metadata(
    html: str,
) -> Dict[str, Any]:
    """Extract movie metadata from a JAVDatabase movie page."""

    page_text = _html_to_text(html)

    # Main <h1>
    title_match = re.search(
        r"(?is)<h1[^>]*>(.*?)</h1>",
        html,
    )

    title = (
        _clean_html_text(title_match.group(1))
        if title_match
        else None
    )

    def field(
        label: str,
        fallbacks,
        max_words: int,
    ) -> Optional[str]:

        return (
            _labeled_single(html, label)
            or _extract_line(
                page_text,
                fallbacks,
                max_words,
            )
        )

    # IMPORTANT:
    # Actual HTML uses "Genre(s):"
    genres = _labeled_links(
        html,
        "Genre(s)",
    )

    # Actual HTML uses "Idol(s)/Actress(es):"
    actresses = _labeled_links(
        html,
        "Idol(s)/Actress(es)",
    )

    # Some older layouts may use simply "Idol".
    if not actresses:
        actresses = _labeled_links(
            html,
            "Idol",
        )

    return {
        "Title": title,

        "DVD ID": field(
            "DVD ID",
            ("DVD ID", "DVD"),
            4,
        ),

        "Content ID": field(
            "Content ID",
            ("Content ID",),
            4,
        ),

        "Release Date": field(
            "Release Date",
            ("Release Date", "Released"),
            4,
        ),

        "Runtime": field(
            "Runtime",
            ("Runtime",),
            8,
        ),

        "Studio": field(
            "Studio",
            ("Studio",),
            8,
        ),

        "Director": field(
            "Director",
            ("Director",),
            8,
        ),

        "Series": (
            _labeled_single(
                html,
                "JAV Series",
            )
            or field(
                "Series",
                ("JAV Series", "Series"),
                8,
            )
        ),

        "Plot": _extract_about(html),

        "Genres": sorted(
            set(genres),
            key=str.casefold,
        ),

        "Actresses": sorted(
            set(actresses),
            key=str.casefold,
        ),
    }


# ============================================================
# POSTER / PREVIEWS
# ============================================================

def parse_poster_url(
    html: str,
) -> Optional[str]:
    """Extract the movie poster URL."""

    container = re.search(
        r'(?is)'
        r'<div[^>]+id="poster-container"[^>]*>'
        r'(.*?)'
        r'</div>',
        html,
    )

    if container:
        image = re.search(
            r'<img[^>]+src=["\']([^"\']+)["\']',
            container.group(1),
            flags=re.IGNORECASE,
        )

        if image:
            return urljoin(
                BASE_URL,
                unescape(image.group(1)),
            )

    # Fallback patterns.
    patterns = (
        r'(?is)'
        r'<div[^>]+class="[^"]*\bposter\b[^"]*"[^>]*>'
        r'.*?<img[^>]+src=["\']([^"\']+)["\']',

        r'(?is)'
        r'<img[^>]+'
        r'alt=["\'][^"\']*JAV Movie Cover[^"\']*["\']'
        r'[^>]+src=["\']([^"\']+)["\']',

        r'(?is)'
        r'<meta[^>]+property=["\']og:image["\']'
        r'[^>]+content=["\']([^"\']+)["\']',
    )

    for pattern in patterns:
        image = re.search(
            pattern,
            html,
            flags=re.IGNORECASE,
        )

        if image:
            return urljoin(
                BASE_URL,
                unescape(image.group(1)),
            )

    return None


def parse_preview_images(
    html: str,
) -> List[Dict[str, Optional[str]]]:
    """Extract gallery preview/full-size images."""

    images = []

    # data-image-src layout.
    anchors = re.compile(
        r'(?is)'
        r'<a([^>]*data-image-src="[^"]+"[^>]*)>'
        r'(.*?)'
        r'</a>'
    )

    for attributes, inner_html in anchors.findall(html):

        preview = re.search(
            r'data-image-src=["\']([^"\']+)["\']',
            attributes,
            flags=re.IGNORECASE,
        )

        full = re.search(
            r'data-image-href=["\']([^"\']+)["\']',
            attributes,
            flags=re.IGNORECASE,
        )

        image = re.search(
            r'<img[^>]+src=["\']([^"\']+)["\']',
            inner_html,
            flags=re.IGNORECASE,
        )

        item = {
            "preview": (
                urljoin(
                    BASE_URL,
                    unescape(preview.group(1)),
                )
                if preview
                else None
            ),
            "full": (
                urljoin(
                    BASE_URL,
                    unescape(full.group(1)),
                )
                if full
                else None
            ),
            "img": (
                urljoin(
                    BASE_URL,
                    unescape(image.group(1)),
                )
                if image
                else None
            ),
        }

        if item["preview"] or item["full"]:
            images.append(item)

    return images


# ============================================================
# COMPLETE MOVIE DETAILS
# ============================================================

def get_movie_details(
    page_url: str,
) -> Dict[str, Any]:
    """
    Fetch complete movie details.

    Returns:
        metadata
        poster
        previews
        NFO
    """

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

    nfo_xml = build_nfo_xml(
        metadata=metadata,
        title=title,
        genres=genres,
        actresses=actresses,
        poster_url=poster_url,
        local_fanart=[],
    )

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

    year = (
        release_date[:4]
        if release_date
        and len(release_date) >= 4
        and release_date[:4].isdigit()
        else None
    )

    for tag_name in (
        "title",
        "originaltitle",
        "sorttitle",
        "localtitle",
    ):
        if title:
            SubElement(
                movie,
                tag_name,
            ).text = title

    if year:
        SubElement(
            movie,
            "year",
        ).text = year

    if release_date:
        SubElement(
            movie,
            "releasedate",
        ).text = release_date

    runtime = re.search(
        r"\d+",
        metadata.get("Runtime") or "",
    )

    if runtime:
        SubElement(
            movie,
            "runtime",
        ).text = runtime.group(0)

    for field_name, tag_name in (
        ("Plot", "plot"),
        ("Studio", "studio"),
        ("Director", "director"),
        ("Series", "set"),
    ):
        value = metadata.get(field_name)

        if value:
            SubElement(
                movie,
                tag_name,
            ).text = value

    for genre in genres:
        SubElement(
            movie,
            "genre",
        ).text = genre

    for actress in actresses:
        actor = SubElement(
            movie,
            "actor",
        )

        SubElement(
            actor,
            "name",
        ).text = actress

    for id_type, value in (
        ("dvdid", metadata.get("DVD ID")),
        ("contentid", metadata.get("Content ID")),
    ):
        if value:
            unique_id = SubElement(
                movie,
                "uniqueid",
                {"type": id_type},
            )

            unique_id.text = value

    if poster_url:
        SubElement(
            movie,
            "thumb",
        ).text = poster_url

    if local_fanart:
        fanart = SubElement(
            movie,
            "fanart",
        )

        for filename in local_fanart:
            SubElement(
                fanart,
                "thumb",
            ).text = filename

    raw_xml = tostring(
        movie,
        encoding="utf-8",
    )

    return (
        minidom
        .parseString(raw_xml)
        .toprettyxml(
            indent="  ",
            encoding="utf-8",
        )
        .decode("utf-8")
    )


# ============================================================
# DOWNLOAD
# ============================================================

def download_file_bytes(
    url: str,
) -> Optional[bytes]:
    """Download an asset into memory."""

    try:
        response = niquests.get(
            url,
            headers=HEADERS,
            timeout=DOWNLOAD_TIMEOUT,
        )

        response.raise_for_status()

        return response.content

    except Exception as exc:
        print(
            f"[JAVDatabase] Download failed: "
            f"{type(exc).__name__}: {exc}"
        )

        return None


# ============================================================
# FILENAME
# ============================================================

def safe_filename(
    name: str,
) -> str:
    """Create a filesystem-safe filename."""

    name = re.sub(
        r'[\x00-\x1f\\/:*?"<>|]+',
        "-",
        name.strip(),
    )

    name = re.sub(
        r"\s+",
        "_",
        name,
    )

    return name.strip("._-")[:200]