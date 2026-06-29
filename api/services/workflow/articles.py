import logging
import re
import requests
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone

logger = logging.getLogger(__name__)


def _apply_min_score_filter(qs, min_score: float):
    """Exclude articles whose importance_score is set and below min_score."""
    if min_score > 0:
        qs = qs.exclude(
            importance_score__isnull=False,
            importance_score__lt=min_score,
        )
    return qs

_FETCH_DEADLINE_SECONDS = 600.0


def fetch_articles(
    source_code: str | None,
    start_date: datetime,
    deadline: datetime | None = None,
) -> int:
    """Fetch from one or all sources starting at start_date and save as Articles.
    Returns the number of newly created articles.

    deadline: if provided, stops between sources once the current time exceeds it
    so the task exits cleanly before the RQ hard-kill fires.
    """
    from services.data import DataService
    from core.models import Source

    sources = (
        list(Source.objects.filter(code=source_code))
        if source_code
        else list(Source.objects.filter(is_enabled=True))
    )
    total = 0
    for i, source in enumerate(sources):
        if deadline is not None and datetime.now(dt_timezone.utc) >= deadline:
            logger.warning(
                '[fetch] deadline reached — stopping after %d/%d source(s)',
                i, len(sources),
            )
            break
        try:
            count = DataService(source).refresh_until(start_date)
            total += count
        except Exception as e:
            count = 0
            logger.error(f'[fetch] Exception - {e}', exc_info=e)
        logger.info(f'[fetch] {source.code}: {count} new article(s)')
    logger.info(f'[fetch] done — {total} new article(s) across {len(sources)} source(s)')
    return total


def _fetch_og_image(url: str) -> str | None:
    """Best-effort: fetch og:image meta tag from a URL. Returns None on any failure."""
    try:
        r = requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'}, stream=True)
        chunk = next(r.iter_content(65536), b'')
        r.close()
        text = chunk.decode('utf-8', errors='ignore')
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            text,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            text,
        )
        return m.group(1).strip() if m else None
    except Exception:
        return None


def process_articles(
    limit: int = 500,
    source_code: str | None = None,
    reprocess: bool = False,
    only_failed: bool = False,
    ids: list | None = None,
) -> int:
    """LLM analyzer extracts category, location, entities, and sentiment; FinBERT adds
    financial sentiment. Returns the number of articles processed.

    only_failed: re-run NLP on articles that were processed but ended up with no location.
    ids: when given, process exactly these article ids (bypasses normal selection).
    """
    import uuid as _uuid
    from core.models import Article, ArticleDocument
    from services.processing.cleaner import ArticleCleaner
    from services.utils import mark_stage

    if ids is not None:
        uuids = [i if isinstance(i, _uuid.UUID) else _uuid.UUID(str(i)) for i in ids]
        articles = list(Article.objects.filter(id__in=uuids))
    else:
        qs = Article.objects.all()
        if source_code:
            qs = qs.filter(source_code=source_code)
        if only_failed:
            qs = qs.filter(processed_on__isnull=False, location__isnull=True)
            articles = [a for a in qs if not (a.extra_data or {}).get('geo_failed')][:limit]
        else:
            if not reprocess:
                from django.conf import settings as _s
                qs = qs.filter(processed_on__isnull=True)
                qs = _apply_min_score_filter(qs, _s.ARTICLE_MIN_IMPORTANCE_TO_PROCESS)
                articles = list(qs.order_by('-importance_score')[:limit])
            else:
                articles = list(qs[:limit])

    if not articles:
        return 0

    cleaner = ArticleCleaner()

    docs = [
        ArticleDocument(
            id=str(article.id),
            title=article.title,
            content=article.content,
            source_code=article.source_code,
            published_on=article.published_on.isoformat(),
        )
        for article in articles
    ]
    # Backfilled historical articles get the lean path: English-only analysis
    # (no Arabic) and no banner scrape. They still geocode + categorize.
    lite_flags = [bool((a.extra_data or {}).get('backfill_week')) for a in articles]

    feature_list = cleaner.clean_batch(docs, lite_flags=lite_flags)

    processed = 0
    for article, features, lite in zip(articles, feature_list, lite_flags):
        article.entities = features.entities
        article.sentiment = features.sentiment
        article.finbert_sentiment = features.finbert_sentiment
        article.location = features.location
        article.latitude = features.latitude
        article.longitude = features.longitude
        article.event_intensity = features.event_intensity
        article.category = features.category
        article.sub_category = features.sub_category
        article.processed_on = timezone.now()
        extra = {**(article.extra_data or {}), 'llm': features.llm_data}
        if only_failed and not features.location:
            extra['geo_failed'] = True
        article.extra_data = extra
        article.translations = features.translations

        mark_stage(article, 'process', ok=True)
        mark_stage(article, 'geocode', ok=bool(features.location),
                   error=None if features.location else 'no location resolved')

        update_fields = [
            'entities', 'sentiment', 'finbert_sentiment', 'location', 'latitude', 'longitude',
            'event_intensity', 'category', 'sub_category', 'processed_on',
            'extra_data', 'translations', 'stage_status',
        ]
        if (
            not lite
            and not article.banner_image_url
            and article.source_url and article.source_url.startswith('https://')
        ):
            og = _fetch_og_image(article.source_url)
            if og:
                article.banner_image_url = og
                update_fields.append('banner_image_url')

        article.save(update_fields=update_fields)
        processed += 1
        location = features.location or '?'
        category = '/'.join(filter(None, [features.category, features.sub_category]))
        logger.info(f'[process] {article.title[:70]} → {category} @ {location}')

    return processed
