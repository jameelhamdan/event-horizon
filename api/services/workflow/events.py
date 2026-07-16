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


def iter_aggregate_windows(start: datetime, end: datetime, window_days: int = 30):
    """Yield (window_start, window_end) pairs covering [start, end) for
    historical aggregation, with every boundary aligned to the same
    CLUSTER_DATE_WINDOW_DAYS ordinal grid _date_window_key uses — so a
    clustering bucket is never split across two aggregate_events() calls
    (a bucket whose articles straddle a call boundary would cluster
    incompletely on both sides)."""
    def _align_down(dt: datetime) -> datetime:
        ordinal = dt.toordinal() - (dt.toordinal() % CLUSTER_DATE_WINDOW_DAYS)
        d = date.fromordinal(ordinal)
        return datetime(d.year, d.month, d.day, tzinfo=dt.tzinfo)

    step = timedelta(days=max(window_days - window_days % CLUSTER_DATE_WINDOW_DAYS,
                              CLUSTER_DATE_WINDOW_DAYS))
    current = _align_down(start)
    while current < end:
        yield current, min(current + step, end)
        current += step


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


def aggregate_events(
    hours: int = 24,
    min_articles: int = 1,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[int, int]:
    """Group processed Articles by (location, category, day) into Events.
    Returns (created_count, updated_count).

    Default window is the trailing ``hours`` (the live pipeline's aggregate
    stage). Pass explicit ``start``/``end`` (both required together) to
    aggregate a historical range instead — used by aggregate_history_task to
    surface backfilled articles as Events, which the trailing window can never
    reach. Safe to re-run over the same range: the upsert below is keyed on
    (location_name, category, calendar day).
    """
    from core.models import Article, Event

    if (start is None) != (end is None):
        raise ValueError('aggregate_events: start and end must be given together')

    run_started = time.monotonic()
    if start is not None:
        window = {'published_on__gte': start, 'published_on__lt': end}
        logger.info('[aggregate] starting run: window=[%s, %s) min_articles=%d', start, end, min_articles)
    else:
        window = {'published_on__gte': timezone.now() - timedelta(hours=hours)}
        logger.info(
            '[aggregate] starting run: hours=%d min_articles=%d lookback=%s',
            hours, min_articles, window['published_on__gte'],
        )

    # defer('content', 'entities'): the week-wide window can be tens of thousands
    # of rows. Only the representative article's content is ever read (a deferred
    # per-event load, cheap next to carrying every body around); entities is an
    # unused always-empty field, never read here — deferring both keeps the
    # biggest recurring in-memory load off these Article fields.
    articles = list(
        Article.objects.filter(
            processed_on__isnull=False,
            location__isnull=False,
            **window,
        ).exclude(location='').defer('content', 'entities')
    )
    logger.info('[aggregate] fetched %d located article(s) in window', len(articles))

    skipped_no_location = Article.objects.filter(
        processed_on__isnull=False, location__isnull=True, **window,
    ).count()
    if skipped_no_location:
        logger.info(
            '[aggregate] %d processed article(s) in window have no location — excluded from '
            'events. The geocode repair stage recovers them.', skipped_no_location,
        )

    if not articles:
        logger.info('[aggregate] no located articles — nothing to do')
        return 0, 0

    # Prefetch every Event already in this window once, keyed the same way the
    # per-sub-cluster upsert below looks them up — replaces N per-sub-cluster
    # "does this event exist" queries with a single one. .only() the upsert-key
    # fields: every other field the update path writes (title, translations,
    # llm_usage, ...) gets explicitly reassigned in that loop before save, so
    # leaving them deferred here doesn't affect the eventual bulk_update.
    existing_events: dict[tuple[str, str, str], 'Event'] = {}
    for ev in Event.objects.filter(**{
        'started_at__gte': window['published_on__gte'],
        **({'started_at__lt': window['published_on__lt']} if 'published_on__lt' in window else {}),
    }).only('location_name', 'category', 'started_at'):
        day_key = ev.started_at.date().isoformat()
        existing_events[(ev.location_name, ev.category, day_key)] = ev

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
    events_to_update: dict = {}
    # bulk_update field list, derived from common_fields on first update so it
    # can't drift from what the update branch actually writes (+ the two fields
    # handled specially there). Captured in-loop; only read when non-empty.
    update_field_names: list[str] = []

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

        from services.forecasting.routing import route_event_to_weighted_symbols, select_route_sentiment
        # Empty topic_slugs — topics aren't known until the tag stage, which then
        # re-routes with the real slugs (higher signal). This inline pass gives the
        # freshly-created event indicators immediately instead of a tag-cadence gap.
        route_sentiment = select_route_sentiment(avg_finbert_sentiment, avg_sentiment)
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

        # Fields written on BOTH create and update, defined once so the two paths
        # (and the bulk_update field list below) can't silently drift apart.
        # Excluded on purpose: content/location_name/started_at (set-once, only on
        # create) and translations/updated_on (update merges rather than replaces —
        # handled explicitly in each branch).
        common_fields = {
            'title': representative.title,
            'category': category,
            'latitude': lat,
            'longitude': lng,
            'latest_article_at': latest_article_at,
            'article_count': len(group),
            'avg_sentiment': avg_sentiment,
            'avg_finbert_sentiment': avg_finbert_sentiment,
            'avg_intensity': avg_intensity,
            'article_ids': article_ids,
            'source_codes': source_codes,
            'sub_categories': sub_categories,
            'affected_indicators': affected_indicators,
            'is_routed': bool(affected_indicators),
            'llm_usage': llm_usage,
        }

        # Upsert on location_name + calendar day. day_start/day_end kept only for
        # the create-race fallback query below (MongoDB backend does not support
        # __date lookups, so an explicit range is needed there).
        day_start = datetime(started_at.year, started_at.month, started_at.day, tzinfo=started_at.tzinfo)
        day_end = day_start + timedelta(days=1)
        upsert_key = (location, category, started_at.date().isoformat())

        event = existing_events.get(upsert_key)

        created = False
        if event is None:
            try:
                event = Event.objects.create(
                    **common_fields,
                    content=representative.content,
                    location_name=location,
                    started_at=started_at,
                    translations=event_translations,
                )
                created = True
                created_count += 1
                existing_events[upsert_key] = event
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
            for field, value in common_fields.items():
                setattr(event, field, value)
            event.translations = {**(event.translations or {}), **event_translations}
            event.updated_on = timezone.now()  # bulk_update below bypasses auto_now
            if not update_field_names:
                update_field_names = list(common_fields) + ['translations', 'updated_on']
            events_to_update[event.pk] = event
            updated_count += 1
            logger.info(
                '[aggregate] Updated  %s [%s] — %d article(s) (sub-cluster %d/%d)',
                location, category, len(group), gi, len(sub_groups),
            )

    if events_to_update:
        Event.objects.bulk_update(
            list(events_to_update.values()), update_field_names, batch_size=500,
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

    Returns one dict per row: {stage, model, label, need, age, action,
    error_sample, last_dispatch}. ``need`` is the total pending; ``age`` is the
    same set bucketed by how long it's been waiting (<1h / 1h-24h / 24h-1w / >1w,
    from services.stages.stage_age_buckets — None for stages without a queryset).
    ``action`` is the stage name posted back to admin_dashboard._handle_reprocess
    (None for informational rows).
    """
    from core.models import Article, Event
    from services.stages import REGISTRY, last_dispatched_at, stage_age_buckets

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
        error_sample = None
        if stage.error_stage_key and stage.model in _models:
            error_sample = _err_sample(_models[stage.model], stage.error_stage_key)
        last = last_dispatched_at(stage)
        # fetch re-fetches every enabled source each cadence (not a stuck-records
        # set) and aggregate is a singleton (no per-record queue). Show both for
        # visibility, but as informational rows: no age buckets, no Reprocess
        # button, and no pending backlog (aggregate → "—"; fetch → source count).
        informational = stage.singleton or not stage.coverage
        enabled = stage.enabled()
        out.append({
            'stage': stage.name, 'model': stage.model, 'label': stage.label,
            'need': None if stage.singleton else (stage.pending_count() if enabled else 0),
            'age': None if informational else (stage_age_buckets(stage) if enabled else None),
            'action': None if informational else (stage.name if enabled else None),
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
