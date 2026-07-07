"""
Historical backfill — fetch top-N articles for a single day window across a
(usually small) set of sources.

Strategy (selected per source in HistoricalBackfillService._build_strategy):

  WikipediaHistoricalService (services/data/wikipedia.py) — the primary path
    The synthetic 'wikipedia-current-events' Source. Discovers each day's
    curated events (with citations to news articles) from the Current Events
    portal's monthly pages — human-selected importance, ~15-25 events/day,
    one cheap API request per month. See that module's docstring.

  WaybackHistoricalService (services/data/wayback.py) — per-publisher supplement
    Publishers with recency-only sitemaps but a stable front-page URL
    (BBC, Guardian, NPR, ... — see FRONTPAGES there): mine the day's
    archived front page from the Wayback Machine; page position doubles
    as the publisher's own importance ranking.

  RSSHistoricalService — per-publisher supplement
    Discovers historical article URLs via the source domain's sitemap
    (robots.txt → /sitemap.xml → /sitemap_index.xml → /news-sitemap.xml,
    merged across whichever candidates return entries). Handles nested
    sitemap indexes (one level). Titles come from <news:title> when
    available, otherwise inferred from the URL slug (and upgraded to the
    article page's own <title> at save time — see _save_day_batch). Only
    a few majors keep multi-year sitemap archives (verified 2026-07: AP,
    FT, Al Jazeera; most others are recency-only) — hence the Wikipedia +
    Wayback paths above.

Candidates are capped per source by recency (no LLM scoring at discovery
time). HistoricalBackfillService.fetch_and_save_day() fetches every requested
source for ONE day window, de-dupes across those sources using the same
title-similarity filter the live fetch path uses, fetches each new article's
body inline, and saves via Article.objects.get_or_create — fully idempotent.
Saved articles are indistinguishable from live ones except for
extra_data['backfill_day'].

Multi-day iteration and NLP processing are NOT this module's job — they live
in services.tasks.backfill_history_task (dispatcher: enumerates day windows ×
source chunks) and backfill_day_chunk_task (worker: calls fetch_and_save_day
then services.workflow.articles.process_articles on the new ids), so that
each Celery task stays bounded to one day × a handful of sources instead of a
whole multi-year range.

Two trade-offs from that chunking, accepted deliberately rather than solved:
  - Cross-source title-dedup only sees the sources in one chunk (a source's
    caller decides chunk membership), not every source for that day — a
    near-duplicate story from two sources in *different* chunks on the same
    day won't be caught. URL-level idempotency (get_or_create) still prevents
    literal duplicate saves.
  - Per-article body-fetch parallelism (previously one Celery job per
    article, fanned out across worker-light) is now inline within each day
    window's fetch — parallelism instead comes from many day-window tasks
    running concurrently across the heavy queue.

Source timeout handling: a source that times out (robots.txt, sitemap, or
article-body fetch) is blocklisted for _SOURCE_BLOCK_TTL_SECONDS via the
shared services.cache.Blocklist (same mechanism services.llm's 429 debounce
uses) — see _is_source_blocked/_block_source below. Blocked sources are
skipped with zero further HTTP calls until the block expires, including
mid-recursion through a sitemap index, so one dead source can't burn the
whole task's time budget one candidate URL at a time.
"""
import datetime
import html as _html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator, TYPE_CHECKING
from urllib.parse import urlparse

import requests

from services.cache import Blocklist, Counter, key_backfill_empty_streak, key_backfill_source_block
from services.data.base import ArticleDatum, ClientServiceException

if TYPE_CHECKING:
    import core.models

logger = logging.getLogger(__name__)

# ── Source timeout blocklist ──────────────────────────────────────────────────
_SOURCE_BLOCK_TTL_SECONDS = 1800  # 30 min "temp blocked" cooldown after a timeout
_source_blocklist = Blocklist()


def _is_source_blocked(source_code: str) -> bool:
    return _source_blocklist.is_blocked(key_backfill_source_block(source_code))


def _block_source(source_code: str, reason: str, ttl: int = _SOURCE_BLOCK_TTL_SECONDS) -> None:
    logger.warning(
        'Backfill source %r blocked (%s) — blocking for %ds',
        source_code, reason, ttl,
    )
    _source_blocklist.block(key_backfill_source_block(source_code), ttl)


