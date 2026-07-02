"""Dependency-light self-tests for services/stages.py — the pipeline stage
registry and its dispatch/execute machinery.

No database or Redis required — registry shape checks are pure, and dispatch
behavior is exercised with monkeypatched selection/claim/enqueue functions.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_stages
"""

from unittest.mock import patch

from tests._runner import bootstrap_django, run

_DJANGO_READY = bootstrap_django()


# ── Registry shape ─────────────────────────────────────────────────────────────

def test_registry_names_match_keys():
    from services.stages import REGISTRY
    for name, stage in REGISTRY.items():
        assert stage.name == name


def test_registry_pipeline_order():
    """Registry order IS pipeline order — upstream stages first."""
    from services.stages import REGISTRY
    names = list(REGISTRY)
    assert names == ['fetch', 'score', 'process', 'geocode', 'aggregate', 'tag', 'route']


def test_registry_queues_valid():
    from services.stages import REGISTRY
    for stage in REGISTRY.values():
        assert stage.queue in ('default', 'heavy'), stage.name


def test_registry_positive_chunking():
    from services.stages import REGISTRY
    for stage in REGISTRY.values():
        assert stage.chunk_size >= 1, stage.name
        assert stage.limit >= 1, stage.name
        assert stage.every_minutes >= 1, stage.name


def test_singleton_stages_have_no_pending_ids():
    from services.stages import REGISTRY
    agg = REGISTRY['aggregate']
    assert agg.singleton
    assert agg.pending_ids is None
    for name in ('fetch', 'score', 'process', 'geocode', 'tag', 'route'):
        assert not REGISTRY[name].singleton, name


def test_process_chunk_matches_analyzer_batch():
    """One process chunk must map to exactly one batched LLM analysis call."""
    from services.stages import REGISTRY
    from services.processing.analyzer import ArticleAnalyzer
    assert REGISTRY['process'].chunk_size == ArticleAnalyzer.ANALYZE_BATCH_SIZE


def test_score_chunk_matches_scorer_batch():
    """One score chunk must map to exactly one batched LLM scoring call."""
    from services.stages import REGISTRY
    from services.scoring import ArticleImportanceScorer
    assert REGISTRY['score'].chunk_size == ArticleImportanceScorer.BATCH_SIZE


def test_claim_stages_also_release():
    """A stage that claims records must be able to release them on failure."""
    from services.stages import REGISTRY
    for stage in REGISTRY.values():
        if stage.claim is not None:
            assert stage.release is not None, stage.name


# ── Dispatch behavior (mocked I/O) ────────────────────────────────────────────

def _patched_stage(stage, **overrides):
    """Return a copy of a Stage with fields replaced (frozen dataclass)."""
    import dataclasses
    return dataclasses.replace(stage, **overrides)


def test_dispatch_chunks_and_counts_jobs():
    from services import stages as S

    calls = []
    fake = _patched_stage(
        S.REGISTRY['tag'],
        pending_ids=lambda limit: list(range(25))[:limit],
        enabled=lambda: True,
    )
    with patch.dict(S.REGISTRY, {'tag': fake}), \
         patch.object(S, '_is_due', return_value=True), \
         patch.object(S, '_mark_dispatched'), \
         patch.object(S, '_enqueue_chunk', side_effect=lambda st, ch: calls.append((st.name, ch))):
        jobs = S.dispatch_stage('tag')
    assert jobs == 3  # 25 ids / chunk_size 10 → 3 chunks
    assert [len(c) for _, c in calls] == [10, 10, 5]


def test_dispatch_skips_when_not_due():
    from services import stages as S
    fake = _patched_stage(S.REGISTRY['tag'], pending_ids=lambda limit: [1, 2, 3])
    with patch.dict(S.REGISTRY, {'tag': fake}), \
         patch.object(S, '_is_due', return_value=False), \
         patch.object(S, '_enqueue_chunk') as enq:
        assert S.dispatch_stage('tag') == 0
    enq.assert_not_called()


