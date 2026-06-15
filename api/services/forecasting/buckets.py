"""As-of quantile bucketing for the two prediction heads (plan §3b).

Both heads are defined by a **trailing as-of quantile** split computed from price
data ``≤ t`` only — never future data:

  * magnitude (direction): 5 balanced return-quantile classes
        strong_down / down / flat / up / strong_up
  * volatility: 3 balanced realized-vol terciles
        calm / normal / elevated

"flat" / "normal" are the *middle* quantile bands, not fixed absolute thresholds.
Balanced-by-construction quantiles also fix class imbalance.

The same helpers serve two callers:
  * feature/prediction time — bucket thresholds derived as-of t.
  * scoring time — classify the realized return / realized vol of an elapsed
    horizon against the *same* as-of thresholds that were stored on the Forecast.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone as dt_timezone

# Trailing lookback for building the as-of distribution.
QUANTILE_LOOKBACK_DAYS = int(os.getenv('BUCKET_QUANTILE_LOOKBACK_DAYS', '90'))

MAGNITUDE_LABELS = ('strong_down', 'down', 'flat', 'up', 'strong_up')
VOLATILITY_LABELS = ('calm', 'normal', 'elevated')


def _ensure_utc(t: datetime) -> datetime:
    return t.replace(tzinfo=dt_timezone.utc) if t.tzinfo is None else t


def _ticks_up_to(symbol: str, at_time: datetime, lookback_days: int) -> list[tuple[datetime, float]]:
    """Sorted (occurred_at, value) for ticks in (at_time - lookback, at_time]."""
    from core import models as core_models

    start = at_time - timedelta(days=lookback_days)
    rows = (
        core_models.PriceTick.objects
        .filter(symbol=symbol, occurred_at__gt=start, occurred_at__lte=at_time)
        .order_by('occurred_at')
        .values_list('occurred_at', 'value')
    )
    return [(_ensure_utc(ts), v) for ts, v in rows if v is not None]


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ── Horizon return series (for the magnitude head) ────────────────────────────

def _value_at_or_before(ticks: list[tuple[datetime, float]], target: datetime) -> float | None:
    """Last value at-or-before target (ticks sorted ascending). Binary search."""
    lo, hi, found = 0, len(ticks) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if ticks[mid][0] <= target:
            found = ticks[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return found


def trailing_horizon_returns(
    ticks: list[tuple[datetime, float]], horizon_hours: int, step_hours: float = 1.0,
) -> list[float]:
    """Series of realized returns over rolling windows of length ``horizon_hours``.

    Stepped at ``step_hours`` across the trailing window so the distribution is
    representative without being dominated by 5-minute autocorrelation.
    """
    if len(ticks) < 2:
        return []
    start_t, end_t = ticks[0][0], ticks[-1][0]
    horizon = timedelta(hours=horizon_hours)
    step = timedelta(hours=step_hours)
    returns: list[float] = []
    cur = start_t + horizon
    while cur <= end_t:
        v_now = _value_at_or_before(ticks, cur)
        v_then = _value_at_or_before(ticks, cur - horizon)
        if v_now is not None and v_then not in (None, 0):
            returns.append((v_now - v_then) / v_then)
        cur += step
    return returns


def realized_volatility(ticks: list[tuple[datetime, float]], window_hours: int) -> float | None:
    """Realized vol = std of log returns of ticks within the trailing window."""
    if not ticks:
        return None
    window_start = ticks[-1][0] - timedelta(hours=window_hours)
    vals = [v for ts, v in ticks if ts >= window_start and v > 0]
    if len(vals) < 3:
        return None
    log_rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals)) if vals[i - 1] > 0]
    if len(log_rets) < 2:
        return None
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(var)


def trailing_volatility_series(
    ticks: list[tuple[datetime, float]], horizon_hours: int, step_hours: float = 6.0,
) -> list[float]:
    """Series of realized vols over rolling windows for the as-of vol distribution."""
    if len(ticks) < 4:
        return []
    start_t, end_t = ticks[0][0], ticks[-1][0]
    horizon = timedelta(hours=horizon_hours)
    step = timedelta(hours=step_hours)
    series: list[float] = []
    cur = start_t + horizon
    while cur <= end_t:
        window = [(ts, v) for ts, v in ticks if cur - horizon <= ts <= cur]
        vol = realized_volatility(window, horizon_hours)
        if vol is not None:
            series.append(vol)
        cur += step
    return series


# ── As-of thresholds + classifiers ────────────────────────────────────────────

def magnitude_thresholds(symbol: str, at_time: datetime, horizon_hours: int) -> list[float] | None:
    """Return [q20, q40, q60, q80] of the as-of horizon-return distribution."""
    at_time = _ensure_utc(at_time)
    ticks = _ticks_up_to(symbol, at_time, QUANTILE_LOOKBACK_DAYS)
    returns = sorted(trailing_horizon_returns(ticks, horizon_hours))
    if len(returns) < 10:
        return None
    return [_quantile(returns, q) for q in (0.2, 0.4, 0.6, 0.8)]


def volatility_thresholds(symbol: str, at_time: datetime, horizon_hours: int) -> list[float] | None:
    """Return [t33, t66] of the as-of realized-vol distribution (terciles)."""
    at_time = _ensure_utc(at_time)
    ticks = _ticks_up_to(symbol, at_time, QUANTILE_LOOKBACK_DAYS)
    series = sorted(trailing_volatility_series(ticks, horizon_hours))
    if len(series) < 6:
        return None
    return [_quantile(series, q) for q in (1 / 3, 2 / 3)]


def classify_magnitude(ret: float, thresholds: list[float]) -> str:
    """Map a realized return to one of the 5 magnitude buckets via as-of thresholds."""
    q20, q40, q60, q80 = thresholds
    if ret <= q20:
        return 'strong_down'
    if ret <= q40:
        return 'down'
    if ret <= q60:
        return 'flat'
    if ret <= q80:
        return 'up'
    return 'strong_up'


def classify_volatility(vol: float, thresholds: list[float]) -> str:
    """Map a realized vol to one of the 3 volatility buckets via as-of terciles."""
    t33, t66 = thresholds
    if vol <= t33:
        return 'calm'
    if vol <= t66:
        return 'normal'
    return 'elevated'
