"""Point-in-time feature assembly for (symbol, at_time) — plan §3a / §5.

Hard guarantees:
  * **Strict as-of filtering.** No event with event-time (``latest_article_at`` =
    max published_on of constituent articles) after ``t`` enters the vector, and no
    price tick after ``t`` is read. Backtest and live use the identical code path.
  * **Volume-invariant news features.** Every count/density feature is normalized
    (share-of-events + trailing-baseline z-score) via a *shared* transform applied
    identically at train and serve, so the sparse backfill corpus and continuous
    live ingestion are comparable (plan §17 / caveat 4).
  * **Realized-vol + log-volume** market features feed the more-learnable
    volatility head.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone as dt_timezone

logger = logging.getLogger(__name__)

# Trailing baseline window for volume-normalizing news counts (days).
BASELINE_DAYS = 30
NEWS_WINDOWS_HOURS = (1, 24, 168)


def _ensure_utc(t: datetime) -> datetime:
    return t.replace(tzinfo=dt_timezone.utc) if t.tzinfo is None else t


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / len(values)) ** 0.5


def volume_normalize(window_count: int, total_in_window: int, baseline_counts: list[int]) -> dict:
    """Shared volume-invariant transform — IDENTICAL at train and serve time.

    Returns:
      * ``share``  — window_count / total_in_window  (∈ [0, 1], inherently scale-free)
      * ``zscore`` — (window_count − mean(baseline)) / std(baseline)  (regime vs. own history)
    """
    share = (window_count / total_in_window) if total_in_window else 0.0
    bmean = _mean([float(c) for c in baseline_counts])
    bstd = _std([float(c) for c in baseline_counts])
    if bmean is None or bstd in (None, 0):
        zscore = 0.0
    else:
        zscore = (window_count - bmean) / bstd
    return {'share': round(share, 4), 'zscore': round(zscore, 4)}


def _routed_symbols_for_event(e: dict) -> list[str]:
    """Affected symbols for an event — prefer the stored deterministic weights."""
    affected = e.get('affected_indicators') or []
    if affected:
        return [a['symbol'] for a in affected if isinstance(a, dict) and a.get('symbol')]
    from services.forecasting.routing import route_event_to_symbols
    return route_event_to_symbols(
        e.get('category', ''), e.get('location_name', ''), e.get('topic_slugs') or [],
    )


def build_feature_vector(symbol: str, at_time: datetime, horizon_hours: int = 24) -> dict:
    """Build the as-of feature dict for (symbol, at_time)."""
    from core import models as core_models
    from services.forecasting import buckets
    from services.forecasting.routing import _country_risk

    at_time = _ensure_utc(at_time)

    # ── Market features (ticks ≤ t only) ──────────────────────────────────────
    tick_start = at_time - timedelta(days=8)
    ticks = [
        (buckets._ensure_utc(ts), v)
        for ts, v in core_models.PriceTick.objects
        .filter(symbol=symbol, occurred_at__lte=at_time, occurred_at__gt=tick_start)
        .order_by('occurred_at')
        .values_list('occurred_at', 'value')
        if v is not None
    ]
    volumes = list(
        core_models.PriceTick.objects
        .filter(symbol=symbol, occurred_at__lte=at_time, occurred_at__gt=at_time - timedelta(hours=24))
        .values_list('volume', flat=True)
    )

    current_price = ticks[-1][1] if ticks else None

    def momentum(hours: int) -> float | None:
        if not current_price:
            return None
        ref = buckets._value_at_or_before(ticks, at_time - timedelta(hours=hours))
        return (current_price - ref) / ref if ref else None

    def moving_avg(hours: int) -> float | None:
        cutoff = at_time - timedelta(hours=hours)
        vals = [v for ts, v in ticks if ts >= cutoff]
        return _mean(vals)

    ma_24h = moving_avg(24)
    ma_168h = moving_avg(168)
    vol_volumes = [float(v) for v in volumes if v not in (None, 0) and v > 0]

    # ── News / event features (event-time ≤ t only) ───────────────────────────
    longest_window_start = at_time - timedelta(hours=max(NEWS_WINDOWS_HOURS))
    baseline_start = at_time - timedelta(days=BASELINE_DAYS)
    events = list(
        core_models.Event.objects
        .filter(latest_article_at__isnull=False,
                latest_article_at__lte=at_time,
                latest_article_at__gt=baseline_start)
        .values('id', 'category', 'avg_sentiment', 'avg_finbert_sentiment',
                'avg_intensity', 'topic_slugs', 'location_name',
                'affected_indicators', 'latest_article_at')
    )

    # Daily baseline counts of routed events (for z-score normalization)
    baseline_daily: dict[str, int] = {}
    window_events: dict[int, list[dict]] = {h: [] for h in NEWS_WINDOWS_HOURS}
    routed_window_events: dict[int, list[dict]] = {h: [] for h in NEWS_WINDOWS_HOURS}
    for e in events:
        et = _ensure_utc(e['latest_article_at'])
        is_routed = symbol in _routed_symbols_for_event(e)
        if is_routed:
            day_key = et.date().isoformat()
            baseline_daily[day_key] = baseline_daily.get(day_key, 0) + 1
        for h in NEWS_WINDOWS_HOURS:
            if et > at_time - timedelta(hours=h):
                window_events[h].append(e)
                if is_routed:
                    routed_window_events[h].append(e)

    baseline_counts = list(baseline_daily.values())

    news_features: dict = {}
    for h in NEWS_WINDOWS_HOURS:
        total = len(window_events[h])
        routed = routed_window_events[h]
        norm = volume_normalize(len(routed), total, baseline_counts)
        news_features[f'routed_share_{h}h'] = norm['share']
        news_features[f'routed_zscore_{h}h'] = norm['zscore']
        news_features[f'routed_count_{h}h'] = len(routed)  # raw, kept for audit only

    # Sentiment over routed events in the 24h window (FinBERT and VADER as distinct features)
    routed_24h = routed_window_events[24]
    vader_vals = [e['avg_sentiment'] for e in routed_24h if e.get('avg_sentiment') is not None]
    finbert_vals = [e['avg_finbert_sentiment'] for e in routed_24h if e.get('avg_finbert_sentiment') is not None]
    intensities = [e['avg_intensity'] for e in routed_24h if e.get('avg_intensity') is not None]

    category_counts: dict[str, int] = {}
    for e in window_events[24]:
        cat = e.get('category') or 'general'
        category_counts[cat] = category_counts.get(cat, 0) + 1

    routed_event_ids = [str(e['id']) for e in routed_24h]
    country_risk = max((_country_risk(e.get('location_name', '')) for e in routed_24h), default=0.0)

    return {
        'symbol':                 symbol,
        'at_time':                at_time.isoformat(),
        'horizon_hours':          horizon_hours,
        # market
        'current_price':          current_price,
        'price_momentum_1h':      momentum(1),
        'price_momentum_24h':     momentum(24),
        'price_momentum_7d':      momentum(168),
        'realized_vol_24h':       buckets.realized_volatility(ticks, 24),
        'realized_vol_7d':        buckets.realized_volatility(ticks, 168),
        'log_volume_mean_24h':    (math.log1p(_mean(vol_volumes)) if vol_volumes else None),
        'value_vs_ma_24h':        ((current_price / ma_24h - 1) if current_price and ma_24h else None),
        'value_vs_ma_7d':         ((current_price / ma_168h - 1) if current_price and ma_168h else None),
        # news (volume-normalized)
        **news_features,
        'news_finbert_mean':      _mean(finbert_vals),
        'news_finbert_std':       _std(finbert_vals),
        'news_vader_mean':        _mean(vader_vals),
        'news_vader_std':         _std(vader_vals),
        'event_intensity_max':    max(intensities) if intensities else None,
        'event_intensity_mean':   _mean(intensities),
        'event_count_24h':        len(window_events[24]),
        'routed_event_count':     len(routed_24h),
        'country_risk':           round(country_risk, 4),
        'event_count_by_category': category_counts,
        # carried separately (not stored in feature_vector)
        'routed_event_ids':       routed_event_ids,
    }
