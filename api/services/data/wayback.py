"""
Wayback Machine front-page mining — historical backfill discovery strategy.

For publishers whose sitemaps are recency-only (verified 2026-07: BBC,
Guardian, NPR, Economist, ProPublica, Foreign Policy, WHO — see
services/data/historical.py), the Internet Archive holds essentially daily
captures of their front pages going back 5+ years. A day's front page IS the
publisher's own editorial importance ranking — exactly the signal sitemap
discovery lacks — so per (source, day): one CDX lookup for that day's
snapshots, one fetch of the capture nearest noon (``id_`` variant = original
HTML, no archive toolbar), then extract same-domain headline links with the
source's article-URL pattern. Page order is preserved into
extra_data['frontpage_rank'] and survives the orchestrator's top_n cap (its
sort is stable and all of a snapshot's datums share published_on), so the cap
keeps the top-of-page stories.

Supported providers are hardcoded in FRONTPAGES (front-page URL + article-path
regex per source code). News-site URL schemes are long-lived; when one does
change, the preflight probe / empty-streak breaker in the orchestrator
surfaces it rather than silently backfilling nothing.

Wayback is free but throttles aggressively under load (measured 2026-07: ~38%
transient failures / p90 40s on rapid sequential calls; 'no results' is
HTTP 200 + empty JSON, throttling is 503/timeouts — so a 200 is authoritative
and everything else retries). All Wayback HTTP goes through _wayback_get():
a module-wide ~2.5s minimum spacing between requests, exponential backoff on
non-200s, an optional egress proxy (settings.WAYBACK_PROXY_URL) if the shared
IP gets rate-limited anyway, and the same per-source timeout blocklist the
sitemap strategy uses.
"""
import datetime
import logging
import random
import re
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import requests

from services.data.base import ArticleDatum
# No _block_source here on purpose: a Wayback failure is Wayback's fault, not
# the news source's — pacing/backoff handles it; the source stays unblocked.
from services.data.historical import _HTTP_HEADERS, _is_source_blocked

if TYPE_CHECKING:
    import core.models

logger = logging.getLogger(__name__)

CDX_URL = 'http://web.archive.org/cdx/search/cdx'
WAYBACK_AVAILABILITY_URL = 'https://archive.org/wayback/available'

_HTTP_TIMEOUT = 30
# Minimum seconds between any two Wayback requests from this process (plus
# jitter) — heavy workers each pace themselves; aggregate stays polite.
_MIN_REQUEST_INTERVAL = 2.5
_BACKOFF_BASE_SECONDS = 4.0
_MAX_RETRIES = 3
# Headline-like anchors only — shorter anchor text is nav/section chrome.
_MIN_ANCHOR_CHARS = 25
# An archive URL prefix on a link inside an id_ capture (rare, but pages that
# were archived with absolute archive links keep it) — strip before use.
_ARCHIVE_PREFIX_RE = re.compile(r'^(?:https?://web\.archive\.org)?/web/\d+(?:id_)?/')

# ── Supported providers ──────────────────────────────────────────────────────
# source code → front page to mine + what an article path looks like there.
FRONTPAGES: dict[str, dict] = {
    # AP has a deep sitemap archive too, but it's dominated by UUID-slugged
    # sports/wire noise with no titles (verified in the 2022 e2e smoke) —
    # front-page mining gives real ranked headlines instead.
    'ap-top':             {'url': 'https://apnews.com',
                           'article_re': re.compile(r'/article/')},
    'bbc-world':          {'url': 'https://www.bbc.com/news',
                           'article_re': re.compile(r'/news/[a-z][a-z0-9-]*-\d{8,}')},
    'bbc-middle-east':    {'url': 'https://www.bbc.com/news/world/middle_east',
                           'article_re': re.compile(r'/news/[a-z][a-z0-9-]*-\d{8,}')},
    'guardian-world':     {'url': 'https://www.theguardian.com/world',
                           'article_re': re.compile(r'/20\d{2}/[a-z]{3}/\d{1,2}/')},
    'guardian-crime':     {'url': 'https://www.theguardian.com/uk/crime',
                           'article_re': re.compile(r'/20\d{2}/[a-z]{3}/\d{1,2}/')},
    'guardian-economics': {'url': 'https://www.theguardian.com/business/economics',
                           'article_re': re.compile(r'/20\d{2}/[a-z]{3}/\d{1,2}/')},
    'npr-world':          {'url': 'https://www.npr.org/sections/world/',
                           'article_re': re.compile(r'/20\d{2}/\d{2}/\d{2}/')},
    'economist-finance':  {'url': 'https://www.economist.com/finance-and-economics',
                           'article_re': re.compile(r'/20\d{2}/\d{2}/\d{2}/')},
    'propublica':         {'url': 'https://www.propublica.org',
                           'article_re': re.compile(r'/article/')},
    'foreign-policy':     {'url': 'https://foreignpolicy.com/',
                           'article_re': re.compile(r'/20\d{2}/\d{2}/\d{2}/')},
    'who-news':           {'url': 'https://www.who.int/news',
                           'article_re': re.compile(r'/news/item/')},
}


