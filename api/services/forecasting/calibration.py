"""Post-hoc calibration + asymmetric per-class gating for the v1 LLM classifier.

Raw LLM confidence is unusable as-is (plan §3b / caveat 5) and the ``down`` class
systematically collapses. This module provides:

  * **Temperature/Platt-style scaling** of raw confidence (per symbol/horizon).
  * **Asymmetric per-class thresholds** — a prediction is only emitted if its
    calibrated confidence clears the class's threshold; otherwise the forecast
    **abstains**.

Defaults are conservative identity-ish values. Tuned parameters (fit on a
walk-forward holdout) are loaded from ``FORECAST_CALIBRATION_JSON`` if set — the
machinery is here so tuning is a data/ops step, not a code change.
"""

from __future__ import annotations

import functools
import json
import logging
import os

logger = logging.getLogger(__name__)

# Per-class confidence floors. The down class is held to a higher bar because it
# is the known failure mode (predict down only when quite sure).
DEFAULT_CLASS_THRESHOLDS: dict[str, float] = {
    'strong_down': 0.60,
    'down':        0.60,
    'flat':        0.45,
    'up':          0.50,
    'strong_up':   0.55,
    # volatility head
    'calm':        0.50,
    'normal':      0.45,
    'elevated':    0.55,
}

# Temperature > 1 softens (shrinks toward 0.5) overconfident LLM probabilities.
DEFAULT_TEMPERATURE = 1.5


@functools.lru_cache(maxsize=1)
def _config() -> dict:
    path = os.getenv('FORECAST_CALIBRATION_JSON')
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        logger.exception('[calibration] failed to load %s — using defaults', path)
        return {}


def _key(symbol: str, horizon_hours: int) -> str:
    return f'{symbol}|{horizon_hours}'


def calibrate_confidence(raw: float, symbol: str, horizon_hours: int) -> float:
    """Temperature-scale a raw confidence into a calibrated probability."""
    cfg = _config().get(_key(symbol, horizon_hours), {})
    temp = float(cfg.get('temperature', DEFAULT_TEMPERATURE))
    raw = max(1e-6, min(1 - 1e-6, raw))
    # Logit-space temperature scaling, then back to probability.
    import math
    logit = math.log(raw / (1 - raw)) / max(temp, 1e-6)
    return 1.0 / (1.0 + math.exp(-logit))


def class_threshold(bucket: str, symbol: str, horizon_hours: int) -> float:
    cfg = _config().get(_key(symbol, horizon_hours), {})
    thresholds = {**DEFAULT_CLASS_THRESHOLDS, **(cfg.get('class_thresholds') or {})}
    return thresholds.get(bucket, 0.5)


def should_abstain(bucket: str, calibrated_conf: float, reliability: str,
                   symbol: str, horizon_hours: int) -> bool:
    """Abstain when reliability is low or calibrated confidence misses the class bar."""
    if reliability == 'low':
        return True
    return calibrated_conf < class_threshold(bucket, symbol, horizon_hours)
