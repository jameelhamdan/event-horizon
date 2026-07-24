"""Server-rendered admin operations dashboard.

A single page under ``/admin/dashboard/`` summarizing pipeline operations and
offering POST actions. Data sources: ``api/crontab`` (upcoming runs),
Flower's ``/workers?json=1`` API (live worker ground truth — individual task
detail lives in the task browser at ``/admin/core/taskrun/``), Flower's
``/api/queues/length`` API (per-queue backlog depth off the Redis broker),
``pipeline_coverage()`` (per-stage gaps), and forecast artifacts/rows.
Registered via a ``get_urls`` shim in ``core/admin.py``.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from django.contrib import admin, messages
from django.shortcuts import redirect, render

logger = logging.getLogger(__name__)


# ── POST action handlers ─────────────────────────────────────────────────────

# One-click crontab-task triggers: action value → (task name, message). The
# queue is never hardcoded here — it's derived from services.task_registry
# (the same map manage.py run_task uses) so a task can't drift onto a
# different queue in one caller and not the other. Every standalone crontab
# task gets a button so any stalled stage can be resumed from the dashboard
# without shell access. The template renders 'generate_newsletter' (sends
# real email to subscribers) behind an are-you-sure confirm() — keep it that
# way if you add destructive ones. Article deletion tasks are intentionally
# NOT exposed here: deletion is disabled (records are kept as training data).
_TASK_ACTIONS = {
    'refresh_topics': ('refresh_topics_task', 'Topic refresh enqueued (Wikipedia current events + LLM enrichment).'),
    'discover_topics': ('discover_topics_task', 'Topic discovery enqueued (LLM over recent events).'),
    'generate_newsletter': ('generate_newsletter_task', 'Newsletter generation enqueued — WILL EMAIL SUBSCRIBERS when it completes.'),
    'pipeline_health': ('pipeline_health_task', 'Health report enqueued — the Health section refreshes on next page load.'),
    'adjust_source_weights': ('adjust_source_weights_task', 'Source-weight adjustment enqueued.'),
    'score_forecasts': ('score_forecasts_task', 'Forecast scoring enqueued.'),
    'refresh_openrouter_models': ('refresh_openrouter_models_task', 'OpenRouter model refresh enqueued.'),
}

_STREAM_NAMES = ('prices', 'notam', 'earthquakes', 'forex')


def _handle_action(request):
    from services.queue import enqueue, enqueue_bulk
    from services import tasks as T
    from services.task_registry import queue_for_task, resolve_task

    action = request.POST.get('dashboard_action', '')
    try:
        if action in _TASK_ACTIONS:
            task_name, msg = _TASK_ACTIONS[action]
            func = resolve_task(task_name)
            if func is None:
                raise ValueError(f'Unknown task {task_name!r}')
            enqueue(func, queue=queue_for_task(task_name))
            _ok(request, msg)
        elif action == 'run_stream':
            _handle_run_stream(request)
        elif action == 'aggregate_history':
            _handle_aggregate_history(request)
        elif action == 'retroactive_tag_topic':
            _handle_retroactive_tag(request)
        elif action == 'run_pipeline':
            # force=True: dispatch every enabled stage with pending work,
            # skipping the per-stage cadence gates (see services/stages.py).
            enqueue(T.pipeline_tick_task, True, queue='default')
            _ok(request, 'Pipeline tick enqueued (all due stages, cadence gates skipped).')
        elif action == 'backfill_prices':
            enqueue_bulk(T.backfill_prices_task, years=10)
            _ok(request, 'Price backfill enqueued (10y, all active symbols).')
        elif action == 'backfill_articles_until':
            _handle_backfill_until(request)
        elif action == 'reprocess_corpus':
            _handle_reprocess_corpus(request)
        elif action == 'retrain_forecast':
            enqueue_bulk(T.train_forecast_model_task)
            enqueue_bulk(T.run_forecast_task)
            _ok(request, 'Forecast retrain + run enqueued (bulk queue).')
        elif action == 'rerun_bootstrap':
            enqueue(T.bootstrap_initial_data_task, True, queue='default', job_timeout=-1)
            _ok(request, 'First-load bootstrap re-triggered (force).')
        elif action == 'reprocess':
            _handle_reprocess(request)
        elif action == 'set_llm_flag':
            _handle_set_llm_flag(request)
        elif action == 'cancel_job':
            _handle_cancel(request)
        else:
            messages.error(request, f'Unknown action: {action}')
    except Exception as exc:  # noqa: BLE001
        logger.exception('[dashboard] action %s failed', action)
        messages.error(request, f'Action failed: {exc}')
    return redirect(request.path)


def _handle_reprocess(request):
    """Re-dispatch one pipeline stage. The posted value is a stage name from
    services/stages.py — the button, the count next to it, and the dispatch all
    read the same registry entry, so they can't disagree."""
    from services.queue import enqueue
    from services.stages import REGISTRY
    from services import tasks as T

    stage = request.POST.get('stage', '')
    if stage not in REGISTRY:
        messages.error(request, f'Unknown reprocess stage: {stage}')
        return
    enqueue(T.dispatch_stage_task, stage, queue='default')
    _ok(request, f'Stage "{stage}" dispatch enqueued.')


