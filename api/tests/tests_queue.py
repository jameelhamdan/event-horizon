"""Dependency-light self-tests for services/queue.py — the enqueue() wrapper
every task call site goes through, and its dev-mode synchronous fallback.

No database, Redis, or Celery worker required — the TASK_QUEUE_ENABLED=True
path mocks a fake Celery task's apply_async() instead of hitting a real
broker, and every test disables TASK_RUN_TRACKING_ENABLED so enqueue()
doesn't try to write a TaskRun row to a Mongo instance that isn't there.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_queue
"""

from unittest.mock import MagicMock, patch

from tests._runner import bootstrap_django, run

bootstrap_django()

from django.test import override_settings  # noqa: E402


def _fake_task(fn):
    """Wrap a plain function so it's callable directly (sync mode) and also
    exposes a mocked apply_async() (async mode) — mirrors a @shared_task."""
    task = MagicMock(side_effect=fn, name=fn.__name__)
    task.name = fn.__name__
    fake_result = MagicMock()
    fake_result.id = 'fake-job-id'
    task.apply_async.return_value = fake_result
    return task, fake_result


def test_enqueue_sync_mode_calls_function_directly():
    from services.queue import enqueue

    calls = []

    def fn(a, b, keyword=None):
        calls.append((a, b, keyword))
        return a + b

    task, _ = _fake_task(fn)

    with override_settings(TASK_QUEUE_ENABLED=False, TASK_RUN_TRACKING_ENABLED=False):
        result = enqueue(task, 1, 2, keyword='x', queue='heavy')

    assert result == 3
    assert calls == [(1, 2, 'x')]
    task.apply_async.assert_not_called()


def test_enqueue_sync_mode_ignores_queue_and_job_timeout_kwargs():
    from services.queue import enqueue

    def fn():
        return 'ok'

    task, _ = _fake_task(fn)

    with override_settings(TASK_QUEUE_ENABLED=False, TASK_RUN_TRACKING_ENABLED=False):
        # queue/job_timeout are Celery-only concerns; sync mode must not choke
        # on them or forward them into the plain function call.
        result = enqueue(task, queue='heavy', job_timeout=-1)

    assert result == 'ok'


def test_enqueue_async_mode_delegates_to_apply_async():
    from services.queue import enqueue

    def fn(x):
        return x

    task, fake_result = _fake_task(fn)

    with override_settings(TASK_QUEUE_ENABLED=True, TASK_RUN_TRACKING_ENABLED=False):
        result = enqueue(task, 42, queue='heavy', job_timeout=-1)

    _, kwargs = task.apply_async.call_args
    assert kwargs['args'] == (42,)
    assert kwargs['queue'] == 'heavy'
    # job_timeout=-1 means no cap — no time_limit kwarg passed through.
    assert 'time_limit' not in kwargs
    assert result is fake_result


def test_enqueue_async_mode_applies_job_timeout():
    from services.queue import enqueue

    task, _ = _fake_task(lambda: None)

    with override_settings(TASK_QUEUE_ENABLED=True, TASK_RUN_TRACKING_ENABLED=False):
        enqueue(task, queue='default', job_timeout=120)

    _, kwargs = task.apply_async.call_args
    assert kwargs['time_limit'] == 120


def test_enqueue_async_mode_creates_task_run_before_apply_async():
    """The TaskRun row must exist before the task can possibly start executing —
    otherwise a fast/local worker could fire task_prerun/task_success before the
    row exists, and those signal handlers (which look the row up by job_id)
    would silently no-op, leaving the row stuck 'queued' forever. Regression
    test for that ordering: _create_task_run() must run, and its job_id must
    match the task_id apply_async() is actually given, before apply_async fires."""
    from services import queue as queue_mod

    task, _ = _fake_task(lambda: None)
    order = []
    task.apply_async.side_effect = lambda *a, **k: order.append(('apply_async', k.get('task_id')))

    captured = {}

    def fake_create_task_run(func, args, kwargs, queue, job_id=''):
        order.append(('create_task_run', job_id))
        captured['job_id'] = job_id

    with override_settings(TASK_QUEUE_ENABLED=True):
        with patch.object(queue_mod, '_create_task_run', side_effect=fake_create_task_run):
            queue_mod.enqueue(task, queue='default')

    assert [step for step, _ in order] == ['create_task_run', 'apply_async']
    assert order[0][1] == order[1][1] == captured['job_id']


def test_enqueue_async_mode_falls_back_to_queue_default_timeout():
    from services.queue import enqueue

    task, _ = _fake_task(lambda: None)

    with override_settings(
        TASK_QUEUE_ENABLED=True, TASK_RUN_TRACKING_ENABLED=False,
        CELERY_QUEUE_TIME_LIMITS={'default': 600, 'heavy': 600, 'bulk': None},
    ):
        enqueue(task, queue='bulk')  # no job_timeout given — bulk's default is None (no cap)

    _, kwargs = task.apply_async.call_args
    assert 'time_limit' not in kwargs


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_enqueue_sync_mode_calls_function_directly,
    test_enqueue_sync_mode_ignores_queue_and_job_timeout_kwargs,
    test_enqueue_async_mode_delegates_to_apply_async,
    test_enqueue_async_mode_applies_job_timeout,
    test_enqueue_async_mode_creates_task_run_before_apply_async,
    test_enqueue_async_mode_falls_back_to_queue_default_timeout,
]


if __name__ == '__main__':
    run(_TESTS)
