"""
Wikipedia Current Events — historical backfill discovery strategy.

The primary discovery path for historical backfill (see
services/data/historical.py for the orchestrator and the sitemap-based
per-publisher strategy this complements). Wikipedia's Current Events portal
is a human-curated list of each day's globally significant events, with
citations to news articles — i.e. it is already the "most important articles
per day" ranking that sitemap discovery can't provide, going back decades.

One monthly page (Portal:Current_events/September_2021) transcludes every
day of the month with per-day sections and their citations intact, so a
whole month of curated events costs ONE parse-API request (~0.5-0.75 MB,
~450-800 citations). The fetched HTML is cached in Redis for 24h
(key_backfill_wiki_month) so the ~30 day-chunk tasks a bulk backfill
dispatches for that month share a single fetch.

Each leaf event (a news sentence with >=1 external citation) becomes one
ArticleDatum: the first cited article URL is the datum's source_url, the
event sentence is title+content (the cited page's own <title>/body are
fetched at save time by HistoricalBackfillService — live first, Wayback
Machine fallback for dead/paywalled/JS-only pages). All articles are
attributed to the synthetic 'wikipedia-current-events' Source
(ensure_wikipedia_source); the cited outlet's domain is kept in
Article.author and extra_data['cite_domain'].

Parsing reuses the section-heading→category mapping and named-topic helpers
from services.topics.sources.current_events (the live topics adapter for the
same portal); ancestor topic names (e.g. "Somali Civil War") are kept in
extra_data['wiki_topics'] for downstream clustering/tagging context.
"""
import datetime
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests
from django.utils.text import slugify

from services.data.base import ArticleDatum
from services.data.historical import _block_source, _is_source_blocked

if TYPE_CHECKING:
    import core.models

logger = logging.getLogger(__name__)

WIKIPEDIA_SOURCE_CODE = 'wikipedia-current-events'
# Default per-day cap when top_n isn't given explicitly. Wikipedia days carry
# ~15-25 curated events; unlike per-publisher sitemap discovery there is no
# noise to trim, so the default keeps essentially all of them (weight-derived
# 2-6 caps would throw away most of the day's curation).
WIKI_DEFAULT_TOP_N = 25

_WIKI_API = 'https://en.wikipedia.org/w/api.php'
_HTTP_TIMEOUT = 30
_MONTH_CACHE_TTL_SECONDS = 24 * 3600
# Leaf <li> text shorter than this is navigation/formatting debris, not an event.
_MIN_EVENT_TEXT_CHARS = 20


def ensure_wikipedia_source() -> 'core.models.Source':
    """Get-or-create the synthetic Source all Wikipedia-cited backfill articles
    are attributed to. is_enabled=False keeps it out of the live fetch stage
    (services/stages.py::_fetch_pending filters is_enabled=True); backfill
    includes it explicitly (services.tasks.backfill_history_task)."""
    import core.models as m
    source, _created = m.Source.objects.get_or_create(
        code=WIKIPEDIA_SOURCE_CODE,
        defaults={
            'type': m.SourceType.WEBSITE,
            'name': 'Wikipedia Current Events',
            'description': (
                'Curated daily world events (with citations to news articles) from '
                "Wikipedia's Current Events portal — historical-backfill discovery "
                'source; not fetched live.'
            ),
            'url': 'https://en.wikipedia.org/wiki/Portal:Current_events',
            'author_slug': 'wikipedia',
            'is_enabled': False,
            'weight': 1.5,
            'weight_locked': True,
        },
    )
    return source


def month_page_title(day: datetime.datetime) -> str:
    """Portal:Current_events/September_2021 — the monthly page transcluding
    every day of that month."""
    return f'Portal:Current_events/{day.strftime("%B_%Y")}'


def day_section_id(day: datetime.datetime) -> str:
    """id= of a day's <div class="current-events-main"> inside the monthly
    page. NOTE: the day is NOT zero-padded ('2021_September_9')."""
    return f'{day.year}_{day.strftime("%B")}_{day.day}'