def _handle_set_llm_flag(request):
    """Flip a live LLM master switch (RuntimeConfig) — 'live' or 'backfill'.

    Takes effect on the next tick / next backfill chunk (services stages and
    backfill_day_chunk_task read RuntimeConfig at execution time), including an
    already-dispatched, in-flight backfill — no restart."""
    from services.runtime_config import set_llm_flag

    flag = request.POST.get('flag', '')
    enabled = request.POST.get('enabled', '').lower() == 'true'
    try:
        label = set_llm_flag(flag, enabled)
    except ValueError as exc:
        messages.error(request, str(exc))
        return
    _ok(request, f'{label} LLM {"enabled" if enabled else "disabled"}.')


def _parse_date_range(request, required: bool):
    """Parse the POST ``start_date``/``end_date`` fields (YYYY-MM-DD) into UTC
    datetimes — the one date-range convention shared by every "Backfill &
    history" action (fetch/annotate/aggregate all take the same two fields).

    ``required=True``: both must be present. ``required=False``: both blank
    means "no filter" — returns ``(None, None)``; exactly one filled is an
    error (give both or neither).

    Returns ``(start, end)`` on success. On invalid input, records a
    ``messages.error`` and returns ``None`` — the caller should check for that
    and return without proceeding.
    """
    from datetime import date, datetime, timezone as dt_timezone

    raws = {f: request.POST.get(f, '').strip() for f in ('start_date', 'end_date')}
    if not required and not any(raws.values()):
        return None, None
    if not required and not all(raws.values()):
        messages.error(request, 'Give both start_date and end_date, or leave both blank.')
        return None

    parsed = {}
    for field, raw in raws.items():
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            messages.error(request, f'Invalid {field} {raw!r} — expected YYYY-MM-DD.')
            return None
        parsed[field] = datetime(d.year, d.month, d.day, tzinfo=dt_timezone.utc)
    if parsed['start_date'] >= parsed['end_date']:
        messages.error(request, 'start_date must be before end_date.')
        return None
    return parsed['start_date'], parsed['end_date']


def _period_phrase(start_date, end_date, if_blank: str) -> str:
    """' for <start> → <end>' if a range was given, else ``if_blank`` — the
    shared phrasing for action messages whose range is optional (annotate
    deferred, healing)."""
    if start_date is None:
        return if_blank
    return f' for {start_date.date()} → {end_date.date()}'


def _handle_backfill_until(request):
    """Backfill articles over an explicit [start_date, end_date) range.

    Mirrors ``manage.py backfill_history``. ``source_code`` is optional —
    blank means all enabled RSS sources. ``annotate_inline`` folds the
    separate "Enable historical-backfill LLM" toggle into this same submit —
    each fetched chunk annotates itself immediately (on fresh, just-fetched
    content — no rehydrate needed) instead of landing in the deferred
    backlog for a later ``Re-process articles`` click. Only ever turns the
    flag ON here; leaving it unticked doesn't turn an already-on flag off,
    since this form isn't the intended off-switch for that shared setting.
    """
    from services.queue import enqueue
    from services import tasks as T

    date_range = _parse_date_range(request, required=True)
    if date_range is None:
        return
    start_date, end_date = date_range
    source_code = request.POST.get('source_code', '').strip()

    if request.POST.get('annotate_inline') in ('1', 'true', 'on'):
        from services.runtime_config import set_llm_flag
        set_llm_flag('backfill', True)

    if source_code:
        import core.models as m
        if not m.Source.objects.filter(code=source_code).exists():
            messages.error(request, f'Source "{source_code}" not found.')
            return
        enqueue(T.backfill_history_task, start_date, end_date, source_code, queue='bulk')
        _ok(request, f'Article backfill enqueued for "{source_code}": {start_date.date()} → {end_date.date()}.')
    else:
        enqueue(T.backfill_history_task, start_date, end_date, None, queue='bulk')
        _ok(request, f'Article backfill enqueued (all sources): {start_date.date()} → {end_date.date()}.')


# Human label per reprocess_corpus_task scope, for the confirmation message.
# The scope set itself lives on the task (tasks.REPROCESS_SCOPES) — single
# source of truth, validated against below.
_REPROCESS_SCOPE_LABELS = {
    'deferred': 'deferred articles',
    'unfinished': 'unfinished articles (+ re-aggregate)',
    'everything': 'every article, already-annotated included',
}


