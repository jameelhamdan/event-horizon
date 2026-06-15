"""Dependency-free self-tests for the leakage-critical pure functions (plan §5, caveat 2).

The *structural* as-of guarantees live in the queries (``features.py`` filters
``latest_article_at__lte=at_time`` and ``occurred_at__lte=at_time``;
``buckets._ticks_up_to`` filters ``occurred_at__lte=at_time``). These tests cover
the pure transforms that must behave identically at train and serve time, plus the
walk-forward split ordering (no future-into-past).

Run from the api/ directory:
    python -m services.forecasting.tests_leakage
"""

from __future__ import annotations


def test_volume_normalize_is_scale_free():
    from services.forecasting.features import volume_normalize
    # Same share regardless of absolute volume → volume-invariant.
    sparse = volume_normalize(2, 4, [1, 2, 3])
    dense = volume_normalize(200, 400, [100, 200, 300])
    assert sparse['share'] == dense['share'] == 0.5, (sparse, dense)
    # Zero baseline std → zscore defaults to 0 (no divide-by-zero).
    assert volume_normalize(5, 10, [3, 3, 3])['zscore'] == 0.0


def test_asymmetric_sentiment_amplifies_negative():
    from services.forecasting.routing import asymmetric_sentiment
    assert asymmetric_sentiment(-0.5) == -0.75      # 1.5× amplification
    assert asymmetric_sentiment(0.5) == 0.5
    assert asymmetric_sentiment(None) == 0.5
    assert abs(asymmetric_sentiment(-1.0)) >= abs(asymmetric_sentiment(1.0))


def test_magnitude_classifier_uses_asof_thresholds():
    from services.forecasting.buckets import classify_magnitude
    thr = [-0.02, -0.005, 0.005, 0.02]  # q20/q40/q60/q80
    assert classify_magnitude(-0.05, thr) == 'strong_down'
    assert classify_magnitude(0.0, thr) == 'flat'
    assert classify_magnitude(0.05, thr) == 'strong_up'


def test_volatility_classifier_terciles():
    from services.forecasting.buckets import classify_volatility
    thr = [0.01, 0.03]  # t33/t66
    assert classify_volatility(0.005, thr) == 'calm'
    assert classify_volatility(0.02, thr) == 'normal'
    assert classify_volatility(0.05, thr) == 'elevated'


def test_walk_forward_never_trains_on_future():
    from services.forecasting.model import walk_forward_splits
    from datetime import datetime, timedelta, timezone
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    samples = [{'t': base + timedelta(hours=i), 'features': [0.0], 'mag_label': 'flat',
                'vol_label': 'normal'} for i in range(50)]
    for train, test in walk_forward_splits(samples, n_folds=4):
        assert max(s['t'] for s in train) < min(s['t'] for s in test), 'future leaked into train'


def test_routing_emits_weighted_symbols_with_sign():
    from services.forecasting.routing import route_event_to_weighted_symbols
    weighted = route_event_to_weighted_symbols(
        'conflict', 'Kyiv, Ukraine', ['ukraine-war'], ['airstrike'], -0.8,
    )
    assert weighted, 'expected routed symbols'
    assert all(w['weight'] < 0 for w in weighted), 'negative sentiment must keep sign'
    assert any(w['symbol'] == '^VIX' for w in weighted), 'VIX should route for conflict'


def _run():
    tests = [v for k, v in globals().items() if k.startswith('test_') and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f'PASS {t.__name__}')
        except AssertionError as e:
            failed += 1
            print(f'FAIL {t.__name__}: {e}')
    print(f'\n{len(tests) - failed}/{len(tests)} passed')
    return failed


if __name__ == '__main__':
    import sys
    sys.exit(_run())
