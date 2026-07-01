"""Server-rendered admin operations dashboard.

A single page under ``/admin/dashboard/`` summarizing pipeline operations and
offering POST actions. Data sources: ``api/crontab`` (upcoming runs), RQ
StartedJobRegistry (in-flight), ``pipeline_coverage()`` (per-stage
gaps), and forecast artifacts/rows. Registered via a ``get_urls`` shim in
``core/admin.py``.
"""

import logging
import os
from datetime import datetime, timezone

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
            enqueue(T.dispatch_fetch_task, queue='default')
            enqueue(T.dispatch_process_articles_task, queue='default')
            enqueue(T.aggregate_events_task, queue='heavy', job_timeout=-1)
            _ok(request, 'Pipeline dispatchers enqueued (fetch → process → aggregate).')
        elif action == 'backfill_prices':
            enqueue(T.backfill_prices_task, years=10, queue='bulk', job_timeout=-1)
            _ok(request, 'Price backfill enqueued (10y, all active symbols).')
        elif action == 'backfill_articles':
            from datetime import datetime, timedelta, timezone as dt_timezone
            now = datetime.now(dt_timezone.utc)
            enqueue(T.backfill_all_sources_task, now - timedelta(days=14), now, None,
                    queue='bulk', job_timeout=-1)
            _ok(request, 'Article backfill enqueued (weighted per-source, all sources).')
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
    from services.queue import enqueue
    from services import tasks as T

    stage = request.POST.get('stage', '')
    if stage == 'geocode':
        n = enqueue(T.dispatch_process_articles_task, only_failed=True, queue='default')
        _ok(request, 'Re-dispatched processed-but-unlocated articles.')
    elif stage == 'process':
        enqueue(T.dispatch_process_articles_task, queue='default')
        _ok(request, 'Re-dispatched unprocessed articles.')
    elif stage == 'tag':
        enqueue(T.dispatch_tag_topics_task, force_retag=False, queue='default')
        _ok(request, 'Re-dispatched untagged events.')
    elif stage == 'route':
        enqueue(T.dispatch_route_events_task, queue='default')
        _ok(request, 'Re-dispatched unrouted events.')
    else:
        messages.error(request, f'Unknown reprocess stage: {stage}')


def _handle_cancel(request):
    job_id = request.POST.get('job_id', '').strip()
    if not job_id:
        messages.error(request, 'No job id provided.')
        return
    import django_rq
    from rq.command import send_stop_job_command
    from rq.job import Job
    conn = django_rq.get_connection('default')
    cancelled = False
    try:
        send_stop_job_command(conn, job_id)  # stop if currently executing
        cancelled = True
    except Exception:  # noqa: BLE001 — not running; try registry cancel
        pass
    try:
        Job.fetch(job_id, connection=conn).cancel()
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

def _throughput():
    """Queue depth per queue — shown in the throughput table."""
    try:
        import django_rq
        from rq.queue import Queue
        conn = django_rq.get_connection('default')
        stats = {}
        for qname in ('default', 'heavy', 'bulk'):
            q = Queue(qname, connection=conn)
            stats[qname] = {
                'today_items': q.count, 'today_runs': q.count, 'today_failed': 0,
                'yest_items': 0, 'yest_runs': 0, 'yest_failed': 0,
                'last_success': None, 'last_error': '',
            }
        return stats
    except Exception as exc:
        logger.debug('[dashboard] throughput unavailable: %s', exc)
        return {}


def _upcoming():
    """Next scheduled time per task from api/crontab."""
    try:
        from core.utils.crontab_schedule import upcoming_runs
        return upcoming_runs()
    except Exception as exc:  # noqa: BLE001
        logger.debug('[dashboard] upcoming unavailable: %s', exc)
        return []


def _in_flight():
    """Currently executing jobs from the RQ StartedJobRegistry."""
    try:
        import django_rq
        from rq.job import Job
        from rq.registry import StartedJobRegistry
        conn = django_rq.get_connection('default')
        running = []
        for qname in ('default', 'heavy', 'bulk'):
            for job_id in StartedJobRegistry(qname, connection=conn).get_job_ids():
                try:
                    job = Job.fetch(job_id, connection=conn)
                    running.append({
                        'task_name': job.func_name.split('.')[-1],
                        'started_at': job.started_at,
                        'job_id': job.id,
                    })
                except Exception:  # noqa: BLE001
                    pass
        return sorted(running, key=lambda x: x['started_at'] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:25]
    except Exception as exc:  # noqa: BLE001
        logger.debug('[dashboard] in_flight unavailable: %s', exc)
        return []


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
        'throughput': _throughput(),
        'upcoming': _upcoming(),
        'in_flight': _in_flight(),
        'coverage': coverage,
        'forecast': _forecast_status(),
        'llm_status': _llm_status(),
    }
    return render(request, 'admin/dashboard.html', context)
