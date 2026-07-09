"""Dependency-light self-tests for the preliminary capstone evaluation
(services/forecasting/evaluate.py) — realized-move labelling and routing
Precision@k, with synthetic bars and events (no Mongo, no LightGBM).

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_forecast_evaluate
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests._runner import bootstrap_django, run

bootstrap_django()

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(closes, start=T0):
    """(close, sigma) pair the way evaluate._return_series builds them."""
    import pandas as pd
    from services.forecasting import evaluate as ev
    idx = pd.DatetimeIndex([start + timedelta(days=i) for i in range(len(closes))])
    close = pd.Series([float(c) for c in closes], index=idx)
    rets = close.pct_change()
    sigma = rets.rolling(ev.SIGMA_WINDOW, min_periods=2).std().shift(1)
    return close, sigma


def _flat_then_jump(n_flat=40, jump=1.10):
    """Nearly-flat history (tiny wiggle so σ>0) ending with one big move."""
    closes = [100 + (0.01 if i % 2 else -0.01) for i in range(n_flat)]
    closes.append(closes[-1] * jump)
    return closes


def test_realized_move_flags_big_jump():
    from services.forecasting.evaluate import _realized_move
    closes = _flat_then_jump()
    close, sigma = _series(closes)
    # Event lands just before the final (jump) bar.
    t = close.index[-1] - timedelta(hours=12)
    ret, affected, ok = _realized_move(close, sigma, t)
    assert ok, 'move should be evaluable'
    assert affected, f'10% jump on flat history must exceed 1σ (ret={ret})'
    assert abs(ret - 0.10) < 0.02


def test_realized_move_ignores_quiet_day():
    from services.forecasting.evaluate import _realized_move
    closes = _flat_then_jump(jump=1.0001)
    close, sigma = _series(closes)
    t = close.index[-1] - timedelta(hours=12)
    ret, affected, ok = _realized_move(close, sigma, t)
    assert ok and not affected, f'0.01% move must not count as affected (ret={ret})'


def test_realized_move_unusable_outside_history():
    from services.forecasting.evaluate import _realized_move
    close, sigma = _series(_flat_then_jump())
    # After the last bar there is no next close; before the first there is no prior.
    assert _realized_move(close, sigma, close.index[-1] + timedelta(days=1))[2] is False
    assert _realized_move(close, sigma, close.index[0] - timedelta(days=1))[2] is False


def test_routing_precision_credits_correct_route():
    """One event routed to the one symbol that jumped -> precision 1.0; routed to
    the flat symbol -> precision 0.0. Base rate sits between."""
    from services.forecasting import evaluate as ev

    jump_close, jump_sigma = _series(_flat_then_jump())
    flat_close, flat_sigma = _series(_flat_then_jump(jump=1.0001))
    series = {'CL=F': (jump_close, jump_sigma), 'SPY': (flat_close, flat_sigma)}
    t = jump_close.index[-1] - timedelta(hours=12)

    def fake_events(weights):
        return [{'t': t, 'w': weights, 'finbert': None, 'sentiment': None,
                 'category': 'conflict', 'topics': set()}], [t]

    with patch.object(ev, '_return_series', return_value=series), \
         patch.object(ev, 'get_panel_symbols', return_value=list(series)):
        with patch.object(ev.feat, '_load_events', return_value=fake_events({'CL=F': 0.9})):
            good = ev.evaluate_routing(T0, T0 + timedelta(days=60), top_k=3)
        with patch.object(ev.feat, '_load_events', return_value=fake_events({'SPY': 0.9})):
            bad = ev.evaluate_routing(T0, T0 + timedelta(days=60), top_k=3)

    assert good['precision_at_k'] == 1.0, good
    assert bad['precision_at_k'] == 0.0, bad
    assert good['random_baseline'] == 0.5, 'one affected of two panel symbols'
    assert good['n_events'] == 1
    # Positive weight on an upward jump -> direction credited.
    assert good['direction_accuracy_on_hits'] == 1.0


def test_routing_skips_events_without_weights():
    from services.forecasting import evaluate as ev
    close, sigma = _series(_flat_then_jump())
    series = {'CL=F': (close, sigma)}
    t = close.index[-1] - timedelta(hours=12)
    empty = [{'t': t, 'w': {}, 'finbert': None, 'sentiment': None,
              'category': 'general', 'topics': set()}], [t]
    with patch.object(ev, '_return_series', return_value=series), \
         patch.object(ev, 'get_panel_symbols', return_value=['CL=F']), \
         patch.object(ev.feat, '_load_events', return_value=empty):
        out = ev.evaluate_routing(T0, T0 + timedelta(days=60), top_k=3)
    assert out['n_events'] == 0 and out['precision_at_k'] is None


if __name__ == '__main__':
    run([
        test_realized_move_flags_big_jump,
        test_realized_move_ignores_quiet_day,
        test_realized_move_unusable_outside_history,
        test_routing_precision_credits_correct_route,
        test_routing_skips_events_without_weights,
    ])
