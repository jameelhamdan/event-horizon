"""Walk-forward backtest for the forecasting layer — the headline evaluation.

Rolling-origin: at each origin t_o, train on rows dated <= t_o (within the train window) and
predict rows dated in (t_o, t_o+step]. Never peeks past the origin. Compares four arms to show
whether the news-event signal actually adds value:

    naive (majority)  →  price-only  →  price + rule-routed events  →  price + LLM-routed events

Reports directional accuracy, macro-F1, ROC-AUC, Brier score and a reliability curve per arm,
per horizon. Writes a JSON report. Self-checks the train/predict split for leakage.

NOTE: the backtest trains raw (uncalibrated) LightGBM per origin for speed; the *served* model
(model.py) is isotonic-calibrated. The reliability curve here therefore reflects the raw model.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from django.conf import settings

from . import features as feat
from .routing import get_panel_symbols

logger = logging.getLogger(__name__)


def _origins(dates, start, end, step_days):
    grid, cur = [], feat.to_utc_ts(start)
    end_ts = feat.to_utc_ts(end)
    while cur <= end_ts:
        grid.append(cur)
        cur += timedelta(days=step_days)
    return grid


def _metrics(y_true, proba, y_pred):
    import numpy as np
    from sklearn.metrics import accuracy_score, f1_score, roc_auc_score, brier_score_loss

    y_true = np.asarray(y_true)
    proba = np.asarray(proba)
    y_pred = np.asarray(y_pred)
    out = {
        'n': int(len(y_true)),
        'accuracy': round(float(accuracy_score(y_true, y_pred)), 4),
        'f1_macro': round(float(f1_score(y_true, y_pred, average='macro', zero_division=0)), 4),
        'up_rate_true': round(float(y_true.mean()), 4),
    }
    try:
        out['roc_auc'] = round(float(roc_auc_score(y_true, proba)), 4)
    except ValueError:
        out['roc_auc'] = None
    try:
        out['brier'] = round(float(brier_score_loss(y_true, proba)), 4)
    except ValueError:
        out['brier'] = None
    # 10-bin reliability curve
    bins = np.clip((proba * 10).astype(int), 0, 9)
    reliability = []
    for b in range(10):
        mask = bins == b
        if mask.sum():
            reliability.append({
                'bin': b / 10, 'pred_mean': round(float(proba[mask].mean()), 4),
                'emp_freq': round(float(y_true[mask].mean()), 4), 'count': int(mask.sum()),
            })
    out['reliability'] = reliability
    return out


def _run_model_arm(frame, horizon, origins, train_window_days):
    """Train raw LGBM per origin, predict the next step. Returns (y_true, proba, y_pred)."""
    from lightgbm import LGBMClassifier

    ycol = f'y_dir_{horizon}'
    cols = feat.feature_columns(frame)
    y_true, proba, y_pred = [], [], []

    for i, t_o in enumerate(origins[:-1]):
        nxt = origins[i + 1]
        lo = t_o - timedelta(days=train_window_days)
        train = frame[(frame['date'] > lo) & (frame['date'] <= t_o)]
        test = frame[(frame['date'] > t_o) & (frame['date'] <= nxt)]
        if len(train) < 50 or test.empty or train[ycol].nunique() < 2:
            continue
        # Leakage self-check: no training row may be dated at/after any test row.
        assert train['date'].max() <= t_o < test['date'].min(), 'LEAKAGE: train/test overlap'

        clf = LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=31,
                             min_child_samples=20, verbose=-1)
        clf.fit(train[cols].astype(float).values, train[ycol].astype(int).values)
        p = clf.predict_proba(test[cols].astype(float).values)[:, 1]
        proba.extend(p.tolist())
        y_pred.extend((p > 0.5).astype(int).tolist())
        y_true.extend(test[ycol].astype(int).tolist())

    return y_true, proba, y_pred


def _run_naive_arm(frame, horizon, origins, train_window_days):
    """Majority-class baseline: predict the train window's dominant direction (constant proba)."""
    ycol = f'y_dir_{horizon}'
    y_true, proba, y_pred = [], [], []
    for i, t_o in enumerate(origins[:-1]):
        nxt = origins[i + 1]
        lo = t_o - timedelta(days=train_window_days)
        train = frame[(frame['date'] > lo) & (frame['date'] <= t_o)]
        test = frame[(frame['date'] > t_o) & (frame['date'] <= nxt)]
        if train.empty or test.empty:
            continue
        up_rate = float(train[ycol].mean())
        pred = 1 if up_rate >= 0.5 else 0
        for _ in range(len(test)):
            proba.append(up_rate)
            y_pred.append(pred)
        y_true.extend(test[ycol].astype(int).tolist())
    return y_true, proba, y_pred


def run_backtest(symbols=None, years=2, step_days=21, train_window_days=None,
                 horizons=None, output_path=None) -> dict:
    """Run the full walk-forward backtest across all arms/horizons; write + return the report."""

    symbols = list(symbols or get_panel_symbols())
    horizons = horizons or settings.FORECAST_HORIZONS_DAYS
    train_window_days = train_window_days or settings.FORECAST_TRAIN_WINDOW_DAYS

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=years * 365)
    # Need history before `start` for features + train window.
    load_start = start - timedelta(days=train_window_days + 60)

    logger.info('[backtest] building frames %s … %s', load_start.date(), end.date())
    frame_events = feat.build_training_frame(symbols, load_start, end, horizons, include_events=True)
    frame_rules = feat.build_training_frame(symbols, load_start, end, horizons,
                                            include_events=True, router='rules')
    frame_price = feat.build_training_frame(symbols, load_start, end, horizons, include_events=False)

    if frame_price.empty:
        return {'error': 'no PriceBar data — run backfill_prices first'}

    all_dates = sorted(frame_price['date'].unique())
    origins = _origins(all_dates, start, end, step_days)

    report = {
        'generated_at': end.isoformat(),
        'symbols': symbols,
        'horizons': horizons,
        'years': years,
        'step_days': step_days,
        'train_window_days': train_window_days,
        'n_origins': len(origins),
        'results': {},
        'caveats': [
            'Reliability curve reflects the raw (uncalibrated) backtest model; served model is calibrated.',
            'Directional prediction of markets is near-random-walk; read accuracy vs the naive baseline.',
            'LLM-routed arm requires events routed with router_source=llm; rule arm with =rules.',
        ],
    }

    arms = {
        'naive': ('naive', frame_price),
        'price_only': ('model', frame_price),
        'price_plus_rule_events': ('model', frame_rules),
        'price_plus_llm_events': ('model', frame_events),
    }

    for h in horizons:
        report['results'][f'h{h}'] = {}
        for arm, (kind, fr) in arms.items():
            if fr is None or fr.empty:
                report['results'][f'h{h}'][arm] = {'n': 0, 'note': 'no data for this arm'}
                continue
            if kind == 'naive':
                yt, pr, yp = _run_naive_arm(fr, h, origins, train_window_days)
            else:
                yt, pr, yp = _run_model_arm(fr, h, origins, train_window_days)
            report['results'][f'h{h}'][arm] = _metrics(yt, pr, yp) if yt else {'n': 0}
            logger.info('[backtest] h%dd %s -> %s', h, arm,
                        report['results'][f'h{h}'][arm].get('accuracy'))

    output_path = output_path or os.path.join(
        os.getcwd(), f'forecast_backtest_{end.strftime("%Y%m%dT%H%M%S")}.json'
    )
    with open(output_path, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2, default=str)
    report['_output_path'] = output_path
    logger.info('[backtest] report written -> %s', output_path)
    return report
