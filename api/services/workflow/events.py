import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Articles are pre-bucketed into windows this wide (days) before semantic
# sub-clustering, so a multi-day event (published_on spread across a few
# days) can still land in one cluster instead of being split by calendar day.
CLUSTER_DATE_WINDOW_DAYS = 3


def _date_window_key(dt: datetime) -> str:
    """Start-of-window date string for *dt*, chunked in CLUSTER_DATE_WINDOW_DAYS blocks."""
    ordinal = dt.toordinal()
    window_start_ordinal = ordinal - (ordinal % CLUSTER_DATE_WINDOW_DAYS)
    return date.fromordinal(window_start_ordinal).isoformat()


def _aggregate_llm_usage(articles: list) -> dict:
    """Sum token counts from constituent articles, grouped by provider."""
    by_provider: dict[str, dict] = {}
    for a in articles:
        u = getattr(a, 'llm_usage', None) or {}
        if not u or not u.get('provider'):
            continue
        p = u['provider']
        if p not in by_provider:
            by_provider[p] = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        by_provider[p]['prompt_tokens']     += u.get('prompt_tokens', 0)
        by_provider[p]['completion_tokens'] += u.get('completion_tokens', 0)
        by_provider[p]['total_tokens']      += u.get('total_tokens', 0)
    if not by_provider:
        return {}
    return {
        'prompt_tokens':     sum(v['prompt_tokens']     for v in by_provider.values()),
        'completion_tokens': sum(v['completion_tokens'] for v in by_provider.values()),
        'total_tokens':      sum(v['total_tokens']      for v in by_provider.values()),
        'by_provider':       by_provider,
        'article_count':     len(articles),
    }


