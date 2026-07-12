"""Dependency-light self-tests for services/utils.py's mark_stage — the
per-record pipeline-stage-status tracker used across workflow/articles.py,
workflow/events.py, and workflow/topics.py.

No database or network required — mark_stage operates on any object with a
(possibly absent) stage_status attribute.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_utils
"""

from types import SimpleNamespace

from tests._runner import bootstrap_django, run

bootstrap_django()


def test_mark_stage_ok_true_has_no_error():
    from services.utils import mark_stage
    record = SimpleNamespace(stage_status={})
    status = mark_stage(record, 'process', ok=True)
    assert status['process']['ok'] is True
    assert status['process']['error'] is None
    assert 'at' in status['process']


def test_mark_stage_ok_false_keeps_error():
    from services.utils import mark_stage
    record = SimpleNamespace(stage_status={})
    status = mark_stage(record, 'tag', ok=False, error='no candidates')
    assert status['tag']['ok'] is False
    assert status['tag']['error'] == 'no candidates'


def test_mark_stage_ok_true_drops_error_even_if_passed():
    from services.utils import mark_stage
    record = SimpleNamespace(stage_status={})
    status = mark_stage(record, 'process', ok=True, error='should be ignored')
    assert status['process']['error'] is None


def test_mark_stage_sets_attribute_on_record():
    from services.utils import mark_stage
    record = SimpleNamespace(stage_status={})
    mark_stage(record, 'route', ok=True)
    assert 'route' in record.stage_status


def test_mark_stage_preserves_other_stages():
    from services.utils import mark_stage
    record = SimpleNamespace(stage_status={'process': {'ok': True, 'at': 'x', 'error': None}})
    mark_stage(record, 'tag', ok=True)
    assert 'process' in record.stage_status
    assert 'tag' in record.stage_status


def test_mark_stage_overwrites_same_stage():
    from services.utils import mark_stage
    record = SimpleNamespace(stage_status={})
    mark_stage(record, 'tag', ok=False, error='keyword fallback')
    mark_stage(record, 'tag', ok=True)
    assert record.stage_status['tag']['ok'] is True
    assert record.stage_status['tag']['error'] is None


def test_mark_stage_handles_missing_stage_status_attribute():
    from services.utils import mark_stage
    record = SimpleNamespace()  # no stage_status attribute at all
    status = mark_stage(record, 'process', ok=True)
    assert status == {'process': {'ok': True, 'at': status['process']['at'], 'error': None}}


def test_mark_stage_returns_the_same_dict_it_sets():
    from services.utils import mark_stage
    record = SimpleNamespace(stage_status={})
    status = mark_stage(record, 'process', ok=True)
    assert status is record.stage_status


def test_map_concurrent_preserves_input_order():
    from services.utils import map_concurrent
    items = list(range(20))
    assert map_concurrent(items, lambda x: x * x, max_workers=8) == [x * x for x in items]


def test_map_concurrent_empty_returns_empty():
    from services.utils import map_concurrent
    assert map_concurrent([], lambda x: x) == []


def test_map_concurrent_uses_default_on_exception():
    from services.utils import map_concurrent

    def flaky(x):
        if x % 2 == 0:
            raise ValueError('boom')
        return x

    out = map_concurrent([0, 1, 2, 3], flaky, max_workers=4, default=None)
    assert out == [None, 1, None, 3]


def test_map_concurrent_default_object_fills_failed_slots():
    from services.utils import map_concurrent

    def always_fail(_):
        raise RuntimeError

    out = map_concurrent(['a', 'b'], always_fail, default=(None, None))
    assert out == [(None, None), (None, None)]


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_mark_stage_ok_true_has_no_error,
    test_mark_stage_ok_false_keeps_error,
    test_mark_stage_ok_true_drops_error_even_if_passed,
    test_mark_stage_sets_attribute_on_record,
    test_mark_stage_preserves_other_stages,
    test_mark_stage_overwrites_same_stage,
    test_mark_stage_handles_missing_stage_status_attribute,
    test_mark_stage_returns_the_same_dict_it_sets,
    test_map_concurrent_preserves_input_order,
    test_map_concurrent_empty_returns_empty,
    test_map_concurrent_uses_default_on_exception,
    test_map_concurrent_default_object_fills_failed_slots,
]


if __name__ == '__main__':
    run(_TESTS)