def _handle_reprocess_corpus(request):
    """Dispatch ``reprocess_corpus_task`` over the corpus (or a date range),
    with the ``scope`` field selecting which rows — the single dashboard control
    behind all re-processing (deferred / unfinished / everything). Every scope
    takes the same optional date range (blank = whole corpus) and ``rehydrate``
    toggle (re-fetch each article body through the current extractor first);
    'deferred' also honours an optional ``limit`` — blank means uncapped (the
    whole deferred backlog in one dispatch), matching how 'unfinished'/
    'everything' already behave with no limit field at all; a number is only
    for deliberately capping a test run smaller."""
    from services.queue import enqueue_bulk
    from services import tasks as T

    scope = request.POST.get('scope', 'deferred')
    if scope not in T.REPROCESS_SCOPES:
        messages.error(request, f'Unknown re-process scope: {scope}')
        return
    date_range = _parse_date_range(request, required=False)
    if date_range is None:
        return
    start_date, end_date = date_range
    rehydrate = request.POST.get('rehydrate') in ('1', 'true', 'on')

    kwargs = {'scope': scope, 'start_date': start_date, 'end_date': end_date, 'rehydrate': rehydrate}
    if scope == 'deferred':
        limit = _int_or(request.POST.get('limit'), None)
        if limit is not None:
            kwargs['limit'] = limit
    enqueue_bulk(T.reprocess_corpus_task, **kwargs)

    period = _period_phrase(start_date, end_date, ' across the whole corpus')
    extra = ' + rehydrate' if rehydrate else ''
    _ok(request, f'Re-processing dispatched: annotate {_REPROCESS_SCOPE_LABELS[scope]}{period}{extra} — '
                  'walks onto the heavy queue and returns immediately. Watch Article states below; safe to re-run.')


def _handle_run_stream(request):
    """Run one stream collector now (prices/notam/earthquakes/forex). The task
    itself no-ops when the stream's feature flag is off, so this is always safe."""
    from services.queue import enqueue
    from services import tasks as T

    name = request.POST.get('stream', '')
    if name not in _STREAM_NAMES:
        messages.error(request, f'Unknown stream: {name}')
        return
    enqueue(T.run_stream_task, name, queue='default')
    _ok(request, f'Stream "{name}" run enqueued.')


def _handle_aggregate_history(request):
    """Aggregate backfilled articles into Events over a historical date range.

    The missing second half of every article backfill: the live aggregate
    stage only looks back 168h, so backfilled articles never form Events until
    aggregate_history_task walks their date range. Idempotent (upsert keyed on
    location/category/day). Mirrors bootstrap_initial_data_task's usage —
    bulk queue, no time cap.
    """
    from services.queue import enqueue_bulk
    from services import tasks as T

    date_range = _parse_date_range(request, required=True)
    if date_range is None:
        return
    start_date, end_date = date_range
    enqueue_bulk(T.aggregate_history_task, start_date, end_date)
    _ok(request, f'Historical aggregation enqueued for {start_date.date()} → {end_date.date()} (bulk queue).')


def _handle_retroactive_tag(request):
    """Retroactively tag historical events for one topic (slug + lookback)."""
    from services.queue import enqueue
    from services import tasks as T
    import core.models as m

    slug = request.POST.get('topic_slug', '').strip()
    if not slug:
        messages.error(request, 'No topic slug provided.')
        return
    if not m.Topic.objects.filter(slug=slug).exists():
        messages.error(request, f'Topic "{slug}" not found.')
        return
    raw_hours = request.POST.get('lookback_hours', '').strip() or '72'
    try:
        lookback_hours = int(raw_hours)
        if lookback_hours <= 0:
            raise ValueError
    except ValueError:
        messages.error(request, f'Invalid lookback hours {raw_hours!r} — expected a positive integer.')
        return
    enqueue(T.retroactive_tag_topic_task, slug, lookback_hours, queue='default')
    _ok(request, f'Retroactive tagging enqueued for "{slug}" (lookback {lookback_hours}h).')


def _handle_cancel(request):
    job_id = request.POST.get('job_id', '').strip()
    if not job_id:
        messages.error(request, 'No job id provided.')
        return
    from app.celery import app as celery_app
    cancelled = False
    try:
        celery_app.control.revoke(job_id, terminate=True, signal='SIGTERM')
        cancelled = True
    except Exception:  # noqa: BLE001
        pass
    if cancelled:
        _ok(request, f'Cancel requested for job {job_id}.')
    else:
        messages.error(request, f'Could not cancel job {job_id}.')


def _ok(request, msg):
    messages.success(request, msg)


