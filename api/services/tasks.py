"""Task functions for the ingestion and aggregation pipeline.

These are Celery tasks (@shared_task) enqueued via services.queue.enqueue.
Calling one directly as a plain function (func(**kwargs)) still runs it
synchronously in-process — used by run_task.py --sync and TASK_QUEUE_ENABLED=False.

The pull-based pipeline (fetch → score → process → aggregate → tag → route) is
NOT a set of per-step tasks anymore — it's declared in
services/stages.py and executed by exactly two tasks here:
pipeline_tick_task (cron, dispatches due stages) and run_stage_chunk_task
(the only fan-out worker). Everything else in this module is either
genuinely scheduled (dailies, maintenance, forecasting) or one-shot
(backfills, bootstrap).
"""

import functools
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from celery import shared_task

from services.workflow import (
    process_articles,
    refresh_topics,
    retroactive_tag_topic,
    discover_topics_from_events,
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


# ── The pipeline: two tasks, all stages ──────────────────────────────────────
# Stage definitions (selection, chunking, cadence, queue) live in
# services/stages.py — see its module docstring. The tick is the only
# scheduler entry point; the chunk task is the only fan-out worker.

@shared_task
@_log_task
def pipeline_tick_task(force: bool = False) -> dict:
    """One scheduler tick: dispatch every enabled stage that is due and has
    pending work (cron: every 10 min). force=True skips the per-stage cadence
    gates — used by the admin dashboard's "Run pipeline" button.
    Returns {stage_name: jobs_enqueued}."""
    from services.stages import run_due_stages
    return run_due_stages(force=force)


@shared_task
@_log_task
def dispatch_stage_task(stage_name: str, force: bool = True) -> int:
    """Dispatch a single stage by name (admin buttons / manual repair).
    Returns jobs enqueued."""
    from services.stages import dispatch_stage
    return dispatch_stage(stage_name, force=force)


@shared_task(**_RETRY_KW)
@_log_task
def run_stage_chunk_task(stage_name: str, ids: list | None = None) -> int:
    """Execute one chunk (or a singleton run) of a pipeline stage. Idempotent —
    every stage handler tolerates re-runs on the same ids."""
    from services.stages import run_chunk
    return run_chunk(stage_name, ids)


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
    # source_code=None (wikipedia + all enabled RSS sources), top_n=None → each
    # source's per-day cap derives from its weight (2–6 by priority; 25 for
    # wikipedia). backfill_history_task does one bounded preflight probe per
    # source, then pure enqueueing (see its docstring) — no job_timeout=-1
    # needed on the bulk queue.
    enqueue(backfill_history_task, start, now, None, queue='bulk')
    if settings.FORECAST_ENABLED:
        enqueue(train_forecast_model_task, queue='bulk', job_timeout=-1)
        enqueue(run_forecast_task, queue='bulk', job_timeout=-1)
    # Surface the backfilled range as Events (the live aggregate stage's 168h
    # lookback can never reach it). Bulk is a 1-worker FIFO queue, so this runs
    # after the price backfill + forecast training above — by then the heavy
    # queue has usually drained the backfill day-chunks. Best-effort ordering
    # only (bulk can't await heavy): aggregate_history_task is idempotent, so
    # any articles processed after it ran are picked up by re-running it
    # (manage.py run_task aggregate_history_task start_date=... end_date=...).
    enqueue(aggregate_history_task, start, now, queue='bulk', job_timeout=-1)

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


@shared_task
@_log_task
def aggregate_full_task() -> int:
    """Daily full-window aggregate sweep. The live 'aggregate' stage clusters only
    the trailing AGGREGATE_LIVE_WINDOW_HOURS (72h) each tick; this re-runs
    aggregate_events over the full EVENT_STAGE_WINDOW_HOURS (168h) once a day so
    multi-day events whose articles span >72h still re-aggregate after aging past
    the live window. Idempotent (upsert keyed on location/category/day), routes
    inline. Returns created+updated."""
    from services.stages import EVENT_STAGE_WINDOW_HOURS
    from services.workflow import aggregate_events
    created, updated = aggregate_events(hours=EVENT_STAGE_WINDOW_HOURS)
    return created + updated


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

# Stream name → settings flag that gates it (checked at run time too, so a
# stray enqueue of a disabled stream is still a no-op).
_STREAM_FLAGS = {
    'prices': 'STREAM_PRICES_ENABLED',
    'notam': 'STREAM_NOTAM_ENABLED',
    'earthquakes': 'STREAM_EARTHQUAKE_ENABLED',
    'forex': 'STREAM_FOREX_ENABLED',
}


@shared_task
def run_stream_task(name: str) -> int:
    """Run one stream collector by name ('prices' | 'notam' | 'earthquakes' |
    'forex') — replaces the four identical per-stream wrapper tasks."""
    from django.conf import settings
    flag = _STREAM_FLAGS[name]
    if not getattr(settings, flag, False):
        return 0
    from services.streams import run_stream
    return run_stream(name)


# ── Health monitoring (A1 / A5) ─────────────────────────────────────────────────

@shared_task
def pipeline_health_task() -> dict:
    """Warn when pipeline outputs go stale. Warnings go to logs (Sentry / log
    alerts); the full report is also persisted to Redis and rendered on the
    admin dashboard's Health section.

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

    # Stage staleness: pending work piling up while the tick hasn't dispatched
    # the stage in 3× its cadence means the tick/queue is stuck, not just slow.
    from services.stages import REGISTRY, last_dispatched_at
    stages_report = {}
    for stage in REGISTRY.values():
        if not stage.enabled():
            continue
        pending = stage.pending_count()
        last = last_dispatched_at(stage)
        stale = (
            pending > 0
            and last is not None
            and last < now - timedelta(minutes=3 * stage.every_minutes)
        )
        stages_report[stage.name] = {
            'pending': pending,
            'last_dispatch': last.isoformat() if last else None,
            'ok': not stale,
        }
        if stale:
            log.warning(
                '[health] stage %r stale — %d pending, last dispatched %s (cadence %dm)',
                stage.name, pending, last, stage.every_minutes,
            )
    report['stages'] = stages_report

    # LLM provider health: a provider that is still being *attempted* but hasn't
    # returned a single success recently is an upstream outage (e.g. Cerebras
    # serving no completions, or Groq erroring hard) that the provider table on
    # the dashboard shows only as a quiet stat. Surfaced here so it fires a
    # warning like stage staleness does, rather than being buried. Uses the
    # per-provider Redis call stats written by services.llm._record_llm_call.
    window_h = getattr(settings, 'LLM_PROVIDER_HEALTH_WINDOW_HOURS', 3)
    providers_report: dict = {}
    try:
        from services.cache import get_redis_client, key_llm_req_stat
        rc = get_redis_client(write=False)

        provider_names: set[str] = set()
        for route in settings.LLM_ROUTES.values():
            provider_names.update([route] if isinstance(route, str) else route)

        def _ts(provider, field):
            raw = rc.get(key_llm_req_stat(provider, field))
            return datetime.fromtimestamp(int(raw), tz=dt_timezone.utc) if raw else None

        cutoff = now - timedelta(hours=window_h)
        for provider in sorted(provider_names):
            err = int(rc.get(key_llm_req_stat(provider, 'err')) or 0)
            last_ok = _ts(provider, 'last_ok')
            last_err = _ts(provider, 'last_err')
            # Attempted recently (last_err within window) but no success within
            # window (last_ok missing or stale) ⇒ upstream outage.
            attempted = last_err is not None and last_err >= cutoff
            succeeded = last_ok is not None and last_ok >= cutoff
            unhealthy = attempted and not succeeded
            providers_report[provider] = {
                'err_total': err,
                'last_ok': last_ok.isoformat() if last_ok else None,
                'last_err': last_err.isoformat() if last_err else None,
                'ok': not unhealthy,
            }
            if unhealthy:
                log.warning(
                    '[health] LLM provider %r unhealthy — no success in %dh, '
                    'last_ok=%s, last_err=%s',
                    provider, window_h, last_ok, last_err,
                )
    except Exception:  # noqa: BLE001 — no Redis in dev; skip provider health
        log.debug('[health] LLM provider health check skipped (no Redis)')
    report['providers'] = providers_report

    # Sweep TaskRun rows orphaned by killed workers (no signal fires on a
    # SIGKILL, so the row would stay 'running' forever) — see queue.py.
    from services.queue import reap_stale_task_runs
    report['task_runs_reaped'] = reap_stale_task_runs()

    from services.cache import KEY_PIPELINE_HEALTH_LAST, cache_set
    try:
        cache_set(KEY_PIPELINE_HEALTH_LAST, {'at': now.isoformat(), 'report': report}, timeout=None)
    except Exception:  # noqa: BLE001 — no Redis in dev; the report itself still returns
        log.exception('[health] failed to persist health report')

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
    skip_preflight: bool = False,
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
    ``resume`` skips (day, source) pairs already recorded in the Redis
    checkpoint set (see services.cache.key_backfill_checkpoint). Checkpoints
    are per source-day and only written for source-days whose fetch actually
    ran (see backfill_day_chunk_task) — days a source spent blocklisted stay
    un-checkpointed, so a --resume rerun recovers them.
    ``progress`` is an optional ``callable(dict)`` — only invoked when a chunk
    actually runs synchronously in this same call (TASK_QUEUE_ENABLED=False,
    where enqueue() returns the chunk task's result directly); skipped when a
    chunk is genuinely queued, since its outcome isn't known yet.

    Before dispatching, each candidate source gets one cheap preflight probe
    (``probe_source_has_sitemap_entries`` — a single wide-window sitemap fetch)
    so a misconfigured source (wrong domain, dead sitemap) is dropped from the
    whole run up front instead of burning every (day × chunk) pair in the
    requested range on a source that's going to return empty every single
    time. Pass ``skip_preflight=True`` to force-dispatch a source anyway (e.g.
    while debugging the probe itself). Sources dropped this way are reported
    under ``skipped_sources`` rather than silently disappearing.

    This makes the dispatcher itself do one blocking HTTP round-trip per
    source before it enqueues anything — previously pure enqueue, no I/O. Only
    safe because this task runs on the ``bulk`` queue (long one-shot jobs, 1
    worker); don't reuse this dispatcher pattern on a queue sized for
    fast/non-blocking work.

    Returns {'days': int, 'sources': int, 'chunks_dispatched': int, 'skipped_sources': list[str]}.
    """
    import core.models as m
    from services.cache import key_backfill_checkpoint, redis_set_members
    from services.data.historical import iter_days, probe_source
    from services.data.wikipedia import WIKIPEDIA_SOURCE_CODE, ensure_wikipedia_source
    from services.queue import enqueue

    start_date = _parse_backfill_date(start_date)
    end_date = _parse_backfill_date(end_date)

    if source_code == WIKIPEDIA_SOURCE_CODE:
        sources = [ensure_wikipedia_source()]
    elif source_code:
        sources = [m.Source.objects.get(code=source_code)]
    else:
        # Wikipedia Current Events is the primary discovery path (curated
        # per-day importance — see services/data/wikipedia.py); per-publisher
        # sitemap sources supplement it. The wiki Source is is_enabled=False
        # (backfill-only), so it must be included explicitly.
        sources = [ensure_wikipedia_source()] + list(
            m.Source.objects.filter(type=m.SourceType.RSS, is_enabled=True).order_by('code')
        )

    skipped_sources: list[str] = []
    if not skip_preflight and not dry_run:
        verified = []
        for source in sources:
            if probe_source(source):
                verified.append(source)
            else:
                skipped_sources.append(source.code)
                logger.warning(
                    '[backfill] source=%r: preflight found no entries in its recent '
                    'window — dropping from this run (pass skip_preflight=True to force)',
                    source.code,
                )
        sources = verified
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
        # Checkpoint members are '{day_iso}:{source_code}' — per source-day, so a
        # --resume rerun re-dispatches exactly the source-days that never actually
        # ran (blocked/errored/deadline), even if the enabled-source set or chunk
        # boundaries changed since the interrupted run.
        pending = [c for c in source_codes if not (resume and f'{day_iso}:{c}' in done)]
        # The wiki source gets a chunk of its own: a day yields ~25 curated
        # events (each with a body fetch + possible Wayback fallback) — a full
        # chunk's workload by itself, and grouping it with sitemap sources
        # would routinely blow the chunk deadline.
        chunks = [[c] for c in pending if c == WIKIPEDIA_SOURCE_CODE]
        rest = [c for c in pending if c != WIKIPEDIA_SOURCE_CODE]
        chunks += [rest[i:i + BACKFILL_CHUNK_SIZE] for i in range(0, len(rest), BACKFILL_CHUNK_SIZE)]
        for chunk in chunks:
            result = enqueue(
                backfill_day_chunk_task, day_start, day_end, chunk, top_n, dry_run,
                checkpoint_key, delay_seconds,
                queue='heavy',
            )
            dispatched += 1
            if progress is not None and isinstance(result, dict):
                progress(result)

    summary = {
        'days': total_days, 'sources': len(sources), 'chunks_dispatched': dispatched,
        'skipped_sources': skipped_sources,
    }
    logger.info('backfill_history_task dispatched: %s', summary)
    return summary


@shared_task
@_log_task
def aggregate_history_task(
    start_date: datetime,
    end_date: datetime,
    window_days: int = 30,
    tag: bool = True,
) -> dict:
    """Aggregate backfilled articles into Events across a historical range.

    The live 'aggregate' stage only looks back EVENT_STAGE_WINDOW_HOURS (168h),
    so articles saved by backfill_history_task would otherwise never form
    Events (and never appear on the map). This walks [start_date, end_date) in
    clustering-grid-aligned windows (services.workflow.events
    .iter_aggregate_windows), running the SAME aggregate_events the live stage
    uses on each — idempotent (upsert keyed on location/category/day), routing
    inline as always. Each window's new/updated events are then topic-tagged
    (``tag=False`` to skip), since the tag repair stage's 168h lookback can't
    reach them either.

    Run it AFTER a backfill's day-chunks have finished processing (the
    dispatcher can't await them). Long single job → bulk queue:
        python manage.py run_task aggregate_history_task \\
            start_date=2021-07-01 end_date=2026-07-01 --sync

    Returns {'windows', 'created', 'updated', 'tagged'}.
    """
    from core import models as m
    from services.workflow import _needs_tagging, aggregate_events, tag_events_by_ids
    from services.workflow.events import iter_aggregate_windows

    start_date = _parse_backfill_date(start_date)
    end_date = _parse_backfill_date(end_date)

    windows = created_total = updated_total = tagged_total = 0
    for w_start, w_end in iter_aggregate_windows(start_date, end_date, window_days=window_days):
        windows += 1
        created, updated = aggregate_events(start=w_start, end=w_end)
        created_total += created
        updated_total += updated
        if tag and (created or updated):
            qs = m.Event.objects.filter(
                started_at__gte=w_start, started_at__lt=w_end,
            ).only('pk', 'topics', 'topics_source')
            ids = [e.pk for e in qs if _needs_tagging(e.topics) or e.topics_source == 'keyword']
            if ids:
                tagged_total += tag_events_by_ids(ids)
        logger.info(
            '[aggregate-history] window %s → %s: created=%d updated=%d',
            w_start.date(), w_end.date(), created, updated,
        )

    return {
        'windows': windows, 'created': created_total,
        'updated': updated_total, 'tagged': tagged_total,
    }


# Source-day outcomes that count as "done" for resume checkpointing — the fetch
# actually ran to a final answer. 'blocked'/'error'/'deadline' stay
# un-checkpointed so a --resume rerun retries them; 'suppressed' (weight=0) is
# deliberate but reversible, so it also stays eligible.
_CHECKPOINTABLE_OUTCOMES = frozenset({'fetched', 'empty'})


@shared_task(**_RETRY_KW)
def backfill_day_chunk_task(
    day_start: datetime,
    day_end: datetime,
    source_codes: list,
    top_n: int | None,
    dry_run: bool,
    checkpoint_key: str | None,
    delay_seconds: float = 0.5,
) -> dict:
    """
    Worker half of the backfill dispatcher: fetch + save + (unless dry_run)
    score → gate → NLP-process one day window across a small chunk of sources
    — the same order the live pipeline uses (score stage, then
    ARTICLE_MIN_IMPORTANCE_TO_PROCESS gate, then process stage).

    Relies on the heavy queue's default ~10min time limit (no per-call
    job_timeout override) plus an internal wall-clock deadline
    (BACKFILL_CHUNK_DEADLINE_SECONDS, threaded into HistoricalBackfillService)
    so it exits cleanly with partial results instead of only relying on
    Celery's hard kill. See services/data/historical.py's module docstring for
    fetch/save/dedup details and the trade-offs this chunking makes.

    Checkpointing is per source-day and outcome-gated: only sources whose fetch
    ran to a real answer ('fetched'/'empty' — see DayResult.outcomes) are marked
    done, so blocklisted/errored/deadline source-days remain resumable.

    Returns {'day': ISO date str, 'sources': [...], 'fetched', 'saved',
    'scored', 'processed', 'outcomes': {source_code: outcome}}.
    """
    from django.conf import settings
    import core.models as m
    from services.cache import redis_set_add
    from services.data.historical import HistoricalBackfillService
    from services.workflow.articles import _apply_min_score_filter

    deadline = datetime.now(dt_timezone.utc) + timedelta(seconds=BACKFILL_CHUNK_DEADLINE_SECONDS)

    sources = list(m.Source.objects.filter(code__in=source_codes))
    service = HistoricalBackfillService(sources=sources, top_n=top_n, delay_seconds=delay_seconds)
    result = service.fetch_and_save_day(day_start, day_end, dry_run=dry_run, deadline=deadline)

    scored = processed = 0
    # Backfill LLM switch OFF: fetch/save only — stop before the LLM-dependent
    # score + process (annotation) steps and leave the saved articles for a later
    # dedicated annotate + aggregate pass. Read from RuntimeConfig at execution
    # time (dashboard-editable), so flipping it applies to this already-dispatched,
    # in-flight backfill on the next chunk — no re-dispatch or restart.
    from services.runtime_config import is_backfill_llm_enabled
    llm_enabled = is_backfill_llm_enabled()
    if not dry_run and not llm_enabled and result.saved_ids:
        # Flag them so the live score/process stages skip them too (they'd
        # otherwise annotate these on the next tick, since their created_on is
        # recent). annotate_deferred_articles_task picks them up on demand.
        m.Article.objects.filter(id__in=result.saved_ids).update(annotation_deferred=True)
        logger.info(
            '[backfill] backfill LLM switch OFF — saved %d article(s) for day=%s '
            'with annotation deferred; run annotate_deferred_articles_task later',
            len(result.saved_ids), day_start.date(),
        )
    if not dry_run and llm_enabled and result.saved_ids:
        # Same order as the live pipeline: score → gate → process. Without
        # this, immediately-processed backfill articles would be skipped by
        # the 'score' stage forever (its predicate is processed_on__isnull)
        # and the min-importance process gate would never apply.
        if settings.ARTICLE_IMPORTANCE_SCORING_ENABLED:
            from services.scoring import score_unscored_articles
            try:
                scored = score_unscored_articles(article_ids=result.saved_ids)
            except Exception:
                # Scoring is best-effort here — a scoring failure must not
                # strand saved articles unprocessed (they'd stay invisible).
                logger.exception('[backfill] scoring failed for day=%s — processing ungated', day_start.date())
        process_qs = _apply_min_score_filter(
            m.Article.objects.filter(id__in=result.saved_ids),
            settings.ARTICLE_MIN_IMPORTANCE_TO_PROCESS,
        )
        process_ids = list(process_qs.values_list('id', flat=True))
        if process_ids:
            processed = process_articles(ids=process_ids)

    if checkpoint_key and not dry_run:
        day_iso = day_start.date().isoformat()
        for code, outcome in result.outcomes.items():
            if outcome not in _CHECKPOINTABLE_OUTCOMES:
                continue
            try:
                redis_set_add(checkpoint_key, f'{day_iso}:{code}', ttl=BACKFILL_CHECKPOINT_TTL_SECONDS)
            except Exception:
                logger.exception('[backfill] failed to mark checkpoint %s/%s:%s', checkpoint_key, day_iso, code)

    return {
        'day': day_start.date().isoformat(), 'sources': source_codes,
        'fetched': result.fetched, 'saved': result.saved,
        'scored': scored, 'processed': processed,
        'annotated': llm_enabled,
        'outcomes': result.outcomes,
    }


@shared_task(**_RETRY_KW)
def annotate_deferred_articles_task(limit: int = 1000, batch_size: int = 50) -> dict:
    """Annotate articles a fetch-only backfill deferred (annotation_deferred=True,
    saved with BACKFILL_LLM_ENABLED=False): score → min-importance gate → process,
    the same order the live pipeline and backfill use, then clear the flag.

    Run this after a fetch-only backfill to annotate the saved articles, then
    aggregate them with aggregate_history_task over the same date range. Bounded
    to ``limit`` articles per call and re-runnable — returns ``remaining`` so you
    can loop until it hits 0. Long job → run on bulk queue or --sync:

        python manage.py run_task annotate_deferred_articles_task limit=2000 --sync

    Returns {'selected', 'scored', 'processed', 'remaining'}.
    """
    from django.conf import settings
    import core.models as m
    from services.workflow.articles import _apply_min_score_filter

    def _remaining() -> int:
        return m.Article.objects.filter(
            annotation_deferred=True, processed_on__isnull=True,
        ).count()

    ids = list(
        m.Article.objects.filter(annotation_deferred=True, processed_on__isnull=True)
        .order_by('created_on').values_list('id', flat=True)[:limit]
    )

    scored = processed = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        if settings.ARTICLE_IMPORTANCE_SCORING_ENABLED:
            from services.scoring import score_unscored_articles
            try:
                scored += score_unscored_articles(article_ids=batch)
            except Exception:
                # Best-effort, mirroring backfill_day_chunk_task: a scoring
                # failure must not strand the batch — process it ungated.
                logger.exception('[annotate-deferred] scoring failed for a batch — processing ungated')
        process_ids = list(
            _apply_min_score_filter(
                m.Article.objects.filter(id__in=batch),
                settings.ARTICLE_MIN_IMPORTANCE_TO_PROCESS,
            ).values_list('id', flat=True)
        )
        if process_ids:
            processed += process_articles(ids=process_ids)
        # Clear the flag for the whole batch — including below-gate articles that
        # won't be processed — so re-runs make forward progress and the set
        # rejoins normal pipeline/cleanup handling.
        m.Article.objects.filter(id__in=batch).update(annotation_deferred=False)

    result = {
        'selected': len(ids), 'scored': scored,
        'processed': processed, 'remaining': _remaining(),
    }
    logger.info('annotate_deferred_articles_task: %s', result)
    return result


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
    rows = []
    for h in settings.FORECAST_HORIZONS_DAYS:
        for p in model.predict(fm, h):
            stream_key = symbol_meta.get(p['symbol'], ('', ''))[0]
            rows.append(core_models.Forecast(
                symbol=p['symbol'], stream_key=stream_key, generated_at=now,
                as_of_date=p['as_of_date'], horizon_days=h, direction=p['direction'],
                proba_up=p['proba_up'], predicted_change_pct=p['predicted_change_pct'],
                predicted_price=p['predicted_price'], band_low=p['band_low'],
                band_high=p['band_high'], confidence=p['confidence'],
                current_value=p['current_value'], router_source=router,
                model_version=p['model_version'],
            ))
    if rows:
        core_models.Forecast.objects.bulk_create(rows)
    return len(rows)


# ── Article maintenance ───────────────────────────────────────────────────────
# (Importance scoring itself is the 'score' stage in services/stages.py.)
# Article deletion is intentionally not implemented: every record is kept as
# training/distillation data. There are no cleanup/prune tasks.

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

    # Load all article IDs referenced by recent events once — avoid per-source DB
    # round-trips. .iterator() streams the scan so the full queryset result cache
    # isn't held alongside the set being built (the window is 30d-bounded, so the
    # set itself stays modest).
    all_event_article_ids: set[str] = {
        str(aid)
        for article_ids in core_models.Event.objects.filter(started_at__gte=cutoff)
        .values_list('article_ids', flat=True)
        .iterator(chunk_size=5000)
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
