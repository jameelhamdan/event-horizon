"""Dependency-light self-tests for the forecasting layer.

Exercises the *real* feature/model/backtest code paths without a database: the ORM loaders
(``features._load_bars`` / ``features._load_events``) are monkeypatched with synthetic data, so
the as-of / leakage logic under test is the production code, not a reimplementation.

Run standalone (no Mongo needed):

    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_forecast

Returns a non-zero exit code if any check fails.
"""

from datetime import datetime, timedelta, timezone

from tests._runner import bootstrap_django, run

bootstrap_django()


def _synthetic_bars(symbols, n=200, seed=0):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    out = {}
    for k, sym in enumerate(symbols):
        idx = pd.to_datetime([start + timedelta(days=i) for i in range(n)], utc=True)
        price = 100.0 + k * 10
        closes, vols = [], []
        for _ in range(n):
            price *= 1 + float(rng.normal(0, 0.01))
            closes.append(price)
            vols.append(float(rng.uniform(1e6, 2e6)))
        out[sym] = pd.DataFrame({'close': closes, 'volume': vols}, index=idx).sort_index()
    return out


def _synthetic_events(symbols, n=200, seed=1):
    import numpy as np
    rng = np.random.default_rng(seed)
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    events = []
    for i in range(n):
        t = start + timedelta(days=i, hours=12)
        sym = symbols[i % len(symbols)]
        events.append({
            't': t, 'w': {sym: float(rng.normal(0, 0.5))},
            'finbert': float(rng.normal(0, 0.3)), 'sentiment': float(rng.normal(0, 0.3)),
            'category': 'economic', 'topics': {'inflation'} if i % 3 == 0 else set(),
        })
    events.sort(key=lambda e: e['t'])
    return events, [e['t'] for e in events]


def _patch_loaders(features, symbols):
    bars = _synthetic_bars(symbols)
    events, ts = _synthetic_events(symbols)
    features._load_bars = lambda syms: {s: bars[s] for s in syms if s in bars}  # noqa: E731
    features._load_events = lambda start, end, router=None: (events, ts)         # noqa: E731
    return bars, events


# ── individual checks ───────────────────────────────────────────────────────────

def test_to_utc_ts():
    from services.forecasting import features as F
    naive = datetime(2024, 1, 1)
    aware = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert str(F.to_utc_ts(naive).tz) == 'UTC'
    assert str(F.to_utc_ts(aware).tz) == 'UTC'
    assert F.to_utc_ts('2024-01-01').tzinfo is not None


def test_router_fallback():
    """route_event_to_weighted_symbols (the deterministic router) always produces panel-valid weights."""
    from services.forecasting.routing import route_event_to_weighted_symbols

    out = route_event_to_weighted_symbols('conflict', 'Ukraine', ['ukraine-war'], ['war'], -0.5)
    assert out and all('symbol' in d and 'weight' in d for d in out)
    assert all(-1.0 <= d['weight'] <= 1.0 for d in out)


def test_metrics_perfect_and_naive():
    from services.forecasting import backtest as B
    yt = [1, 0, 1, 0, 1, 0]
    perfect = B._metrics(yt, [0.9, 0.1, 0.8, 0.2, 0.95, 0.05], yt)
    assert perfect['accuracy'] == 1.0
    assert perfect['brier'] is not None and perfect['brier'] < 0.05
    assert isinstance(perfect['reliability'], list)


def test_asof_no_leakage():
    """A future event (after t) must NOT change the as-of feature row."""
    from services.forecasting import features as F
    symbols = ['GC=F', 'SPY']
    bars, events = _patch_loaders(F, symbols)

    as_of = datetime(2023, 4, 1, tzinfo=timezone.utc)
    base = F.build_feature_matrix(as_of_date=as_of, symbols=symbols)
    assert not base.empty

    # Inject an event dated AFTER as_of, rebuild, and confirm features are identical.
    future_t = as_of + timedelta(days=10)
    events.append({'t': future_t, 'w': {'GC=F': 5.0}, 'finbert': 1.0, 'sentiment': 1.0,
                   'category': 'economic', 'topics': {'inflation'}})
    events.sort(key=lambda e: e['t'])
    F._load_events = lambda start, end, router=None: (events, [e['t'] for e in events])  # noqa: E731
    after = F.build_feature_matrix(as_of_date=as_of, symbols=symbols)

    cols = F.feature_columns(base)
    g_base = base[base['symbol'] == 'GC=F'][cols].reset_index(drop=True)
    g_after = after[after['symbol'] == 'GC=F'][cols].reset_index(drop=True)
    assert g_base.equals(g_after), 'LEAKAGE: a future-dated event changed the as-of features'


def test_training_labels_and_no_future_features():
    """Labels look forward (by design); features must not. Verify both."""
    from services.forecasting import features as F
    symbols = ['GC=F', 'SPY']
    _patch_loaders(F, symbols)
    start = datetime(2023, 2, 1, tzinfo=timezone.utc)
    end = datetime(2023, 6, 1, tzinfo=timezone.utc)
    frame = F.build_training_frame(symbols, start, end, horizons=(1, 5), include_events=True)
    assert not frame.empty
    for col in ('y_dir_1', 'y_ret_1', 'y_dir_5', 'y_ret_5'):
        assert col in frame.columns
    assert set(frame['y_dir_1'].unique()).issubset({0, 1})
    # every event-window feature column exists (event fusion wired in)
    assert 'evw_sum_1d' in F.feature_columns(frame)
    assert 'topic_inflation' in F.feature_columns(frame)


def test_train_predict_roundtrip():
    """Full model path (skipped cleanly if lightgbm is unavailable)."""
    try:
        import lightgbm  # noqa: F401
    except ImportError:
        print('  - test_train_predict_roundtrip SKIPPED (lightgbm not installed)')
        return
    import tempfile
    from django.conf import settings
    from services.forecasting import features as F, model as M
    symbols = ['GC=F', 'SPY']
    _patch_loaders(F, symbols)
    frame = F.build_training_frame(
        symbols, datetime(2023, 2, 1, tzinfo=timezone.utc),
        datetime(2023, 7, 1, tzinfo=timezone.utc), horizons=(1,), include_events=True)
    settings.FORECAST_MODEL_DIR = tempfile.mkdtemp()
    M.clear_cache()
    M.train(frame, 1)
    fm = F.build_feature_matrix(symbols=symbols)
    preds = M.predict(fm, 1)
    assert preds and all(
        {'symbol', 'direction', 'proba_up', 'predicted_price', 'band_low', 'band_high'} <= set(p)
        for p in preds)


_TESTS = [
    test_to_utc_ts, test_router_fallback, test_metrics_perfect_and_naive,
    test_asof_no_leakage, test_training_labels_and_no_future_features,
    test_train_predict_roundtrip,
]


if __name__ == '__main__':
    run(_TESTS)