def supports_wayback(source_code: str) -> bool:
    return source_code in FRONTPAGES


# ── Polite shared client ─────────────────────────────────────────────────────

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _proxy_attempt_order() -> list:
    """Egress hops to cycle through across a request's retries: direct first,
    then the WAYBACK_PROXY_POOL (legacy WAYBACK_PROXY_URL folded in). Empty pool
    → [None], i.e. always-direct (unchanged behaviour)."""
    from services.data.proxy import WAYBACK_PROXIES
    return WAYBACK_PROXIES.attempt_order()


def _throttle() -> None:
    global _last_request_at
    with _throttle_lock:
        wait = _last_request_at + _MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.5))
        _last_request_at = time.monotonic()


def _wayback_get(
    url: str,
    params: dict | None = None,
    deadline: datetime.datetime | None = None,
    retries: int = _MAX_RETRIES,
    timeout: int = _HTTP_TIMEOUT,
) -> requests.Response | None:
    """GET against Wayback with pacing + backoff. An HTTP 200 is authoritative
    (empty CDX results come back 200 + '[]'); non-200s and connection errors
    are treated as throttling and retried. Returns None on exhaustion or when
    the deadline would be crossed."""
    from services.data.proxy import as_proxies
    order = _proxy_attempt_order()
    for attempt in range(retries + 1):
        if deadline is not None and datetime.datetime.now(datetime.timezone.utc) >= deadline:
            return None
        _throttle()
        try:
            # Rotate egress IP per retry (direct first) — a per-IP throttle/block
            # on one hop retries from the next.
            resp = requests.get(
                url, params=params, headers=_HTTP_HEADERS,
                timeout=timeout, proxies=as_proxies(order[attempt % len(order)]),
            )
            if resp.status_code == 200:
                return resp
            logger.debug('wayback HTTP %s url=%r (attempt %d)', resp.status_code, url, attempt + 1)
        except requests.RequestException as exc:
            logger.debug('wayback request failed url=%r: %s (attempt %d)', url, exc, attempt + 1)
        if attempt < retries:
            time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
    return None


def cdx_snapshots(
    page_url: str,
    day_start: datetime.datetime,
    day_end: datetime.datetime,
    deadline: datetime.datetime | None = None,
    limit: int = 50,
) -> list[str]:
    """Timestamps ('YYYYMMDDhhmmss') of OK captures of *page_url* in the window.
    Empty list is authoritative only if the CDX request itself succeeded —
    None-from-_wayback_get (throttled out) also returns [], and the caller's
    outcome bookkeeping treats a blocked/errored day as retryable anyway."""
    resp = _wayback_get(CDX_URL, params={
        'url': page_url,
        'from': day_start.strftime('%Y%m%d'),
        'to': (day_end - datetime.timedelta(seconds=1)).strftime('%Y%m%d'),
        'output': 'json', 'fl': 'timestamp', 'filter': 'statuscode:200',
        'limit': limit,
    }, deadline=deadline)
    if resp is None:
        return []
    try:
        rows = resp.json()
    except ValueError:
        return []
    return [r[0] for r in rows[1:]] if rows else []


_SNAPSHOT_TS_RE = re.compile(r'/web/(\d{14})(?:id_)?/')


def fetch_nearest_capture(
    page_url: str,
    around: datetime.datetime,
    deadline: datetime.datetime | None = None,
) -> tuple[str | None, str | None]:
    """(actual capture timestamp, page HTML) via the direct redirect form —
    ``/web/{ts}id_/{url}`` redirects to the capture nearest *ts* with no CDX
    involved. This is the only listing-free lookup that works for domains the
    public CDX API refuses ('this type of CDX query requires authorization' —
    e.g. theguardian.com, verified 2026-07). The caller must check the
    returned timestamp is acceptably close: Wayback happily redirects to a
    capture months away when nothing nearer exists."""
    requested = around.strftime('%Y%m%d%H%M%S')
    resp = _wayback_get(
        f'https://web.archive.org/web/{requested}id_/{page_url}', deadline=deadline, retries=1,
    )
    if resp is None:
        return None, None
    m = _SNAPSHOT_TS_RE.search(resp.url)
    return (m.group(1) if m else requested), resp.text


# ── Strategy ─────────────────────────────────────────────────────────────────

