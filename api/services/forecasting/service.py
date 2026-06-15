"""Multi-horizon, two-head LLM v1 forecast service (decision-support) — plan §3.

v1 is **decision-support, not the autonomous predictor**: it emits a reliability
rating and may **abstain**, applies asymmetric per-class thresholds + post-hoc
calibration, and is always reported against naive baselines. The quantitative v2
classifier (``model.py``) is the primary predictor.

Two heads per (symbol, horizon): ``magnitude_bucket`` (direction, 5-class) and
``volatility_bucket`` (realized-vol regime, 3-class). One Forecast row per
(symbol, horizon, run) — the single multi-horizon LLM JSON is split into rows.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone

logger = logging.getLogger(__name__)

# Default (symbol, stream_key) pairs to forecast.
DEFAULT_SYMBOLS: list[tuple[str, str]] = [
    ('GC=F',     'commodity'),   # Gold
    ('CL=F',     'commodity'),   # Crude Oil
    ('NG=F',     'commodity'),   # Natural Gas
    ('ZW=F',     'commodity'),   # Wheat
    ('^VIX',     'index'),       # Volatility index
    ('DX-Y.NYB', 'index'),       # US Dollar index
    ('^TNX',     'bond'),        # 10Y Treasury yield
    ('SPY',      'stock'),       # S&P 500 ETF
    ('BTC-USD',  'crypto'),      # Bitcoin
    ('ETH-USD',  'crypto'),      # Ethereum
]

# Horizon label → hours. 1h is crypto-only (24/7 instruments).
HORIZON_LABELS: dict[str, int] = {'1h': 1, '1d': 24, '1w': 168}
CRYPTO_ONLY_HOURS = {1}

_MAGNITUDE_TO_DIRECTION = {
    'strong_down': 'down', 'down': 'down', 'flat': 'neutral',
    'up': 'up', 'strong_up': 'up',
}
_VALID_MAGNITUDE = set(_MAGNITUDE_TO_DIRECTION)
_VALID_VOLATILITY = {'calm', 'normal', 'elevated'}
_VALID_RELIABILITY = {'high', 'med', 'low'}


def _horizons_for(stream_key: str) -> list[tuple[str, int]]:
    """Applicable (label, hours) horizons for a stream. 1h only for 24/7 crypto."""
    out = []
    for label, hours in HORIZON_LABELS.items():
        if hours in CRYPTO_ONLY_HOURS and stream_key != 'crypto':
            continue
        out.append((label, hours))
    return out


def run_forecasts(symbols: list[tuple[str, str]] | None = None) -> int:
    """Generate multi-horizon two-head forecasts. Returns count of Forecast rows created."""
    from services.forecasting.features import build_feature_vector
    from services.forecasting import buckets, calibration
    from services.llm import LLMError, get_llm_service
    from core import models as core_models

    if symbols is None:
        symbols = DEFAULT_SYMBOLS

    now = datetime.now(tz=dt_timezone.utc)
    created = 0

    try:
        llm = get_llm_service()
    except LLMError as e:
        logger.error('Cannot initialize LLM for forecasting: %s', e)
        return 0

    for symbol, stream_key in symbols:
        try:
            horizons = _horizons_for(stream_key)
            features = build_feature_vector(symbol, now)
            if features['current_price'] is None:
                logger.debug('No price data for %s — skipping', symbol)
                continue

            # Precompute as-of bucket thresholds per horizon (stored for scoring).
            thresholds: dict[int, dict] = {}
            for _label, hours in horizons:
                thresholds[hours] = {
                    'magnitude': buckets.magnitude_thresholds(symbol, now, hours),
                    'volatility': buckets.volatility_thresholds(symbol, now, hours),
                }

            prompt = _build_prompt(symbol, features, horizons)
            try:
                raw = llm.chat([{'role': 'user', 'content': prompt}], temperature=0.2)
            except LLMError as e:
                logger.warning('LLM forecast failed for %s: %s', symbol, e)
                continue

            parsed = _parse_response(raw)
            if parsed is None:
                logger.warning('Unparseable LLM response for %s: %s', symbol, raw[:200])
                continue

            base_fv = {k: v for k, v in features.items() if k != 'routed_event_ids'}
            model_name = getattr(llm, '_model', 'unknown')

            for label, hours in horizons:
                res = parsed.get(label)
                if not res:
                    continue

                mag = res['magnitude_bucket']
                vol = res['volatility_bucket']
                reliability = res['reliability']
                raw_conf = res['confidence']

                cal_conf = calibration.calibrate_confidence(raw_conf, symbol, hours)
                abstain = (
                    res['abstain']
                    or calibration.should_abstain(mag, cal_conf, reliability, symbol, hours)
                )

                fv = {
                    **base_fv,
                    'horizon_hours': hours,
                    'raw_confidence': raw_conf,
                    'magnitude_thresholds': thresholds[hours]['magnitude'],
                    'volatility_thresholds': thresholds[hours]['volatility'],
                }

                core_models.Forecast.objects.create(
                    symbol=symbol,
                    stream_key=stream_key,
                    generated_at=now,
                    horizon_hours=hours,
                    direction=_MAGNITUDE_TO_DIRECTION.get(mag, 'neutral'),
                    confidence=round(cal_conf, 4),
                    magnitude_bucket=mag,
                    volatility_bucket=vol,
                    reliability=reliability,
                    abstained=bool(abstain),
                    predicted_value=features['current_price'],
                    model_name=model_name,
                    reasoning=res.get('reasoning', ''),
                    event_ids=features.get('routed_event_ids', []),
                    feature_vector=fv,
                )
                created += 1
                logger.info(
                    'Forecast %s +%dh → mag=%s vol=%s rel=%s abstain=%s (conf=%.2f)',
                    symbol, hours, mag, vol, reliability, abstain, cal_conf,
                )
        except Exception:
            logger.exception('Unexpected error forecasting %s', symbol)

    return created


# ── Trading-session snapping (plan §3c) ───────────────────────────────────────

def snap_to_session_close(target: datetime, stream_key: str) -> datetime:
    """For non-24/7 instruments, snap a target time to the next trading-session close.

    Crypto trades 24/7 and is returned unchanged. For everything else we skip
    weekends and snap to ~21:00 UTC (US cash-session close) so scoring reads a real
    session close rather than a stale weekend/holiday tick.
    """
    if stream_key == 'crypto':
        return target
    t = target
    # Advance off weekends (Sat=5, Sun=6).
    while t.weekday() >= 5:
        t = (t + timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    # If before the session close, snap to today's close; else next weekday close.
    close = t.replace(hour=21, minute=0, second=0, microsecond=0)
    if t > close:
        nxt = t + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        close = nxt.replace(hour=21, minute=0, second=0, microsecond=0)
    return close


def score_forecasts() -> int:
    """Fill actual buckets for forecasts whose horizon has elapsed. Returns count scored."""
    from core import models as core_models
    from services.forecasting import buckets

    now = datetime.now(tz=dt_timezone.utc)
    scored = 0

    pending = core_models.Forecast.objects.filter(actual_value__isnull=True)
    for forecast in pending:
        gen = forecast.generated_at
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=dt_timezone.utc)

        target = gen + timedelta(hours=forecast.horizon_hours)
        target = snap_to_session_close(target, forecast.stream_key)
        if now < target:
            continue

        tick = (
            core_models.PriceTick.objects
            .filter(symbol=forecast.symbol, occurred_at__gte=target)
            .order_by('occurred_at')
            .first()
        )
        if tick is None:
            continue

        actual_value = tick.value
        base = forecast.predicted_value
        if not base:
            base_tick = (
                core_models.PriceTick.objects
                .filter(symbol=forecast.symbol, occurred_at__lte=gen)
                .order_by('-occurred_at')
                .first()
            )
            base = base_tick.value if base_tick else None

        update_fields = ['actual_value']
        forecast.actual_value = actual_value

        fv = forecast.feature_vector or {}
        mag_thr = fv.get('magnitude_thresholds')
        if base and mag_thr:
            ret = (actual_value - base) / base
            forecast.actual_bucket = buckets.classify_magnitude(ret, mag_thr)
            update_fields.append('actual_bucket')

        vol_thr = fv.get('volatility_thresholds')
        if vol_thr:
            horizon_ticks = [
                (buckets._ensure_utc(ts), v)
                for ts, v in core_models.PriceTick.objects
                .filter(symbol=forecast.symbol,
                        occurred_at__gt=target - timedelta(hours=forecast.horizon_hours),
                        occurred_at__lte=tick.occurred_at)
                .order_by('occurred_at')
                .values_list('occurred_at', 'value')
                if v is not None
            ]
            realized = buckets.realized_volatility(horizon_ticks, forecast.horizon_hours)
            if realized is not None:
                forecast.actual_volatility_bucket = buckets.classify_volatility(realized, vol_thr)
                update_fields.append('actual_volatility_bucket')

        forecast.save(update_fields=update_fields)
        scored += 1

    return scored


def _build_prompt(symbol: str, features: dict, horizons: list[tuple[str, int]]) -> str:
    def fmt(v):
        return f'{v:.4f}' if isinstance(v, (int, float)) else 'N/A'

    horizon_keys = ', '.join(f'"{label}"' for label, _ in horizons)
    per_horizon = (
        '{"magnitude_bucket": "strong_down"|"down"|"flat"|"up"|"strong_up", '
        '"volatility_bucket": "calm"|"normal"|"elevated", '
        '"confidence": 0.0-1.0, "reliability": "high"|"med"|"low", '
        '"abstain": true|false, "reasoning": "one sentence"}'
    )
    schema = '{' + ', '.join(f'"{label}": {per_horizon}' for label, _ in horizons) + '}'

    return f"""You are a quantitative analyst providing DECISION SUPPORT (not an autonomous trader).