# ── Consecutive-empty-day circuit breaker ─────────────────────────────────────
# A source whose sitemap discovery *succeeds* but keeps returning zero entries
# for the requested window (wrong domain, dead sitemap, mismatched date range —
# see RSSHistoricalService's feeds-subdomain docstring) never trips the timeout
# blocklist above, since nothing times out. Bulk backfills dispatch every
# (day, source) pair up front (see services.tasks.backfill_history_task), so
# there's no natural place to "cancel" already-queued chunk tasks — instead,
# each chunk task reports its own source-level empty/non-empty result here, and
# once a source racks up _EMPTY_STREAK_THRESHOLD empty days in a row we block it
# the same way a timeout would, so every subsequent already-queued chunk task
# for that source skips with zero HTTP calls (fetch_day already checks
# _is_source_blocked first) instead of repeating an identical, doomed crawl.
#
# Only genuine 'empty' outcomes count (discovery ran, no error/timeout/block —
# see fetch_and_save_day's outcome logic); the threshold is sized so a
# weekday-only or low-volume publisher's legitimate quiet stretch (weekend +
# holidays) doesn't trip it — day-chunks also execute out of order across
# concurrent heavy workers, so "consecutive" is approximate.
_EMPTY_STREAK_THRESHOLD = 8
# Long enough to span a bulk backfill's full dispatch window (all chunks for a
# multi-month range are enqueued immediately, so this can't just be the 30min
# timeout TTL — it needs to outlive the whole run).
_EMPTY_STREAK_TTL_SECONDS = 6 * 3600
_empty_streak_counter = Counter()


def _note_empty_day(source_code: str) -> int:
    """Increment and return the consecutive-empty-day counter for a source."""
    return _empty_streak_counter.incr(key_backfill_empty_streak(source_code), ttl=_EMPTY_STREAK_TTL_SECONDS)


def _note_nonempty_day(source_code: str) -> None:
    _empty_streak_counter.reset(key_backfill_empty_streak(source_code))


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

@dataclass
class DayResult:
    day: datetime.datetime
    fetched: int                      # total candidates collected across the given sources
    saved_ids: list = field(default_factory=list)  # newly-created Article ids (for process_articles)
    # Per-source outcome for this day window:
    #   'fetched'  — discovery ran and returned candidates
    #   'empty'    — discovery ran cleanly but matched nothing (counts toward the
    #                empty-streak circuit breaker; still checkpointable — a quiet
    #                news day is a real, final result)
    #   'blocked'  — skipped (or emptied) by the timeout/empty-streak blocklist
    #   'error'    — discovery raised; result unknown
    #   'deadline' — never attempted (wall-clock budget ran out first)
    # Only 'fetched'/'empty' should be checkpointed as done — the rest must stay
    # eligible for a --resume rerun (see services.tasks.backfill_day_chunk_task).
    outcomes: dict = field(default_factory=dict)

    @property
    def saved(self) -> int:
        return len(self.saved_ids)


class HistoricalServiceError(ClientServiceException):
    code = 'historical_error'


# ---------------------------------------------------------------------------
# Day helpers
# ---------------------------------------------------------------------------

def iter_days(
    start: datetime.datetime,
    end: datetime.datetime,
) -> Iterator[tuple[datetime.datetime, datetime.datetime]]:
    """Yield (day_start, day_end) pairs covering [start, end), one calendar day each."""
    current = datetime.datetime(start.year, start.month, start.day, tzinfo=datetime.timezone.utc)
    while current < end:
        yield current, current + datetime.timedelta(days=1)
        current += datetime.timedelta(days=1)


# ---------------------------------------------------------------------------
# RSS historical strategy
# ---------------------------------------------------------------------------

_SITEMAP_NS = 'http://www.sitemaps.org/schemas/sitemap/0.9'
_NEWS_NS = 'http://www.google.com/schemas/sitemap-news/0.9'
_HTTP_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; HistoricalBackfiller/1.0)'}
_HTTP_TIMEOUT = 15


def _strip_feed_subdomain(netloc: str) -> str:
    """Feed content is often served from a 'feeds.' subdomain that has no
    sitemap of its own — verified: feeds.apnews.com/sitemap.xml 404s while
    apnews.com/sitemap.xml exists. Strip only that one specific prefix; other
    subdomains are left alone since stripping them in general is unsafe (a
    subdomain can host a genuinely distinct site with its own sitemap).
    """
    if netloc.startswith('feeds.') and netloc.count('.') >= 2:
        return netloc[len('feeds.'):]
    return netloc