class WikipediaHistoricalService:
    """Same fetch_day() interface as RSSHistoricalService so
    HistoricalBackfillService can drive either strategy interchangeably."""

    def __init__(
        self, source: 'core.models.Source', max_candidates: int | None = None,
        first_match_only: bool = False,
    ) -> None:
        self._source = source
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
                'WikipediaHistorical day=%s: skipped (temporarily blocked)', day_start.date(),
            )
            return []

        html = self._get_month_html(day_start)
        if not html:
            return []

        events = parse_day_events(html, day_start)
        if self._first_match_only and events:
            events = events[:1]
        datums = [self._event_to_datum(e, day_start) for e in events]
        if self._max_candidates and len(datums) > self._max_candidates:
            datums = datums[: self._max_candidates]
        logger.info(
            'WikipediaHistorical day=%s: %d curated event(s) discovered',
            day_start.date(), len(datums),
        )
        return datums

    def _get_month_html(self, day: datetime.datetime) -> str:
        from services.cache import cache_get, cache_set, key_backfill_wiki_month
        from django.conf import settings

        key = key_backfill_wiki_month(day.strftime('%Y-%m'))
        try:
            cached = cache_get(key)
        except Exception:  # noqa: BLE001 — no Redis in dev
            cached = None
        if cached:
            return cached

        title = month_page_title(day)
        try:
            resp = requests.get(
                _WIKI_API,
                params={
                    'action': 'parse', 'page': title, 'prop': 'text',
                    'format': 'json', 'disableeditsection': '1',
                },
                timeout=_HTTP_TIMEOUT,
                headers={'User-Agent': f'{settings.APP_NAME}/1.0'},
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.Timeout:
            _block_source(self._source.code, f'wikipedia parse API timeout: {title}')
            return ''
        except (requests.RequestException, ValueError) as exc:
            logger.warning('WikipediaHistorical fetch failed for %s: %s', title, exc)
            return ''

        if 'error' in data:
            # Nonexistent month page (e.g. before the portal existed) — a real
            # empty answer, not an error worth blocking over.
            logger.info('WikipediaHistorical: no page %s (%s)', title, data['error'].get('code'))
            return ''
        html = data.get('parse', {}).get('text', {}).get('*', '')
        if html:
            try:
                cache_set(key, html, timeout=_MONTH_CACHE_TTL_SECONDS)
            except Exception:  # noqa: BLE001
                pass
        return html

    def _event_to_datum(self, event: dict, day_start: datetime.datetime) -> ArticleDatum:
        cite = event['cites'][0]
        domain = urlparse(cite).netloc.removeprefix('www.')
        return ArticleDatum(
            source_url=cite,
            author=domain or self._source.name,
            author_slug=slugify(domain)[:100] or self._source.author_slug or self._source.code,
            title=event['text'][:200],
            content=event['text'],
            published_on=day_start + datetime.timedelta(hours=12),
            extra_data={
                'wiki_event': True,
                'wiki_topics': event['topics'],
                'wiki_category': event['category'],
                'cited_urls': event['cites'],
                'cite_domain': domain,
                # The event sentence is a serviceable title, but the cited
                # page's own <title> is better — let the save path upgrade it.
                'title_from_slug': True,
            },
        )


def probe_wikipedia_source(source: 'core.models.Source') -> bool:
    """Preflight (see services.tasks.backfill_history_task): does last month's
    page parse to at least one cited event? One request, cached like any
    month fetch."""
    day = (datetime.datetime.now(datetime.timezone.utc).replace(day=1)
           - datetime.timedelta(days=14))
    svc = WikipediaHistoricalService(source, max_candidates=1, first_match_only=True)
    return bool(svc.fetch_day(day, day + datetime.timedelta(days=1)))


# ---------------------------------------------------------------------------
# Monthly-page parsing
# ---------------------------------------------------------------------------

# Trailing citation markers left in the leaf text once anchors are flattened,
# e.g. " (Reuters)", " (AP) (BBC News)".
_TRAILING_CITE_RE = re.compile(r'\s*\(\s*[^()]{1,80}\s*\)\s*$')


def parse_day_events(month_html: str, day: datetime.datetime) -> list[dict]:
    """Extract one calendar day's leaf events from a monthly portal page.

    Returns [{'text', 'cites', 'topics', 'category'}]: the event sentence,
    its external citation URLs (in page order), ancestor named-topic titles,
    and the category mapped from the section heading (same mapping the live
    topics adapter uses).
    """
    from scrapling.parser import Selector
    from services.topics.sources.current_events import _SKIP_HEADINGS, _section_to_category

    page = Selector(month_html)
    section_id = day_section_id(day)
    day_divs = [d for d in page.css('div.current-events-main') if d.attrib.get('id') == section_id]
    if not day_divs:
        return []

    events: list[dict] = []
    for container in day_divs[0].css('div.current-events-content'):
        category = 'general'
        for child in container.children:
            tag = getattr(child, 'tag', None)

            # Section heading — new (<p><b>…</b></p>) and old
            # (<div class="current-events-content-heading">) formats, same as
            # the live adapter's _parse_day.
            if tag == 'p':
                b_tags = child.css('b')
                if b_tags:
                    heading = b_tags[0].get_all_text(strip=True)
                    if heading.lower() not in _SKIP_HEADINGS:
                        category = _section_to_category(heading)
                continue
            if tag == 'div' and child.has_class('current-events-content-heading'):
                heading = child.get_all_text(strip=True)
                if heading.lower() not in _SKIP_HEADINGS:
                    category = _section_to_category(heading)
                continue
            if tag != 'ul':
                continue

            _walk_event_list(child, [], category, events)
    return events


def _walk_event_list(ul_node, topic_stack: list[str], category: str, out: list[dict]) -> None:
    """Recurse a day section's <ul>: named-topic <li>s (those with a nested
    <ul>) extend the topic stack; leaf <li>s with external citations become
    events. Unlike the live topics adapter (which keeps the named topics and
    skips the leaves), backfill wants exactly the leaves."""
    from services.topics.sources.current_events import _li_nested_ul, _li_topic_name

    for li in ul_node.children:
        if getattr(li, 'tag', None) != 'li':
            continue
        nested = _li_nested_ul(li)
        if nested is not None:
            name = _li_topic_name(li)
            _walk_event_list(nested, topic_stack + ([name] if name else []), category, out)
            continue

        cites = [
            a.attrib.get('href', '')
            for a in li.css('a.external')
            if a.attrib.get('href', '').startswith('http')
        ]
        if not cites:
            continue
        text = _leaf_event_text(li)
        if len(text) < _MIN_EVENT_TEXT_CHARS:
            continue
        out.append({'text': text, 'cites': cites, 'topics': list(topic_stack), 'category': category})


def _leaf_event_text(li_node) -> str:
    """Leaf <li> text with the trailing '(Reuters) (AP)' citation-anchor
    residue stripped."""
    text = re.sub(r'\s+', ' ', li_node.get_all_text(strip=True)).strip()
    while True:
        stripped = _TRAILING_CITE_RE.sub('', text)
        if stripped == text:
            return text
        text = stripped