def _int_or(value, default: int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Data gathering ───────────────────────────────────────────────────────────

def _upcoming():
    """Next scheduled time per task from api/crontab."""
    try:
        from core.utils.crontab_schedule import upcoming_runs
        return upcoming_runs()
    except Exception as exc:  # noqa: BLE001
        logger.debug('[dashboard] upcoming unavailable: %s', exc)
        return []


def _flower_status():
    """Live worker snapshot from Flower's events-based state (ground truth for
    what is actually online/executing, unlike best-effort TaskRun rows).

    ``GET {FLOWER_INTERNAL_URL}/flower/workers?json=1`` returns
    ``{"data": [worker dicts]}`` — event counters keyed 'task-received',
    'task-succeeded', … plus hostname/status/active/processed/loadavg. Counter
    keys contain dashes, so normalize them here (templates can't look them up).
    """
    from django.conf import settings

    try:
        import requests

        resp = requests.get(f'{settings.FLOWER_INTERNAL_URL}/flower/workers', params={'json': 1}, timeout=4)
        resp.raise_for_status()
        data = resp.json().get('data', [])
    except Exception as exc:  # noqa: BLE001
        logger.debug('[dashboard] flower unavailable: %s', exc)
        return {'available': False}

    workers = [
        {
            'hostname': w.get('hostname', '?'),
            'online': bool(w.get('status')),
            'active': w.get('active') or 0,
            'processed': w.get('processed') or 0,
            'succeeded': w.get('task-succeeded', 0),
            'failed': w.get('task-failed', 0),
            'retried': w.get('task-retried', 0),
            'loadavg': w.get('loadavg'),
        }
        for w in data
    ]
    workers.sort(key=lambda w: w['hostname'])
    return {'available': True, 'workers': workers}


# Friendly label for each queue — mirrors the worker-type split documented in
# CLAUDE.md (default = light I/O, heavy = NLP/LLM model stack, bulk = long
# one-shot jobs + pure dispatchers).
_QUEUE_LABELS = {
    'default': 'default — light I/O (fetch, stream collectors, stage dispatch)',
    'heavy': 'heavy — NLP/LLM (annotate/refine/aggregate/tag)',
    'bulk': 'bulk — long jobs + dispatchers (backfills, reprocess, forecast)',
}


def _queue_depths():
    """Backlog depth per Celery queue — how many messages are sitting in Redis
    waiting for a worker, as opposed to ``_flower_status``'s per-worker
    "active" count (currently executing). This is what actually tells you
    whether e.g. the heavy queue is backed up (single worker, easy to
    saturate — see reprocess_corpus_task's double-dispatch risk).

    ``GET {FLOWER_INTERNAL_URL}/flower/api/queues/length`` returns
    ``{"active_queues": [{"name": "heavy", "messages": 1234}, ...]}`` — Flower
    reads this straight off the Redis broker, so it reflects reality even if
    a worker container is down.
    """
    from django.conf import settings

    try:
        import requests

        resp = requests.get(f'{settings.FLOWER_INTERNAL_URL}/flower/api/queues/length', timeout=4)
        resp.raise_for_status()
        queues = resp.json().get('active_queues', [])
    except Exception as exc:  # noqa: BLE001
        logger.debug('[dashboard] flower queue depth unavailable: %s', exc)
        return {'available': False}

    rows = [
        {'name': q.get('name', '?'), 'label': _QUEUE_LABELS.get(q.get('name'), q.get('name', '?')),
         'messages': q.get('messages') or 0}
        for q in queues
    ]
    rows.sort(key=lambda r: r['name'])
    return {'available': True, 'rows': rows}


# Health report is written every 30 min (crontab) — older than 2× that means
# the health task itself (or the whole cron/queue path) is stuck.
_HEALTH_MAX_AGE_MINUTES = 60


def _health_status():
    """Last pipeline_health_task report from Redis, template-ready.

    Returns {'available', 'at', 'report_stale', 'freshness', 'current_topics',
    'stages'} — see services.tasks.pipeline_health_task for the report shape.
    """
    try:
        from services.cache import KEY_PIPELINE_HEALTH_LAST, cache_get
        payload = cache_get(KEY_PIPELINE_HEALTH_LAST)
    except Exception as exc:  # noqa: BLE001
        logger.debug('[dashboard] health unavailable: %s', exc)
        payload = None
    if not payload:
        return {'available': False}

    report = payload.get('report') or {}
    at = None
    try:
        at = datetime.fromisoformat(payload.get('at', ''))
    except (TypeError, ValueError):
        pass
    report_stale = at is None or at < datetime.now(timezone.utc) - timedelta(minutes=_HEALTH_MAX_AGE_MINUTES)

    freshness = [
        {'name': name, 'latest': info.get('latest'), 'ok': info.get('ok', False)}
        for name, info in report.items()
        if isinstance(info, dict) and 'latest' in info
    ]
    stages = [
        {
            'name': name,
            'pending': info.get('pending'),
            'last_dispatch': info.get('last_dispatch'),
            'ok': info.get('ok', False),
        }
        for name, info in (report.get('stages') or {}).items()
    ]
    providers = [
        {
            'name': name,
            'err_total': info.get('err_total'),
            'last_ok': info.get('last_ok'),
            'last_err': info.get('last_err'),
            'ok': info.get('ok', False),
        }
        for name, info in (report.get('providers') or {}).items()
    ]
    return {
        'available': True,
        'at': at,
        'report_stale': report_stale,
        'freshness': freshness,
        'current_topics': report.get('current_topics'),
        'stages': stages,
        'providers': providers,
    }


def _forecast_status():
    from django.conf import settings
    from core.models import Forecast

    status = {'enabled': getattr(settings, 'FORECAST_ENABLED', False), 'artifacts': []}
    model_dir = getattr(settings, 'FORECAST_MODEL_DIR', '')
    if model_dir and os.path.isdir(model_dir):
        for fn in sorted(os.listdir(model_dir)):
            if fn.endswith('.joblib'):
                p = os.path.join(model_dir, fn)
                status['artifacts'].append({'name': fn, 'mtime': datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.utc)})
    latest = Forecast.objects.order_by('-generated_at').values_list('generated_at', flat=True).first()
    status['last_forecast'] = latest
    scored = Forecast.objects.filter(is_correct__isnull=False)
    total = scored.count()
    correct = scored.filter(is_correct=True).count()
    status['accuracy'] = round(correct / total, 3) if total else None
    status['scored'] = total
    return status


