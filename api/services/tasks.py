"""Task functions for the ingestion and aggregation pipeline.

These are plain Python functions enqueued via django-rq (services.queue.enqueue).
"""

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from services.workflow import (
    fetch_articles,
    process_articles,
    aggregate_events,
    tag_events_by_ids,
    refresh_topics,
    retroactive_tag_topic,
    discover_topics_from_events,
    _needs_tagging,
)

logger = logging.getLogger(__name__)

DEFAULT_FETCH_MINUTES = 20          # look-back window for fetch tasks (2× interval)
DEFAULT_PROCESS_LIMIT = 1000
DEFAULT_AGGREGATE_HOURS = 24
DEFAULT_AGGREGATE_MIN_ARTICLES = 1

# Fan-out tuning — dispatchers cap records enqueued per tick; chunk size amortises overhead.
PROCESS_CHUNK_SIZE = 1
PROCESS_DISPATCH_LIMIT = 500
TAG_DISPATCH_LIMIT = 500
ROUTE_DISPATCH_LIMIT = 500
TAG_CHUNK_SIZE = 10     # one LLM call per chunk (LLMTopicMatcher.BATCH_SIZE)
ROUTE_CHUNK_SIZE = 10
BOOTSTRAP_ARTICLE_YEARS = 1


# ── Text pipeline ─────────────────────────────────────────────────────────────

def aggregate_events_task(
    hours: int = DEFAULT_AGGREGATE_HOURS,
    min_articles: int = DEFAULT_AGGREGATE_MIN_ARTICLES,
) -> tuple[int, int]:
    result = aggregate_events(hours=hours, min_articles=min_articles)
    from services.queue import enqueue
    enqueue(dispatch_tag_topics_task, hours, queue='default')
    return result


# ── Fan-out: dispatcher → per-record worker (WA3) ────────────────────────────────
# A light dispatcher (default queue) selects pending records and enqueues one worker
# job per record/chunk so the queue spreads work across all workers. Workers are
# idempotent. Downstream steps run on their own schedule (eventually-consistent).

def fetch_source_task(source_code: str, start_date: datetime | None = None) -> int:
    """Fetch one source. Idempotent per source (RSS de-dupes on save)."""
    now = datetime.now(dt_timezone.utc)
    if start_date is None:
        start_date = now - timedelta(minutes=DEFAULT_FETCH_MINUTES)
    return fetch_articles(source_code, start_date)


def dispatch_fetch_task(start_date: datetime | None = None) -> int:
    """Enqueue one fetch_source_task per enabled source. Returns sources dispatched."""
    from core import models as core_models
    from services.queue import enqueue, make_retry

    now = datetime.now(dt_timezone.utc)
    if start_date is None:
        start_date = now - timedelta(minutes=DEFAULT_FETCH_MINUTES)
    codes = list(core_models.Source.objects.filter(is_enabled=True).values_list('code', flat=True))
    retry = make_retry()
    for code in codes:
        enqueue(fetch_source_task, code, start_date, queue='default', retry=retry)
    return len(codes)


def process_articles_chunk_task(ids: list, only_failed: bool = False) -> int:
    """Process a chunk of articles by id (idempotent)."""
    return process_articles(ids=ids, only_failed=only_failed)


def process_article_task(article_id, only_failed: bool = False) -> int:
    """Process a single article by id (idempotent)."""
    return process_articles(ids=[article_id], only_failed=only_failed)