class RSSHistoricalService:
    """
    Discovers historical article URLs via the source domain's XML sitemap.

    Sitemap discovery order (results from every candidate that returns
    entries are merged, deduped by URL — a source with both a full-history
    sitemap index and a recency-only Google News sitemap will use both):
      1. Source.sitemap_url, if the operator has set an explicit override —
         either because the real sitemap lives at a non-default path
         (dawn-pk: /feeds/sitemap, scmp-world: /sitemap/archives-0.xml,
         africa-news: /sitemaps/en/sitemap.xml, allafrica:
         /misc/sitemap/aans-urls-en.xml) or to lock in a standard-path
         sitemap that's already confirmed working (aljazeera-world,
         arab-news, brookings, techcrunch all at /sitemap.xml)
      2. Sitemap: directives in robots.txt
      3. /sitemap.xml
      4. /sitemap_index.xml
      5. /news-sitemap.xml

    Nested sitemap indexes are followed one level deep. Sub-sitemaps are
    fetched closest-lastmod-to-the-window first, capped at
    ``_MAX_SUBSITEMAPS_PER_INDEX`` — see ``_parse_sitemap_index`` for why
    (some publishers date-partition their index into thousands of entries).

    NOTE: ``self._base_url`` is derived from ``source.url``'s scheme+netloc.
    A ``feeds.`` subdomain is stripped (verified: feeds.apnews.com/sitemap.xml
    404s while apnews.com/sitemap.xml serves 228 dated sub-sitemaps) — that's
    the only subdomain form fixed here. Other non-standard feed hosts (e.g. a
    source whose feed lives on a bespoke CDN name with no simple relationship
    to the main site) remain a known limitation.
    """

    # Drop sub-sitemaps whose lastmod is more than this many days before the window.
    _LASTMOD_PRUNE_DAYS = 7
    # Hard cap on sub-sitemaps recursed into per index, after proximity sorting —
    # bounds worst-case request volume against date-partitioned indexes with
    # thousands of entries (verified live: Al Jazeera's sitemap index has one
    # <sitemap> per calendar day, 700+ visible going back to at least 2024).
    _MAX_SUBSITEMAPS_PER_INDEX = 40

    def __init__(
        self, source: 'core.models.Source', max_candidates: int | None = None,
        first_match_only: bool = False,
    ) -> None:
        self._source = source
        # Cap on entries kept per day. Sorted by recency (a weak relevance proxy)
        # before this cap is applied, since there's no LLM score to rank by.
        self._max_candidates = max_candidates
        # Existence probe mode (see probe_source_has_sitemap_entries): stop all
        # discovery the moment ANY entry is found, instead of exhaustively
        # crawling every candidate sitemap + sub-sitemap and capping afterward.
        self._first_match_only = first_match_only
        parsed = urlparse(source.url)
        netloc = _strip_feed_subdomain(parsed.netloc)
        self._base_url = f'{parsed.scheme}://{netloc}'
        self._deadline: datetime.datetime | None = None  # set per fetch_day() call

    def fetch_day(
        self,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
        deadline: datetime.datetime | None = None,
    ) -> list[ArticleDatum]:
        """deadline: wall-clock cutoff (see HistoricalBackfillService.fetch_and_save_day)
        — checked between sitemap candidates and sub-sitemap fetches so a slow-but-not-
        technically-timing-out source can't alone consume the caller's whole time budget."""
        if _is_source_blocked(self._source.code):
            logger.info(
                'RSSHistorical source=%r day=%s: skipped (temporarily blocked)',
                self._source.code, day_start.date(),
            )
            return []

        self._deadline = deadline
        entries = self._discover_entries(day_start, day_end)
        if not entries:
            logger.info(
                'RSSHistorical source=%r day=%s: no sitemap entries found',
                self._source.code, day_start.date(),
            )
            return []

        if self._max_candidates and len(entries) > self._max_candidates:
            entries.sort(
                key=lambda e: e['date'] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
                reverse=True,
            )
            entries = entries[: self._max_candidates]

        datums = [self._entry_to_datum(e) for e in entries]
        logger.info(
            'RSSHistorical source=%r day=%s: %d entries discovered',
            self._source.code, day_start.date(), len(datums),
        )
        return datums

    # ------------------------------------------------------------------
    # Sitemap discovery
    # ------------------------------------------------------------------

    def _discover_entries(
        self,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
    ) -> list[dict]:
        """Return {url, title, date} dicts whose date falls in the window, merged
        (deduped by URL) across every sitemap candidate that yields entries."""
        seen_urls: set[str] = set()
        merged: list[dict] = []
        for sm_url in self._candidate_sitemap_urls():
            if self._deadline_passed():
                break
            for entry in self._parse_sitemap(sm_url, day_start, day_end):
                if entry['url'] not in seen_urls:
                    seen_urls.add(entry['url'])
                    merged.append(entry)
            if self._first_match_only and merged:
                break
        return merged

    def _candidate_sitemap_urls(self) -> list[str]:
        """Return sitemap URL candidates: explicit Source.sitemap_url override
        first (if set), then robots.txt directives, then standard paths."""
        candidates: list[str] = []

        if self._source.sitemap_url:
            candidates.append(self._source.sitemap_url)

        try:
            resp = requests.get(
                f'{self._base_url}/robots.txt',
                headers=_HTTP_HEADERS,
                timeout=_HTTP_TIMEOUT,
            )
            if resp.ok:
                for line in resp.text.splitlines():
                    if line.lower().startswith('sitemap:'):
                        url = line.split(':', 1)[1].strip()
                        if url:
                            candidates.append(url)
        except requests.Timeout:
            _block_source(self._source.code, 'robots.txt timeout')
        except requests.RequestException:
            pass

        for path in ('/sitemap.xml', '/sitemap_index.xml', '/news-sitemap.xml'):
            candidates.append(f'{self._base_url}{path}')

        # Deduplicate, preserve order
        seen: set[str] = set()
        return [u for u in candidates if not (u in seen or seen.add(u))]  # type: ignore[func-returns-value]

    def _parse_sitemap(
        self,
        url: str,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
    ) -> list[dict]:
        """Parse a sitemap URL; recurses into sitemap indexes (one level)."""
        if _is_source_blocked(self._source.code):
            return []
        try:
            resp = requests.get(url, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
        except requests.Timeout:
            _block_source(self._source.code, f'sitemap timeout: {url}')
            return []
        except requests.RequestException as exc:
            logger.debug('Sitemap fetch failed url=%r: %s', url, exc)
            return []

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            logger.debug('Sitemap parse error url=%r: %s', url, exc)
            return []

        local_tag = root.tag.split('}')[-1] if '}' in root.tag else root.tag

        if local_tag == 'sitemapindex':
            return self._parse_sitemap_index(root, day_start, day_end)

        return self._extract_urlset_entries(root, day_start, day_end)

    def _parse_sitemap_index(
        self,
        root: ET.Element,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
    ) -> list[dict]:
        """Recurse into sub-sitemaps, closest-to-the-window first, capped at
        ``_MAX_SUBSITEMAPS_PER_INDEX``.

        Some publishers use date-partitioned indexes where each sub-sitemap
        covers exactly one day/month and ``lastmod`` IS that date (e.g. Al
        Jazeera: one <sitemap> per calendar day going back to 2003 — 8000+
        entries). Others use ``lastmod`` as a "when this listing was last
        touched" signal on a much smaller, topic-based set of sub-sitemaps
        (e.g. Brookings' ~90 category/type sitemaps). Rather than guess which
        kind a source is, sort by proximity of ``lastmod`` to the requested
        window and cap how many get fetched — cheap for the small-index case,
        and for the huge date-partitioned case it finds the 1-2 genuinely
        relevant sub-sitemaps first instead of crawling all of them.
        """
        candidates: list[tuple[datetime.datetime | None, str]] = []
        for sm_el in root.findall(f'{{{_SITEMAP_NS}}}sitemap'):
            loc_el = sm_el.find(f'{{{_SITEMAP_NS}}}loc')
            if loc_el is None or not loc_el.text:
                continue
            sm_date = None
            lastmod_el = sm_el.find(f'{{{_SITEMAP_NS}}}lastmod')
            if lastmod_el is not None and lastmod_el.text:
                sm_date = _parse_sitemap_date(lastmod_el.text.strip())
                # Clearly-stale sub-sitemaps (lastmod well before our window) are
                # dropped outright regardless of the proximity cap below.
                if sm_date and sm_date < day_start - datetime.timedelta(days=self._LASTMOD_PRUNE_DAYS):
                    continue
            candidates.append((sm_date, loc_el.text.strip()))

        def proximity(item: tuple[datetime.datetime | None, str]) -> datetime.timedelta:
            sm_date, _ = item
            if sm_date is None:
                return datetime.timedelta.max  # undated — try last
            if sm_date < day_start:
                return day_start - sm_date
            if sm_date >= day_end:
                return sm_date - day_end
            return datetime.timedelta(0)  # lastmod falls inside the window

        candidates.sort(key=proximity)

        entries: list[dict] = []
        for _, sub_url in candidates[: self._MAX_SUBSITEMAPS_PER_INDEX]:
            if self._deadline_passed():
                logger.info(
                    'RSSHistorical source=%r: deadline reached mid sub-sitemap recursion — stopping',
                    self._source.code,
                )
                break
            entries.extend(self._parse_sitemap(sub_url, day_start, day_end))
            if self._first_match_only and entries:
                break
        return entries

    def _deadline_passed(self) -> bool:
        return self._deadline is not None and datetime.datetime.now(datetime.timezone.utc) >= self._deadline

    def _extract_urlset_entries(
        self,
        root: ET.Element,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
    ) -> list[dict]:
        entries: list[dict] = []
        for url_el in root.findall(f'{{{_SITEMAP_NS}}}url'):
            entry = _extract_sitemap_entry(url_el)
            if entry is None or entry['date'] is None:
                continue
            entry_date: datetime.datetime = entry['date']
            if entry_date.tzinfo is None:
                entry_date = entry_date.replace(tzinfo=datetime.timezone.utc)
            if day_start <= entry_date < day_end:
                entries.append(entry)
        return entries

    # ------------------------------------------------------------------
    # Entry → ArticleDatum
    # ------------------------------------------------------------------

    def _entry_to_datum(self, entry: dict) -> ArticleDatum:
        title = entry['title'] or _slug_from_url(entry['url'])
        return ArticleDatum(
            source_url=entry['url'],
            author=self._source.name,
            author_slug=self._source.author_slug or self._source.code,
            title=title[:200],
            content=title,
            published_on=entry['date'],
            extra_data={
                'sitemap_title': entry['title'],
                # No <news:title> — slug-derived titles can be garbage (FT
                # uses UUID slugs). _save_day_batch upgrades these to the
                # article page's own <title> when the body fetch gets one.
                'title_from_slug': entry['title'] is None,
            },
        )


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

# Wide enough that a source's genuine sitemap (whatever its update cadence) is
# very likely to have *something* in it if the discovery path is even hitting
# the right domain — deliberately not the actual backfill window, so the probe
# (first_match_only) usually stops after the first sitemap page it opens.
_PREFLIGHT_LOOKBACK_DAYS = 90
# Wall-clock budget per probe — keeps the dispatcher's serial per-source
# preflight loop bounded even against a slow-but-not-timing-out host.
_PREFLIGHT_DEADLINE_SECONDS = 45


def probe_source_has_sitemap_entries(source: 'core.models.Source') -> bool:
    """One-shot sanity check: does this source's sitemap discovery return ANY
    entries in a wide recent window?

    Meant to run once per source before a bulk multi-day backfill dispatches
    N day-chunk tasks for it (see services.tasks.backfill_history_task) — a
    misconfigured source (wrong domain from feed-subdomain stripping, dead
    sitemap, etc.) will return empty for every single day of a multi-month
    range; this catches that in one request instead of after dispatching and
    burning the whole range on it. ``first_match_only`` + the deadline keep it
    to a handful of requests, not a full 40-sub-sitemap crawl.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    window_start = now - datetime.timedelta(days=_PREFLIGHT_LOOKBACK_DAYS)
    deadline = now + datetime.timedelta(seconds=_PREFLIGHT_DEADLINE_SECONDS)
    strategy = RSSHistoricalService(source, max_candidates=1, first_match_only=True)
    try:
        return bool(strategy.fetch_day(window_start, now, deadline=deadline))
    except HistoricalServiceError:
        return False


def probe_source(source: 'core.models.Source') -> bool:
    """Strategy-aware preflight — routes to the right existence probe for the
    source (see backfill_history_task's preflight loop)."""
    from services.data.wayback import probe_wayback_source, supports_wayback
    from services.data.wikipedia import WIKIPEDIA_SOURCE_CODE, probe_wikipedia_source
    if source.code == WIKIPEDIA_SOURCE_CODE:
        return probe_wikipedia_source(source)
    if supports_wayback(source.code):
        return probe_wayback_source(source)
    return probe_source_has_sitemap_entries(source)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class HistoricalBackfillService:
    """
    Fetches + saves historical articles for ONE day window across a set of
    sources (usually a small chunk — see module docstring for why multi-day
    iteration and chunking live in services.tasks, not here).

    Usage:
        service = HistoricalBackfillService(sources, top_n=5)
        result = service.fetch_and_save_day(day_start, day_end, deadline=...)
        print(result.day.date(), result.fetched, result.saved, result.saved_ids)

    Every source in ``sources`` is fetched (capped to top_n candidates by
    recency), candidates are merged and passed through the same title-dedup
    filter the live fetch path uses, article bodies are fetched inline, then
    saved via Article.objects.get_or_create() keyed on (source_code,
    source_type, source_url) — fully idempotent. NLP processing is the
    caller's job — services.tasks.backfill_day_chunk_task scores
    ``result.saved_ids`` (LLM importance), applies the same
    ARTICLE_MIN_IMPORTANCE_TO_PROCESS gate the live pipeline uses, and only
    then runs services.workflow.articles.process_articles — the live
    score → gate → process order.

    Backfill metadata is stored in Article.extra_data under key:
      backfill_day — ISO date string of the day window the article was found in
    """

    def __init__(
        self,
        sources: list,
        top_n: int | None = None,
        delay_seconds: float = 0.5,
        fetch_body: bool = True,
        candidate_factor: int = 4,
    ) -> None:
        self.sources = sources
        self.top_n = top_n
        self.delay_seconds = delay_seconds
        self.fetch_body = fetch_body
        # Discover at most top_n * candidate_factor entries per source per day
        # (0/None = unlimited) before the recency cap to top_n is applied.
        self.candidate_factor = candidate_factor
        self._strategies = {s.code: self._build_strategy(s) for s in sources}

    def _resolve_top_n(self, source: 'core.models.Source') -> int:
        if self.top_n is not None:
            return self.top_n
        from services.data.wikipedia import WIKI_DEFAULT_TOP_N, WIKIPEDIA_SOURCE_CODE
        if source.code == WIKIPEDIA_SOURCE_CODE:
            # Already-curated events — the weight-derived 2-6 cap would throw
            # away most of the day's curation (see WIKI_DEFAULT_TOP_N).
            return WIKI_DEFAULT_TOP_N
        from services.tasks import _weighted_top_n
        return _weighted_top_n(source.weight)

    def _build_strategy(self, source: 'core.models.Source'):
        import core.models as m
        from services.data.wayback import WaybackHistoricalService, supports_wayback
        from services.data.wikipedia import WIKIPEDIA_SOURCE_CODE, WikipediaHistoricalService
        if source.code == WIKIPEDIA_SOURCE_CODE:
            return WikipediaHistoricalService(source, max_candidates=self._resolve_top_n(source))
        if supports_wayback(source.code):
            # Recency-only-sitemap publishers: mine the day's archived front
            # page instead (editorial rank ≥ sitemap recency as an importance
            # proxy anyway). No candidate_factor over-fetch — links arrive
            # rank-ordered, so top_n directly keeps the most prominent.
            return WaybackHistoricalService(source, max_candidates=self._resolve_top_n(source))
        if source.type != m.SourceType.RSS:
            raise HistoricalServiceError(
                f'No historical strategy for source type "{source.type}". '
                f'Supported: rss, {WIKIPEDIA_SOURCE_CODE}.'
            )
        top_n = self._resolve_top_n(source)
        max_candidates = top_n * self.candidate_factor if self.candidate_factor else None
        return RSSHistoricalService(source, max_candidates=max_candidates)

    def fetch_and_save_day(
        self,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
        dry_run: bool = False,
        deadline: datetime.datetime | None = None,
    ) -> DayResult:
        """
        Fetch + save this instance's sources for one calendar day.

        dry_run  — discover but do not write to the database.
        deadline — wall-clock cutoff; stops fetching further sources once passed
                   so a chunk task exits cleanly with partial results
                   instead of relying solely on Celery's hard task time limit.
        """
        source_datums: list[tuple['core.models.Source', ArticleDatum]] = []
        fetched_total = 0
        outcomes: dict[str, str] = {}
        for source in self.sources:
            if deadline is not None and datetime.datetime.now(datetime.timezone.utc) >= deadline:
                logger.warning('[backfill] deadline reached — stopping mid source list for day=%s', day_start.date())
                outcomes[source.code] = 'deadline'
                continue
            top_n = self._resolve_top_n(source)
            if top_n == 0:
                outcomes[source.code] = 'suppressed'  # weight=0 — operator-suppressed
                continue
            if _is_source_blocked(source.code):
                outcomes[source.code] = 'blocked'
                continue
            try:
                candidates = self._strategies[source.code].fetch_day(day_start, day_end, deadline=deadline)
            except HistoricalServiceError as exc:
                logger.error('fetch_day failed source=%s day=%s: %s', source.code, day_start.date(), exc)
                outcomes[source.code] = 'error'
                candidates = []
            else:
                if candidates:
                    outcomes[source.code] = 'fetched'
                    _note_nonempty_day(source.code)
                elif _is_source_blocked(source.code):
                    # A timeout mid-fetch blocked it — result unknown, not "empty".
                    outcomes[source.code] = 'blocked'
                else:
                    outcomes[source.code] = 'empty'
                    streak = _note_empty_day(source.code)
                    if streak >= _EMPTY_STREAK_THRESHOLD:
                        _block_source(
                            source.code, f'{streak} consecutive empty backfill days',
                            ttl=_EMPTY_STREAK_TTL_SECONDS,
                        )

            fetched_total += len(candidates)
            candidates.sort(
                key=lambda d: d.get('published_on') or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
                reverse=True,
            )
            for datum in candidates[:top_n]:
                source_datums.append((source, datum))
            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)

        saved_ids = [] if dry_run else self._save_day_batch(source_datums, day_start, deadline=deadline)
        return DayResult(day=day_start, fetched=fetched_total, saved_ids=saved_ids, outcomes=outcomes)

    def _save_day_batch(
        self,
        source_datums: list[tuple['core.models.Source', ArticleDatum]],
        day_start: datetime.datetime,
        deadline: datetime.datetime | None = None,
    ) -> list:
        """Cross-source title-dedup (within this instance's source chunk — see
        module docstring's trade-off note), then fetch each new article's body
        and save it, inline (no further Celery fan-out — see module docstring).
        Returns the list of newly-created Article ids.

        deadline — same wall-clock cutoff as fetch_and_save_day's discovery loop.
        Body-fetch is a per-article blocking HTTP call, so a chunk that used most
        of its budget on discovery could otherwise blow through the remaining
        budget here with no check at all; past the deadline, remaining articles
        are still saved (title-only, no get_or_create is skipped) but without a
        body fetch, so the task still returns cleanly instead of risking Celery's
        hard kill mid-save (which would also skip the checkpoint mark entirely).
        """
        import core.models as m
        from django.conf import settings
        from services.cache import key_backfill_title_dedup
        from services.data import _filter_title_dupes

        if not source_datums:
            return []

        datums = []
        for source, datum in source_datums:
            d = dict(datum)
            d['_source_code'] = source.code
            d['_source_type'] = source.type
            datums.append(d)

        if getattr(settings, 'ARTICLE_DEDUP_TITLE_ENABLED', True):
            # Pool keyed per HISTORICAL day: near-duplicate titles only mean
            # "same story" within the same news day — day-chunks for different
            # years run concurrently and must not dedup against each other.
            datums = _filter_title_dupes(
                datums,
                threshold=getattr(settings, 'ARTICLE_DEDUP_JACCARD_THRESHOLD', 0.75),
                hours=getattr(settings, 'ARTICLE_DEDUP_HOURS', 24),
                cache_key=key_backfill_title_dedup(day_start.date().isoformat()),
            )

        by_source: dict[tuple[str, str], list[dict]] = {}
        for datum in datums:
            key = (datum.pop('_source_code'), datum.pop('_source_type'))
            by_source.setdefault(key, []).append(datum)

        saved_ids = []
        for (source_code, source_type), source_batch in by_source.items():
            urls = [d['source_url'] for d in source_batch]
            existing = set(
                m.Article.objects.filter(
                    source_code=source_code, source_type=source_type, source_url__in=urls,
                ).values_list('source_url', flat=True)
            )
            for datum in source_batch:
                if datum['source_url'] in existing:
                    continue
                fields = dict(datum)
                past_deadline = deadline is not None and datetime.datetime.now(datetime.timezone.utc) >= deadline
                if self.fetch_body and not past_deadline:
                    page_title, body = fetch_article_page(datum['source_url'], source_code=source_code)
                    if (not body or is_junk_page_title(page_title)) and not (
                        deadline is not None and datetime.datetime.now(datetime.timezone.utc) >= deadline
                    ):
                        # Dead, JS-only, or paywalled (junk <title> = the body
                        # is paywall chrome too) — the capture closest to the
                        # backfill day usually still has the real text. The
                        # fallback is 2 paced Wayback requests, so re-check the
                        # deadline first (the pre-loop check predates the live
                        # fetch above).
                        wb_title, wb_body = fetch_wayback_page(datum['source_url'], around=day_start)
                        if wb_body:
                            page_title, body = wb_title, wb_body
                    if body:
                        fields['content'] = body
                    if (page_title and not is_junk_page_title(page_title)
                            and datum.get('extra_data', {}).get('title_from_slug')):
                        # Discovery only had a slug/event-sentence title; the
                        # article page's own <title> is strictly better —
                        # unless it's paywall/interstitial junk.
                        fields['title'] = page_title[:200]
                fields['extra_data'] = {
                    **datum.get('extra_data', {}), 'backfill_day': day_start.date().isoformat(),
                }
                article, created = m.Article.objects.get_or_create(
                    source_code=source_code, source_type=source_type,
                    source_url=datum['source_url'], defaults=fields,
                )
                if created:
                    saved_ids.append(article.id)

        return saved_ids


# ---------------------------------------------------------------------------
# Sitemap parsing helpers
# ---------------------------------------------------------------------------

def _extract_sitemap_entry(url_el: ET.Element) -> dict | None:
    """Return {url, title, date} from a <url> element, or None if no <loc>."""
    loc_el = url_el.find(f'{{{_SITEMAP_NS}}}loc')
    if loc_el is None or not loc_el.text:
        return None
    url = loc_el.text.strip()

    # Date: prefer <news:publication_date>, fall back to <lastmod>
    date: datetime.datetime | None = None
    pub_date_el = url_el.find(f'.//{{{_NEWS_NS}}}publication_date')
    if pub_date_el is not None and pub_date_el.text:
        date = _parse_sitemap_date(pub_date_el.text.strip())
    if date is None:
        lastmod_el = url_el.find(f'{{{_SITEMAP_NS}}}lastmod')
        if lastmod_el is not None and lastmod_el.text:
            date = _parse_sitemap_date(lastmod_el.text.strip())

    # Title: prefer <news:title>
    title: str | None = None
    title_el = url_el.find(f'.//{{{_NEWS_NS}}}title')
    if title_el is not None and title_el.text:
        title = title_el.text.strip()

    return {'url': url, 'title': title, 'date': date}


def _parse_sitemap_date(value: str) -> datetime.datetime | None:
    """Parse ISO 8601 / W3C date strings found in sitemaps."""
    value = value.strip()
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


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

_WAYBACK_AVAILABILITY_URL = 'https://archive.org/wayback/available'


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
    url: str, source_code: str | None = None, timeout: int = _HTTP_TIMEOUT,
) -> tuple[str | None, str | None]:
    """Best-effort (page title, plain-text body) for a historical article URL.

    Backfill candidates arrive title-only (sitemap entries / Wikipedia event
    sentences); without body text the NLP step can't geocode them, so they'd
    never aggregate into Events (and never hit the map). This pulls the page
    and extracts <title> + paragraph text — good enough for geocoding +
    category. Returns (None, None) on any failure (the caller falls back to
    the Wayback Machine, then to the discovery title).

    source_code: when given, participates in the same timeout blocklist as sitemap
    discovery (skipped if already blocked; a Timeout here blocks it too) — a source
    whose article pages are timing out is treated the same as one whose sitemap is.

    Called inline from HistoricalBackfillService._save_day_batch (one day-window's
    worth of articles at a time, not fanned out further — see module docstring).
    """
    if source_code and _is_source_blocked(source_code):
        return None, None
    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.Timeout:
        if source_code:
            _block_source(source_code, f'article body timeout: {url}')
        return None, None
    except requests.RequestException as exc:
        logger.debug('body fetch failed url=%r: %s', url, exc)
        return None, None
    return _extract_title_and_text(resp.text)


def fetch_article_body(url: str, source_code: str | None = None, timeout: int = _HTTP_TIMEOUT) -> str | None:
    """Body-only convenience wrapper around fetch_article_page."""
    return fetch_article_page(url, source_code=source_code, timeout=timeout)[1]


def fetch_wayback_page(
    url: str, around: datetime.datetime | None = None, timeout: int = _HTTP_TIMEOUT,
) -> tuple[str | None, str | None]:
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


def _slug_from_url(url: str) -> str:
    """Extract a readable title hint from the URL path when no title is available."""
    path = urlparse(url).path.rstrip('/')
    slug = path.split('/')[-1] if path else url
    slug = re.sub(r'\.\w+$', '', slug)      # strip extension
    slug = re.sub(r'[-_]', ' ', slug)       # dashes/underscores → spaces
    slug = re.sub(r'\d{6,}', '', slug)      # strip IDs like 12345678
    return slug.strip()[:120].title() or url