def _llm_status():
    """
    Per-provider call stats (ok/err/avg_ms) + debounce state from Redis.
    Debounce keys: llm:debounce:{provider}:{hash}  — count active ones per provider.
    Stats keys:    llm:req:{provider}:{ok|err|ms|last_ok|last_err}
    """
    try:
        from django.conf import settings
        from services.cache import get_redis_client, key_llm_debounce_scan_pattern, key_llm_req_stat
        rc = get_redis_client(write=False)

        all_providers: set[str] = set()
        for route in settings.LLM_ROUTES.values():
            # A route is a name or a list of legs; a leg is a name or a set of
            # names (balanced group). Flatten one level so groups don't leak an
            # unhashable set into all_providers.
            for leg in ([route] if isinstance(route, str) else route):
                if isinstance(leg, (set, frozenset, list, tuple)):
                    all_providers.update(leg)
                else:
                    all_providers.add(leg)

        rows = []
        for provider in sorted(all_providers):
            ok  = int(rc.get(key_llm_req_stat(provider, 'ok'))  or 0)
            err = int(rc.get(key_llm_req_stat(provider, 'err')) or 0)
            ms  = float(rc.get(key_llm_req_stat(provider, 'ms')) or 0)
            avg_ms = int(ms / (ok + err)) if (ok + err) else 0

            last_ok_ts  = rc.get(key_llm_req_stat(provider, 'last_ok'))
            last_err_ts = rc.get(key_llm_req_stat(provider, 'last_err'))
            last_ok  = datetime.fromtimestamp(int(last_ok_ts),  tz=timezone.utc) if last_ok_ts  else None
            last_err = datetime.fromtimestamp(int(last_err_ts), tz=timezone.utc) if last_err_ts else None

            # Count active debounce tokens and find shortest remaining TTL
            debounce_keys = rc.keys(key_llm_debounce_scan_pattern(provider))
            debounced = 0
            min_ttl: int | None = None
            for k in debounce_keys:
                ttl = rc.ttl(k)
                if ttl > 0:
                    debounced += 1
                    if min_ttl is None or ttl < min_ttl:
                        min_ttl = ttl

            rows.append({
                'provider': provider,
                'ok': ok,
                'err': err,
                'avg_ms': avg_ms,
                'debounced': debounced,
                'cooldown_s': min_ttl,
                'last_ok': last_ok,
                'last_err': last_err,
            })
        return rows
    except Exception as exc:
        logger.debug('[dashboard] llm_status unavailable: %s', exc)
        return []


# Pipeline position for each stage — mirrors the fetch → {analyze | annotate →
# refine} → aggregate → tag → route diagram in CLAUDE.md. analyze/annotate
# share step 2: they're parallel branches over the same 'fetched' input,
# partitioned by article freshness (see services/stages.py), not a sequence.
# Used to number the Stages table and to tie the Actions tab's historical-range
# equivalents (Backfill & history) to the same terminology/order.
_STAGE_STEP = {'fetch': 1, 'analyze': 2, 'annotate': 2, 'refine': 3, 'aggregate': 4, 'tag': 5, 'route': 6}

# 'aggregate' is a singleton stage (no per-record Reprocess button — see
# pipeline_coverage) whose only manual trigger for a historical date range
# lives in the Actions tab instead of here; point there rather than leaving
# the row's action cell silently empty.
_STAGE_NOTE = {'aggregate': 'historical range: see Backfill & history ↓'}


def _coverage():
    from services.workflow import pipeline_coverage
    rows = pipeline_coverage()
    for row in rows:
        row['step'] = _STAGE_STEP.get(row['stage'])
        row['note'] = _STAGE_NOTE.get(row['stage'])
    return rows


