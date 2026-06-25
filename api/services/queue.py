"""Queue helper — thin wrapper around django-rq.

Use enqueue(func, *args, **kwargs) everywhere. When TASK_QUEUE_ENABLED=False
(dev default) the function is called synchronously in the current process.
"""
import django_rq
from django.conf import settings


def enqueue(func, *args, queue: str = 'default', job_timeout: int | None = None,
            retry=None, depends_on=None, **kwargs):
    """Enqueue func on an RQ queue, or call it synchronously when TASK_QUEUE_ENABLED=False.

    queue selects 'default' (light I/O) or 'heavy' (NLP/LLM).
    job_timeout overrides the queue DEFAULT_TIMEOUT; pass -1 for no cap.
    Returns the RQ Job when queued, or the function's return value when sync.
    """
    if settings.TASK_QUEUE_ENABLED:
        extra = {}
        if job_timeout is not None:
            extra['job_timeout'] = job_timeout
        if retry is not None:
            extra['retry'] = retry
        if depends_on is not None:
            extra['depends_on'] = depends_on
        return django_rq.get_queue(queue).enqueue(func, *args, **kwargs, **extra)
    return func(*args, **kwargs)


def make_retry(max_attempts: int = 3, intervals=None):
    """Build an rq.Retry (or None if queuing disabled / RQ unavailable)."""
    if not settings.TASK_QUEUE_ENABLED:
        return None
    try:
        from rq import Retry
        return Retry(max=max_attempts, interval=intervals or [60, 300, 900])
    except Exception:  # noqa: BLE001
        return None