def dispatch_process_articles_task(limit: int | None = None, only_failed: bool = False, chunk_size: int | None = None) -> int:
    """Select unprocessed (or un-located) articles and fan them out. Returns jobs enqueued."""
    from core import models as core_models
    from services.queue import enqueue, make_retry

    limit = limit or PROCESS_DISPATCH_LIMIT
    chunk_size = max(1, chunk_size or PROCESS_CHUNK_SIZE)
    if only_failed:
        qs = core_models.Article.objects.filter(processed_on__isnull=False, location__isnull=True)
        ids = [a.id for a in qs.only('id', 'extra_data') if not (a.extra_data or {}).get('geo_failed')][:limit]
    else:
        from django.conf import settings as _s
        from services.workflow.articles import _apply_min_score_filter
        qs = core_models.Article.objects.filter(processed_on__isnull=True)
        qs = _apply_min_score_filter(qs, _s.ARTICLE_MIN_IMPORTANCE_TO_PROCESS)
        ids = list(qs.order_by('-importance_score').values_list('id', flat=True)[:limit])
    if not ids:
        return 0
    retry = make_retry()
    enq = 0
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        if len(chunk) == 1:
            enqueue(process_article_task, chunk[0], only_failed, queue='heavy', retry=retry)
        else:
            enqueue(process_articles_chunk_task, chunk, only_failed, queue='heavy', retry=retry)
        enq += 1
    return enq


def tag_events_chunk_task(event_ids: list) -> int:
    """Tag a chunk of events by id (one LLM call). Idempotent."""
    return tag_events_by_ids(event_ids)


def dispatch_tag_topics_task(hours: int = DEFAULT_AGGREGATE_HOURS, force_retag: bool = False,
                             limit: int | None = None) -> int:
    """Select events needing tags and fan them out in chunks of 10. Returns jobs enqueued."""
    from core import models as core_models
    from services.queue import enqueue, make_retry
    limit = limit or TAG_DISPATCH_LIMIT
    lookback = datetime.now(dt_timezone.utc) - timedelta(hours=hours)
    qs = core_models.Event.objects.filter(started_at__gte=lookback).only('pk', 'topics', 'topics_source')
    if not force_retag:
        events = [e for e in qs if _needs_tagging(e.topics) or e.topics_source == 'keyword']
        ids = [e.pk for e in events[:limit]]
    else:
        ids = list(qs.values_list('pk', flat=True)[:limit])
    if not ids:
        return 0
    retry = make_retry()
    enq = 0
    for i in range(0, len(ids), TAG_CHUNK_SIZE):
        enqueue(tag_events_chunk_task, ids[i:i + TAG_CHUNK_SIZE], queue='heavy', retry=retry)
        enq += 1
    return enq


def route_events_chunk_task(event_ids: list, source: str | None = None) -> int:
    """Route a chunk of events by id. Idempotent."""
    from django.conf import settings
    from core import models as core_models
    from services.routing import route_events

    src = source or settings.FORECAST_ROUTER
    events = list(core_models.Event.objects.filter(pk__in=list(event_ids)))
    return route_events(events, source=src)


def dispatch_route_events_task(hours: int = 168, source: str | None = None,
                               limit: int | None = None) -> int:
    """Select recent events and fan out routing in chunks of 10. Returns jobs enqueued."""
    from core import models as core_models
    from services.queue import enqueue, make_retry

    limit = limit or ROUTE_DISPATCH_LIMIT
    start = datetime.now(dt_timezone.utc) - timedelta(hours=hours)
    ids = list(core_models.Event.objects.filter(started_at__gte=start)
               .values_list('pk', flat=True)[:limit])
    if not ids:
        return 0
    retry = make_retry()
    enq = 0
    for i in range(0, len(ids), ROUTE_CHUNK_SIZE):
        enqueue(route_events_chunk_task, ids[i:i + ROUTE_CHUNK_SIZE], source, queue='heavy', retry=retry)
        enq += 1
    return enq


# ── Configurationless first-load bootstrap (WA4) ─────────────────────────────────