Forecast {symbol} for horizons: {horizon_keys}.

For each horizon predict TWO things:
  1. magnitude_bucket — directional return class (quantile-balanced).
  2. volatility_bucket — realized-volatility regime (this is the more learnable target).

Be honest: set "reliability" to "low" and "abstain": true when the signal is weak.
News is lagged and largely priced-in; do not force a directional guess.

As-of features (only data available now):
Current price: {fmt(features.get('current_price'))}
1h momentum: {fmt(features.get('price_momentum_1h'))}
24h momentum: {fmt(features.get('price_momentum_24h'))}
7d momentum: {fmt(features.get('price_momentum_7d'))}
Realized vol 24h: {fmt(features.get('realized_vol_24h'))}
Realized vol 7d: {fmt(features.get('realized_vol_7d'))}
Value vs 24h MA: {fmt(features.get('value_vs_ma_24h'))}
Routed events (24h share): {fmt(features.get('routed_share_24h'))} (z={fmt(features.get('routed_zscore_24h'))})
FinBERT news sentiment: {fmt(features.get('news_finbert_mean'))} (−1 bearish → +1 bullish)
VADER news sentiment: {fmt(features.get('news_vader_mean'))}
Max event intensity: {fmt(features.get('event_intensity_max'))}
Country-risk weight: {fmt(features.get('country_risk'))}
Events by category (24h): {json.dumps(features.get('event_count_by_category', {}))}

