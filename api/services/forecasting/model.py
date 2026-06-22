"""LightGBM forecasting models: directional classifier + magnitude regressor, per horizon.

Per horizon we train two pooled models (symbol one-hot encoded so a single model covers the
whole panel):
  * classifier  → calibrated P(up)        → ``direction``
  * regressor   → predicted return (%)    → ``predicted_price`` + confidence band

Artifacts persist to ``settings.FORECAST_MODEL_DIR`` as ``model_h{horizon}.joblib`` and load
lazily (cached). LightGBM/sklearn/joblib are optional at import time: if missing, training
raises a clear error and prediction returns an empty list.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from django.conf import settings

from .features import build_feature_matrix, feature_columns

logger = logging.getLogger(__name__)

_cache: dict[int, dict] = {}


def _model_path(horizon: int) -> str:
    return os.path.join(settings.FORECAST_MODEL_DIR, f'model_h{horizon}.joblib')


def _require_libs():
    try:
        import joblib  # noqa: F401
        import lightgbm  # noqa: F401
        import numpy  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            'Forecasting requires lightgbm, scikit-learn, joblib, numpy — '
            f'install them (pip install -r requirements.txt). Missing: {exc.name}'
        ) from exc


def train(frame, horizon: int) -> dict:
    """Fit classifier + regressor for ``horizon`` on ``frame``; persist + return the artifact."""
    _require_libs()
    import joblib
    import numpy as np
    from lightgbm import LGBMClassifier, LGBMRegressor
    from sklearn.calibration import CalibratedClassifierCV

    ycol_dir, ycol_ret = f'y_dir_{horizon}', f'y_ret_{horizon}'
    df = frame.dropna(subset=[ycol_dir, ycol_ret])
    cols = feature_columns(df)
    X = df[cols].astype(float).values
    y_dir = df[ycol_dir].astype(int).values
    y_ret = df[ycol_ret].astype(float).values

    n = len(df)
    if n < 50 or len(np.unique(y_dir)) < 2:
        raise RuntimeError(f'h{horizon}: not enough/blanced data to train (n={n})')

    base_clf = LGBMClassifier(
        n_estimators=300, learning_rate=0.05, num_leaves=31,
        subsample=0.8, colsample_bytree=0.8, min_child_samples=20, verbose=-1,
    )
    # Calibrate P(up). Isotonic needs samples; fall back to sigmoid, then raw on failure.
    clf = base_clf
    try:
        method = 'isotonic' if n >= 500 else 'sigmoid'
        clf = CalibratedClassifierCV(base_clf, method=method, cv=3)
        clf.fit(X, y_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning('h%d: calibration failed (%s) — using raw classifier', horizon, exc)
        clf = base_clf
        clf.fit(X, y_dir)

    reg = LGBMRegressor(
        n_estimators=300, learning_rate=0.05, num_leaves=31,
        subsample=0.8, colsample_bytree=0.8, min_child_samples=20, verbose=-1,
    )
    reg.fit(X, y_ret)
    resid_std = float(np.std(y_ret - reg.predict(X)))

    artifact = {
        'horizon': horizon,
        'clf': clf,
        'reg': reg,
        'columns': cols,
        'resid_std': resid_std,
        'n_samples': n,
        'model_version': datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
        'trained_at': datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(settings.FORECAST_MODEL_DIR, exist_ok=True)
    joblib.dump(artifact, _model_path(horizon))
    _cache[horizon] = artifact
    logger.info('[model] trained h%dd on %d samples -> %s', horizon, n, _model_path(horizon))
    return artifact


def load(horizon: int) -> dict | None:
    """Load (and cache) the artifact for ``horizon``, or None if not trained."""
    if horizon in _cache:
        return _cache[horizon]
    path = _model_path(horizon)
    if not os.path.exists(path):
        return None
    try:
        import joblib
        _cache[horizon] = joblib.load(path)
        return _cache[horizon]
    except Exception as exc:  # noqa: BLE001
        logger.warning('[model] failed to load h%d artifact: %s', horizon, exc)
        return None


def predict(features_df, horizon: int) -> list[dict]:
    """Predict for each row in ``features_df`` (from build_feature_matrix). Empty if no model."""
    artifact = load(horizon)
    if artifact is None or features_df.empty:
        return []
    import numpy as np

    cols = artifact['columns']
    X = features_df.reindex(columns=cols, fill_value=0.0).astype(float).values
    proba_up = artifact['clf'].predict_proba(X)[:, 1]
    ret = artifact['reg'].predict(X)
    resid = artifact['resid_std']

    out = []
    for i, (_, row) in enumerate(features_df.iterrows()):
        p = float(proba_up[i])
        r = float(ret[i])
        close = float(row['close'])
        direction = 'up' if p > 0.55 else 'down' if p < 0.45 else 'neutral'
        predicted_price = close * (1 + r)
        out.append({
            'symbol': row['symbol'],
            'horizon_days': horizon,
            'proba_up': round(p, 4),
            'direction': direction,
            'predicted_change_pct': round(r * 100, 4),
            'predicted_price': round(predicted_price, 4),
            'band_low': round(close * (1 + r - resid), 4),
            'band_high': round(close * (1 + r + resid), 4),
            'confidence': round(abs(p - 0.5) * 2, 4),
            'current_value': round(close, 4),
            'model_version': artifact['model_version'],
            'as_of_date': row['date'].to_pydatetime(),
        })
    return out


def clear_cache():
    _cache.clear()