def bootstrap_initial_data_task(force: bool = False) -> int:
    """One-time, idempotent first-load backfill so deployment is configurationless.

    Enqueues full price history + top-10/week article backfill for every enabled RSS
    source, then trains/runs the forecast. Guarded by a persisted cache flag and a
    PriceBar-presence heuristic so it runs exactly once. Trigger manually or via admin dashboard.
    """
    import logging
    from django.core.cache import cache
    from django.conf import settings
    from core import models as core_models
    from services.queue import enqueue, make_retry

    log = logging.getLogger(__name__)
    FLAG = 'bootstrap:initial_data:done'
    if not force:
        if cache.get(FLAG):
            return 0
        if core_models.PriceBar.objects.exists():
            cache.set(FLAG, True, timeout=None)
            return 0

    retry = make_retry()
    now = datetime.now(dt_timezone.utc)
    start = now - timedelta(days=365 * BOOTSTRAP_ARTICLE_YEARS)

    # Long one-shot seeds go on the bulk queue so they don't block the live pipeline.
    enqueue(backfill_prices_task, years=10, queue='bulk', job_timeout=-1, retry=retry)
    # top_n=None → each source's per-week cap derives from its weight (10–25 by priority).
    enqueue(backfill_all_sources_task, start, now, None, queue='bulk', job_timeout=-1, retry=retry)
    if settings.FORECAST_ENABLED:
        enqueue(train_forecast_model_task, queue='bulk', job_timeout=-1, retry=retry)
        enqueue(run_forecast_task, queue='bulk', job_timeout=-1)

    cache.set(FLAG, True, timeout=None)
    log.info('[bootstrap] initial data backfill enqueued (article window %dy)', BOOTSTRAP_ARTICLE_YEARS)
    return 1


# ── Topic tasks ────────────────────────────────────────────────────────────────

def refresh_topics_task() -> int:
    return refresh_topics()


def retroactive_tag_topic_task(slug: str, lookback_hours: int = 72) -> int:
    return retroactive_tag_topic(slug=slug, lookback_hours=lookback_hours)


def discover_topics_task(hours: int = 6) -> int:
    return discover_topics_from_events(hours=hours)


# ── Stream tasks ───────────────────────────────────────────────────────────────

def fetch_prices_task() -> int:
    from django.conf import settings
    if not settings.STREAM_PRICES_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('prices')


def fetch_notams_task() -> int:
    from django.conf import settings
    if not settings.STREAM_NOTAM_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('notam')


def fetch_earthquakes_task() -> int:
    from django.conf import settings
    if not settings.STREAM_EARTHQUAKE_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('earthquakes')


def fetch_forex_task() -> int:
    from django.conf import settings
    if not settings.STREAM_FOREX_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('forex')


# ── Health monitoring (A1 / A5) ─────────────────────────────────────────────────

def pipeline_health_task() -> dict:
    """Warn when pipeline outputs go stale. Logs only — surfaced via Sentry / log alerts.

    Covers the highest-risk silent failures (A1): no fetched articles, stale stream
    data on undocumented APIs, and — the single-source topic risk (A5) — zero current
    topics, which means the Wikipedia scraper has likely broken.
    """
    import logging
    from django.conf import settings
    from core import models as core_models

    log = logging.getLogger('pipeline.health')
    now = datetime.now(dt_timezone.utc)
    report: dict = {}

    def _aware(dt):
        return dt.replace(tzinfo=dt_timezone.utc) if dt and dt.tzinfo is None else dt

    def _check_stale(model, field, minutes, label):
        latest = _aware(model.objects.order_by(f'-{field}').values_list(field, flat=True).first())
        ok = latest is not None and latest >= now - timedelta(minutes=minutes)
        report[label] = {'latest': latest.isoformat() if latest else None, 'ok': ok}
        if not ok:
            log.warning('[health] %s stale — latest=%s (threshold=%dm)', label, latest, minutes)

    _check_stale(core_models.Article, 'created_on', 180, 'articles')
    if settings.STREAM_PRICES_ENABLED:
        _check_stale(core_models.PriceTick, 'occurred_at', 60, 'prices')
    if settings.STREAM_EARTHQUAKE_ENABLED:
        _check_stale(core_models.EarthquakeRecord, 'occurred_at', 360, 'earthquakes')

    # A5: Wikipedia is the only topic source — zero current topics ⇒ likely broken.
    current_topics = core_models.Topic.objects.filter(is_current=True).count()
    report['current_topics'] = current_topics
    if current_topics == 0:
        log.warning('[health] zero current topics — the Wikipedia topic source may be broken')

    return report


