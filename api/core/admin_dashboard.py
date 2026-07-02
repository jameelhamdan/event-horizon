"""Server-rendered admin operations dashboard.

A single page under ``/admin/dashboard/`` summarizing pipeline operations and
offering POST actions. Data sources: ``api/crontab`` (upcoming runs),
``core.models.TaskRun`` (per-queue queued/running/failed counts, linking into
the task browser at ``/admin/core/taskrun/`` for individual task detail —
that's our RQ-admin / Flower equivalent), ``pipeline_coverage()`` (per-stage
gaps), and forecast artifacts/rows. Registered via a ``get_urls`` shim in
``core/admin.py``.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from django.contrib import admin, messages
from django.shortcuts import redirect, render

logger = logging.getLogger(__name__)


# ── POST action handlers ─────────────────────────────────────────────────────

def _handle_action(request):
    from services.queue import enqueue
    from services import tasks as T

    action = request.POST.get('dashboard_action', '')
    try:
        if action == 'run_pipeline':
            # force=True: dispatch every enabled stage with pending work,
            # skipping the per-stage cadence gates (see services/stages.py).
            enqueue(T.pipeline_tick_task, True, queue='default')
            _ok(request, 'Pipeline tick enqueued (all due stages, cadence gates skipped).')
        elif action == 'backfill_prices':
            enqueue(T.backfill_prices_task, years=10, queue='bulk', job_timeout=-1)
            _ok(request, 'Price backfill enqueued (10y, all active symbols).')
        elif action == 'backfill_articles':
            from datetime import datetime, timedelta, timezone as dt_timezone
            now = datetime.now(dt_timezone.utc)
            enqueue(T.backfill_history_task, now - timedelta(days=14), now, None, queue='bulk')
            _ok(request, 'Article backfill enqueued (weighted per-source, all sources).')
        elif action == 'backfill_articles_until':
            _handle_backfill_until(request)
        elif action == 'retrain_forecast':
            enqueue(T.train_forecast_model_task, queue='bulk', job_timeout=-1)
            enqueue(T.run_forecast_task, queue='bulk', job_timeout=-1)
            _ok(request, 'Forecast retrain + run enqueued (bulk queue).')
        elif action == 'rerun_bootstrap':
            enqueue(T.bootstrap_initial_data_task, True, queue='default', job_timeout=-1)
            _ok(request, 'First-load bootstrap re-triggered (force).')
        elif action == 'reprocess':
            _handle_reprocess(request)
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


def _handle_backfill_until(request):
    """Backfill articles from now backward to an operator-chosen date.

    Mirrors ``manage.py backfill_history --until``. ``source_code`` is
    optional — blank means all enabled RSS sources.
    """
    from datetime import date, datetime, timezone as dt_timezone

    from services.queue import enqueue
    from services import tasks as T

    until_raw = request.POST.get('until_date', '').strip()
    source_code = request.POST.get('source_code', '').strip()

    try:
        until = date.fromisoformat(until_raw)
    except ValueError:
        messages.error(request, f'Invalid date {until_raw!r} — expected YYYY-MM-DD.')
        return

    start_date = datetime(until.year, until.month, until.day, tzinfo=dt_timezone.utc)
    end_date = datetime.now(dt_timezone.utc)
    if start_date >= end_date:
        messages.error(request, 'Date must be in the past.')
        return

    if source_code:
        import core.models as m
        if not m.Source.objects.filter(code=source_code).exists():
            messages.error(request, f'Source "{source_code}" not found.')
            return
        enqueue(T.backfill_history_task, start_date, end_date, source_code, queue='bulk')
        _ok(request, f'Article backfill enqueued for "{source_code}" back to {until}.')
    else:
        enqueue(T.backfill_history_task, start_date, end_date, None, queue='bulk')
        _ok(request, f'Article backfill enqueued (all sources) back to {until}.')


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


# ── Data gathering ───────────────────────────────────────────────────────────

def _broker_depths() -> dict:
    """Broker queue depth per queue.

    Celery's default Redis transport stores pending (unclaimed) task messages
    as a Redis list keyed by queue name, so a plain LLEN gives the queue depth
    without needing a result backend or app.control round-trip. Shown next to
    TaskRun's 'queued' count — the two disagreeing persistently means lost or
    untracked messages.
    """
    try:
        import redis
        from django.conf import settings
        conn = redis.Redis.from_url(settings.CELERY_BROKER_URL)
        return {q: conn.llen(q) for q in ('default', 'heavy', 'bulk')}
    except Exception as exc:
        logger.debug('[dashboard] broker depths unavailable: %s', exc)
        return {}


def _upcoming():
    """Next scheduled time per task from api/crontab."""
    try:
        from core.utils.crontab_schedule import upcoming_runs
        return upcoming_runs()
    except Exception as exc:  # noqa: BLE001
        logger.debug('[dashboard] upcoming unavailable: %s', exc)
        return []


def _queue_summary():
    """Per-queue queued/running/failed-today counts, each linking into the task
    browser (/admin/core/taskrun/) filtered to that queue+status — replaces the
    old ad hoc in-flight table now that individual tasks live in TaskRun admin."""
    try:
        from django.conf import settings
        from django.urls import reverse
        from core.models import TaskRun

        base = reverse('admin:core_taskrun_changelist')
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        workers = getattr(settings, 'CELERY_QUEUE_WORKERS', {})
        depths = _broker_depths()
        rows = []
        for q in ('default', 'heavy', 'bulk'):
            counts = {}
            for status in (TaskRun.Status.QUEUED, TaskRun.Status.RUNNING):
                counts[status] = TaskRun.objects.filter(queue=q, status=status).count()
            counts['failed'] = TaskRun.objects.filter(
                queue=q, status=TaskRun.Status.FAILED, started_at__gte=cutoff,
            ).count()
            rows.append({
                'queue': q,
                'workers': workers.get(q),
                'depth': depths.get(q),
                'queued': counts[TaskRun.Status.QUEUED],
                'queued_url': f'{base}?queue={q}&status={TaskRun.Status.QUEUED}',
                'running': counts[TaskRun.Status.RUNNING],
                'running_url': f'{base}?queue={q}&status={TaskRun.Status.RUNNING}',
                'failed': counts['failed'],
                'failed_url': f'{base}?queue={q}&status={TaskRun.Status.FAILED}',
                'all_url': f'{base}?queue={q}',
            })
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.debug('[dashboard] queue_summary unavailable: %s', exc)
        return []


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
    report_stale = (
        at is None
        or at < datetime.now(timezone.utc) - timedelta(minutes=_HEALTH_MAX_AGE_MINUTES)
    )

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
    return {
        'available': True,
        'at': at,
        'report_stale': report_stale,
        'freshness': freshness,
        'current_topics': report.get('current_topics'),
        'stages': stages,
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
                status['artifacts'].append({
                    'name': fn,
                    'mtime': datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.utc),
                })
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
            if isinstance(route, str):
                all_providers.add(route)
            else:
                all_providers.update(route)

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


def dashboard_view(request):
    if request.method == 'POST':
        return _handle_action(request)

    from services.workflow import pipeline_coverage
    try:
        coverage = pipeline_coverage()
    except Exception as exc:  # noqa: BLE001
        logger.exception('[dashboard] coverage failed')
        coverage = []
        messages.warning(request, f'Coverage unavailable: {exc}')

    context = {
        **admin.site.each_context(request),
        'title': 'Operations Dashboard',
        'health': _health_status(),
        'upcoming': _upcoming(),
        'queue_summary': _queue_summary(),
        'coverage': coverage,
        'forecast': _forecast_status(),
        'llm_status': _llm_status(),
    }
    return render(request, 'admin/dashboard.html', context)
