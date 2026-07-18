import logging
import re
import requests
from datetime import datetime, timedelta, timezone as dt_timezone

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


# Cursor floor: never fetch further back than this on the live path — feeds
# rarely retain more than a day, and anything older is historical-backfill
# territory (services/data/historical.py).
FETCH_CURSOR_MAX_LOOKBACK_HOURS = 24


def fetch_source(source_code: str, start: datetime | None = None) -> int:
    """Fetch one source from its ``last_fetched_at`` cursor and advance it.

    The cursor (start of the last *successful* fetch) replaces the old fixed
    ``now − 20min`` window, so downtime longer than the fetch interval no
    longer drops articles published during the gap — the next successful run
    picks up exactly where the last one left off (clamped to
    FETCH_CURSOR_MAX_LOOKBACK_HOURS; URL-level get_or_create makes any overlap
    free). The cursor only advances on success: a fetch that raises leaves it
    untouched, so the window keeps widening until the source recovers.

    start: explicit window start (CLI/e2e override) — still clamped to the
    lookback floor; anything older is historical-backfill territory.
    """
    from services.data import DataService
    from core.models import Source

    source = Source.objects.filter(code=source_code).first()
    if source is None or not source.is_enabled:
        return 0

    now = datetime.now(dt_timezone.utc)
    floor = now - timedelta(hours=FETCH_CURSOR_MAX_LOOKBACK_HOURS)
    if start is None:
        start = source.last_fetched_at or floor
    if start < floor:
        start = floor

    count = DataService(source).refresh_until(start)
    # Stamp with the time *before* the fetch began, so articles published while
    # the fetch was running fall inside the next run's window.
    source.last_fetched_at = now
    source.save(update_fields=['last_fetched_at'])
    logger.info('[fetch] %s: %d new article(s) (since %s)', source.code, count, start)
    return count


def fetch_sources(source_code: str | None = None, start: datetime | None = None) -> int:
    """Fetch one or all enabled sources via the cursor-correct ``fetch_source``
    path (the same one the fetch stage uses). CLI/e2e convenience — per-source
    failures are logged and skipped so one broken feed doesn't stop the sweep.
    Returns the number of newly created articles.
    """
    from core.models import Source

    codes = (
        [source_code]
        if source_code
        else list(Source.objects.filter(is_enabled=True).values_list('code', flat=True))
    )
    total = 0
    for code in codes:
        try:
            total += fetch_source(code, start=start)
        except Exception:
            logger.exception('[fetch] %s failed', code)
    logger.info('[fetch] done — %d new article(s) across %d source(s)', total, len(codes))
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


def process_articles(ids: list) -> int:
    """Run NLP on exactly these article ids. LLM analyzer extracts category and a
    place name (geocoded inline to lat/lon via a local geonamescache lookup —
    there is no separate geocode step); local VADER adds sentiment; FinBERT adds
    financial sentiment. Returns the number of articles marked processed.

    Selection lives with the caller — the ``_process_pending`` predicate in
    services/stages.py is the single source of truth for what needs processing;
    this function is the id-driven executor.

    A *failed* LLM analysis (``llm_error`` set) does NOT stamp ``processed_on``:
    the article stays unprocessed so the process stage retries it, rather than a
    degraded 'general'/no-location result masquerading as done. A *successful*
    analysis that simply resolves no location is processed and terminal (it just
    never aggregates into an event).
    """
    import uuid as _uuid
    from core.models import Article, ArticleDocument
    from services.processing.cleaner import ArticleCleaner
    from services.utils import mark_stage

    uuids = [i if isinstance(i, _uuid.UUID) else _uuid.UUID(str(i)) for i in ids]
    articles = list(Article.objects.filter(id__in=uuids))

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
    lite_flags = [bool((a.extra_data or {}).get('backfill_day')) for a in articles]

    feature_list = cleaner.clean_batch(docs, lite_flags=lite_flags)

    # Banner scrapes are independent HTTP fetches (og:image) — run the whole
    # chunk's fetches concurrently instead of serially (was up to chunk_size *
    # 5s of blocking HTTP inside this loop, one slow/unresponsive source could
    # stall the entire chunk).
    banner_targets = [
        (i, article.source_url)
        for i, (article, lite) in enumerate(zip(articles, lite_flags))
        if not lite and not article.banner_image_url and article.source_url.startswith('https://')
    ]
    banner_results: dict[int, str] = {}
    if banner_targets:
        from services.utils import map_concurrent
        ogs = map_concurrent(banner_targets, lambda t: _fetch_og_image(t[1]), max_workers=8)
        banner_results = {banner_targets[k][0]: og for k, og in enumerate(ogs) if og}

    # Base field set written for every article; banner_image_url is added to the
    # bulk_update field list only when at least one article in the chunk got a
    # fresh og:image (articles without one are written unchanged — harmless).
    update_fields = [
        'entities', 'sentiment', 'finbert_sentiment', 'location', 'latitude', 'longitude',
        'event_intensity', 'category', 'sub_category', 'processed_on',
        'extra_data', 'translations', 'llm_usage', 'stage_status',
    ]
    to_save = []
    for i, (article, features, lite) in enumerate(zip(articles, feature_list, lite_flags)):
        if features.sentiment is not None:
            article.sentiment = features.sentiment
        if features.finbert_sentiment is not None:
            article.finbert_sentiment = features.finbert_sentiment
        article.location = features.location
        article.latitude = features.latitude
        article.longitude = features.longitude
        article.event_intensity = features.event_intensity
        article.category = features.category
        article.sub_category = features.sub_category
        # Only a successful analysis marks the article processed. A failed LLM
        # call leaves processed_on NULL so the process stage retries it instead
        # of stamping a degraded result as done (see the function docstring).
        if features.llm_error is None:
            article.processed_on = timezone.now()
        article.extra_data = {**(article.extra_data or {}), 'llm': features.llm_data}
        article.translations = features.translations
        article.llm_usage = features.llm_usage

        # ok=True here previously regardless of whether the LLM call actually
        # succeeded, so a fully-failed analysis (silently falling back to
        # category='general'/no location) looked identical to a real success
        # in stage_status. Surface the real outcome so it shows up in
        # pipeline_coverage()'s error_sample instead of hiding degraded data.
        mark_stage(article, 'process', ok=features.llm_error is None, error=features.llm_error)

        og = banner_results.get(i)
        if og:
            article.banner_image_url = og
            if 'banner_image_url' not in update_fields:
                update_fields.append('banner_image_url')

        to_save.append(article)
        location = features.location or '?'
        category = '/'.join(filter(None, [features.category, features.sub_category]))
        logger.info(f'[process] {article.title[:70]} → {category} @ {location}')

    if to_save:
        Article.objects.bulk_update(to_save, update_fields, batch_size=500)

    return len(to_save)
