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
# Upper bound on HTML fed to trafilatura (a real lxml parser, not the O(N) regex
# fallback). Sized well above large news pages — the Guardian front-loads ~200 KB
# of inline scripts/JSON-LD before the article body, so a tighter cap truncated
# the body off the page and forced the nav-only regex fallback.
_HTML_MAX_CHARS = 4_000_000
# The regex fallback's own cap — its backreference patterns degrade O(N) per
# unclosed tag, so it stays tightly bounded regardless of _HTML_MAX_CHARS.
_REGEX_HTML_MAX_CHARS = 200_000
_SCRIPT_STYLE_RE = re.compile(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>')
# Boilerplate strips for the regex fallback extractor (_extract_body_regex),
# used only when trafilatura returns nothing. Backreference \1 so the matching
# close tag ends each strip.
_CHROME_TAG_RE = re.compile(r'(?is)<(nav|header|footer|aside)\b[^>]*>.*?</\1>')
_BOILERPLATE_CLASS_RE = re.compile(  # <div class="nav">/<ul class="menu">/… containers
    r'(?is)<(\w+)(?=[^>]*\b(?:class|id)\s*=\s*["\'][^"\']*'
    r'(?:nav|menu|sidebar|breadcrumb|cookie|subscribe|newsletter-signup|'
    r'social-share|share-bar|related-(?:links|articles|content)|'
    r'advert|promo|byline-social|site-header|site-footer|masthead)'
    r'[^"\']*["\'])[^>]*>.*?</\1>'
)
_ARTICLE_MAIN_RE = re.compile(r'(?is)<(?:article|main)\b[^>]*>(.*?)</(?:article|main)>')
_PARAGRAPH_RE = re.compile(r'(?is)<p[^>]*>(.*?)</p>')
_A_TAG_TEXT_RE = re.compile(r'(?is)<a[^>]*>(.*?)</a>')
_TAG_RE = re.compile(r'(?s)<[^>]+>')
_TITLE_RE = re.compile(r'(?is)<title[^>]*>(.*?)</title>')

# A <p> shorter than this with no sentence-ending punctuation is a menu label,
# not prose; a short <p> with >60% of its text inside <a> tags is a link list.
_MIN_PARAGRAPH_CHARS = 15
_SENTENCE_END_RE = re.compile(r'[.!?]\s*$')
_LINK_DENSITY_THRESHOLD = 0.6
_LINK_DENSITY_MAX_CHARS = 200


def _is_boilerplate_paragraph(p_html: str, plain_text: str) -> bool:
    """True if a <p> block's own shape says "chrome", not article prose —
    either mostly link text (a nav/menu/related-links list) or too short to
    be a real sentence (a bare menu label)."""
    if not plain_text:
        return True
    if len(plain_text) < _MIN_PARAGRAPH_CHARS and not _SENTENCE_END_RE.search(plain_text):
        return True
    if len(plain_text) <= _LINK_DENSITY_MAX_CHARS:
        link_text = ' '.join(_TAG_RE.sub(' ', a) for a in _A_TAG_TEXT_RE.findall(p_html))
        if link_text and len(link_text) / len(plain_text) >= _LINK_DENSITY_THRESHOLD:
            return True
    return False

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


# Subscription-wall / metering interstitials that replace the article body with
# a CTA. Strong, unambiguous phrases only — a bare "subscribe" also appears in
# legitimate article footers, so it is deliberately excluded. Verified live: FT
# ("Subscribe to unlock this article Try unlimited access"), Project Syndicate
# ("Available exclusively to PS subscribers").
_PAYWALL_MARKER_RE = re.compile(
    r'subscribe to (?:unlock|read|continue)'
    r'|to (?:continue|keep) reading'
    r'|(?:try|for) unlimited access|unlimited digital access'
    r'|already a subscriber|available exclusively to (?:\w+ )?subscribers'
    r'|this (?:article|content|story) is (?:for|available to|exclusive)'
    r'|sign in to read|become a (?:member|subscriber)',
    re.IGNORECASE,
)
# A wall interrupts the body early; a genuine footer CTA sits at the very end of
# a long article. Treat a marker as a wall only when it appears within this
# leading fraction of the extracted text, so a real article that closes with a
# subscribe call-to-action is kept.
_PAYWALL_MARKER_MAX_POSITION = 0.6


def _is_paywall_body(text: str | None) -> bool:
    """True if the extracted "body" is really a subscription wall / metering
    interstitial rather than article prose — a strong paywall CTA phrase in the
    leading portion of the text. Position-gated so a full article that merely
    ends with a subscribe CTA is not discarded (verified: keeps SCMP/Wired/
    Foreign Policy leads, drops FT/Project Syndicate wall text)."""
    if not text:
        return False
    m = _PAYWALL_MARKER_RE.search(text)
    return bool(m) and m.start() <= len(text) * _PAYWALL_MARKER_MAX_POSITION


# URL path segments that mark a non-article page regardless of site/CMS —
# staff bios, contact forms, tag/category indexes, author archives, homepages,
# affiliate/advisor SEO hubs (Forbes /advisor/ listicles: "Personal Loan
# Requirements" etc.). These routinely share a news sitemap/feed with real
# articles. Shared with services/data/historical.py (sitemap discovery skips
# them up front).
_NON_ARTICLE_PATH_SEGMENTS = frozenset({
    'people', 'staff', 'contact', 'contact-us', 'author', 'authors',
    'tag', 'tags', 'category', 'categories', 'about', 'about-us', 'topic', 'topics',
    'advisor',
})
# An image/asset URL whose final path segment is a pixel-dimension slug
# ("205x205_property-1the-checkup-mail-icon") — a CMS asset, never an article
# (observed live: mit-tech-review ingested "205X205 Property 1The Checkup Mail
# Icon" from /205x205_property-1the-checkup-mail-icon/).
_IMAGE_DIMENSION_SLUG_RE = re.compile(r'^\d+x\d+(?:[_-]|$)')
# A title that is itself a bare URL — the slug-from-URL fallback fired and the
# page never yielded a real <title> (observed: allafrica stories ingested with
# title = "https://allafrica.com/stories/…").
_RAW_URL_TITLE_RE = re.compile(r'^\s*https?://', re.I)


def is_non_article_url(source_url: str | None) -> bool:
    """True if a URL's shape alone marks it a non-article page — homepage/root,
    a tag/category/author/staff/advisor index, or an image-asset slug. One
    definition shared by is_junk_article (soft-delete) and historical.py sitemap
    discovery (skip up front) so the two never drift."""
    from urllib.parse import urlparse
    path = urlparse(source_url or '').path.strip('/').lower()
    if not path:  # homepage / section root
        return True
    segments = path.split('/')
    if _NON_ARTICLE_PATH_SEGMENTS & set(segments):
        return True
    return bool(_IMAGE_DIMENSION_SLUG_RE.match(segments[-1]))


def is_junk_article(title: str | None, source_url: str | None) -> bool:
    """True if a stored article is not a real article and should be
    soft-deleted (is_deleted=True) rather than classified: a raw-URL title, a
    paywall/error interstitial (is_junk_page_title), or a non-article page
    (homepage/root, tag/category/author/staff/advisor index, image asset) by its
    URL path. Detection is conservative — clear structural junk only, not
    thin-but-real articles — since it removes the row from every ordinary
    queryset."""
    if not title or _RAW_URL_TITLE_RE.search(title) or is_junk_page_title(title):
        return True
    return is_non_article_url(source_url)


def _extract_title_and_text(page_html: str) -> tuple[str | None, str | None]:
    """(<title> text, main article text capped at _BODY_MAX_CHARS) from raw HTML.

    Body extraction runs trafilatura first — a maintained, benchmark-topping
    (F1 ≈ 0.94) main-content extractor that strips nav/share-bars/subscription
    chrome and recovers article text the old regex pass missed entirely (thin
    or empty extractions on Brookings/BBC/Al Jazeera/Radio Okapi were all
    measured live). It stays pure-Python with no headless browser, so it fits
    this module's dependency-light, thread-safe fetch model. The regex pass
    (_extract_body_regex) remains as a fallback for the rare page trafilatura
    returns nothing for, so a bad extraction never leaves us worse off than
    before. Title still comes from the raw <title> tag — downstream slug-title
    upgrades (_save_day_batch) compare against it and its behavior is stable.
    """
    page_html = page_html[:_HTML_MAX_CHARS]
    title_m = _TITLE_RE.search(page_html)
    title = _html.unescape(re.sub(r'\s+', ' ', title_m.group(1))).strip() if title_m else None

    text = _extract_body_trafilatura(page_html) or _extract_body_regex(page_html)
    # A subscription wall ("Subscribe to unlock this article …") is not a body:
    # drop it so callers fall back to Wayback (historical) or leave the existing
    # paywall-free RSS summary in place (rehydrate) instead of storing CTA chrome.
    if _is_paywall_body(text):
        text = None
    return title or None, (text or '')[:_BODY_MAX_CHARS] or None


def _extract_body_trafilatura(page_html: str) -> str | None:
    """Main article text via trafilatura, whitespace-normalized — or None if it
    can't find a main body (falls through to the regex extractor)."""
    try:
        import trafilatura
    except ImportError:
        return None
    extracted = trafilatura.extract(
        page_html, include_comments=False, include_tables=False, favor_precision=True,
    )
    if not extracted:
        return None
    return ' '.join(extracted.split()).strip() or None


def _extract_body_regex(page_html: str) -> str | None:
    """Fallback body extractor — a regex pass, not a real parser. Strip
    script/style, strip structural chrome (nav/header/footer/aside) and
    class-named boilerplate containers, narrow to <article>/<main> if present,
    then drop any remaining <p> that still looks like chrome by its own shape
    (link-dense, or too short to be a sentence). See _is_boilerplate_paragraph.
    """
    # Cap before regex work: the backreference patterns degrade O(N) per
    # unclosed tag; 200 KB is ample for any news article's paragraph text.
    html = _SCRIPT_STYLE_RE.sub(' ', page_html[:_REGEX_HTML_MAX_CHARS])
    html = _CHROME_TAG_RE.sub(' ', html)
    html = _BOILERPLATE_CLASS_RE.sub(' ', html)
    main_m = _ARTICLE_MAIN_RE.search(html)
    if main_m:
        html = main_m.group(1)

    paragraphs = []
    for p_html in _PARAGRAPH_RE.findall(html):
        plain = _html.unescape(re.sub(r'\s+', ' ', _TAG_RE.sub(' ', p_html))).strip()
        if not _is_boilerplate_paragraph(p_html, plain):
            paragraphs.append(plain)
    return ' '.join(paragraphs).strip() or None


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
    from services.data.proxy import EGRESS_PROXIES

    if source_code and _is_source_blocked(source_code):
        return None, None
    try:
        # Rotate egress IPs on a block (403/429/503) so a rate-limited source can
        # retry from a fresh IP; direct-first, so no proxy cost when nothing
        # blocks. Empty pool → a single direct fetch (unchanged behaviour).
        resp = EGRESS_PROXIES.get(url, headers=HTTP_HEADERS, timeout=timeout)
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