# ── Backfill tasks ─────────────────────────────────────────────────────────────

def _weighted_top_n(weight: float | None, lo: int = 10, hi: int = 25) -> int:
    """Map a Source.weight (0.1–2.0 credibility multiplier) to a per-week article cap.

    Higher-priority sources keep more articles per week. weight 0.1 → ``lo``,
    1.0 → ~17, 2.0 → ``hi``. Reuses the existing credibility signal so backfill
    volume tracks source priority with no extra config.
    """
    if weight is not None and weight == 0:
        return 0  # weight=0 means suppressed — skip backfill entirely
    w = 1.0 if weight is None else min(max(weight, 0.1), 2.0)
    return round(lo + (w - 0.1) / 1.9 * (hi - lo))


def backfill_history_task(
    source_code: str,
    start_date: datetime,
    end_date: datetime,
    top_n: int | None = None,
    delay_seconds: float = 0.5,
    dry_run: bool = False,
    resume: bool = False,
    progress=None,
) -> dict:
    """
    Backfill top-N articles per ISO week for a source.

    Enqueue with job_timeout=-1 (no cap) since multi-year backtracks can take
    longer than the standard 30-minute task timeout.

    ``start_date`` / ``end_date`` accept either ``datetime`` objects or
    ``YYYY-MM-DD`` strings (the latter so the task is trivially enqueueable).
    ``top_n=None`` derives the per-week cap from the source's ``weight`` (10–25 by
    priority); pass an int to override for all weeks.
    ``resume`` skips ISO weeks already recorded in the Django cache checkpoint;
    ``progress`` is an optional ``callable(WeekResult)`` invoked per week (the
    management command passes one to echo per-week lines to stdout).

    Returns {'weeks': int, 'fetched': int, 'saved': int}.
    """
    import logging

    import core.models as m
    from django.core.cache import cache
    from services.data.historical import HistoricalBackfillService

    logger = logging.getLogger(__name__)

    start_date = _parse_backfill_date(start_date)
    end_date = _parse_backfill_date(end_date)

    source = m.Source.objects.get(code=source_code)
    resolved_top_n = top_n if top_n is not None else _weighted_top_n(source.weight)
    if resolved_top_n == 0:
        logger.info('backfill_history_task %s: skipped (weight=0)', source_code)
        return {'weeks': 0, 'fetched': 0, 'saved': 0}
    service = HistoricalBackfillService(
        source=source,
        start_date=start_date,
        end_date=end_date,
        top_n=resolved_top_n,
        delay_seconds=delay_seconds,
    )

    checkpoint_key = f'backfill:{source_code}:{start_date.date()}:{end_date.date()}:done'
    resume_weeks: set[str] = (cache.get(checkpoint_key) or set()) if resume else set()

    total_weeks = total_fetched = total_saved = 0
    for result in service.run(resume_weeks=resume_weeks, dry_run=dry_run):
        total_weeks += 1
        total_fetched += result.fetched
        total_saved += result.saved
        if progress is not None:
            progress(result)
        if resume and not dry_run:
            resume_weeks.add(result.week_start.isoformat())
            cache.set(checkpoint_key, resume_weeks, timeout=None)

    summary = {'weeks': total_weeks, 'fetched': total_fetched, 'saved': total_saved}
    logger.info('backfill_history_task %s done: %s', source_code, summary)
    return summary


