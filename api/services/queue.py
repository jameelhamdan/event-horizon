"""Queue helper — thin wrapper around Celery (Redis broker).

Use enqueue(func, *args, **kwargs) everywhere. When TASK_QUEUE_ENABLED=False
(dev default) the function is called synchronously in the current process.
func must be a Celery task (decorated with @shared_task in services.tasks /
newsletter.tasks); retries are declared on the task itself (autoretry_for /
retry_backoff), not passed in here.

Every call also writes a core.models.TaskRun row (best-effort — tracking must
never break the task it's tracking) so task history, durations, and errors are
queryable in /admin/core/taskrun/. A row stuck in status='running' long past
its usual duration is the signal for a hung/deadlocked job that the task time
limit didn't catch cleanly.
"""
import logging
import time

from celery.signals import task_failure, task_success
from django.conf import settings

logger = logging.getLogger(__name__)


def _task_name(func) -> str:
    """Celery Task objects expose .name (the registered dotted task name);
    plain functions (sync-mode callers, tests) fall back to __name__."""
    return getattr(func, 'name', None) or getattr(func, '__name__', str(func))


def enqueue(func, *args, queue: str = 'default', job_timeout: int | None = None, **kwargs):
    """Enqueue func as a Celery task, or call it synchronously when TASK_QUEUE_ENABLED=False.

    queue selects 'default' (light I/O), 'heavy' (NLP/LLM), or 'bulk' (one-shot).
    job_timeout overrides the queue's CELERY_QUEUE_TIME_LIMITS default; pass -1 for no cap.
    Returns the Celery AsyncResult when queued, or the function's return value when sync.
    """
    if settings.TASK_QUEUE_ENABLED:
        if job_timeout == -1:
            limit = None
        elif job_timeout is not None:
            limit = job_timeout
        else:
            limit = settings.CELERY_QUEUE_TIME_LIMITS.get(queue)
        extra = {'time_limit': limit} if limit is not None else {}
        result = func.apply_async(args=args, kwargs=kwargs, queue=queue, **extra)
        _create_task_run(func, args, kwargs, queue, job_id=result.id)
        return result
    return _run_sync(func, args, kwargs, queue)


# ── TaskRun tracking ─────────────────────────────────────────────────────────
# Best-effort throughout: a Mongo hiccup while recording history must never
# fail (or silently corrupt the return value of) the task it's tracking.

def _safe_params(args: tuple, kwargs: dict, max_items: int = 20) -> dict:
    """Shrink args/kwargs to something small and JSON/BSON-safe for storage.

    Long lists (e.g. a chunk of 500 article UUIDs) get truncated; anything
    that isn't a plain primitive/list/dict is stringified rather than risking
    a serialization error on save().
    """
    def conv(v):
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, (list, tuple, set)):
            items = list(v)
            shown = [conv(x) for x in items[:max_items]]
            if len(items) > max_items:
                shown.append(f'...+{len(items) - max_items} more')
            return shown
        if isinstance(v, dict):
            return {str(k): conv(val) for k, val in list(v.items())[:max_items]}
        return str(v)[:200]

    return {
        'args': [conv(a) for a in args],
        'kwargs': {str(k): conv(v) for k, v in kwargs.items()},
    }


def _create_task_run(func, args: tuple, kwargs: dict, queue: str, job_id: str = ''):
    if not settings.TASK_RUN_TRACKING_ENABLED:
        return
    try:
        from django.utils import timezone
        from core.models import TaskRun
        TaskRun.objects.create(
            task_name=_task_name(func),
            queue=queue,
            status=TaskRun.Status.RUNNING,
            started_at=timezone.now(),
            params=_safe_params(args, kwargs),
            job_id=job_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception('[queue] failed to create TaskRun row for %r', _task_name(func))


def _finish_task_run(run, status, *, t0: float | None = None, error: str | None = None, items=None):
    try:
        from django.utils import timezone
        run.status = status
        run.finished_at = timezone.now()
        run.duration_ms = (
            int((time.monotonic() - t0) * 1000) if t0 is not None
            else int((run.finished_at - run.started_at).total_seconds() * 1000)
        )
        if error is not None:
            run.error = error[:4000]
        if items is not None:
            run.items = items
        run.save(update_fields=['status', 'finished_at', 'duration_ms', 'error', 'items'])
    except Exception:  # noqa: BLE001
        logger.exception('[queue] failed to finalize TaskRun %s', getattr(run, 'pk', '?'))


def _run_sync(func, args: tuple, kwargs: dict, queue: str):
    """TASK_QUEUE_ENABLED=False path: call func in-process, tracked the same way."""
    run = None
    if settings.TASK_RUN_TRACKING_ENABLED:
        try:
            from django.utils import timezone
            from core.models import TaskRun
            run = TaskRun.objects.create(
                task_name=_task_name(func),
                queue=queue,
                status=TaskRun.Status.RUNNING,
                started_at=timezone.now(),
                params=_safe_params(args, kwargs),
                job_id='',
            )
        except Exception:  # noqa: BLE001
            logger.exception('[queue] failed to create TaskRun row for %r', _task_name(func))

    t0 = time.monotonic()
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        if run is not None:
            from core.models import TaskRun
            _finish_task_run(run, TaskRun.Status.FAILED, t0=t0, error=f'{type(exc).__name__}: {exc}')
        raise
    if run is not None:
        from core.models import TaskRun
        _finish_task_run(run, TaskRun.Status.SUCCESS, t0=t0, items=result if isinstance(result, int) else None)
    return result


@task_success.connect
def _on_task_success(sender=None, result=None, **kwargs) -> None:
    """Celery task_success signal — runs in the worker process right after a task returns."""
    if not settings.TASK_RUN_TRACKING_ENABLED:
        return
    job_id = getattr(getattr(sender, 'request', None), 'id', None)
    if not job_id:
        return
    try:
        from core.models import TaskRun
        run = TaskRun.objects.filter(job_id=job_id).order_by('-started_at').first()
        if run is not None:
            _finish_task_run(run, TaskRun.Status.SUCCESS, items=result if isinstance(result, int) else None)
    except Exception:  # noqa: BLE001
        logger.exception('[queue] task_success tracking failed for job %s', job_id)


@task_failure.connect
def _on_task_failure(sender=None, task_id=None, exception=None, **kwargs) -> None:
    """Celery task_failure signal — runs in the worker process after a task raises."""
    if not settings.TASK_RUN_TRACKING_ENABLED:
        return
    try:
        from core.models import TaskRun
        run = TaskRun.objects.filter(job_id=task_id).order_by('-started_at').first()
        if run is not None:
            error = f'{type(exception).__name__}: {exception}'
            _finish_task_run(run, TaskRun.Status.FAILED, error=error)
    except Exception:  # noqa: BLE001
        logger.exception('[queue] task_failure tracking failed for job %s', task_id)
