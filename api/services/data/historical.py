"""
Historical backfill — fetch top-N articles per ISO week from a source.

Strategy (per source type):

  RSSHistoricalService
    Discovers historical article URLs via the source domain's sitemap
    (robots.txt → /sitemap.xml → /sitemap_index.xml → /news-sitemap.xml).
    Handles nested sitemap indexes (one level).  Titles come from
    <news:title> when available, otherwise inferred from the URL slug.
    Ranks by LLM significance score (batch of 30 headlines per call).
    Falls back to score=5.0 per article on any LLM error.

Both return List[RankedArticle].  HistoricalBackfillService sorts, slices
to top-N, and saves via Article.objects.get_or_create — fully idempotent.
"""
import datetime
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator, TYPE_CHECKING
from urllib.parse import urlparse

import requests

from services.data.base import ArticleDatum, ClientServiceException

if TYPE_CHECKING:
    import core.models

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

@dataclass
class RankedArticle:
    datum: ArticleDatum
    score: float
    rank_signal: str  # 'engagement' | 'llm'


@dataclass
class WeekResult:
    week_start: datetime.datetime
    fetched: int   # total candidates collected for this week
    saved: int     # Article records newly created


class HistoricalServiceError(ClientServiceException):
    code = 'historical_error'


# ---------------------------------------------------------------------------
# Week helpers
# ---------------------------------------------------------------------------

def _iso_week_start(dt: datetime.datetime) -> datetime.datetime:
    """Return Monday 00:00 UTC of the ISO week containing dt."""
    d = dt.date() - datetime.timedelta(days=dt.weekday())
    return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)


def iter_weeks(
    start: datetime.datetime,
    end: datetime.datetime,
) -> Iterator[tuple[datetime.datetime, datetime.datetime]]:
    """Yield (week_start, week_end) pairs covering [start, end)."""
    current = _iso_week_start(start)
    while current < end:
        yield current, current + datetime.timedelta(weeks=1)
        current += datetime.timedelta(weeks=1)


# ---------------------------------------------------------------------------
# RSS historical strategy
# ---------------------------------------------------------------------------

_SITEMAP_NS = 'http://www.sitemaps.org/schemas/sitemap/0.9'
_NEWS_NS = 'http://www.google.com/schemas/sitemap-news/0.9'
_LLM_BATCH_SIZE = 30
_HTTP_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; HistoricalBackfiller/1.0)'}
_HTTP_TIMEOUT = 15