def backfill_all_sources_task(
    start_date: datetime,
    end_date: datetime,
    top_n: int | None = None,
    delay_seconds: float = 0.5,
    dry_run: bool = False,
    resume: bool = False,
    progress=None,
    on_source_start=None,
) -> dict:
    """
    Backfill every enabled RSS source over the same date range, sequentially.

    Only ``SourceType.RSS`` sources are eligible (the historical backfill has no
    strategy for other types); disabled sources are skipped. Running sources one
    at a time keeps API rate-limit pressure bounded. ``top_n=None`` lets each
    source derive its per-week cap from its own ``weight`` (10–25 by priority);
    pass an int to force the same cap everywhere. ``on_source_start`` is an
    optional ``callable(Source)`` invoked before each source; ``progress`` is
    forwarded per week to :func:`backfill_history_task`.

    Enqueue with job_timeout=-1 — backfilling many sources can run for hours.

    Returns {'sources': int, 'weeks': int, 'fetched': int, 'saved': int,
             'per_source': {code: {weeks, fetched, saved}}}.
    """
    import logging

    import core.models as m

    logger = logging.getLogger(__name__)

    start_date = _parse_backfill_date(start_date)
    end_date = _parse_backfill_date(end_date)

    sources = list(
        m.Source.objects.filter(type=m.SourceType.RSS, is_enabled=True).order_by('code')
    )

    totals = {'sources': 0, 'weeks': 0, 'fetched': 0, 'saved': 0, 'per_source': {}}
    for source in sources:
        if on_source_start is not None:
            on_source_start(source)
        summary = backfill_history_task(
            source.code, start_date, end_date,
            top_n=top_n, delay_seconds=delay_seconds, dry_run=dry_run, resume=resume,
            progress=progress,
        )
        totals['per_source'][source.code] = summary
        totals['sources'] += 1
        for key in ('weeks', 'fetched', 'saved'):
            totals[key] += summary[key]

    logger.info(
        'backfill_all_sources_task done: %s source(s), %s saved',
        totals['sources'], totals['saved'],
    )
    return totals


def _parse_backfill_date(value) -> datetime:
    """Normalize a backfill bound to a UTC datetime (accepts YYYY-MM-DD strings)."""
    if isinstance(value, datetime):
        return value
    d = datetime.strptime(value, '%Y-%m-%d')
    return d.replace(tzinfo=dt_timezone.utc)


def backfill_save_article_task(
    source_code: str,
    source_type: str,
    datum: dict,
    extra_data: dict,
    importance_score: float,
    fetch_body: bool = True,
) -> int:
    """Per-article backfill worker: fetch the body (so the article geocodes + renders
    on the map) and save one Article. Idempotent via get_or_create on source_url.

    Fanned out one-per-article from the backfill onto the light queue, so the worker
    pool provides the concurrency for the network-bound body fetch — no in-process
    threads. Returns 1 if a new Article was created, else 0.
    """
    import core.models as m
    from services.data.historical import fetch_article_body

    if m.Article.objects.filter(
        source_code=source_code, source_type=source_type, source_url=datum['source_url'],
    ).exists():
        return 0

    fields = {**datum}
    if fetch_body:
        body = fetch_article_body(datum['source_url'])
        if body:
            fields['content'] = body
    fields['extra_data'] = extra_data
    fields['importance_score'] = importance_score
    fields['importance_source'] = 'llm'
    _, created = m.Article.objects.get_or_create(
        source_code=source_code,
        source_type=source_type,
        source_url=datum['source_url'],
        defaults=fields,
    )
    return 1 if created else 0


# ── Forecasting tasks (event-fused symbol prediction) ────────────────────────────

def backfill_prices_task(
    symbols: list[str] | None = None, years: int = 10, full: bool = False,
) -> int:
    """Backfill daily OHLC PriceBar history for the indicator panel.

    Incremental by default (only the tail since the last stored bar is fetched), so
    the weekly scheduled run is cheap; the first run on an empty table pulls the full
    ``years`` window. ``full=True`` forces a complete re-pull.
    """
    from services.forecasting.history import backfill_all
    results = backfill_all(symbols=symbols, years=years, full=full)
    return sum(results.values())