def aggregate_events(hours: int = 24, min_articles: int = 1) -> tuple[int, int]:
    """Group processed Articles by (location, category, day) into Events.
    Returns (created_count, updated_count).
    """
    from core.models import Article, Event

    run_started = time.monotonic()
    lookback = timezone.now() - timedelta(hours=hours)
    logger.info('[aggregate] starting run: hours=%d min_articles=%d lookback=%s', hours, min_articles, lookback)

    # defer('content'): the week-wide window can be thousands of rows and only
    # each sub-cluster's representative article's content is ever read (a
    # deferred-field load per event — cheap next to carrying every body around).
    articles = list(
        Article.objects.filter(
            processed_on__isnull=False,
            location__isnull=False,
            published_on__gte=lookback,
        ).exclude(location='').defer('content')
    )
    logger.info('[aggregate] fetched %d located article(s) in window', len(articles))

    skipped_no_location = Article.objects.filter(
        processed_on__isnull=False, location__isnull=True, published_on__gte=lookback,
    ).count()
    if skipped_no_location:
        logger.info(
            '[aggregate] %d processed article(s) in window have no location — excluded from '
            'events. The geocode repair stage recovers them.', skipped_no_location,
        )

    if not articles:
        logger.info('[aggregate] no located articles — nothing to do')
        return 0, 0

    from services.processing.clustering import get_clusterer

    # Group by (city, country, category, N-day window) then semantic sub-cluster —
    # windowing (not exact calendar day) lets a story that runs a few days
    # land in one bucket instead of being split at the day boundary.
    buckets: dict[tuple[str, str, str, str], list] = defaultdict(list)
    for article in articles:
        llm = (article.extra_data or {}).get('llm', {})
        city = llm.get('city') or ''
        country = llm.get('country') or ''
        category_key = article.category or 'general'
        window_key = _date_window_key(article.published_on)
        buckets[(city, country, category_key, window_key)].append(article)
    logger.info('[aggregate] grouped into %d bucket(s) (window=%dd)', len(buckets), CLUSTER_DATE_WINDOW_DAYS)

    clusterer = get_clusterer()
    cluster_started = time.monotonic()
    bucket_items = list(buckets.items())
    # One batched embedding pass across every bucket (far cheaper on CPU than one
    # encode() call per bucket) — see SemanticClusterer.cluster_many.
    clustered_per_bucket = clusterer.cluster_many([group for _, group in bucket_items])

    sub_groups: list[list] = []
    for i, ((city, country, category_key, window_key), group) in enumerate(bucket_items, start=1):
        clustered = clustered_per_bucket[i - 1]
        logger.info(
            '[aggregate] cluster bucket %d/%d (%s, %s, %s, %s): %d article(s) -> %d sub-cluster(s)',
            i, len(bucket_items), city or '-', country or '-', category_key, window_key,
            len(group), len(clustered),
        )
        sub_groups.extend(clustered)
    logger.info(
        '[aggregate] clustering done: %d bucket(s) -> %d sub-cluster(s) in %.2fs total',
        len(bucket_items), len(sub_groups), time.monotonic() - cluster_started,
    )

    created_count = updated_count = 0

    for gi, group in enumerate(sub_groups, start=1):
        if gi == 1 or gi % 25 == 0:
            logger.info('[aggregate] processing sub-cluster %d/%d', gi, len(sub_groups))

        if len(group) < min_articles:
            logger.debug('[aggregate] skip sub-cluster %d/%d: below min_articles (%d)', gi, len(sub_groups), len(group))
            continue

        llm = (group[0].extra_data or {}).get('llm', {})
        city = llm.get('city') or ''
        country = llm.get('country') or ''

        location = ', '.join(filter(None, [city, country])) or (group[0].location or '')
        if not location:
            logger.debug('[aggregate] skip sub-cluster %d/%d: no resolvable location', gi, len(sub_groups))
            continue

        representative = max(group, key=lambda a: a.event_intensity or 0)
        sentiments = [a.sentiment for a in group if a.sentiment is not None]
        finbert_sentiments = [a.finbert_sentiment for a in group if a.finbert_sentiment is not None]
        intensities = [a.event_intensity for a in group if a.event_intensity is not None]
        avg_sentiment = round(sum(sentiments) / len(sentiments), 4) if sentiments else None
        avg_finbert_sentiment = (
            round(sum(finbert_sentiments) / len(finbert_sentiments), 4) if finbert_sentiments else None
        )
        base_intensity = round(sum(intensities) / len(intensities), 4) if intensities else None
        # Corroboration boost: more articles covering the same event → higher importance.
        corroboration_boost = min(len(group) / 10.0, 1.0) * 0.3
        avg_intensity = round(min((base_intensity or 0) + corroboration_boost, 1.0), 4) if base_intensity is not None else None

        started_at = min(a.published_on for a in group)
        latest_article_at = max(a.published_on for a in group)
        article_ids = [str(a.id) for a in group]
        source_codes = list({a.source_code for a in group})

        categories = [a.category for a in group if a.category]
        category = max(set(categories), key=categories.count) if categories else 'general'
        sub_categories = sorted({a.sub_category for a in group if a.sub_category})

        from services.forecasting.routing import route_event_to_weighted_symbols
        route_sentiment = avg_finbert_sentiment if avg_finbert_sentiment is not None else avg_sentiment
        route_started = time.monotonic()
        affected_indicators = route_event_to_weighted_symbols(
            category, location, [], sub_categories, route_sentiment,
        )
        route_elapsed = time.monotonic() - route_started
        if route_elapsed > 0.5:
            logger.warning(
                '[aggregate] sub-cluster %d/%d: routing took %.2fs (location=%s, category=%s)',
                gi, len(sub_groups), route_elapsed, location, category,
            )
        llm_usage = _aggregate_llm_usage(group)

        lats = [a.latitude for a in group if a.latitude is not None]
        lngs = [a.longitude for a in group if a.longitude is not None]
        lat = round(sum(lats) / len(lats), 6) if lats else representative.latitude
        lng = round(sum(lngs) / len(lngs), 6) if lngs else representative.longitude

        rep_translations = getattr(representative, 'translations', {}) or {}
        event_translations: dict = {}
        for lang, fields in rep_translations.items():
            if not isinstance(fields, dict):
                continue
            lang_city = fields.get('city') or ''
            lang_country = fields.get('country') or ''
            lang_location = ', '.join(p for p in [lang_city, lang_country] if p) or location
            event_translations[lang] = {
                'title': fields.get('title') or representative.title,
                'location_name': lang_location,
            }

        # Upsert on location_name + calendar day.
        # Use explicit datetime range — MongoDB backend does not support __date lookups.
        day_start = datetime(started_at.year, started_at.month, started_at.day, tzinfo=started_at.tzinfo)
        day_end = day_start + timedelta(days=1)

        event = Event.objects.filter(
            location_name=location,
            category=category,
            started_at__gte=day_start,
            started_at__lt=day_end,
        ).first()

        created = False
        if event is None:
            try:
                Event.objects.create(
                    title=representative.title,
                    content=representative.content,
                    category=category,
                    location_name=location,
                    latitude=lat,
                    longitude=lng,
                    started_at=started_at,
                    latest_article_at=latest_article_at,
                    article_count=len(group),
                    avg_sentiment=avg_sentiment,
                    avg_finbert_sentiment=avg_finbert_sentiment,
                    avg_intensity=avg_intensity,
                    article_ids=article_ids,
                    source_codes=source_codes,
                    sub_categories=sub_categories,
                    affected_indicators=affected_indicators,
                    translations=event_translations,
                    llm_usage=llm_usage,
                )
                created = True
                created_count += 1
                logger.info(
                    '[aggregate] Created  %s [%s] — %d article(s) (sub-cluster %d/%d)',
                    location, category, len(group), gi, len(sub_groups),
                )
            except Exception:
                # Usually a concurrent run already created this event — re-fetch
                # and update below. Log it: if the re-fetch also comes up empty,
                # the create failed for a real reason (validation, connection)
                # and this sub-cluster is otherwise silently dropped.
                logger.exception(
                    '[aggregate] Event create failed for %s [%s] — retrying as update',
                    location, category,
                )
                event = Event.objects.filter(
                    location_name=location,
                    category=category,
                    started_at__gte=day_start,
                    started_at__lt=day_end,
                ).first()
                if event is None:
                    logger.error(
                        '[aggregate] sub-cluster %d/%d dropped: create failed and no '
                        'existing event found for %s [%s]', gi, len(sub_groups), location, category,
                    )

        if not created and event is not None:
            event.title = representative.title
            event.category = category
            event.latitude = lat
            event.longitude = lng
            event.latest_article_at = latest_article_at
            event.article_count = len(group)
            event.avg_sentiment = avg_sentiment
            event.avg_finbert_sentiment = avg_finbert_sentiment
            event.avg_intensity = avg_intensity
            event.article_ids = article_ids
            event.source_codes = source_codes
            event.sub_categories = sub_categories
            event.affected_indicators = affected_indicators
            event.translations = {**(event.translations or {}), **event_translations}
            event.llm_usage = llm_usage
            event.save()
            updated_count += 1
            logger.info(
                '[aggregate] Updated  %s [%s] — %d article(s) (sub-cluster %d/%d)',
                location, category, len(group), gi, len(sub_groups),
            )

    logger.info(
        '[aggregate] run complete: created=%d updated=%d in %.2fs total',
        created_count, updated_count, time.monotonic() - run_started,
    )
    return created_count, updated_count


