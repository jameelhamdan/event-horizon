"""Server-rendered admin operations dashboard.

A single page under ``/admin/dashboard/`` summarizing pipeline operations and
offering POST actions. Data sources: ``api/crontab`` (upcoming runs),
Flower's ``/workers?json=1`` API (live worker ground truth — individual task
detail lives in the task browser at ``/admin/core/taskrun/``),
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

# One-click crontab-task triggers: action value → (task name, queue, message).
# Every standalone crontab task gets a button so any stalled stage can be
# resumed from the dashboard without shell access. Queues mirror run_task.py's
# authoritative map (HEAVY_TASKS / BULK_TASKS, everything else 'default').
# The template renders 'prune_stale_articles' (deletes data) and
# 'generate_newsletter' (sends real email to subscribers) behind an
# are-you-sure confirm() — keep it that way if you add more destructive ones.
_TASK_ACTIONS = {
    'refresh_topics': ('refresh_topics_task', 'heavy', 'Topic refresh enqueued (Wikipedia current events + LLM enrichment).'),
    'discover_topics': ('discover_topics_task', 'heavy', 'Topic discovery enqueued (LLM over recent events).'),
    'generate_newsletter': ('generate_newsletter_task', 'heavy', 'Newsletter generation enqueued — WILL EMAIL SUBSCRIBERS when it completes.'),
    'pipeline_health': ('pipeline_health_task', 'default', 'Health report enqueued — the Health section refreshes on next page load.'),
    'cleanup_low_importance': ('cleanup_low_importance_articles_task', 'default', 'Low-importance article cleanup enqueued.'),
    'prune_stale_articles': ('prune_stale_articles_task', 'default', 'Stale-article prune enqueued (deletes articles).'),
    'adjust_source_weights': ('adjust_source_weights_task', 'default', 'Source-weight adjustment enqueued.'),
    'score_forecasts': ('score_forecasts_task', 'default', 'Forecast scoring enqueued.'),
    'refresh_openrouter_models': ('refresh_openrouter_models_task', 'default', 'OpenRouter model refresh enqueued.'),
}

_STREAM_NAMES = ('prices', 'notam', 'earthquakes', 'forex')


def _resolve_task(name: str):
    """Find a *_task function in services.tasks or newsletter.tasks — same
    resolution rule as manage.py run_task."""
    from newsletter import tasks as newsletter_tasks
    from services import tasks as service_tasks
    for module in (service_tasks, newsletter_tasks):
        func = getattr(module, name, None)
        if callable(func):
            return func
    raise ValueError(f'Unknown task {name!r}')


def _handle_action(request):
    from services.queue import enqueue
    from services import tasks as T

    action = request.POST.get('dashboard_action', '')
    try:
        if action in _TASK_ACTIONS:
            task_name, queue, msg = _TASK_ACTIONS[action]
            enqueue(_resolve_task(task_name), queue=queue)
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
    from datetime import date, datetime, timezone as dt_timezone

    from services.queue import enqueue
    from services import tasks as T

    parsed = []
    for field in ('start_date', 'end_date'):
        raw = request.POST.get(field, '').strip()
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            messages.error(request, f'Invalid {field} {raw!r} — expected YYYY-MM-DD.')
            return
        parsed.append(datetime(d.year, d.month, d.day, tzinfo=dt_timezone.utc))
    start_date, end_date = parsed
    if start_date >= end_date:
        messages.error(request, 'start_date must be before end_date.')
        return
    enqueue(T.aggregate_history_task, start_date, end_date, queue='bulk', job_timeout=-1)
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
        resp = requests.get(
            f'{settings.FLOWER_INTERNAL_URL}/flower/workers',
            params={'json': 1}, timeout=4,
        )
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


def _coverage():
    from services.workflow import pipeline_coverage
    return pipeline_coverage()


# ── Activity-per-month chart (events by category + total articles) ─────────

_CHART_MONTHS_BACK = 12 * 5  # 5 years

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
    """Events per month (stacked by category) plus articles per month, for
    the last 5 years — exact counts of what's actually in the database.
    Volume is bounded upstream by importance-score retention in the pipeline
    (see ``services.tasks.cap_monthly_article_importance_task``), not by
    sampling or slicing here.

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

    article_counts = [
        Article.objects.filter(published_on__gte=start, published_on__lt=end).count()
        for start, end, _label in buckets
    ]

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
        'articles_total': sum(article_counts),
        'articles_color': _ARTICLES_LINE_COLOR,
        'series': series,
        'svg': _render_activity_chart_svg(labels, categories, series, article_counts),
    }