def train_forecast_model_task() -> int:
    """Train the LightGBM classifier + regressor for every configured horizon."""
    from django.conf import settings
    if not settings.FORECAST_ENABLED:
        return 0
    from services.forecasting import features, model

    end = datetime.now(dt_timezone.utc)
    start = end - timedelta(days=settings.FORECAST_TRAIN_WINDOW_DAYS + 60)
    frame = features.build_training_frame(
        start=start, end=end, horizons=settings.FORECAST_HORIZONS_DAYS, include_events=True,
    )
    if frame.empty:
        return 0
    trained = 0
    for h in settings.FORECAST_HORIZONS_DAYS:
        try:
            model.train(frame, h)
            trained += 1
        except RuntimeError as exc:
            import logging
            logging.getLogger(__name__).warning('[forecast] train h%d skipped: %s', h, exc)
    return trained


def run_forecast_task() -> int:
    """Write one Forecast row per (panel symbol, horizon) from the latest models."""
    from django.conf import settings
    if not settings.FORECAST_ENABLED:
        return 0
    from services.forecasting import features, model
    from services.market_symbols import get_symbol_meta
    from core import models as core_models

    fm = features.build_feature_matrix(include_events=True)
    if fm.empty:
        return 0
    symbol_meta = get_symbol_meta()
    now = datetime.now(dt_timezone.utc)
    router = settings.FORECAST_ROUTER
    created = 0
    for h in settings.FORECAST_HORIZONS_DAYS:
        for p in model.predict(fm, h):
            stream_key = symbol_meta.get(p['symbol'], ('', ''))[0]
            core_models.Forecast.objects.create(
                symbol=p['symbol'], stream_key=stream_key, generated_at=now,
                as_of_date=p['as_of_date'], horizon_days=h, direction=p['direction'],
                proba_up=p['proba_up'], predicted_change_pct=p['predicted_change_pct'],
                predicted_price=p['predicted_price'], band_low=p['band_low'],
                band_high=p['band_high'], confidence=p['confidence'],
                current_value=p['current_value'], router_source=router,
                model_version=p['model_version'],
            )
            created += 1
    return created


# ── Article importance scoring ────────────────────────────────────────────────

def score_articles_task(hours: int = 2, article_ids: list | None = None) -> int:
    """
    LLM-score articles that have no importance_score.
    article_ids: when given, re-score exactly these articles (ignores hours).
    hours: when article_ids is None, score rows created in this window.
    """
    from django.conf import settings
    if not settings.ARTICLE_IMPORTANCE_SCORING_ENABLED:
        return 0
    from services.scoring import score_unscored_articles
    return score_unscored_articles(hours=hours, article_ids=article_ids)


def cleanup_low_importance_articles_task() -> int:
    """
    Delete unprocessed low-importance articles older than ARTICLE_CLEANUP_GRACE_HOURS.
    Approximates "gate before storage" without blocking the fetch path.
    """
    from django.conf import settings
    from core import models as core_models

    min_score   = settings.ARTICLE_MIN_IMPORTANCE
    grace_hours = settings.ARTICLE_CLEANUP_GRACE_HOURS
    cutoff      = datetime.now(dt_timezone.utc) - timedelta(hours=grace_hours)

    deleted, _ = core_models.Article.objects.filter(
        importance_score__isnull=False,
        importance_score__lt=min_score,
        processed_on__isnull=True,
        created_on__lt=cutoff,
    ).delete()
    logger.info('[cleanup] deleted %d low-importance unprocessed articles', deleted)
    return deleted


def prune_stale_articles_task() -> int:
    """
    Delete processed articles that could never contribute to an event:
    location not resolved AND older than ARTICLE_STALE_PROCESSED_DAYS.
    """
    from django.conf import settings
    from core import models as core_models

    stale_days = settings.ARTICLE_STALE_PROCESSED_DAYS
    cutoff     = datetime.now(dt_timezone.utc) - timedelta(days=stale_days)

    candidates = list(
        core_models.Article.objects.filter(
            location__isnull=True,
            processed_on__lt=cutoff,
        ).only('id', 'extra_data')
    )
    ids = [a.id for a in candidates if (a.extra_data or {}).get('geo_failed')]
    if not ids:
        return 0

    deleted, _ = core_models.Article.objects.filter(id__in=ids).delete()
    logger.info('[cleanup] pruned %d stale unlocated articles', deleted)
    return deleted


