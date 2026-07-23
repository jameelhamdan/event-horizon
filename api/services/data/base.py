import datetime
from typing_extensions import TypedDict
from typing import Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    import core.models


class _ArticleDatumRequired(TypedDict):
    source_url: str
    author: str
    author_slug: str
    title: str
    content: str
    published_on: datetime.datetime
    extra_data: dict


class ArticleDatum(_ArticleDatumRequired, total=False):
    """Required base fields plus optional banner_image_url."""
    banner_image_url: str


def article_datum(
    source: 'core.models.Source', *, source_url: str, title: str, content: str,
    published_on: datetime.datetime, extra_data: dict,
    author: str | None = None, author_slug: str | None = None,
    banner_image_url: str | None = None,
) -> ArticleDatum:
    """Build an ArticleDatum, applying the source-field conventions every
    discovery path (rss / sitemap / wayback / wikipedia) repeated: ``author``
    defaults to the source name, ``author_slug`` to the source's slug (or its
    code), and ``title`` is truncated to the model's 200-char limit. Pass
    ``author``/``author_slug`` explicitly to override (e.g. Wikipedia derives
    them from the cited article's domain)."""
    datum: ArticleDatum = {
        'source_url': source_url,
        'author': author or source.name,
        'author_slug': author_slug or source.author_slug or source.code,
        'title': title[:200],
        'content': content,
        'published_on': published_on,
        'extra_data': extra_data,
    }
    if banner_image_url:
        datum['banner_image_url'] = banner_image_url
    return datum


class ClientServiceException(Exception):
    code = 'data_client_error'


class BaseClientService:
    """
    Define base class for data services and base interfaces
    """

    def __init__(self, source: 'core.models.Source'):
        self.source = source

    def fetch_data(self, start_date: datetime.datetime) -> Iterator[ArticleDatum]:
        yield from []
