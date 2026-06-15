"""v2 quantitative classifier — the primary predictor (plan §3b).

A gradient-boosted classifier (LightGBM) per head, trained on the historical
backfill with **as-of features** and **walk-forward / time-based validation only**
(never random k-fold — it leaks future into past).

Design notes:
  * Features come from ``features.build_feature_vector`` (as-of, volume-normalized).
  * Labels come from ``buckets`` using the realized future return/vol over the
    horizon. Computing the label requires data after ``t`` — that is the supervised
    target, not leakage; the *features* remain strictly as-of.
  * Models persist to ``FORECAST_MODEL_DIR`` (default ``./forecast_models``).
  * Degrades gracefully: if lightgbm/numpy are unavailable, training raises a clear
    error and ``predict`` returns None so v1 (LLM) remains the fallback.

LightGBM is an optional dependency (see requirements.txt comment).
"""

from __future__ import annotations

import logging
import math
import os
import pickle
from datetime import datetime, timedelta, timezone as dt_timezone

logger = logging.getLogger(__name__)

MODEL_DIR = os.getenv('FORECAST_MODEL_DIR', os.path.join(os.getcwd(), 'forecast_models'))

# Fixed, ordered numeric feature keys — the contract between train and serve.
FEATURE_KEYS = [
    'price_momentum_1h', 'price_momentum_24h', 'price_momentum_7d',
    'realized_vol_24h', 'realized_vol_7d', 'log_volume_mean_24h',
    'value_vs_ma_24h', 'value_vs_ma_7d',
    'routed_share_1h', 'routed_zscore_1h',
    'routed_share_24h', 'routed_zscore_24h',
    'routed_share_168h', 'routed_zscore_168h',
    'news_finbert_mean', 'news_finbert_std',
    'news_vader_mean', 'news_vader_std',
    'event_intensity_max', 'event_intensity_mean',
    'event_count_24h', 'routed_event_count', 'country_risk',
]


def vectorize(features: dict) -> list[float]:
    """Flatten a feature dict to the fixed numeric vector (missing → 0.0)."""
    out = []
    for k in FEATURE_KEYS:
        v = features.get(k)
        out.append(float(v) if isinstance(v, (int, float)) and not _isnan(v) else 0.0)
    return out


def _isnan(v) -> bool:
    try:
        return math.isnan(v)
    except (TypeError, ValueError):
        return False


def _model_path(symbol: str, horizon_hours: int, head: str) -> str:
    safe = symbol.replace('/', '_').replace('=', '_').replace('^', '_')
    return os.path.join(MODEL_DIR, f'{safe}_{horizon_hours}h_{head}.pkl')


# ── Training-set construction ─────────────────────────────────────────────────

def build_training_set(symbol: str, horizon_hours: int, step_hours: int = 24) -> list[dict]:
    """Build [{'t', 'features', 'mag_label', 'vol_label'}] over the historical corpus.

    Iterates forecast-times on a ``step_hours`` grid across the symbol's tick
    history. Features are as-of t; labels are the realized future buckets.
    """
    from core import models as core_models
    from services.forecasting import buckets
    from services.forecasting.features import build_feature_vector

    span = (
        core_models.PriceTick.objects.filter(symbol=symbol)
        .order_by('occurred_at').values_list('occurred_at', flat=True)
    )
    times = list(span)
    if len(times) < 2:
        return []
    start = buckets._ensure_utc(times[0]) + timedelta(days=buckets.QUANTILE_LOOKBACK_DAYS)
    end = buckets._ensure_utc(times[-1]) - timedelta(hours=horizon_hours)

    samples: list[dict] = []
    t = start
    while t <= end:
        mag_thr = buckets.magnitude_thresholds(symbol, t, horizon_hours)
        vol_thr = buckets.volatility_thresholds(symbol, t, horizon_hours)
        if mag_thr and vol_thr:
            ticks = buckets._ticks_up_to(symbol, t + timedelta(hours=horizon_hours),
                                         buckets.QUANTILE_LOOKBACK_DAYS)
            base = buckets._value_at_or_before(ticks, t)
            future = buckets._value_at_or_before(ticks, t + timedelta(hours=horizon_hours))
            if base and future:
                ret = (future - base) / base
                window = [(ts, v) for ts, v in ticks if t <= ts <= t + timedelta(hours=horizon_hours)]
                realized_vol = buckets.realized_volatility(window, horizon_hours)
                if realized_vol is not None:
                    features = build_feature_vector(symbol, t, horizon_hours)
                    samples.append({
                        't': t,
                        'features': vectorize(features),
                        'mag_label': buckets.classify_magnitude(ret, mag_thr),
                        'vol_label': buckets.classify_volatility(realized_vol, vol_thr),
                    })
        t += timedelta(hours=step_hours)
    return samples