def pipeline_coverage() -> list[dict]:
    """Per-stage count of records pending at a step + a sample error.

    Built directly from the stage registry (services/stages.py): each row's
    ``need`` comes from the stage's own ``pending_count`` — the SAME predicate
    the tick dispatcher uses — so the displayed count, the Reprocess button's
    effect, and what the cron actually dispatches cannot drift apart.

    Returns one dict per row: {stage, model, label, need, action, error_sample,
    last_dispatch}. ``action`` is the stage name posted back to
    admin_dashboard._handle_reprocess (None for informational rows).
    """
    from core.models import Article, Event
    from services.stages import REGISTRY, last_dispatched_at

    def _err_sample(model, stage_key):
        try:
            row = model.objects.filter(**{f'stage_status__{stage_key}__ok': False}).only('stage_status').first()
            if row:
                return ((row.stage_status or {}).get(stage_key) or {}).get('error')
        except Exception:  # noqa: BLE001 — nested JSON lookup may be unsupported
            pass
        return None

    _models = {'article': Article, 'event': Event}

    out: list[dict] = []
    for stage in REGISTRY.values():
        if stage.singleton or not stage.coverage:
            continue  # no per-record pending set (aggregate) / not a stuck signal (fetch)
        error_sample = None
        if stage.error_stage_key and stage.model in _models:
            error_sample = _err_sample(_models[stage.model], stage.error_stage_key)
        last = last_dispatched_at(stage)
        out.append({
            'stage': stage.name, 'model': stage.model, 'label': stage.label,
            'need': stage.pending_count() if stage.enabled() else 0,
            'action': stage.name if stage.enabled() else None,
            'error_sample': error_sample,
            'last_dispatch': last,
        })

        if stage.name == 'score':
            # Informational: articles deliberately excluded by the importance
            # cutoff — no action, the same filter excludes them from dispatch.
            from django.conf import settings
            min_score = getattr(settings, 'ARTICLE_MIN_IMPORTANCE_TO_PROCESS', 0)
            low_score_count = (
                Article.objects.filter(
                    processed_on__isnull=True,
                    importance_score__isnull=False, importance_score__lt=min_score,
                ).count()
                if min_score > 0 else 0
            )
            out.append({
                'stage': 'low_score', 'model': 'article',
                'label': 'Unprocessed, below importance threshold (by design)',
                'need': low_score_count, 'action': None, 'error_sample': None,
                'last_dispatch': None,
            })
    return out
