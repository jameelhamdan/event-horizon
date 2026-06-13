import datetime
from django.utils import timezone
import core.models
import logging

from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from services.data.base import BaseClientService


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
        created = 0
        for datum in list(self.client.fetch_data(start_date)):
            _, was_created = core.models.Article.objects.get_or_create(
                source_code=self.source.code,
                source_type=self.source.type,
                source_url=datum['source_url'],
                defaults=datum,
            )
            if was_created:
                created += 1
                logger.info(f'[fetch] {self.source.code}: {datum["title"][:80]}')
        return created

    def refresh_latest_data(self):
        start_date = timezone.now() - datetime.timedelta(hours=1)
        self.refresh_until(start_date)