class WaybackHistoricalService:
    """Same fetch_day() interface as RSSHistoricalService/
    WikipediaHistoricalService so HistoricalBackfillService can drive any of
    them interchangeably (see _build_strategy there)."""

    def __init__(
        self, source: 'core.models.Source', max_candidates: int | None = None,
        first_match_only: bool = False,
    ) -> None:
        if source.code not in FRONTPAGES:
            from services.data.historical import HistoricalServiceError
            raise HistoricalServiceError(
                f'Source {source.code!r} has no Wayback front-page config.'
            )
        self._source = source
        self._config = FRONTPAGES[source.code]
        self._max_candidates = max_candidates
        self._first_match_only = first_match_only

    def fetch_day(
        self,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
        deadline: datetime.datetime | None = None,
    ) -> list[ArticleDatum]:
        if _is_source_blocked(self._source.code):
            logger.info(
                'WaybackHistorical source=%r day=%s: skipped (temporarily blocked)',
                self._source.code, day_start.date(),
            )
            return []

        page_html: str | None = None
        timestamps = cdx_snapshots(self._config['url'], day_start, day_end, deadline=deadline)
        if timestamps:
            ts = _nearest_noon(timestamps)
            resp = _wayback_get(
                f'https://web.archive.org/web/{ts}id_/{self._config["url"]}',
                deadline=deadline, retries=1,
            )
            page_html = resp.text if resp is not None else None
        else:
            # CDX empty — either genuinely no capture, or the domain is
            # CDX-restricted (e.g. theguardian.com): the direct redirect form
            # still works, but only counts if it lands inside the day window.
            ts, page_html = fetch_nearest_capture(
                self._config['url'], day_start + datetime.timedelta(hours=12), deadline=deadline,
            )
            if ts is not None and not (day_start.strftime('%Y%m%d') <= ts[:8] < day_end.strftime('%Y%m%d')):
                logger.info(
                    'WaybackHistorical source=%r day=%s: nearest capture is %s — outside the day',
                    self._source.code, day_start.date(), ts[:8],
                )
                page_html = None

        if not page_html:
            logger.info(
                'WaybackHistorical source=%r day=%s: no usable front-page capture',
                self._source.code, day_start.date(),
            )
            return []

        links = extract_frontpage_links(self._config['url'], page_html, self._config['article_re'])
        cap = 1 if self._first_match_only else self._max_candidates
        if cap and len(links) > cap:
            links = links[:cap]
        datums = [
            self._link_to_datum(url, anchor, ts, rank)
            for rank, (anchor, url) in enumerate(links)
        ]
        logger.info(
            'WaybackHistorical source=%r day=%s: %d headline link(s) from capture %s',
            self._source.code, day_start.date(), len(datums), ts,
        )
        return datums

    def _link_to_datum(self, url: str, anchor: str, ts: str, rank: int) -> ArticleDatum:
        published = datetime.datetime.strptime(ts, '%Y%m%d%H%M%S').replace(
            tzinfo=datetime.timezone.utc,
        )
        return ArticleDatum(
            source_url=url,
            author=self._source.name,
            author_slug=self._source.author_slug or self._source.code,
            title=anchor[:200],
            content=anchor,
            published_on=published,
            extra_data={
                'wayback_snapshot': ts,
                'frontpage_rank': rank,
                # Anchor text is the real headline; no page-<title> upgrade needed.
                'title_from_slug': False,
            },
        )


def probe_wayback_source(source: 'core.models.Source') -> bool:
    """Preflight: does the source's front page have ANY recent capture? One
    CDX request; falls back to the direct redirect form for CDX-restricted
    domains (see backfill_history_task's preflight loop)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    page_url = FRONTPAGES[source.code]['url']
    if cdx_snapshots(page_url, now - datetime.timedelta(days=90), now, limit=1):
        return True
    ts, page_html = fetch_nearest_capture(page_url, now - datetime.timedelta(days=30))
    if not page_html or ts is None:
        return False
    return ts[:8] >= (now - datetime.timedelta(days=120)).strftime('%Y%m%d')


# ── Front-page link extraction ───────────────────────────────────────────────

def _nearest_noon(timestamps: list[str]) -> str:
    """Capture closest to 12:00 — mid-day pages carry the day's settled
    editorial ranking (midnight captures still show yesterday's layout)."""
    def distance(ts: str) -> int:
        return abs(int(ts[8:10]) * 60 + int(ts[10:12]) - 720)
    return min(timestamps, key=distance)


def extract_frontpage_links(
    page_url: str, page_html: str, article_re: re.Pattern,
) -> list[tuple[str, str]]:
    """(anchor text, absolute URL) pairs for same-domain article links, in page
    order (rank), deduped by URL. Anchors shorter than _MIN_ANCHOR_CHARS are
    dropped (nav/section chrome, not headlines)."""
    from scrapling.parser import Selector

    host = urlparse(page_url).netloc.removeprefix('www.')
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for a in Selector(page_html).css('a'):
        href = (a.attrib.get('href') or '').split('#')[0]
        if not href:
            continue
        href = _ARCHIVE_PREFIX_RE.sub('', href)
        full = urljoin(page_url, href)
        parsed = urlparse(full)
        if parsed.scheme not in ('http', 'https'):
            continue
        if parsed.netloc.removeprefix('www.') != host:
            continue
        if not article_re.search(parsed.path):
            continue
        anchor = re.sub(r'\s+', ' ', a.get_all_text(strip=True)).strip()
        if len(anchor) < _MIN_ANCHOR_CHARS:
            continue
        if full in seen:
            continue
        seen.add(full)
        links.append((anchor, full))
    return links
