"""Preliminary capstone evaluation for the forecasting layer.

Two headline numbers (see plans/initial-evaluation.md, phase 1a):

  * Routing Precision@k — did the rule router's top-k ``affected_indicators``
    actually move? An (event, symbol) pair counts as *affected* when the
    symbol's first daily close after the event moves more than 1 trailing σ
    (σ from pre-event bars only). Reported against the pooled base rate,
    which is the expected precision of routing at random.
  * 24h return MAE — walk-forward LightGBM regressor vs the zero-return
    (efficient-market) baseline, on the same frame the backtest uses.

Direction-accuracy arms live in backtest.py; this module only adds what the
capstone tables need on top of it. Event time is ``latest_article_at``
throughout (never ``started_at`` — see the leakage note on the Event model).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from django.conf import settings

from . import features as feat
from .routing import get_panel_symbols

logger = logging.getLogger(__name__)

SIGMA_WINDOW = 30       # trailing bars for the ±1σ "affected" threshold
SIGMA_MIN_PERIODS = 20


def _return_series(symbols):
    """Per symbol: (close series, trailing σ series shifted so σ at bar d uses only pre-d returns)."""
    bars = feat._load_bars(symbols)
    out = {}
    for sym, df in bars.items():
        rets = df['close'].pct_change()
        sigma = rets.rolling(SIGMA_WINDOW, min_periods=SIGMA_MIN_PERIODS).std().shift(1)
        out[sym] = (df['close'], sigma)
    return out


def _realized_move(close, sigma, t):
    """Next-bar move after event time t: (return, affected, usable)."""
    pos = close.index.searchsorted(t, side='right')
    if pos < 1 or pos >= len(close):
        return None, None, False
    s = sigma.iloc[pos]
    if s is None or s != s or s <= 0:  # NaN / degenerate σ
        return None, None, False
    ret = float(close.iloc[pos] / close.iloc[pos - 1] - 1.0)
    return ret, abs(ret) > float(s), True


def evaluate_routing(start, end, top_k=3):
    """Precision@k of the rule router's affected_indicators vs realized ±1σ moves."""
    symbols = list(get_panel_symbols())
    series = _return_series(symbols)
    events, _ = feat._load_events(start, end, router='rules')

    n_events = 0
    slots = hits = 0
    direction_hits = direction_total = 0
    pair_total = pair_affected = 0  # base rate over the full (event, panel) grid

    for ev in events:
        if not ev['w']:
            continue
        # Base-rate denominator: every panel symbol with usable data at this event time.
        usable = {}
        for sym in series:
            ret, affected, ok = _realized_move(*series[sym], ev['t'])
            if ok:
                usable[sym] = (ret, affected)
                pair_total += 1
                pair_affected += int(affected)
        if not usable:
            continue

        top = sorted(ev['w'].items(), key=lambda kv: abs(kv[1]), reverse=True)[:top_k]
        top = [(s, w) for s, w in top if s in usable]
        if not top:
            continue
        n_events += 1
        for sym, weight in top:
            ret, affected = usable[sym]
            slots += 1
            if affected:
                hits += 1
                direction_total += 1
                if (weight > 0) == (ret > 0):
                    direction_hits += 1

    base_rate = pair_affected / pair_total if pair_total else None
    return {
        'top_k': top_k,
        'n_events': n_events,
        'n_panel_symbols': len(series),
        'precision_at_k': round(hits / slots, 4) if slots else None,
        'random_baseline': round(base_rate, 4) if base_rate is not None else None,
        'direction_accuracy_on_hits': (
            round(direction_hits / direction_total, 4) if direction_total else None
        ),
        'affected_definition': f'|next-close return| > 1 trailing σ ({SIGMA_WINDOW}d, pre-event only)',
    }


