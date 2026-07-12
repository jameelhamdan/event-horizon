"""Article body/title hydration for historical backfill.

Backfill candidates arrive title-only (sitemap entries, Wikipedia event
sentences); without body text the NLP step can't geocode them, so they never
aggregate into Events. This module pulls a page and extracts <title> +
paragraph text — enough for geocoding + category classification — with a
Wayback Machine fallback for pages that are dead, paywalled, or JS-only.

Split out of services.data.historical so the HTTP/HTML hydration concern lives
apart from day-window discovery and the save/dedup pipeline (and so the
backfill's parallel-fetch step has a clean, testable surface). historical.py
re-exports the public names here for backward compatibility.

Source-timeout blocklisting (_is_source_blocked/_block_source) still lives in
historical.py — imported lazily below to keep the module graph acyclic.
"""

import html as _html
import logging
import re

import requests

logger = logging.getLogger(__name__)

HTTP_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; HistoricalBackfiller/1.0)'}
HTTP_TIMEOUT = 15

_WAYBACK_AVAILABILITY_URL = 'https://archive.org/wayback/available'

_BODY_MAX_CHARS = 4000
_SCRIPT_STYLE_RE = re.compile(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>')
_PARAGRAPH_RE = re.compile(r'(?is)<p[^>]*>(.*?)</p>')
_TAG_RE = re.compile(r'(?s)<[^>]+>')
_TITLE_RE = re.compile(r'(?is)<title[^>]*>(.*?)</title>')

# Paywall / interstitial / error page titles — never worth adopting as an
# article title, and a signal the page body is chrome too (verified live:
# FT's paywall serves <title>Subscribe to read</title>, which the smoke test
# turned into Events literally titled "Subscribe to read").
_JUNK_TITLE_RE = re.compile(
    r'^\s*(subscribe|sign.?in|log.?in|register)\b'
    r'|\b(access denied|forbidden|page not found|are you a robot'
    r'|attention required|just a moment|enable (javascript|cookies)|captcha)\b'
    r'|^\s*(404|403|401)\b',
    re.IGNORECASE,
)


def is_junk_page_title(title: str | None) -> bool:
    return bool(title) and bool(_JUNK_TITLE_RE.search(title))


def _extract_title_and_text(page_html: str) -> tuple[str | None, str | None]:
    """(<title> text, paragraph text capped at _BODY_MAX_CHARS) from raw HTML."""
    # Cap before regex work: the backreference pattern degrades O(N) per unclosed
    # <script>/<style> tag; 200 KB is ample for any news article's paragraph text.
    page_html = page_html[:200_000]
    title_m = _TITLE_RE.search(page_html)
    title = _html.unescape(re.sub(r'\s+', ' ', title_m.group(1))).strip() if title_m else None
    html = _SCRIPT_STYLE_RE.sub(' ', page_html)
    paragraphs = _PARAGRAPH_RE.findall(html)
    text = ' '.join(_TAG_RE.sub(' ', p) for p in paragraphs)
    text = _html.unescape(re.sub(r'\s+', ' ', text)).strip()
    return title or None, text[:_BODY_MAX_CHARS] or None


def fetch_article_page(
    url: str, source_code: str | None = None, timeout: int = HTTP_TIMEOUT,
) -> tuple[str | None, str | None]:
    """Best-effort (page title, plain-text body) for a historical article URL.

    Returns (None, None) on any failure (the caller falls back to the Wayback
    Machine, then to the discovery title).

    source_code: when given, participates in the same timeout blocklist as sitemap
    discovery (skipped if already blocked; a Timeout here blocks it too) — a source
    whose article pages are timing out is treated the same as one whose sitemap is.

    Thread-safe: touches only requests + the Redis-backed source blocklist (no
    ORM), so backfill fans these out concurrently across a day-window's articles.
    """
    from services.data.historical import _block_source, _is_source_blocked

    if source_code and _is_source_blocked(source_code):
        return None, None
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.Timeout:
        if source_code:
            _block_source(source_code, f'article body timeout: {url}')
        return None, None
    except requests.RequestException as exc:
        logger.debug('body fetch failed url=%r: %s', url, exc)
        return None, None
    return _extract_title_and_text(resp.text)


def fetch_article_body(url: str, source_code: str | None = None, timeout: int = HTTP_TIMEOUT) -> str | None:
    """Body-only convenience wrapper around fetch_article_page."""
    return fetch_article_page(url, source_code=source_code, timeout=timeout)[1]


def fetch_wayback_page(url, around=None, timeout: int = HTTP_TIMEOUT) -> tuple[str | None, str | None]:
    """(title, body) for *url* from the Wayback Machine capture closest to
    ``around`` — the fallback when the live page is dead, paywalled, or
    JS-only (historical cited URLs frequently are).

    Uses the availability API to find the closest capture, then fetches the
    ``id_`` variant (original HTML, no archive toolbar). Both requests go
    through services.data.wayback's polite shared client (module-wide pacing,
    optional proxy) with a single retry each — a backfill flood of dead URLs
    must not hammer Wayback, and a miss just means the caller saves the
    article with its discovery title.
    """
    from services.data.wayback import _wayback_get

    params = {'url': url}
    if around is not None:
        params['timestamp'] = around.strftime('%Y%m%d')
    resp = _wayback_get(_WAYBACK_AVAILABILITY_URL, params=params, retries=1, timeout=timeout)
    if resp is None:
        return None, None
    try:
        snapshot = (resp.json().get('archived_snapshots') or {}).get('closest') or {}
    except ValueError:
        return None, None
    if not snapshot.get('available') or not snapshot.get('timestamp'):
        return None, None
    archive_url = f'https://web.archive.org/web/{snapshot["timestamp"]}id_/{url}'
    resp = _wayback_get(archive_url, retries=1, timeout=timeout)
    if resp is None:
        return None, None
    return _extract_title_and_text(resp.text)