def test_dispatch_force_overrides_cadence_but_not_enabled():
    from services import stages as S
    fake = _patched_stage(S.REGISTRY['tag'], pending_ids=lambda limit: [1], enabled=lambda: False)
    with patch.dict(S.REGISTRY, {'tag': fake}), \
         patch.object(S, '_is_due', return_value=False), \
         patch.object(S, '_enqueue_chunk') as enq:
        assert S.dispatch_stage('tag', force=True) == 0
    enq.assert_not_called()


def test_dispatch_no_pending_no_jobs():
    from services import stages as S
    fake = _patched_stage(S.REGISTRY['tag'], pending_ids=lambda limit: [])
    with patch.dict(S.REGISTRY, {'tag': fake}), \
         patch.object(S, '_is_due', return_value=True), \
         patch.object(S, '_mark_dispatched') as mark, \
         patch.object(S, '_enqueue_chunk') as enq:
        assert S.dispatch_stage('tag') == 0
    enq.assert_not_called()
    # No work → no dispatch recorded, so the next tick re-checks immediately.
    mark.assert_not_called()


def test_dispatch_releases_unclaimed_on_enqueue_failure():
    from services import stages as S

    claimed, released = [], []
    fake = _patched_stage(
        S.REGISTRY['process'],
        pending_ids=lambda limit: [1, 2, 3, 4],
        claim=lambda ids: claimed.extend(ids),
        release=lambda ids: released.extend(ids),
        chunk_size=2,
        enabled=lambda: True,
    )

    boom = [False]

    def enqueue_then_fail(stage, chunk):
        if boom[0]:
            raise RuntimeError('broker down')
        boom[0] = True  # first chunk fine, second raises

    with patch.dict(S.REGISTRY, {'process': fake}), \
         patch.object(S, '_is_due', return_value=True), \
         patch.object(S, '_mark_dispatched'), \
         patch.object(S, '_enqueue_chunk', side_effect=enqueue_then_fail):
        try:
            S.dispatch_stage('process')
            assert False, 'expected RuntimeError'
        except RuntimeError:
            pass
    assert claimed == [1, 2, 3, 4]
    # Chunk [1, 2] was enqueued; [3, 4] never made it → released.
    assert released == [3, 4]


def test_singleton_dispatch_enqueues_once():
    from services import stages as S
    calls = []
    with patch.object(S, '_is_due', return_value=True), \
         patch.object(S, '_mark_dispatched'), \
         patch.object(S, '_enqueue_chunk', side_effect=lambda st, ch: calls.append((st.name, ch))):
        jobs = S.dispatch_stage('aggregate')
    assert jobs == 1
    assert calls == [('aggregate', None)]


def test_run_chunk_routes_to_handler():
    from services import stages as S
    fake = _patched_stage(S.REGISTRY['tag'], handler=lambda ids: len(ids))
    with patch.dict(S.REGISTRY, {'tag': fake}):
        assert S.run_chunk('tag', [1, 2, 3]) == 3


def test_run_due_stages_isolates_stage_failures():
    """One stage blowing up must not stop later stages from dispatching."""
    from services import stages as S

    def dispatch(name, force=False):
        if name == 'score':
            raise RuntimeError('kaboom')
        return 1 if name in ('fetch', 'tag') else 0

    with patch.object(S, 'dispatch_stage', side_effect=dispatch):
        results = S.run_due_stages()
    assert results['score'] == -1     # failure is visible, not swallowed silently
    assert results['fetch'] == 1
    assert results['tag'] == 1
    assert 'process' not in results   # 0 jobs → omitted


TESTS = [
    test_registry_names_match_keys,
    test_registry_pipeline_order,
    test_registry_queues_valid,
    test_registry_positive_chunking,
    test_singleton_stages_have_no_pending_ids,
    test_process_chunk_matches_analyzer_batch,
    test_score_chunk_matches_scorer_batch,
    test_claim_stages_also_release,
    test_dispatch_chunks_and_counts_jobs,
    test_dispatch_skips_when_not_due,
    test_dispatch_force_overrides_cadence_but_not_enabled,
    test_dispatch_no_pending_no_jobs,
    test_dispatch_releases_unclaimed_on_enqueue_failure,
    test_singleton_dispatch_enqueues_once,
    test_run_chunk_routes_to_handler,
    test_run_due_stages_isolates_stage_failures,
]


if __name__ == '__main__':
    run(TESTS)
