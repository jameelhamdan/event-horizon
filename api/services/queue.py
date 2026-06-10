"""Queue helper — thin wrapper around django-rq.

Use ``enqueue(func, *args, **kwargs)`` everywhere instead of calling
``queue.enqueue()`` directly.  When ``TASK_QUEUE_ENABLED=False`` (the dev
default) the function is called synchronously in the current process instead
of being pushed to Redis.
"""

import os

import django_rq
from django.conf import settings

_JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT_SECONDS", "1800"))


def enqueue(func, *args, queue: str = 'default', job_timeout: int | None = None, **kwargs):
    """Enqueue *func* on an RQ queue.

    ``queue`` selects between ``'default'`` (light I/O tasks) and ``'heavy'``
    (NLP / LLM tasks).  Falls back to a direct synchronous call when
    ``TASK_QUEUE_ENABLED`` is ``False`` so that local development works without
    Redis.

    ``job_timeout`` overrides the global default (``JOB_TIMEOUT_SECONDS``).
    Pass ``-1`` for no timeout (useful for long-running backfill jobs).
    """
    if getattr(settings, 'TASK_QUEUE_ENABLED', False):
        rq_queue = django_rq.get_queue(queue)
        timeout = job_timeout if job_timeout is not None else _JOB_TIMEOUT
        rq_queue.enqueue(func, *args, job_timeout=timeout, **kwargs)
    else:
        func(*args, **kwargs)
