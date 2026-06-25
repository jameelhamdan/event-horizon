"""
RSSService — fetch articles from an RSS/Atom feed.

RSS is the most reliable ingestion method for news sites:
  - Structured XML (title, link, summary, published date, author)
  - No HTML scraping or CSS selector maintenance
  - Stable format (RSS 2.0 / Atom 1.0)
  - Virtually every major outlet publishes one

source.url  → the RSS feed URL  (e.g. https://www.example.com/rss/news/)
source.name → used as fallback author
"""
import datetime
import logging
from typing import Iterator, TYPE_CHECKING

import feedparser
import requests

from services.data.base import BaseClientService, ArticleDatum, ClientServiceException

if TYPE_CHECKING:
    import core.models

logger = logging.getLogger(__name__)

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; NewsFetcher/1.0; +https://github.com/)'
    ),
}


class RSSException(ClientServiceException):
    code = 'rss_error'


class RSSService(BaseClientService):

    def fetch_data(self, start_date: datetime.datetime) -> Iterator[ArticleDatum]:
        url = self.source.url
        if not url:
            raise RSSException('Source URL missing')

        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=datetime.timezone.utc)

        logger.info('RSSService fetching feed url=%r since=%s', url, start_date)

        # feedparser can fetch directly but doesn't honour custom headers/timeouts
        # — fetch raw bytes ourselves, then parse
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RSSException(f'Failed to fetch feed {url}: {exc}')

        feed = feedparser.parse(resp.content)

        if feed.bozo and not feed.entries:
            raise RSSException(
                f'Feed parse error for {url}: {feed.bozo_exception}'
            )

        logger.info('Feed "%s" — %d entries', feed.feed.get('title', url), len(feed.entries))

        for entry in feed.entries:
            datum = _entry_to_datum(entry, start_date, self.source)
            if datum is not None:
                yield datum


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry_to_datum(
    entry,
    start_date: datetime.datetime,
    source: 'core.models.Source',
) -> ArticleDatum | None:
    link = entry.get('link') or ''
    if not link:
        return None

    # --- Title ---
    title = (entry.get('title') or '').strip()
    if not title:
        return None

    # --- Published date ---
    published_on = _parse_entry_date(entry)
    if published_on is None:
        # No date in feed — assume now so it's not filtered out
        published_on = datetime.datetime.now(tz=datetime.timezone.utc)
    elif published_on.tzinfo is None:
        published_on = published_on.replace(tzinfo=datetime.timezone.utc)

    if published_on <= start_date:
        return None

    # --- Content ---
    # Prefer full content_detail over summary
    content = ''
    if entry.get('content'):
        # feedparser returns a list of content objects
        content = entry['content'][0].get('value', '')
    if not content:
        content = entry.get('summary') or entry.get('description') or ''
    content = _strip_html(content).strip()

    if not content:
        return None

    # --- Content length filter ---
    from django.conf import settings as _s
    min_words = getattr(_s, 'ARTICLE_MIN_WORD_COUNT', 30)
    if min_words > 0 and len(f'{title} {content}'.split()) < min_words:
        return None

    # --- Author ---
    author = (
        entry.get('author')
        or _first(entry.get('authors', []), 'name')
        or source.name
    ).strip()

    datum = ArticleDatum(
        source_url=link,
        author=author,
        author_slug=source.author_slug or source.code,
        title=title[:200],
        content=content,
        published_on=published_on,
        extra_data={'feed_id': entry.get('id') or link},
    )

    image_url = _extract_image_url(entry)
    if image_url:
        datum['banner_image_url'] = image_url

    return datum


def _parse_entry_date(entry) -> datetime.datetime | None:
    """Try published_parsed then updated_parsed (both are time.struct_time from feedparser)."""
    import calendar
    for key in ('published_parsed', 'updated_parsed'):
        t = entry.get(key)
        if t:
            try:
                ts = calendar.timegm(t)  # struct_time in UTC → POSIX timestamp
                return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            except Exception:
                continue
    return None


def _strip_html(text: str) -> str:
    """Remove HTML tags from feed summary/content."""
    import re
    return re.sub(r'<[^>]+>', ' ', text)


def _first(lst: list, key: str) -> str:
    for item in lst:
        val = item.get(key)
        if val:
            return val
    return ''


_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')


def _looks_like_image(url: str) -> bool:
    lower = url.lower().split('?')[0]
    return any(lower.endswith(ext) for ext in _IMAGE_EXTS)


def _extract_image_url(entry) -> str | None:
    """Extract the first usable image URL from a feedparser entry."""
    # 1. media:content
    for m in entry.get('media_content', []):
        url = m.get('url', '')
        if not url:
            continue
        medium = m.get('medium', '')
        if medium == 'image' or (not medium and _looks_like_image(url)):
            return url

    # 2. media:thumbnail
    for t in entry.get('media_thumbnail', []):
        url = t.get('url', '')
        if url:
            return url

    # 3. RSS enclosures
    for enc in entry.get('enclosures', []):
        if enc.get('type', '').startswith('image/'):
            return enc.get('href') or enc.get('url') or ''

    return None