Respond with JSON only (no markdown), one object per horizon:
{schema}"""


def _parse_response(raw: str) -> dict | None:
    """Parse the multi-horizon JSON into {label: {validated fields}}."""
    text = re.sub(r'^```(?:json)?\s*', '', raw.strip())
    text = re.sub(r'\s*```$', '', text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Fall back to first balanced object
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            return None

    if not isinstance(data, dict):
        return None

    out: dict[str, dict] = {}
    for label, res in data.items():
        if label not in HORIZON_LABELS or not isinstance(res, dict):
            continue
        mag = str(res.get('magnitude_bucket', '')).lower()
        vol = str(res.get('volatility_bucket', '')).lower()
        if mag not in _VALID_MAGNITUDE or vol not in _VALID_VOLATILITY:
            continue
        reliability = str(res.get('reliability', 'low')).lower()
        if reliability not in _VALID_RELIABILITY:
            reliability = 'low'
        try:
            confidence = max(0.0, min(1.0, float(res.get('confidence', 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        out[label] = {
            'magnitude_bucket': mag,
            'volatility_bucket': vol,
            'confidence': confidence,
            'reliability': reliability,
            'abstain': bool(res.get('abstain', False)),
            'reasoning': str(res.get('reasoning', '')),
        }
    return out or None