def evaluate_return_mae(start, end, step_days=30, train_window_days=None):
    """Walk-forward 1-day-return MAE: LightGBM regressor vs zero-return baseline."""
    import numpy as np
    from lightgbm import LGBMRegressor

    train_window_days = train_window_days or settings.FORECAST_TRAIN_WINDOW_DAYS
    load_start = start - timedelta(days=train_window_days + 60)
    frame = feat.build_training_frame(get_panel_symbols(), load_start, end,
                                      horizons=(1,), include_events=True, router='rules')
    if frame.empty:
        return {'error': 'no PriceBar data — run backfill_prices first'}

    cols = feat.feature_columns(frame)
    folds = []
    abs_err_model, abs_err_zero = [], []
    cur = feat.to_utc_ts(start)
    end_ts = feat.to_utc_ts(end)
    while cur + timedelta(days=step_days) <= end_ts:
        origin = cur
        nxt = cur + timedelta(days=step_days)
        lo = cur - timedelta(days=train_window_days)
        train = frame[(frame['date'] > lo) & (frame['date'] <= cur)]
        test = frame[(frame['date'] > cur) & (frame['date'] <= nxt)]
        cur = nxt
        if len(train) < 50 or test.empty:
            continue
        assert train['date'].max() <= test['date'].min(), 'LEAKAGE: train/test overlap'

        reg = LGBMRegressor(n_estimators=200, learning_rate=0.05, num_leaves=31,
                            min_child_samples=20, verbose=-1)
        reg.fit(train[cols].astype(float).values, train['y_ret_1'].astype(float).values)
        pred = reg.predict(test[cols].astype(float).values)
        y = test['y_ret_1'].astype(float).values
        em, ez = np.abs(pred - y), np.abs(y)
        abs_err_model.extend(em.tolist())
        abs_err_zero.extend(ez.tolist())
        folds.append({
            'origin': origin.isoformat(),
            'n_test': int(len(y)),
            'mae_model': round(float(em.mean()), 6),
            'mae_zero': round(float(ez.mean()), 6),
        })

    if not abs_err_model:
        return {'error': 'not enough data for any walk-forward fold'}
    mae_m = float(np.mean(abs_err_model))
    mae_z = float(np.mean(abs_err_zero))
    return {
        'horizon_days': 1,
        'n_folds': len(folds),
        'n_predictions': len(abs_err_model),
        'mae_model': round(mae_m, 6),
        'mae_zero_baseline': round(mae_z, 6),
        'improvement_pct': round((1 - mae_m / mae_z) * 100, 2) if mae_z else None,
        'folds': folds,
    }


def run_evaluation(days=365, top_k=3, step_days=30, output_path=None) -> dict:
    """Run both evaluations and write the JSON report (default: <repo>/eval/)."""
    end = datetime.now(timezone.utc) - timedelta(days=2)  # let realized bars settle
    start = end - timedelta(days=days)

    logger.info('[evaluate] routing precision@%d over %s … %s', top_k, start.date(), end.date())
    routing = evaluate_routing(start, end, top_k=top_k)
    logger.info('[evaluate] routing -> %s (base rate %s)',
                routing.get('precision_at_k'), routing.get('random_baseline'))

    logger.info('[evaluate] return MAE walk-forward, step=%dd', step_days)
    mae = evaluate_return_mae(start, end, step_days=step_days)
    logger.info('[evaluate] MAE -> model %s vs zero %s',
                mae.get('mae_model'), mae.get('mae_zero_baseline'))

    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'window': {'start': start.isoformat(), 'end': end.isoformat(), 'days': days},
        'routing_precision': routing,
        'return_mae': mae,
        'caveats': [
            'Preliminary numbers for the capstone doc; see plans/initial-evaluation.md.',
            'Routing base rate is the expected precision of random top-k routing.',
        ],
    }

    if output_path is None:
        eval_dir = os.path.join(str(settings.BASE_DIR), 'eval')
        os.makedirs(eval_dir, exist_ok=True)
        output_path = os.path.join(eval_dir, 'forecasting_report.json')
    with open(output_path, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2, default=str)
    report['_output_path'] = output_path
    logger.info('[evaluate] report written -> %s', output_path)
    return report