def _article_states():
    """Article census by pipeline stage (the stored Article.stage field — the
    same field the annotate/refine stage predicates filter on, so the numbers
    can't drift from what the stages actually select). Terminal articles are
    split into located / no-location for visibility into location coverage.

    Returns a list of {key, label, count, tone, hint} ordered fetched→located.
    """
    from django.db.models import Q
    from core import models as m

    A = m.Article.objects
    live = A.exclude(annotation_deferred=True)  # deferred is its own bucket
    # Terminal = annotated-confident or refined (both fully analysed).
    terminal = live.filter(stage__in=[m.Article.STAGE_ANNOTATED, m.Article.STAGE_REFINED])
    no_loc = Q(location__isnull=True) | Q(location='')

    rows = [
        ('fetched', 'Fetched — awaiting analysis', 'muted',
         live.filter(stage=m.Article.STAGE_FETCHED).count(),
         'queued for analyze (fresh live traffic, cloud LLM) or annotate '
         '(historical/backfill, on-prem NLP) — see the coverage table below '
         'for the exact split'),
        ('refine', 'Annotated — awaiting judge', 'muted',
         live.filter(stage=m.Article.STAGE_REFINE).count(),
         'low-confidence classification, queued for the refine stage'),
        ('located', 'Annotated · located', 'ok',
         terminal.filter(location__isnull=False).exclude(location='').count(),
         'geocoded inline — flows on to events'),
        ('no_location', 'Annotated · no location', 'muted',
         terminal.filter(no_loc).count(),
         'analysed but no place resolved — terminal (never aggregates), kept for training'),
        ('deferred', 'Deferred — awaiting (re)annotation', 'warn',
         A.filter(annotation_deferred=True).count(),
         'backfill + reset analysis-failures; parked off the live pipeline'),
    ]
    total = sum(r[3] for r in rows)
    return {
        'total': total,
        'rows': [
            {'key': k, 'label': lbl, 'tone': tone, 'count': n, 'hint': hint,
             'pct': round(100 * n / total, 1) if total else 0.0}
            for (k, lbl, tone, n, hint) in rows
        ],
    }


def _llm_flags():
    """Current live/backfill LLM master-switch states for the Actions toggles."""
    from services.runtime_config import llm_flags
    return llm_flags()


# ── Activity-per-month chart (events by category + total articles) ─────────

_CHART_MONTHS_BACK = 12 * 10  # 10 years

# EventCategory value -> swatch color. Legacy 'protest'/'crime' get colors too
# since old data still carries them (see EventCategory in core/models.py).
_CATEGORY_COLORS = {
    'conflict': '#e05252',
    'disaster': '#e0a052',
    'economic': '#5cb85c',
    'political': '#79aec8',
    'health': '#b19cd9',
    'general': '#8b93a0',
    'protest': '#d9c74a',
    'crime': '#c9648c',
}
_ARTICLES_LINE_COLOR = '#e8e8e8'
# Annotated/refined slots — validated as a categorical pair against this
# dashboard's dark chart surface (scripts/validate_palette.js in the dataviz
# skill: lightness band, chroma floor, CVD separation, contrast all pass).
_ANNOTATED_COLOR = '#3987e5'
_REFINED_COLOR = '#d95926'

_CHART_W, _CHART_H = 900, 260
_CHART_PAD_L, _CHART_PAD_R, _CHART_PAD_T, _CHART_PAD_B = 40, 40, 10, 26


def _month_buckets(months_back):
    """[(start, end, label), ...] oldest-first UTC calendar-month ranges."""
    now = datetime.now(timezone.utc)
    buckets = []
    year, month = now.year, now.month
    for _ in range(months_back):
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = (
            datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=timezone.utc)
        )
        buckets.append((start, end, start.strftime('%b %Y')))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    buckets.reverse()
    return buckets


def _activity_chart():
    """Two per-month series for the last 10 years, feeding the two charts
    rendered below (_render_events_chart_svg / _render_processing_chart_svg):
    Events (stacked by category) and Articles-by-processing-depth (Annotated
    + Refined, stacked, against a total-ingested reference line) — exact
    counts of what's actually in the database. No deletion/capping task
    exists anywhere in the pipeline (every article is kept as training data —
    see CLAUDE.md), so these are true totals, not a sample or a slice.

    Events are fetched as one ranged query (``category``, ``started_at``
    only) and bucketed in Python — the codebase bans the ``__date`` ORM
    lookup and has no precedent for Mongo ``$group``/``annotate(Count(...))``,
    so per-bucket-loop-in-Python is the established pattern (see e.g.
    evaluate_freshness.py). Articles are counted with one indexed
    ``.count()`` per month bucket instead, since a decade of raw per-source
    ingest is too large to pull into Python for two-field bucketing; Article
    had no standalone index on ``published_on`` before core/migrations 0012
    (only a compound ``(processed_on, published_on)`` one, which doesn't
    serve a ``published_on``-only filter), so this used to be a full
    collection scan per bucket — 0012 also adds ``(published_on,
    importance_score)``, whose prefix now serves the plain range filter.
    """
    from core.models import Article, Event, EventCategory

    buckets = _month_buckets(_CHART_MONTHS_BACK)
    n = len(buckets)
    earliest = buckets[0][0]

    event_counts = [{} for _ in range(n)]
    rows = Event.objects.filter(started_at__gte=earliest).values('category', 'started_at')
    for row in rows:
        ts = row['started_at']
        if ts is None:
            continue
        for i, (start, end, _label) in enumerate(buckets):
            if start <= ts < end:
                counts = event_counts[i]
                counts[row['category']] = counts.get(row['category'], 0) + 1
                break

    # Three counts per bucket (ingested / annotated / refined) instead of one —
    # same per-bucket .count() approach as before, just filtered further, so
    # this stays index-served rather than pulling rows into Python.
    article_counts, annotated_counts, refined_counts = [], [], []
    for start, end, _label in buckets:
        qs = Article.objects.filter(published_on__gte=start, published_on__lt=end)
        article_counts.append(qs.count())
        annotated_counts.append(qs.filter(stage=Article.STAGE_ANNOTATED).count())
        refined_counts.append(qs.filter(stage=Article.STAGE_REFINED).count())

    category_labels = dict(EventCategory.choices)
    categories = [c for c in category_labels if any(b.get(c) for b in event_counts)]
    labels = [label for _start, _end, label in buckets]
    series = {c: [event_counts[i].get(c, 0) for i in range(n)] for c in categories}

    return {
        'labels': labels,
        'categories': [
            {
                'key': c,
                'label': category_labels[c],
                'color': _CATEGORY_COLORS.get(c, '#666'),
                'total': sum(series[c]),
            }
            for c in categories
        ],
        'events_svg': _render_events_chart_svg(labels, categories, series, category_labels),
        'processing_svg': _render_processing_chart_svg(labels, annotated_counts, refined_counts, article_counts),
        'articles_total': sum(article_counts),
        'annotated_total': sum(annotated_counts),
        'refined_total': sum(refined_counts),
        'annotated_color': _ANNOTATED_COLOR,
        'refined_color': _REFINED_COLOR,
        'articles_color': _ARTICLES_LINE_COLOR,
    }


