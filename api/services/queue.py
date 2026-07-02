"""Queue helper — thin wrapper around Celery (Redis broker).

Use enqueue(func, *args, **kwargs) everywhere. When TASK_QUEUE_ENABLED=False
(dev default) the function is called synchronously in the current process.
func must be a Celery task (decorated with @shared_task in services.tasks /
newsletter.tasks); retries are declared on the task itself (autoretry_for /
retry_backoff), not passed in here.

Every call also writes a core.models.TaskRun row (best-effort — tracking must
never break the task it's tracking) so individual tasks — status, args/kwargs,
result, error/traceback — are browsable at /admin/core/taskrun/ (our RQ-admin /
Flower equivalent). A row stuck in 'running' long past its usual duration is
the signal for a hung/deadlocked job that the task time limit didn't catch
cleanly; a row stuck in 'queued' means nothing picked it up.
"""
import logging
import time

from celery.signals import task_failure, task_prerun, task_retry, task_revoked, task_success
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
        # Pre-generate the task id so the TaskRun row exists *before* apply_async can
        # possibly be picked up by a worker — otherwise a fast/local worker can fire
        # task_prerun/task_success before this row is created, and those signal
        # handlers (which look the row up by job_id) silently no-op, leaving the row
        # permanently stuck at 'queued' for a task that already ran to completion.
        import uuid
        task_id = str(uuid.uuid4())
        _create_task_run(func, args, kwargs, queue, job_id=task_id)
        return func.apply_async(args=args, kwargs=kwargs, queue=queue, task_id=task_id, **extra)
    return _run_sync(func, args, kwargs, queue)


# ── TaskRun tracking ─────────────────────────────────────────────────────────
# Best-effort throughout: a Mongo hiccup while recording history must never
# fail (or silently corrupt the return value of) the task it's tracking.

