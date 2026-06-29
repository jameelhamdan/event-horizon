"""
Wikipedia Current Events adapter.

Fetches Portal:Current_events daily subpages via the Wikipedia parse API.

HTML structure per day:
  <div class="current-events-content description">
    <p><b>Armed conflicts and attacks</b></p>
    <ul>
      <li>Middle Eastern crisis          ← depth-0 named situation (has nested <ul>)
        <ul>
          <li>2026 Iran war              ← depth-1 named situation (has nested <ul>)
            <ul>
              <li>News event text...     ← leaf, skip
            </ul>
          </li>
        </ul>
      </li>
    </ul>
    <p><b>Disasters and accidents</b></p>
    <ul>...</ul>
  </div>

A <li> is a named topic if it contains a nested <ul> (it groups sub-events).
Leaf <li> items (plain news sentences) have no nested <ul> and are skipped.
We recurse one level deep (depth 0 + depth 1) to capture both
"Middle Eastern crisis" and "2026 Iran war" without going too granular.
"""
import logging
from datetime import date, timedelta

import requests
from django.conf import settings
from django.utils.text import slugify
from scrapling.parser import Selector
from services.utils import tokenize as _tokenize
from services.topics.types import TopicDict

logger = logging.getLogger(__name__)

WIKI_API = 'https://en.wikipedia.org/w/api.php'

_SECTION_CATEGORY: list[tuple[str, str]] = [
    # Conflict — checked first (most specific)
    ('armed',         'conflict'),
    ('conflict',      'conflict'),
    ('war',           'conflict'),
    ('attack',        'conflict'),
    ('terrorism',     'conflict'),
    ('military',      'conflict'),
    ('insurgency',    'conflict'),
    # Protest
    ('protest',       'protest'),
    ('demonstration', 'protest'),
    ('unrest',        'protest'),
    ('riot',          'protest'),
    # Disaster
    ('disaster',      'disaster'),
    ('accident',      'disaster'),
    ('environment',   'disaster'),
    ('health',        'disaster'),
    ('epidemic',      'disaster'),
    ('pandemic',      'disaster'),
    ('earthquake',    'disaster'),
    ('flood',         'disaster'),
    # Political
    ('politic',       'political'),
    ('election',      'political'),
    ('diplomat',      'political'),
    ('international', 'political'),
    ('relation',      'political'),
    ('governance',    'political'),
    ('government',    'political'),
    ('sanction',      'political'),
    # Economic
    ('business',      'economic'),
    ('economic',      'economic'),
    ('finance',       'economic'),
    ('trade',         'economic'),
    ('market',        'economic'),
    ('energy',        'economic'),
    # Crime
    ('crime',         'crime'),
    ('law',           'crime'),
    ('justice',       'crime'),
    ('corruption',    'crime'),
]

_SKIP_HEADINGS = frozenset({
    'contents', 'navigation', 'references', 'notes', 'see also',
    'external links', 'further reading', 'footnotes',
})



def _section_to_category(heading: str) -> str:
    lower = heading.lower()
    for keyword, cat in _SECTION_CATEGORY:
        if keyword in lower:
            return cat
    return 'general'


def _subpage_title(d: date) -> str:
    return f"Portal:Current_events/{d.strftime('%Y_%B')}_{d.day:02d}"


def _fetch_html(title: str) -> str:
    try:
        r = requests.get(
            WIKI_API,
            params={
                'action': 'parse',
                'page': title,
                'prop': 'text',
                'format': 'json',
                'disableeditsection': '1',
            },
            timeout=30,
            headers={'User-Agent': f'{settings.APP_NAME}/1.0'},
        )
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            return ''
        return data.get('parse', {}).get('text', {}).get('*', '')
    except Exception as e:
        logger.warning('[topics:current-events] Fetch failed for %s: %s', title, e)
        return ''