# Shared SVG geometry/chrome for both activity charts below. No JS charting
# library exists anywhere in this repo (the React frontend's Recharts dep
# doesn't reach the Django admin), so these draw directly in Python rather
# than pulling in a new client-side dependency. Each chart gets its own single
# y-axis — events and article-processing volume differ by orders of
# magnitude, so per the dataviz method they're two charts, not one chart with
# two y-scales (a dual axis conflates two different scales into one
# misleading line).
_PLOT_W = _CHART_W - _CHART_PAD_L - _CHART_PAD_R
_PLOT_H = _CHART_H - _CHART_PAD_T - _CHART_PAD_B


def _px(i, n):
    return _CHART_PAD_L + (_PLOT_W * i / (n - 1) if n > 1 else _PLOT_W / 2)


def _py(v, scale):
    return _CHART_PAD_T + _PLOT_H - (_PLOT_H * v / scale)


def _nice_max(v):
    import math
    v = max(v, 1)
    magnitude = 10 ** max(len(str(int(v))) - 1, 0)
    return int(math.ceil(v / magnitude) * magnitude) or 1


def _chart_svg_open(aria_label):
    return f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" class="ops-chart-svg" role="img" aria-label="{aria_label}">'


def _chart_gridlines(y_max):
    """Single left-axis gridlines + labels (0/25/50/75/100%)."""
    parts = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        gy = _py(y_max * frac, y_max)
        parts.append(
            f'<line x1="{_CHART_PAD_L}" y1="{gy:.1f}" x2="{_CHART_W - _CHART_PAD_R}" y2="{gy:.1f}" '
            f'class="ops-chart-gridline"/>'
        )
        parts.append(
            f'<text x="{_CHART_PAD_L - 6}" y="{gy:.1f}" class="ops-chart-axis-label" '
            f'text-anchor="end" dominant-baseline="middle">{int(y_max * frac)}</text>'
        )
    return parts


