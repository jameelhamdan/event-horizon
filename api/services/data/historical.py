"""
Historical backfill — fetch top-N articles for a single day window across a
(usually small) set of sources.

Strategy (per source type):

  RSSHistoricalService
    Discovers historical article URLs via the source domain's sitemap
    (robots.txt → /sitemap.xml → /sitemap_index.xml → /news-sitemap.xml,
    merged across whichever candidates return entries). Handles nested
    sitemap indexes (one level). Titles come from <news:title> when
    available, otherwise inferred from the URL slug.

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

from services.cache import Blocklist, key_backfill_source_block
from services.data.base import ArticleDatum, ClientServiceException

if TYPE_CHECKING:
    import core.models

logger = logging.getLogger(__name__)

# ── Source timeout blocklist ──────────────────────────────────────────────────
_SOURCE_BLOCK_TTL_SECONDS = 1800  # 30 min "temp blocked" cooldown after a timeout
_source_blocklist = Blocklist()


def _is_source_blocked(source_code: str) -> bool:
    return _source_blocklist.is_blocked(key_backfill_source_block(source_code))


def _block_source(source_code: str, reason: str) -> None:
    logger.warning(
        'Backfill source %r timed out (%s) — blocking for %ds',
        source_code, reason, _SOURCE_BLOCK_TTL_SECONDS,
    )
    _source_blocklist.block(key_backfill_source_block(source_code), _SOURCE_BLOCK_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

@dataclass
class DayResult:
    day: datetime.datetime
    fetched: int                      # total candidates collected across the given sources
    saved_ids: list = field(default_factory=list)  # newly-created Article ids (for process_articles)

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
    ) -> None:
        self._source = source
        # Cap on entries kept per day. Sorted by recency (a weak relevance proxy)
        # before this cap is applied, since there's no LLM score to rank by.
        self._max_candidates = max_candidates
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
            extra_data={'sitemap_title': entry['title']},
        )


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
    caller's job (services.tasks.backfill_day_chunk_task calls
    services.workflow.articles.process_articles on ``result.saved_ids``);
    importance *scoring* is untouched — score_articles_task's normal cron
    still picks these up via created_on.

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
        from services.tasks import _weighted_top_n
        return _weighted_top_n(source.weight)

    def _build_strategy(self, source: 'core.models.Source'):
        import core.models as m
        if source.type != m.SourceType.RSS:
            raise HistoricalServiceError(
                f'No historical strategy for source type "{source.type}". Supported: rss.'
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
                   (mirrors services.workflow.articles.fetch_articles's ``deadline``
                   param) so a chunk task exits cleanly with partial results
                   instead of relying solely on Celery's hard task time limit.
        """
        source_datums: list[tuple['core.models.Source', ArticleDatum]] = []
        fetched_total = 0
        for source in self.sources:
            if deadline is not None and datetime.datetime.now(datetime.timezone.utc) >= deadline:
                logger.warning('[backfill] deadline reached — stopping mid source list for day=%s', day_start.date())
                break
            top_n = self._resolve_top_n(source)
            if top_n == 0:
                continue  # weight=0 — suppressed
            try:
                candidates = self._strategies[source.code].fetch_day(day_start, day_end, deadline=deadline)
            except HistoricalServiceError as exc:
                logger.error('fetch_day failed source=%s day=%s: %s', source.code, day_start.date(), exc)
                candidates = []
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
        return DayResult(day=day_start, fetched=fetched_total, saved_ids=saved_ids)

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
            datums = _filter_title_dupes(
                datums,
                threshold=getattr(settings, 'ARTICLE_DEDUP_JACCARD_THRESHOLD', 0.75),
                hours=getattr(settings, 'ARTICLE_DEDUP_HOURS', 24),
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
                    body = fetch_article_body(datum['source_url'], source_code=source_code)
                    if body:
                        fields['content'] = body
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


def fetch_article_body(url: str, source_code: str | None = None, timeout: int = _HTTP_TIMEOUT) -> str | None:
    """Best-effort plain-text body for a historical article URL.

    Backfill candidates come from sitemaps/CDX as title-only; without body text the
    NLP step can't geocode them, so they'd never aggregate into Events (and never
    hit the map). This pulls the page and extracts paragraph text — good enough for
    geocoding + category. Returns None on any failure (caller falls back to the
    title).

    source_code: when given, participates in the same timeout blocklist as sitemap
    discovery (skipped if already blocked; a Timeout here blocks it too) — a source
    whose article pages are timing out is treated the same as one whose sitemap is.

    Called inline from HistoricalBackfillService._save_day_batch (one day-window's
    worth of articles at a time, not fanned out further — see module docstring).
    """
    if source_code and _is_source_blocked(source_code):
        return None
    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.Timeout:
        if source_code:
            _block_source(source_code, f'article body timeout: {url}')
        return None
    except requests.RequestException as exc:
        logger.debug('body fetch failed url=%r: %s', url, exc)
        return None

    # Cap before regex work: the backreference pattern degrades O(N) per unclosed
    # <script>/<style> tag; 200 KB is ample for any news article's paragraph text.
    html = _SCRIPT_STYLE_RE.sub(' ', resp.text[:200_000])
    paragraphs = _PARAGRAPH_RE.findall(html)
    text = ' '.join(_TAG_RE.sub(' ', p) for p in paragraphs)
    text = _html.unescape(re.sub(r'\s+', ' ', text)).strip()
    return text[:_BODY_MAX_CHARS] or None


def _slug_from_url(url: str) -> str:
    """Extract a readable title hint from the URL path when no title is available."""
    path = urlparse(url).path.rstrip('/')
    slug = path.split('/')[-1] if path else url
    slug = re.sub(r'\.\w+$', '', slug)      # strip extension
    slug = re.sub(r'[-_]', ' ', slug)       # dashes/underscores → spaces
    slug = re.sub(r'\d{6,}', '', slug)      # strip IDs like 12345678
    return slug.strip()[:120].title() or url