def adjust_source_weights_task() -> int:
    """
    Nudge Source.weight based on 30-day event yield rate.
    Sources whose articles consistently land in Events drift up; noisy sources drift down.
    weight_locked=True sources are skipped. Independent of ARTICLE_IMPORTANCE_SCORING_ENABLED.
    """
    from core import models as core_models

    cutoff  = datetime.now(dt_timezone.utc) - timedelta(days=30)
    sources = list(core_models.Source.objects.filter(is_enabled=True))
    if not sources:
        return 0

    # Load all article IDs referenced by recent events once — avoid per-source DB round-trips.
    event_article_ids_raw = list(
        core_models.Event.objects.filter(started_at__gte=cutoff)
        .values_list('article_ids', flat=True)
    )
    all_event_article_ids: set[str] = {
        str(aid)
        for article_ids in event_article_ids_raw
        for aid in (article_ids or [])
    }

    adjusted = 0
    for source in sources:
        if source.weight_locked:
            continue

        source_article_ids = list(
            core_models.Article.objects.filter(
                source_code=source.code,
                created_on__gte=cutoff,
            ).values_list('id', flat=True)
        )
        total = len(source_article_ids)
        if total < 10:
            continue

        event_count = sum(1 for aid in source_article_ids if str(aid) in all_event_article_ids)
        yield_rate  = event_count / total

        if yield_rate >= 0.3:
            new_weight = min(round(source.weight + 0.1, 2), 2.0)
        elif yield_rate < 0.1:
            new_weight = max(round(source.weight - 0.1, 2), 0.5)
        else:
            continue

        if abs(new_weight - source.weight) > 0.001:
            source.weight = new_weight
            source.save(update_fields=['weight'])
            logger.info(
                '[weights] %s: yield=%.0f%% → weight %.2f',
                source.code, yield_rate * 100, new_weight,
            )
            adjusted += 1

    return adjusted


def score_forecasts_task() -> int:
    """Fill realized outcomes for forecasts whose horizon has elapsed."""
    from core import models as core_models

    pending = list(core_models.Forecast.objects.filter(realized_direction__isnull=True))
    if not pending:
        return 0

    now = datetime.now(dt_timezone.utc)

    # Batch PriceBar lookups by symbol to avoid one query per forecast.
    earliest_by_symbol: dict[str, datetime] = {}
    for f in pending:
        if f.current_value is None or f.current_value == 0:
            continue
        prev = earliest_by_symbol.get(f.symbol)
        if prev is None or f.as_of_date < prev:
            earliest_by_symbol[f.symbol] = f.as_of_date

    # {symbol: [(date, close), ...]} sorted ascending — one query per symbol.
    bars_by_symbol: dict[str, list[tuple]] = {}
    for symbol, earliest in earliest_by_symbol.items():
        bars_by_symbol[symbol] = list(
            core_models.PriceBar.objects.filter(
                symbol=symbol, interval='1d', date__gt=earliest,
            ).order_by('date').values_list('date', 'close')
        )

    to_save = []
    for f in pending:
        if f.current_value is None or f.current_value == 0:
            continue
        bars = bars_by_symbol.get(f.symbol, [])
        future = [close for date, close in bars if date > f.as_of_date][:f.horizon_days]
        if len(future) < f.horizon_days:
            continue  # horizon not elapsed yet
        ret = future[-1] / f.current_value - 1
        f.realized_change_pct = round(ret * 100, 4)
        f.realized_direction = 'up' if ret > 0 else 'down'
        pred_up = f.proba_up > 0.5
        f.is_correct = (pred_up and ret > 0) or (not pred_up and ret <= 0)
        f.scored_at = now
        to_save.append(f)

    if to_save:
        core_models.Forecast.objects.bulk_update(
            to_save, ['realized_change_pct', 'realized_direction', 'is_correct', 'scored_at'],
        )
    return len(to_save)