def _li_topic_name(li_node) -> str:
    """
    Extract the topic name from a named-situation <li>.
    Returns text of the first wiki article <a> link before any nested <ul>, or ''.
    """
    for part in li_node.children:
        if getattr(part, 'tag', None) == 'ul':
            break
        if getattr(part, 'tag', None) == 'a':
            href = part.attrib.get('href', '')
            if href.startswith('/wiki/') and ':' not in href[6:]:  # skip Special:, File:, etc.
                text = part.get_all_text(strip=True)
                if len(text) >= 4:
                    return text
    return ''


def _li_nested_ul(li_node):
    """Return the first nested <ul> inside this <li>, or None."""
    for part in li_node.children:
        if getattr(part, 'tag', None) == 'ul':
            return part
    return None


def _emit_topic(name: str, category: str, source_url: str, results: dict) -> None:
    slug = slugify(name)[:80]
    if not slug or slug in results:
        return
    results[slug] = {
        'slug': slug,
        'name': name[:255],
        'keywords': sorted(_tokenize(name))[:15],
        'source_id': 'wikipedia-current-events',
        'description': '',
        'category': category,
        'source_url': source_url,
        'is_current': True,
    }


def _parse_day(html: str, source_url: str) -> dict[str, TopicDict]:
    page = Selector(html)
    results: dict[str, TopicDict] = {}

    containers = page.css('div.current-events-content')
    if not containers:
        return {}

    for container in containers:
        current_category = 'general'

        for child in container.children:
            tag = getattr(child, 'tag', None)

            # Section heading — two formats:
            #   New: <p><b>Armed conflicts and attacks</b></p>
            #   Old: <div class="current-events-content-heading">text</div>
            if tag == 'p':
                b_tags = child.css('b')
                if b_tags:
                    heading = b_tags[0].get_all_text(strip=True)
                    if heading.lower() not in _SKIP_HEADINGS:
                        current_category = _section_to_category(heading)
                continue

            if tag == 'div' and child.has_class('current-events-content-heading'):
                heading = child.get_all_text(strip=True)
                if heading.lower() not in _SKIP_HEADINGS:
                    current_category = _section_to_category(heading)
                continue

            if tag != 'ul':
                continue

            # Depth-0: direct <li> children of the section <ul>
            for li0 in child.children:
                if getattr(li0, 'tag', None) != 'li':
                    continue

                nested0 = _li_nested_ul(li0)
                if nested0 is None:
                    continue  # leaf news event, skip

                name0 = _li_topic_name(li0)
                if name0:
                    _emit_topic(name0, current_category, source_url, results)

                # Depth-1: direct <li> children of the depth-0 nested <ul>
                for li1 in nested0.children:
                    if getattr(li1, 'tag', None) != 'li':
                        continue

                    nested1 = _li_nested_ul(li1)
                    if nested1 is None:
                        continue  # leaf news event, skip

                    name1 = _li_topic_name(li1)
                    if name1:
                        _emit_topic(name1, current_category, source_url, results)

    return results


class WikipediaCurrentEventsAdapter:
    source_id = 'wikipedia-current-events'
    display_name = 'Wikipedia Current Events'

    def __init__(self, num_days: int = 30):
        self.num_days = num_days

    def fetch(self) -> list[TopicDict]:
        today = date.today()
        # Merge across days: later days win on slug collision (most recent wins)
        merged: dict[str, TopicDict] = {}

        for i in range(self.num_days - 1, -1, -1):
            d = today - timedelta(days=i)
            title = _subpage_title(d)
            source_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"

            html = _fetch_html(title)
            if not html:
                logger.debug('[topics:current-events] No content for %s', title)
                continue

            day_topics = _parse_day(html, source_url)
            merged.update(day_topics)
            logger.info('[topics:current-events] %s → %d topic(s)', title, len(day_topics))

        topics = list(merged.values())
        if not topics:
            logger.warning('[topics:current-events] Zero topics across %d day(s)', self.num_days)
        return topics