class RSSHistoricalService:
    """
    Discovers historical article URLs via the source domain's XML sitemap and
    ranks them using LLM significance scoring on headlines.

    Sitemap discovery order:
      1. Sitemap: directives in robots.txt
      2. /sitemap.xml
      3. /sitemap_index.xml
      4. /news-sitemap.xml

    Nested sitemap indexes are followed one level deep.  Sub-sitemaps whose
    lastmod is more than a week before week_start are pruned to save requests.

    LLM scoring: batches of 30 headlines → significance score 1–10.
    On LLM failure the whole batch defaults to 5.0.
    """

    def __init__(self, source: 'core.models.Source') -> None:
        self._source = source
        parsed = urlparse(source.url)
        self._base_url = f'{parsed.scheme}://{parsed.netloc}'

    def fetch_week(
        self,
        week_start: datetime.datetime,
        week_end: datetime.datetime,
    ) -> list[RankedArticle]:
        entries = self._discover_entries(week_start, week_end)
        if not entries:
            logger.info(
                'RSSHistorical source=%r week=%s: no sitemap entries found',
                self._source.code, week_start.date(),
            )
            return []

        scored = self._score_entries(entries)
        logger.info(
            'RSSHistorical source=%r week=%s: %d entries scored',
            self._source.code, week_start.date(), len(scored),
        )
        return scored

    # ------------------------------------------------------------------
    # Sitemap discovery
    # ------------------------------------------------------------------

    def _discover_entries(
        self,
        week_start: datetime.datetime,
        week_end: datetime.datetime,
    ) -> list[dict]:
        """Return a list of {url, title, date} dicts whose date falls in the window."""
        for sm_url in self._candidate_sitemap_urls():
            entries = self._parse_sitemap(sm_url, week_start, week_end)
            if entries:
                return entries
        return []

    def _candidate_sitemap_urls(self) -> list[str]:
        """Return sitemap URL candidates, checking robots.txt first."""
        candidates: list[str] = []

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
        week_start: datetime.datetime,
        week_end: datetime.datetime,
    ) -> list[dict]:
        """Parse a sitemap URL; recurses into sitemap indexes (one level)."""
        try:
            resp = requests.get(url, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
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
            return self._parse_sitemap_index(root, week_start, week_end)

        return self._extract_urlset_entries(root, week_start, week_end)

    def _parse_sitemap_index(
        self,
        root: ET.Element,
        week_start: datetime.datetime,
        week_end: datetime.datetime,
    ) -> list[dict]:
        entries: list[dict] = []
        for sm_el in root.findall(f'{{{_SITEMAP_NS}}}sitemap'):
            loc_el = sm_el.find(f'{{{_SITEMAP_NS}}}loc')
            if loc_el is None or not loc_el.text:
                continue

            # Prune sub-sitemaps whose lastmod is clearly before our window
            lastmod_el = sm_el.find(f'{{{_SITEMAP_NS}}}lastmod')
            if lastmod_el is not None and lastmod_el.text:
                sm_date = _parse_sitemap_date(lastmod_el.text.strip())
                if sm_date and sm_date < week_start - datetime.timedelta(days=7):
                    continue

            sub_url = loc_el.text.strip()
            entries.extend(self._parse_sitemap(sub_url, week_start, week_end))

        return entries

    def _extract_urlset_entries(
        self,
        root: ET.Element,
        week_start: datetime.datetime,
        week_end: datetime.datetime,
    ) -> list[dict]:
        entries: list[dict] = []
        for url_el in root.findall(f'{{{_SITEMAP_NS}}}url'):
            entry = _extract_sitemap_entry(url_el)
            if entry is None or entry['date'] is None:
                continue
            entry_date: datetime.datetime = entry['date']
            if entry_date.tzinfo is None:
                entry_date = entry_date.replace(tzinfo=datetime.timezone.utc)
            if week_start <= entry_date < week_end:
                entries.append(entry)
        return entries

    # ------------------------------------------------------------------
    # LLM significance scoring
    # ------------------------------------------------------------------

    def _score_entries(self, entries: list[dict]) -> list[RankedArticle]:
        """Batch-score entries by LLM significance; return RankedArticle list."""
        ranked: list[RankedArticle] = []
        for i in range(0, len(entries), _LLM_BATCH_SIZE):
            batch = entries[i: i + _LLM_BATCH_SIZE]
            scores = self._llm_score_batch(batch)
            for entry, score in zip(batch, scores):
                title = entry['title'] or _slug_from_url(entry['url'])
                datum = ArticleDatum(
                    source_url=entry['url'],
                    author=self._source.name,
                    author_slug=self._source.author_slug or self._source.code,
                    title=title[:200],
                    content=title,
                    published_on=entry['date'],
                    extra_data={'sitemap_title': entry['title']},
                )
                ranked.append(RankedArticle(datum=datum, score=score, rank_signal='llm'))
        return ranked

    def _llm_score_batch(self, entries: list[dict]) -> list[float]:
        """Ask the LLM to rate a batch of headlines 1–10. Returns a parallel list of scores."""
        from services.llm import get_llm_service, LLMError

        lines = '\n'.join(
            f'{i + 1}. {entry["title"] or _slug_from_url(entry["url"])}'
            for i, entry in enumerate(entries)
        )
        prompt = (
            'Rate each news headline by global significance on a scale of 1.0–10.0.\n'
            'Consider: geopolitical impact, affected population, economic consequences, novelty.\n\n'
            f'{lines}\n\n'
            'Return a JSON array — one object per headline:\n'
            '[{"i": 1, "score": 7.5}, {"i": 2, "score": 4.0}, ...]\n'
            'Return only the JSON array, no other text.'
        )
        default = [5.0] * len(entries)
        try:
            llm = get_llm_service('historical')
            raw = llm.chat([{'role': 'user', 'content': prompt}])
            raw = re.sub(r'^```(?:json)?\s*', '', (raw or '').strip())
            raw = re.sub(r'\s*```$', '', raw)
            # Models sometimes wrap the array in prose ("Here is the JSON: [...]")
            # or return nothing at all — isolate the array before parsing.
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if not match:
                logger.warning(
                    'LLM batch score returned no JSON array (%r); defaulting to 5.0',
                    raw[:120],
                )
                return default
            data = json.loads(match.group(0))
            score_map = {item['i']: float(item['score']) for item in data}
            expected = set(range(1, len(entries) + 1))
            missing = expected - score_map.keys()
            if missing:
                logger.warning(
                    'LLM batch score index mismatch: expected 1-%d, missing %s; using 5.0 defaults',
                    len(entries), sorted(missing),
                )
            return [score_map.get(i + 1, 5.0) for i in range(len(entries))]
        except LLMError as exc:
            logger.warning('LLM batch scoring failed (%s); defaulting to 5.0', exc)
            return default
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning('LLM batch score parse error (%s); defaulting to 5.0', exc)
            return default


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class HistoricalBackfillService:
    """
    Orchestrates week-by-week historical backfill for a single source.

    Usage:
        service = HistoricalBackfillService(source, start_date, end_date, top_n=10)
        for result in service.run():
            print(result.week_start.date(), result.fetched, result.saved)

    run() is fully idempotent: Article.objects.get_or_create() keyed on
    (source_code, source_type, source_url) skips already-imported articles.

    Backfill metadata is stored in Article.extra_data under keys:
      backfill_week  — ISO week start string
      backfill_score — float ranking score
      rank_signal    — 'engagement' | 'llm'
    """

    def __init__(
        self,
        source: 'core.models.Source',
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        top_n: int = 10,
        delay_seconds: float = 1.0,
    ) -> None:
        self.source = source
        self.start_date = start_date
        self.end_date = end_date
        self.top_n = top_n
        self.delay_seconds = delay_seconds
        self._strategy = self._build_strategy()

    def _build_strategy(self):
        import core.models as m
        if self.source.type == m.SourceType.RSS:
            return RSSHistoricalService(self.source)
        raise HistoricalServiceError(
            f'No historical strategy for source type "{self.source.type}". '
            'Supported: rss.'
        )

    def run(
        self,
        resume_weeks: set[str] | None = None,
        dry_run: bool = False,
    ) -> Iterator[WeekResult]:
        """
        Yield a WeekResult for each ISO week in [start_date, end_date).

        resume_weeks  — set of week_start.isoformat() strings to skip.
        dry_run       — rank but do not write to the database.
        """
        for week_start, week_end in iter_weeks(self.start_date, self.end_date):
            if resume_weeks and week_start.isoformat() in resume_weeks:
                logger.debug('Skipping week %s (checkpoint)', week_start.date())
                continue

            try:
                candidates = self._strategy.fetch_week(week_start, week_end)
            except HistoricalServiceError as exc:
                logger.error('fetch_week failed week=%s: %s', week_start.date(), exc)
                yield WeekResult(week_start=week_start, fetched=0, saved=0)
                continue

            top = sorted(candidates, key=lambda r: r.score, reverse=True)[: self.top_n]
            saved = 0 if dry_run else self._save_articles(top, week_start)

            yield WeekResult(week_start=week_start, fetched=len(candidates), saved=saved)

            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)

    def _save_articles(
        self,
        ranked: list[RankedArticle],
        week_start: datetime.datetime,
    ) -> int:
        import core.models as m
        created = 0
        for item in ranked:
            datum = item.datum
            defaults: dict = {**datum}
            defaults['extra_data'] = {
                **datum.get('extra_data', {}),
                'backfill_week': week_start.isoformat(),
                'backfill_score': round(item.score, 2),
                'rank_signal': item.rank_signal,
            }
            _, was_created = m.Article.objects.get_or_create(
                source_code=self.source.code,
                source_type=self.source.type,
                source_url=datum['source_url'],
                defaults=defaults,
            )
            if was_created:
                created += 1
                logger.info(
                    '[backfill] %s | score=%.0f | %s',
                    week_start.date(), item.score, datum['title'][:80],
                )
        return created


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


def _slug_from_url(url: str) -> str:
    """Extract a readable title hint from the URL path when no title is available."""
    path = urlparse(url).path.rstrip('/')
    slug = path.split('/')[-1] if path else url
    slug = re.sub(r'\.\w+$', '', slug)      # strip extension
    slug = re.sub(r'[-_]', ' ', slug)       # dashes/underscores → spaces
    slug = re.sub(r'\d{6,}', '', slug)      # strip IDs like 12345678
    return slug.strip()[:120].title() or url