def _safe_value(v, max_items: int = 20):
    """Shrink an arbitrary value (task args/kwargs or a return value) to
    something small and JSON/BSON-safe for storage.

    Long lists (e.g. a chunk of 500 article UUIDs) get truncated; anything
    that isn't a plain primitive/list/dict is stringified rather than risking
    a serialization error on save().
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple, set)):
        items = list(v)
        shown = [_safe_value(x, max_items) for x in items[:max_items]]
        if len(items) > max_items:
            shown.append(f'...+{len(items) - max_items} more')
        return shown
    if isinstance(v, dict):
        return {str(k): _safe_value(val, max_items) for k, val in list(v.items())[:max_items]}
    return str(v)[:200]


def _safe_params(args: tuple, kwargs: dict, max_items: int = 20) -> dict:
    return {
        'args': [_safe_value(a, max_items) for a in args],
        'kwargs': {str(k): _safe_value(v, max_items) for k, v in kwargs.items()},
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
            status=TaskRun.Status.QUEUED,
            started_at=timezone.now(),
            params=_safe_params(args, kwargs),
            job_id=job_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception('[queue] failed to create TaskRun row for %r', _task_name(func))


def _finish_task_run(run, status, *, t0: float | None = None, error: str | None = None,
                      traceback_str: str | None = None, result=None, has_result: bool = False):
    try:
        from django.utils import timezone
        run.status = status
        run.finished_at = timezone.now()
        if t0 is not None:
            run.duration_ms = int((time.monotonic() - t0) * 1000)
        else:
            # Worker-signal path (no t0): prefer picked_up_at (actual run start) over
            # started_at (enqueue time) so duration_ms reflects processing time, not
            # processing time + however long the task sat in the queue.
            run_started = run.picked_up_at or run.started_at
            run.duration_ms = int((run.finished_at - run_started).total_seconds() * 1000)
        update_fields = ['status', 'finished_at', 'duration_ms']
        if error is not None:
            run.error = error[:4000]
            update_fields.append('error')
        if traceback_str is not None:
            run.traceback = traceback_str[:8000]
            update_fields.append('traceback')
        if has_result:
            run.result = _safe_value(result)
            update_fields.append('result')
            if isinstance(result, int):
                run.items = result
                update_fields.append('items')
        run.save(update_fields=update_fields)
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
                # No real queueing happens inline — go straight to 'running'.
                status=TaskRun.Status.RUNNING,
                started_at=timezone.now(),
                picked_up_at=timezone.now(),
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
        _finish_task_run(run, TaskRun.Status.SUCCESS, t0=t0, result=result, has_result=True)
    return result


def _find_run(job_id):
    from core.models import TaskRun
    if not job_id:
        return None
    return TaskRun.objects.filter(job_id=job_id).order_by('-started_at').first()


@task_prerun.connect
def _on_task_prerun(sender=None, task_id=None, **kwargs) -> None:
    """Celery task_prerun signal — fires in the worker right before a task body runs."""
    if not settings.TASK_RUN_TRACKING_ENABLED:
        return
    try:
        from django.utils import timezone
        from core.models import TaskRun
        run = _find_run(task_id)
        if run is not None and run.status == TaskRun.Status.QUEUED:
            run.status = TaskRun.Status.RUNNING
            run.picked_up_at = timezone.now()
            run.save(update_fields=['status', 'picked_up_at'])
    except Exception:  # noqa: BLE001
        logger.exception('[queue] task_prerun tracking failed for job %s', task_id)


@task_retry.connect
def _on_task_retry(sender=None, request=None, reason=None, **kwargs) -> None:
    """Celery task_retry signal — fires when a failed task is rescheduled for another attempt."""
    if not settings.TASK_RUN_TRACKING_ENABLED:
        return
    job_id = getattr(request, 'id', None)
    try:
        from core.models import TaskRun
        run = _find_run(job_id)
        if run is not None:
            run.retries += 1
            run.status = TaskRun.Status.QUEUED
            run.error = str(reason)[:4000]
            run.save(update_fields=['retries', 'status', 'error'])
    except Exception:  # noqa: BLE001
        logger.exception('[queue] task_retry tracking failed for job %s', job_id)


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
        run = _find_run(job_id)
        if run is not None:
            _finish_task_run(run, TaskRun.Status.SUCCESS, result=result, has_result=True)
    except Exception:  # noqa: BLE001
        logger.exception('[queue] task_success tracking failed for job %s', job_id)


@task_failure.connect
def _on_task_failure(sender=None, task_id=None, exception=None, einfo=None, **kwargs) -> None:
    """Celery task_failure signal — runs in the worker process after a task raises
    (only once retries, if any, are exhausted — see _on_task_retry for the interim state)."""
    if not settings.TASK_RUN_TRACKING_ENABLED:
        return
    try:
        from core.models import TaskRun
        run = _find_run(task_id)
        if run is not None:
            error = f'{type(exception).__name__}: {exception}'
            _finish_task_run(run, TaskRun.Status.FAILED, error=error,
                              traceback_str=str(einfo) if einfo is not None else None)
    except Exception:  # noqa: BLE001
        logger.exception('[queue] task_failure tracking failed for job %s', task_id)


@task_revoked.connect
def _on_task_revoked(sender=None, request=None, terminated=None, signum=None, expired=None, **kwargs) -> None:
    """Celery task_revoked signal — fires when a task is cancelled (app.control.revoke),
    whether it was queued, actively running, or expired before being picked up."""
    if not settings.TASK_RUN_TRACKING_ENABLED:
        return
    job_id = getattr(request, 'id', None)
    try:
        from core.models import TaskRun
        run = _find_run(job_id)
        if run is not None and run.status in (TaskRun.Status.QUEUED, TaskRun.Status.RUNNING):
            reason = 'expired' if expired else ('terminated' if terminated else 'revoked')
            _finish_task_run(run, TaskRun.Status.CANCELLED, error=reason)
    except Exception:  # noqa: BLE001
        logger.exception('[queue] task_revoked tracking failed for job %s', job_id)