def walk_forward_splits(samples: list[dict], n_folds: int = 4):
    """Yield (train, test) splits expanding forward in time (never random k-fold)."""
    samples = sorted(samples, key=lambda s: s['t'])
    n = len(samples)
    if n < n_folds + 1:
        return
    fold = n // (n_folds + 1)
    for i in range(1, n_folds + 1):
        cut = fold * i
        train, test = samples[:cut], samples[cut:cut + fold]
        if train and test:
            yield train, test


def train(symbol: str, horizon_hours: int, n_folds: int = 4) -> dict:
    """Train both heads for (symbol, horizon) with walk-forward validation.

    Returns {'mag_cv_accuracy', 'vol_cv_accuracy', 'n_samples'} and persists the
    final models (trained on all data) to disk. Raises RuntimeError if deps missing.
    """
    try:
        import numpy as np
        import lightgbm as lgb
    except ImportError as e:
        raise RuntimeError(
            'v2 training requires lightgbm + numpy. Install: pip install lightgbm numpy'
        ) from e

    samples = build_training_set(symbol, horizon_hours)
    if len(samples) < 50:
        return {'n_samples': len(samples), 'skipped': 'insufficient data'}

    os.makedirs(MODEL_DIR, exist_ok=True)
    results: dict = {'n_samples': len(samples)}

    for head, label_key in (('magnitude', 'mag_label'), ('volatility', 'vol_label')):
        fold_acc: list[float] = []
        for train_s, test_s in walk_forward_splits(samples, n_folds):
            classes = sorted({s[label_key] for s in train_s})
            cls_idx = {c: i for i, c in enumerate(classes)}
            X_tr = np.array([s['features'] for s in train_s])
            y_tr = np.array([cls_idx[s[label_key]] for s in train_s])
            X_te = np.array([s['features'] for s in test_s])
            booster = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05,
                                         num_leaves=31, min_child_samples=10)
            booster.fit(X_tr, y_tr)
            preds = booster.predict(X_te)
            inv = {i: c for c, i in cls_idx.items()}
            correct = sum(1 for p, s in zip(preds, test_s) if inv.get(int(p)) == s[label_key])
            fold_acc.append(correct / len(test_s))

        # Final model on all data
        classes = sorted({s[label_key] for s in samples})
        cls_idx = {c: i for i, c in enumerate(classes)}
        X = np.array([s['features'] for s in samples])
        y = np.array([cls_idx[s[label_key]] for s in samples])
        final = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05,
                                   num_leaves=31, min_child_samples=10)
        final.fit(X, y)
        with open(_model_path(symbol, horizon_hours, head), 'wb') as fh:
            pickle.dump({'model': final, 'classes': classes, 'feature_keys': FEATURE_KEYS}, fh)

        results[f'{head}_cv_accuracy'] = round(sum(fold_acc) / len(fold_acc), 4) if fold_acc else None

    logger.info('[v2] trained %s +%dh — %s', symbol, horizon_hours, results)
    return results


def predict(symbol: str, horizon_hours: int, features: dict) -> dict | None:
    """Predict both heads with the trained v2 models. Returns None if unavailable."""
    try:
        import numpy as np
    except ImportError:
        return None

    out: dict = {}
    for head in ('magnitude', 'volatility'):
        path = _model_path(symbol, horizon_hours, head)
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as fh:
            bundle = pickle.load(fh)
        vec = np.array([vectorize(features)])
        proba = bundle['model'].predict_proba(vec)[0]
        idx = int(proba.argmax())
        out[head] = {
            'bucket': bundle['classes'][idx],
            'confidence': round(float(proba[idx]), 4),
        }
    return out