def _chart_x_labels(labels):
    """X-axis labels, thinned to roughly one per year over a 10y window."""
    from django.utils.html import escape
    n = len(labels)
    step = max(1, n // 10)
    return [
        f'<text x="{_px(i, n):.1f}" y="{_CHART_H - 6}" class="ops-chart-axis-label" '
        f'text-anchor="middle">{escape(labels[i])}</text>'
        for i in range(0, n, step)
    ]


def _chart_hover_rects(n, tooltip_lines):
    """One transparent full-height rect per bucket (drawn last, on top),
    spanning the midpoints to its neighbors, each carrying that month's
    breakdown in a data-tooltip attribute — the tiny JS snippet in
    dashboard.html positions a div from it on mousemove. tooltip_lines is a
    list (one per bucket) of list[str]."""
    from django.utils.html import escape
    plot_left, plot_right = _CHART_PAD_L, _CHART_W - _CHART_PAD_R
    parts = []
    for i in range(n):
        x_left = plot_left if i == 0 else (_px(i - 1, n) + _px(i, n)) / 2
        x_right = plot_right if i == n - 1 else (_px(i, n) + _px(i + 1, n)) / 2
        tooltip = escape('\n'.join(tooltip_lines[i]))
        parts.append(
            f'<rect x="{x_left:.1f}" y="{_CHART_PAD_T}" width="{(x_right - x_left):.1f}" '
            f'height="{_PLOT_H:.1f}" class="ops-chart-hover" data-tooltip="{tooltip}"/>'
        )
    return parts


def _stacked_area_path(counts, base, n, y_max):
    """One <path> for a stacked-area layer sitting on top of ``base`` (the
    running cumulative from lower layers). Returns (path_d, new_base)."""
    top = [base[i] + counts[i] for i in range(n)]
    pts_top = [(_px(i, n), _py(top[i], y_max)) for i in range(n)]
    pts_bottom = [(_px(i, n), _py(base[i], y_max)) for i in range(n)][::-1]
    d = 'M ' + ' L '.join(f'{x:.1f},{y:.1f}' for x, y in pts_top + pts_bottom) + ' Z'
    return d, top


def _render_events_chart_svg(labels, categories, series, category_labels):
    """Stacked-area SVG, one axis (events per month, by category)."""
    from django.utils.safestring import mark_safe

    n = len(labels)
    if not n:
        return mark_safe('')

    event_totals = [sum(series[c][i] for c in categories) for i in range(n)] if categories else [0] * n
    y_max = _nice_max(max(event_totals))

    parts = [_chart_svg_open('Events per month by category')]
    parts += _chart_gridlines(y_max)

    cum = [0] * n
    for c in categories:
        d, cum = _stacked_area_path(series[c], cum, n, y_max)
        color = _CATEGORY_COLORS.get(c, '#666')
        parts.append(f'<path d="{d}" fill="{color}" fill-opacity="0.75" stroke="{color}" stroke-width="1"/>')

    parts += _chart_x_labels(labels)

    tooltip_lines = []
    for i in range(n):
        lines = [labels[i]]
        for c in categories:
            v = series[c][i]
            if v:
                lines.append(f'{category_labels[c]}: {v}')
        tooltip_lines.append(lines)
    parts += _chart_hover_rects(n, tooltip_lines)

    parts.append('</svg>')
    return mark_safe(''.join(parts))


def _render_processing_chart_svg(labels, annotated_counts, refined_counts, article_counts):
    """Stacked-area (Annotated + Refined — both terminal "fully analysed"
    states, see Article states below) with a total-ingested reference line,
    one axis (article count — same unit across all three series)."""
    from django.utils.safestring import mark_safe

    n = len(labels)
    if not n:
        return mark_safe('')

    y_max = _nice_max(max(article_counts) if article_counts else 0)

    parts = [_chart_svg_open('Articles processed per month by pipeline stage, vs. total ingested')]
    parts += _chart_gridlines(y_max)

    cum = [0] * n
    for counts, color in ((annotated_counts, _ANNOTATED_COLOR), (refined_counts, _REFINED_COLOR)):
        d, cum = _stacked_area_path(counts, cum, n, y_max)
        parts.append(f'<path d="{d}" fill="{color}" fill-opacity="0.75" stroke="{color}" stroke-width="1"/>')

    line_pts = [(_px(i, n), _py(v, y_max)) for i, v in enumerate(article_counts)]
    line_d = 'M ' + ' L '.join(f'{x:.1f},{y:.1f}' for x, y in line_pts)
    parts.append(f'<path d="{line_d}" fill="none" stroke="{_ARTICLES_LINE_COLOR}" stroke-width="2" stroke-dasharray="4 3"/>')

    parts += _chart_x_labels(labels)

    tooltip_lines = [
        [labels[i], f'Annotated: {annotated_counts[i]}', f'Refined: {refined_counts[i]}',
         f'Ingested: {article_counts[i]}']
        for i in range(n)
    ]
    parts += _chart_hover_rects(n, tooltip_lines)

    parts.append('</svg>')
    return mark_safe(''.join(parts))


def dashboard_view(request):
    if request.method == 'POST':
        return _handle_action(request)

    # The sections are independent (Mongo, Redis, Flower HTTP, crontab file),
    # so fetch them concurrently — page latency is the slowest source, not the
    # sum. Threads, not an async view: every fetcher is blocking I/O and the
    # ORM is thread-safe with per-thread connections.
    fetchers = {
        'health': _health_status,
        'upcoming': _upcoming,
        'flower': _flower_status,
        'queue_depths': _queue_depths,
        'coverage': _coverage,
        'article_states': _article_states,
        'forecast': _forecast_status,
        'llm_status': _llm_status,
        'llm_flags': _llm_flags,
        'activity_chart': _activity_chart,
    }
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {name: pool.submit(fn) for name, fn in fetchers.items()}
    results, errors = {}, {}
    for name, fut in futures.items():
        try:
            results[name] = fut.result()
        except Exception as exc:  # noqa: BLE001
            logger.exception('[dashboard] %s failed', name)
            results[name] = [] if name == 'coverage' else None
            errors[name] = exc
    for name, exc in errors.items():
        messages.warning(request, f'{name} unavailable: {exc}')

    now = datetime.now(timezone.utc)
    context = {
        **admin.site.each_context(request),
        'title': 'Operations Dashboard',
        # Prefills "1. Fetch articles" so the common case (last 14 days, all
        # sources) needs no typing — submit the fields as shown.
        'default_backfill_start': (now - timedelta(days=14)).date().isoformat(),
        'default_backfill_end': now.date().isoformat(),
        **results,
    }
    return render(request, 'admin/dashboard.html', context)
