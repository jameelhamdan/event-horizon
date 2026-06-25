import datetime
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

import core.models
from services.text_utils import tokenize as _tokenize_title, jaccard as _jaccard

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from services.data.base import BaseClientService

_DEDUP_CACHE_KEY = 'article_title_dedup'
_DEDUP_MAX_SETS  = 2000  # rolling window cap to avoid unbounded growth


def _filter_title_dupes(datums: list, threshold: float = 0.75, hours: int = 24) -> list:
    """
    Drop datums whose title is a near-duplicate of a recently stored article title.
    Maintains a rolling window of title token sets in Django's cache (Redis in production).
    Race conditions between workers are acceptable — URL dedup in get_or_create is the
    definitive guard; this filter reduces noise before it reaches the DB.
    """
    cached: list[frozenset] = cache.get(_DEDUP_CACHE_KEY) or []
    # new_sets grows as we accept datums; checked against incoming ones too (intra-batch dedup).
    new_sets = list(cached)
    kept = []

    for datum in datums:
        tokens = _tokenize_title(datum.get('title', ''))
        if not tokens:
            kept.append(datum)
            continue
        # Check against new_sets (not just cached) so two near-identical articles arriving
        # in the same batch don't both slip through.
        if any(_jaccard(tokens, existing) >= threshold for existing in new_sets):
            logger.debug('[dedup] near-duplicate title skipped: %s', datum.get('title', '')[:80])
            continue
        kept.append(datum)
        new_sets.append(tokens)

    if len(new_sets) > _DEDUP_MAX_SETS:
        new_sets = new_sets[-_DEDUP_MAX_SETS:]

    if new_sets != cached:
        cache.set(_DEDUP_CACHE_KEY, new_sets, timeout=hours * 3600)

    return kept


class DataServiceException(Exception):
    code = 'data_service_error'


class DataService:
    def __init__(self, source: core.models.Source):
        self.source = source
        self.client = self.get_data_client(self.source)

    @classmethod
    def get_data_client(cls, source: core.models.Source) -> 'BaseClientService':
        if source.type == core.models.SourceType.RSS:
            from . import rss
            return rss.RSSService(source)

        raise DataServiceException('Source Client for type "%s" not defined' % source.type)

    def refresh_until(self, start_date: datetime.datetime) -> int:
        """Fetch and persist articles; returns the number of newly created articles."""
        datums = list(self.client.fetch_data(start_date))

        if getattr(settings, 'ARTICLE_DEDUP_TITLE_ENABLED', True):
            datums = _filter_title_dupes(
                datums,
                threshold=getattr(settings, 'ARTICLE_DEDUP_JACCARD_THRESHOLD', 0.75),
                hours=getattr(settings, 'ARTICLE_DEDUP_HOURS', 24),
            )

        created = 0
        for datum in datums:
            _, was_created = core.models.Article.objects.get_or_create(
                source_code=self.source.code,
                source_type=self.source.type,
                source_url=datum['source_url'],
                defaults=datum,
            )
            if was_created:
                created += 1
                logger.info('[fetch] %s: %s', self.source.code, datum['title'][:80])
        return created

    def refresh_latest_data(self):
        start_date = timezone.now() - datetime.timedelta(hours=1)
        self.refresh_until(start_date)