def _render_activity_chart_svg(labels, categories, series, article_counts):
    """Server-rendered stacked-area SVG with an overlaid articles line on a
    secondary (right) axis — no JS charting library exists anywhere in this
    repo (the React frontend's Recharts dep doesn't reach the Django admin),
    so this draws the chart directly in Python rather than pulling in a new
    client-side dependency for one graph. Events and articles differ by
    orders of magnitude in volume, hence the separate axes."""
    import math
    from django.utils.html import escape
    from django.utils.safestring import mark_safe

    n = len(labels)
    if not n:
        return mark_safe('')

    def nice_max(v):
        v = max(v, 1)
        magnitude = 10 ** max(len(str(int(v))) - 1, 0)
        return int(math.ceil(v / magnitude) * magnitude) or 1

    event_totals = [sum(series[c][i] for c in categories) for i in range(n)] if categories else [0] * n
    y_max = nice_max(max(event_totals))
    y2_max = nice_max(max(article_counts) if article_counts else 0)

    plot_w = _CHART_W - _CHART_PAD_L - _CHART_PAD_R
    plot_h = _CHART_H - _CHART_PAD_T - _CHART_PAD_B

    def px(i):
        return _CHART_PAD_L + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def py(v, scale):
        return _CHART_PAD_T + plot_h - (plot_h * v / scale)

    parts = [
        f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" class="ops-chart-svg" '
        f'role="img" aria-label="Events per month by category, with total articles per month">'
    ]

    # Gridlines + dual y-axis labels (0, 25%, 50%, 75%, 100%) — left = events, right = articles
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        gy = py(y_max * frac, y_max)
        parts.append(
            f'<line x1="{_CHART_PAD_L}" y1="{gy:.1f}" x2="{_CHART_W - _CHART_PAD_R}" y2="{gy:.1f}" '
            f'class="ops-chart-gridline"/>'
        )
        parts.append(f'<text x="{_CHART_PAD_L - 6}" y="{gy:.1f}" class="ops-chart-axis-label" text-anchor="end" dominant-baseline="middle">{int(y_max * frac)}</text>')
        parts.append(f'<text x="{_CHART_W - _CHART_PAD_R + 6}" y="{gy:.1f}" class="ops-chart-axis-label ops-chart-axis-label-r" text-anchor="start" dominant-baseline="middle">{int(y2_max * frac)}</text>')

    # Stacked area layers, one path per category (left axis)
    cum = [0] * n
    for c in categories:
        top = [cum[i] + series[c][i] for i in range(n)]
        pts_top = [(px(i), py(top[i], y_max)) for i in range(n)]
        pts_bottom = [(px(i), py(cum[i], y_max)) for i in range(n)][::-1]
        path_pts = pts_top + pts_bottom
        d = 'M ' + ' L '.join(f'{x:.1f},{y:.1f}' for x, y in path_pts) + ' Z'
        color = _CATEGORY_COLORS.get(c, '#666')
        parts.append(f'<path d="{d}" fill="{color}" fill-opacity="0.75" stroke="{color}" stroke-width="1"/>')
        cum = top

    # Total-articles overlay line (right axis)
    line_pts = [(px(i), py(v, y2_max)) for i, v in enumerate(article_counts)]
    line_d = 'M ' + ' L '.join(f'{x:.1f},{y:.1f}' for x, y in line_pts)
    parts.append(f'<path d="{line_d}" fill="none" stroke="{_ARTICLES_LINE_COLOR}" stroke-width="2" stroke-dasharray="4 3"/>')

    # X-axis labels — thin out to roughly one per year over a 10y window
    step = max(1, n // 10)
    for i in range(0, n, step):
        parts.append(
            f'<text x="{px(i):.1f}" y="{_CHART_H - 6}" class="ops-chart-axis-label" '
            f'text-anchor="middle">{escape(labels[i])}</text>'
        )

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
        'coverage': _coverage,
        'forecast': _forecast_status,
        'llm_status': _llm_status,
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

    context = {
        **admin.site.each_context(request),
        'title': 'Operations Dashboard',
        **results,
    }
    return render(request, 'admin/dashboard.html', context)
