"""Task functions for the ingestion and aggregation pipeline.

These are Celery tasks (@shared_task) enqueued via services.queue.enqueue.
Calling one directly as a plain function (func(**kwargs)) still runs it
synchronously in-process — used by run_task.py --sync and TASK_QUEUE_ENABLED=False.
"""

import functools
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from celery import shared_task

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


def _log_task(func):
    """Log a task's start, duration, and outcome (result or exception).

    TaskRun (core.models, updated by services/queue.py's Celery signal handlers)
    already tracks queued/running/success/failed in the DB, but a log line gives
    an operator tailing logs a starting timestamp and a running duration to
    notice "this has been going for way longer than usual" well before that.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        name = func.__name__
        t0 = time.monotonic()
        logger.info('[task] %s starting args=%r kwargs=%r', name, args, kwargs)
        try:
            result = func(*args, **kwargs)
        except Exception:
            logger.exception('[task] %s FAILED after %.1fs', name, time.monotonic() - t0)
            raise
        logger.info('[task] %s done in %.1fs -> %r', name, time.monotonic() - t0, result)
        return result
    return wrapper

# Retry policy for tasks fanned out from a dispatcher — declared on the task
# itself (Celery convention), not passed in at the enqueue() call site.
_RETRY_KW = dict(autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 3})

DEFAULT_FETCH_MINUTES = 20          # look-back window for fetch tasks (2× interval)
DEFAULT_PROCESS_LIMIT = 1000
DEFAULT_AGGREGATE_HOURS = 168        # widened from 24h so events don't age out of the tag-dispatch window before being tagged
DEFAULT_AGGREGATE_MIN_ARTICLES = 1

# Fan-out tuning — dispatchers cap records enqueued per tick; chunk size amortises overhead.
# Matches ArticleAnalyzer.ANALYZE_BATCH_SIZE so each chunk maps to exactly one batched LLM call
# (mirrors TAG_CHUNK_SIZE below for topic tagging).
PROCESS_CHUNK_SIZE = 8
PROCESS_DISPATCH_LIMIT = 500
PROCESS_QUEUE_CLAIM_TTL_HOURS = 6  # claim lease so a slow queue doesn't get re-dispatched every tick
TAG_DISPATCH_LIMIT = 500
ROUTE_DISPATCH_LIMIT = 500
TAG_CHUNK_SIZE = 10     # events per chunk (EmbeddingTopicMatcher batch, local — no LLM call)
ROUTE_CHUNK_SIZE = 10
BOOTSTRAP_ARTICLE_YEARS = 1

# One backfill_day_chunk_task covers one day × this many sources. Sized so the
# worst case (sitemap discovery for each source + body-fetch + NLP processing
# for up to BACKFILL_CHUNK_SIZE × top_n articles) stays comfortably inside the
# heavy queue's existing 600s/10min default time limit (CELERY_QUEUE_TIME_LIMITS)
# — no per-call job_timeout override needed. BACKFILL_CHUNK_DEADLINE_SECONDS is
# a wall-clock cutoff passed into HistoricalBackfillService well inside that hard
# limit, so the task can exit cleanly with partial results instead of relying
# solely on Celery's SIGKILL.
BACKFILL_CHUNK_SIZE = 3
BACKFILL_CHUNK_DEADLINE_SECONDS = 480
# Backfill resume checkpoints (Redis SET) expire after this long — an abandoned
# backfill's checkpoint shouldn't live in Redis forever (mirrors the source
# timeout blocklist and LLM debounce keys, which also always carry a TTL).
BACKFILL_CHECKPOINT_TTL_SECONDS = 30 * 24 * 3600


# ── Text pipeline ─────────────────────────────────────────────────────────────

@shared_task
@_log_task
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

@shared_task(**_RETRY_KW)
@_log_task
def fetch_source_task(source_code: str, start_date: datetime | None = None) -> int:
    """Fetch one source. Idempotent per source (RSS de-dupes on save)."""
    now = datetime.now(dt_timezone.utc)
    if start_date is None:
        start_date = now - timedelta(minutes=DEFAULT_FETCH_MINUTES)
    return fetch_articles(source_code, start_date)


@shared_task
@_log_task
def dispatch_fetch_task(start_date: datetime | None = None) -> int:
    """Enqueue one fetch_source_task per enabled source. Returns sources dispatched."""
    from core import models as core_models
    from services.queue import enqueue

    now = datetime.now(dt_timezone.utc)
    if start_date is None:
        start_date = now - timedelta(minutes=DEFAULT_FETCH_MINUTES)
    codes = list(core_models.Source.objects.filter(is_enabled=True).values_list('code', flat=True))
    for code in codes:
        enqueue(fetch_source_task, code, start_date, queue='default')
    return len(codes)


@shared_task(**_RETRY_KW)
@_log_task
def process_articles_chunk_task(ids: list, only_failed: bool = False) -> int:
    """Process a chunk of articles by id (idempotent)."""
    return process_articles(ids=ids, only_failed=only_failed)


@shared_task(**_RETRY_KW)
@_log_task
def process_article_task(article_id, only_failed: bool = False) -> int:
    """Process a single article by id (idempotent)."""
    return process_articles(ids=[article_id], only_failed=only_failed)


@shared_task
@_log_task
def dispatch_process_articles_task(limit: int | None = None, only_failed: bool = False, chunk_size: int | None = None) -> int:
    """Select unprocessed (or un-located) articles and fan them out. Returns jobs enqueued."""
    from core import models as core_models
    from services.queue import enqueue

    limit = limit or PROCESS_DISPATCH_LIMIT
    chunk_size = max(1, chunk_size or PROCESS_CHUNK_SIZE)
    if only_failed:
        from django.db.models import Q
        qs = core_models.Article.objects.filter(processed_on__isnull=False).filter(
            Q(location__isnull=True) | Q(location='')
        )
        ids = [a.id for a in qs.only('id', 'extra_data') if not (a.extra_data or {}).get('geo_failed')][:limit]
    else:
        from django.db.models import Q
        from django.conf import settings as _s
        from services.workflow.articles import _apply_min_score_filter
        now = datetime.now(dt_timezone.utc)
        claim_cutoff = now - timedelta(hours=PROCESS_QUEUE_CLAIM_TTL_HOURS)
        qs = core_models.Article.objects.filter(processed_on__isnull=True)
        # Skip articles whose earlier dispatch is still (presumably) in flight —
        # avoids re-enqueueing duplicate jobs when the heavy queue is backlogged.
        qs = qs.filter(Q(process_queued_at__isnull=True) | Q(process_queued_at__lt=claim_cutoff))
        qs = _apply_min_score_filter(qs, _s.ARTICLE_MIN_IMPORTANCE_TO_PROCESS)
        ids = list(qs.order_by('-importance_score').values_list('id', flat=True)[:limit])
    if not ids:
        return 0
    if not only_failed:
        core_models.Article.objects.filter(id__in=ids).update(process_queued_at=now)
    enq = 0
    enqueued_ids: set = set()
    try:
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i:i + chunk_size]
            if len(chunk) == 1:
                enqueue(process_article_task, chunk[0], only_failed, queue='heavy')
            else:
                enqueue(process_articles_chunk_task, chunk, only_failed, queue='heavy')
            enqueued_ids.update(chunk)
            enq += 1
    except Exception:
        if not only_failed:
            # Release the claim on articles a mid-loop failure never actually got to
            # enqueue, so the next dispatch tick can pick them up immediately instead
            # of waiting out PROCESS_QUEUE_CLAIM_TTL_HOURS for a job that was never queued.
            unclaimed = [aid for aid in ids if aid not in enqueued_ids]
            if unclaimed:
                core_models.Article.objects.filter(id__in=unclaimed).update(process_queued_at=None)
        raise
    return enq


@shared_task(**_RETRY_KW)
@_log_task
def tag_events_chunk_task(event_ids: list) -> int:
    """Tag a chunk of events by id (local embedding matcher, no LLM call). Idempotent."""
    return tag_events_by_ids(event_ids)


@shared_task
@_log_task
def dispatch_tag_topics_task(hours: int = DEFAULT_AGGREGATE_HOURS, force_retag: bool = False,
                             limit: int | None = None) -> int:
    """Select events needing tags and fan them out in chunks of 10. Returns jobs enqueued."""
    from core import models as core_models
    from services.queue import enqueue
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
    enq = 0
    for i in range(0, len(ids), TAG_CHUNK_SIZE):
        enqueue(tag_events_chunk_task, ids[i:i + TAG_CHUNK_SIZE], queue='heavy')
        enq += 1
    return enq


@shared_task(**_RETRY_KW)
@_log_task
def route_events_chunk_task(event_ids: list) -> int:
    """Route a chunk of events by id. Idempotent."""
    from core import models as core_models
    from services.routing import route_events

    events = list(core_models.Event.objects.filter(pk__in=list(event_ids)))
    return route_events(events)


@shared_task
@_log_task
def dispatch_route_events_task(hours: int = 720, limit: int | None = None) -> int:
    """Select recent events and fan out routing in chunks of 10. Returns jobs enqueued."""
    from core import models as core_models
    from services.queue import enqueue

    limit = limit or ROUTE_DISPATCH_LIMIT
    start = datetime.now(dt_timezone.utc) - timedelta(hours=hours)
    ids = list(core_models.Event.objects.filter(started_at__gte=start).values_list('pk', flat=True)[:limit])
    if not ids:
        return 0
    enq = 0
    for i in range(0, len(ids), ROUTE_CHUNK_SIZE):
        enqueue(route_events_chunk_task, ids[i:i + ROUTE_CHUNK_SIZE], queue='heavy')
        enq += 1
    return enq


# ── Configurationless first-load bootstrap (WA4) ─────────────────────────────────

@shared_task
def bootstrap_initial_data_task(force: bool = False) -> int:
    """One-time, idempotent first-load backfill so deployment is configurationless.

    Enqueues full price history + weighted per-day article backfill for every enabled
    RSS source, then trains/runs the forecast. Guarded by a persisted cache flag and a
    PriceBar-presence heuristic so it runs exactly once. Trigger manually or via admin dashboard.
    """
    import logging
    from django.conf import settings
    from core import models as core_models
    from services.cache import KEY_BOOTSTRAP_INITIAL_DATA_DONE, cache_get, cache_set
    from services.queue import enqueue

    log = logging.getLogger(__name__)
    if not force:
        if cache_get(KEY_BOOTSTRAP_INITIAL_DATA_DONE):
            return 0
        if core_models.PriceBar.objects.exists():
            cache_set(KEY_BOOTSTRAP_INITIAL_DATA_DONE, True, timeout=None)
            return 0

    now = datetime.now(dt_timezone.utc)
    start = now - timedelta(days=365 * BOOTSTRAP_ARTICLE_YEARS)

    # Long one-shot seeds go on the bulk queue so they don't block the live pipeline.
    enqueue(backfill_prices_task, years=10, queue='bulk', job_timeout=-1)
    # source_code=None (all enabled RSS sources), top_n=None → each source's per-day
    # cap derives from its weight (2–6 by priority). backfill_history_task is a pure
    # dispatcher (see its docstring) — cheap enough that it doesn't need job_timeout=-1.
    enqueue(backfill_history_task, start, now, None, queue='bulk')
    if settings.FORECAST_ENABLED:
        enqueue(train_forecast_model_task, queue='bulk', job_timeout=-1)
        enqueue(run_forecast_task, queue='bulk', job_timeout=-1)

    cache_set(KEY_BOOTSTRAP_INITIAL_DATA_DONE, True, timeout=None)
    log.info('[bootstrap] initial data backfill enqueued (article window %dy)', BOOTSTRAP_ARTICLE_YEARS)
    return 1


# ── Topic tasks ────────────────────────────────────────────────────────────────

@shared_task
@_log_task
def refresh_topics_task() -> int:
    return refresh_topics()


@shared_task
@_log_task
def retroactive_tag_topic_task(slug: str, lookback_hours: int = 72) -> int:
    return retroactive_tag_topic(slug=slug, lookback_hours=lookback_hours)


@shared_task
@_log_task
def discover_topics_task(hours: int = 6) -> int:
    return discover_topics_from_events(hours=hours)


# ── LLM maintenance ──────────────────────────────────────────────────────────────

@shared_task
def refresh_openrouter_models_task() -> dict:
    """Discover currently-available free OpenRouter models and cache the top picks.

    Free-model availability fluctuates (deprecations, rate limits, reasoning leaks),
    so this probes the roster daily and caches the working models in Redis for the
    LLM layer to use. No-ops unless OPENROUTER_DYNAMIC_MODELS is enabled.
    """
    from django.conf import settings
    if not getattr(settings, 'OPENROUTER_DYNAMIC_MODELS', False):
        return {'enabled': False, 'models': []}
    from services.llm import discovery
    models = discovery.refresh()
    return {'enabled': True, 'count': len(models), 'models': models}


# ── Stream tasks ───────────────────────────────────────────────────────────────

@shared_task
def fetch_prices_task() -> int:
    from django.conf import settings
    if not settings.STREAM_PRICES_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('prices')


@shared_task
def fetch_notams_task() -> int:
    from django.conf import settings
    if not settings.STREAM_NOTAM_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('notam')


@shared_task
def fetch_earthquakes_task() -> int:
    from django.conf import settings
    if not settings.STREAM_EARTHQUAKE_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('earthquakes')


@shared_task
def fetch_forex_task() -> int:
    from django.conf import settings
    if not settings.STREAM_FOREX_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('forex')


# ── Health monitoring (A1 / A5) ─────────────────────────────────────────────────

@shared_task
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

def _weighted_top_n(weight: float | None, lo: int = 2, hi: int = 6) -> int:
    """Map a Source.weight (0.1–2.0 credibility multiplier) to a per-day article cap.

    Higher-priority sources keep more articles per day. weight 0.1 → ``lo``,
    1.0 → ~4, 2.0 → ``hi``. Reuses the existing credibility signal so backfill
    volume tracks source priority with no extra config.
    """
    if weight is not None and weight == 0:
        return 0  # weight=0 means suppressed — skip backfill entirely
    w = 1.0 if weight is None else min(max(weight, 0.1), 2.0)
    return round(lo + (w - 0.1) / 1.9 * (hi - lo))


@shared_task
def backfill_history_task(
    start_date: datetime,
    end_date: datetime,
    source_code: str | None = None,
    top_n: int | None = None,
    delay_seconds: float = 0.5,
    dry_run: bool = False,
    resume: bool = False,
    progress=None,
) -> dict:
    """
    Dispatcher: enumerate every (day, source-chunk) pair covering
    [start_date, end_date) across one or all enabled RSS sources, and enqueue
    one backfill_day_chunk_task per pair. Does no fetching/saving/processing
    itself — see services/data/historical.py's module docstring for why that
    work is chunked onto bounded heavy-queue workers instead.

    ``start_date`` / ``end_date`` accept either ``datetime`` objects or
    ``YYYY-MM-DD`` strings (the latter so the task is trivially enqueueable).
    ``source_code=None`` backfills every enabled RSS source; pass a code to
    restrict to one source. ``top_n=None`` derives the per-source-per-day cap
    from each source's ``weight`` (2–6 by priority); pass an int to override.
    ``resume`` skips (day, chunk) pairs already recorded in the Redis
    checkpoint set (see services.cache.key_backfill_checkpoint).
    ``progress`` is an optional ``callable(dict)`` — only invoked when a chunk
    actually runs synchronously in this same call (TASK_QUEUE_ENABLED=False,
    where enqueue() returns the chunk task's result directly); skipped when a
    chunk is genuinely queued, since its outcome isn't known yet.

    Returns {'days': int, 'sources': int, 'chunks_dispatched': int}.
    """
    import core.models as m
    from services.cache import key_backfill_checkpoint, redis_set_members
    from services.data.historical import iter_days
    from services.queue import enqueue

    start_date = _parse_backfill_date(start_date)
    end_date = _parse_backfill_date(end_date)

    if source_code:
        sources = [m.Source.objects.get(code=source_code)]
    else:
        sources = list(
            m.Source.objects.filter(type=m.SourceType.RSS, is_enabled=True).order_by('code')
        )
    source_codes = [s.code for s in sources]

    checkpoint_key = key_backfill_checkpoint(str(start_date.date()), str(end_date.date()))
    done: set[str] = set()
    if resume:
        try:
            done = redis_set_members(checkpoint_key)
        except Exception:
            logger.exception('[backfill] failed to read resume checkpoint — dispatching everything')

    total_days = dispatched = 0
    for day_start, day_end in iter_days(start_date, end_date):
        total_days += 1
        day_iso = day_start.date().isoformat()
        for chunk_start in range(0, len(source_codes), BACKFILL_CHUNK_SIZE):
            chunk = source_codes[chunk_start:chunk_start + BACKFILL_CHUNK_SIZE]
            # Content-addressed, not index-based: if the enabled-source set changes
            # between an interrupted run and a --resume rerun, an index-based key
            # (f'{day}:{chunk_index}') would silently point at a *different* set of
            # sources than the original run covered, and --resume would skip them
            # without ever actually backfilling that day for the new source set.
            chunk_key = f"{day_iso}:{'-'.join(chunk)}"
            if resume and chunk_key in done:
                continue
            result = enqueue(
                backfill_day_chunk_task, day_start, day_end, chunk, top_n, dry_run,
                checkpoint_key if resume else None, chunk_key, delay_seconds,
                queue='heavy',
            )
            dispatched += 1
            if progress is not None and isinstance(result, dict):
                progress(result)

    summary = {'days': total_days, 'sources': len(sources), 'chunks_dispatched': dispatched}
    logger.info('backfill_history_task dispatched: %s', summary)
    return summary


@shared_task(**_RETRY_KW)
def backfill_day_chunk_task(
    day_start: datetime,
    day_end: datetime,
    source_codes: list,
    top_n: int | None,
    dry_run: bool,
    checkpoint_key: str | None,
    chunk_key: str,
    delay_seconds: float = 0.5,
) -> dict:
    """
    Worker half of the backfill dispatcher: fetch + save + (unless dry_run)
    NLP-process one day window across a small chunk of sources.

    Relies on the heavy queue's default ~10min time limit (no per-call
    job_timeout override) plus an internal wall-clock deadline
    (BACKFILL_CHUNK_DEADLINE_SECONDS, threaded into HistoricalBackfillService)
    so it exits cleanly with partial results instead of only relying on
    Celery's hard kill. See services/data/historical.py's module docstring for
    fetch/save/dedup details and the trade-offs this chunking makes.

    Returns {'day': ISO date str, 'sources': [...], 'fetched', 'saved', 'processed'}.
    """
    import core.models as m
    from services.cache import redis_set_add
    from services.data.historical import HistoricalBackfillService

    deadline = datetime.now(dt_timezone.utc) + timedelta(seconds=BACKFILL_CHUNK_DEADLINE_SECONDS)

    sources = list(m.Source.objects.filter(code__in=source_codes))
    service = HistoricalBackfillService(sources=sources, top_n=top_n, delay_seconds=delay_seconds)
    result = service.fetch_and_save_day(day_start, day_end, dry_run=dry_run, deadline=deadline)

    processed = 0
    if not dry_run and result.saved_ids:
        processed = process_articles(ids=result.saved_ids)

    if checkpoint_key and not dry_run:
        try:
            redis_set_add(checkpoint_key, chunk_key, ttl=BACKFILL_CHECKPOINT_TTL_SECONDS)
        except Exception:
            logger.exception('[backfill] failed to mark checkpoint %s/%s', checkpoint_key, chunk_key)

    return {
        'day': day_start.date().isoformat(), 'sources': source_codes,
        'fetched': result.fetched, 'saved': result.saved, 'processed': processed,
    }


def _parse_backfill_date(value) -> datetime:
    """Normalize a backfill bound to a UTC datetime (accepts YYYY-MM-DD strings)."""
    if isinstance(value, datetime):
        return value
    d = datetime.strptime(value, '%Y-%m-%d')
    return d.replace(tzinfo=dt_timezone.utc)


# ── Forecasting tasks (event-fused symbol prediction) ────────────────────────────

@shared_task(**_RETRY_KW)
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


@shared_task(**_RETRY_KW)
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


@shared_task
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

@shared_task
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


@shared_task
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


@shared_task
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


@shared_task
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


@shared_task
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
