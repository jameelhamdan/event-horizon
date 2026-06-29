"""Leak-free, as-of feature engineering for the forecasting layer.

One row per (symbol, date). **No data dated after `t` may enter the row**: price features use
``PriceBar.date <= t``; event features use ``Event.latest_article_at <= t``. The label (training
only) is the realized return between two real price nodes, ``close@t -> close@t+horizon`` — that
is the supervised truth, computed from future data on purpose (not leakage).

Pure helpers; pandas/numpy are imported lazily so importing this module never hard-fails.
"""

import bisect
import logging
import math
from datetime import timedelta

from .routing import get_panel_symbols

logger = logging.getLogger(__name__)

EVENT_WINDOWS = (1, 3, 7)          # days
SENT_WINDOW = 3                    # days for sentiment aggregation
CATEGORIES = ('conflict', 'economic', 'political', 'disaster', 'health')
# High-signal tagged topics (router knows these). Presence/confidence become features.
TOPIC_FEATURES = (
    'ukraine-war', 'russia-ukraine', 'middle-east-conflict', 'iran', 'opec',
    'fed-rates', 'us-economy', 'china-economy', 'us-china-trade', 'inflation',
    'crypto', 'bitcoin',
)
MIN_HISTORY = 25                   # bars required before a row is usable
META_COLS = ('symbol', 'date', 'close')


def to_utc_ts(value):
    """Coerce a datetime/str to a UTC pandas Timestamp (tz-aware or naive both OK)."""
    import pandas as pd
    ts = pd.Timestamp(value)
    return ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')


# ── data loading ──────────────────────────────────────────────────────────────

def _load_bars(symbols):
    import pandas as pd
    from core.models import PriceBar

    # Single query for all symbols instead of N round-trips.
    all_rows = list(
        PriceBar.objects.filter(symbol__in=symbols, interval='1d')
        .order_by('symbol', 'date').values('symbol', 'date', 'close', 'volume')
    )
    grouped: dict[str, list] = {}
    for row in all_rows:
        grouped.setdefault(row['symbol'], []).append(row)

    out = {}
    for sym in symbols:
        rows = grouped.get(sym, [])
        if len(rows) < MIN_HISTORY:
            continue
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'], utc=True)
        df = df.dropna(subset=['close']).drop_duplicates('date').set_index('date').sort_index()
        out[sym] = df
    return out


def _load_events(start, end, router=None):
    """Return events sorted by latest_article_at; each as a light dict.

    ``router`` (optional 'llm'/'rules') filters by ``Event.router_source`` — used by the
    backtest to compare rule-routed vs LLM-routed event features.
    """
    from core.models import Event

    qs = Event.objects.filter(latest_article_at__gte=start, latest_article_at__lte=end)
    if router:
        qs = qs.filter(router_source=router)
    events = []
    for e in qs.order_by('latest_article_at').values(
        'latest_article_at', 'affected_indicators', 'avg_finbert_sentiment',
        'avg_sentiment', 'category', 'topic_slugs',
    ):
        weights = {}
        for ind in (e['affected_indicators'] or []):
            sym = ind.get('symbol')
            if sym:
                try:
                    weights[sym] = float(ind.get('weight') or 0.0)
                except (TypeError, ValueError):
                    pass
        events.append({
            't': to_utc_ts(e['latest_article_at']),
            'w': weights,
            'finbert': e['avg_finbert_sentiment'],
            'sentiment': e['avg_sentiment'],
            'category': e['category'],
            'topics': set(e['topic_slugs'] or []),
        })
    ts = [e['t'] for e in events]
    return events, ts


# ── feature computation ─────────────────────────────────────────────────────────

def _rsi(series, period=14):
    import numpy as np
    diff = series.diff().dropna()
    if len(diff) < period:
        return 50.0
    gain = diff.clip(lower=0).tail(period).mean()
    loss = (-diff.clip(upper=0)).tail(period).mean()
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100 - 100 / (1 + rs))


def _price_features(close, volume, t):
    import numpy as np
    s = close.loc[:t]
    if len(s) < MIN_HISTORY:
        return None
    logret = np.log(s).diff()

    def ret(n):
        return float(s.iloc[-1] / s.iloc[-1 - n] - 1) if len(s) > n else 0.0

    sma20 = float(s.tail(20).mean())
    feat = {
        'ret_1d': ret(1), 'ret_5d': ret(5), 'ret_20d': ret(20),
        'vol_20d': float(logret.tail(20).std() or 0.0),
        'mom_sma20': (float(s.iloc[-1] / sma20 - 1) if sma20 else 0.0),
        'rsi_14': _rsi(s, 14) / 100.0,
    }
    v = volume.loc[:t].dropna().tail(20)
    if len(v) > 2 and v.std():
        feat['vol_z'] = float((v.iloc[-1] - v.mean()) / v.std())
    else:
        feat['vol_z'] = 0.0
    return feat


def _event_features(window_events, symbol, t):
    """Aggregate routed events (already pre-sliced to last max(EVENT_WINDOWS) days)."""
    feat = {}
    for w in EVENT_WINDOWS:
        feat[f'evw_sum_{w}d'] = 0.0
        feat[f'evw_cnt_{w}d'] = 0.0
        feat[f'evw_maxabs_{w}d'] = 0.0
    feat['evw_decay_7d'] = 0.0
    fb_vals, sent_vals = [], []
    cat_cnt = {c: 0.0 for c in CATEGORIES}
    topics_present = {tp: 0.0 for tp in TOPIC_FEATURES}

    for e in window_events:
        weight = e['w'].get(symbol)
        # Clamp to ≥ 0: sub-second rounding or clock skew can yield a tiny
        # negative value, which would flip the exp() decay into amplification.
        age_days = max(0.0, (t - e['t']).total_seconds() / 86400.0)
        touches = weight is not None and weight != 0.0
        if touches:
            for w in EVENT_WINDOWS:
                if age_days <= w:
                    feat[f'evw_sum_{w}d'] += weight
                    feat[f'evw_cnt_{w}d'] += 1.0
                    feat[f'evw_maxabs_{w}d'] = max(feat[f'evw_maxabs_{w}d'], abs(weight))
            feat['evw_decay_7d'] += weight * math.exp(-age_days / 3.0)
            if age_days <= SENT_WINDOW:
                if e['finbert'] is not None:
                    fb_vals.append(e['finbert'])
                if e['sentiment'] is not None:
                    sent_vals.append(e['sentiment'])
                if e['category'] in cat_cnt:
                    cat_cnt[e['category']] += 1.0
            for tp in e['topics']:
                if tp in topics_present:
                    topics_present[tp] = 1.0

    feat['news_finbert_mean'] = (sum(fb_vals) / len(fb_vals)) if fb_vals else 0.0
    feat['news_finbert_min'] = min(fb_vals) if fb_vals else 0.0
    feat['news_sentiment_mean'] = (sum(sent_vals) / len(sent_vals)) if sent_vals else 0.0
    for c in CATEGORIES:
        feat[f'cat_{c}'] = cat_cnt[c]
    for tp in TOPIC_FEATURES:
        feat[f'topic_{tp}'] = topics_present[tp]
    return feat


def _zero_event_features():
    """All event features set to 0 — for the price-only ablation arm."""
    feat = {}
    for w in EVENT_WINDOWS:
        feat[f'evw_sum_{w}d'] = feat[f'evw_cnt_{w}d'] = feat[f'evw_maxabs_{w}d'] = 0.0
    feat['evw_decay_7d'] = 0.0
    feat['news_finbert_mean'] = feat['news_finbert_min'] = feat['news_sentiment_mean'] = 0.0
    for c in CATEGORIES:
        feat[f'cat_{c}'] = 0.0
    for tp in TOPIC_FEATURES:
        feat[f'topic_{tp}'] = 0.0
    return feat


def _symbol_onehot(symbol):
    return {f'sym_{s}': (1.0 if s == symbol else 0.0) for s in get_panel_symbols()}


# ── frame builders ──────────────────────────────────────────────────────────────

def build_training_frame(symbols=None, start=None, end=None, horizons=(1, 5),
                         include_events=True, router=None):
    """Return a pandas DataFrame: one row per (symbol, date) with features + labels.

    Labels per horizon h: ``y_ret_{h}`` (realized return) and ``y_dir_{h}`` (1=up, 0=down).
    """
    import pandas as pd

    symbols = list(symbols or get_panel_symbols())
    bars = _load_bars(symbols)
    if not bars:
        return pd.DataFrame()

    # Event window covers the whole frame; pad the load start by the max window.
    ev_start = start - timedelta(days=max(EVENT_WINDOWS) + 1) if start else None
    events, ev_ts = ([], [])
    if include_events:
        # Use the union span of all bars if no explicit start/end.
        lo = ev_start or min(df.index.min() for df in bars.values()).to_pydatetime()
        hi = end or max(df.index.max() for df in bars.values()).to_pydatetime()
        events, ev_ts = _load_events(lo, hi, router=router)

    rows = []
    max_h = max(horizons)
    for sym, df in bars.items():
        close, volume = df['close'], df['volume']
        idx = df.index
        for pos in range(MIN_HISTORY, len(idx) - max_h):
            t = idx[pos]
            if start and t < to_utc_ts(start):
                continue
            if end and t > to_utc_ts(end):
                continue
            pf = _price_features(close, volume, t)
            if pf is None:
                continue
            row = {'symbol': sym, 'date': t, 'close': float(close.iloc[pos])}
            row.update(pf)
            if include_events and events:
                lo_i = bisect.bisect_left(ev_ts, t - timedelta(days=max(EVENT_WINDOWS)))
                hi_i = bisect.bisect_right(ev_ts, t)
                row.update(_event_features(events[lo_i:hi_i], sym, t))
            else:
                row.update(_zero_event_features())
            row.update(_symbol_onehot(sym))
            for h in horizons:
                fut = float(close.iloc[pos + h])
                ret = fut / row['close'] - 1.0
                row[f'y_ret_{h}'] = ret
                row[f'y_dir_{h}'] = 1 if ret > 0 else 0
            rows.append(row)

    return pd.DataFrame(rows)


def build_feature_matrix(as_of_date=None, symbols=None, include_events=True, router=None):
    """Return one feature row per symbol as-of ``as_of_date`` (latest bar <= as_of). No labels."""
    import pandas as pd

    symbols = list(symbols or get_panel_symbols())
    bars = _load_bars(symbols)
    if not bars:
        return pd.DataFrame()

    t_cut = to_utc_ts(as_of_date) if as_of_date else None
    events, ev_ts = ([], [])
    if include_events:
        hi = (as_of_date or max(df.index.max() for df in bars.values()).to_pydatetime())
        lo = hi - timedelta(days=max(EVENT_WINDOWS) + 1)
        events, ev_ts = _load_events(lo, hi, router=router)

    rows = []
    for sym, df in bars.items():
        s = df.loc[:t_cut] if t_cut is not None else df
        if len(s) < MIN_HISTORY:
            continue
        t = s.index[-1]
        pf = _price_features(df['close'], df['volume'], t)
        if pf is None:
            continue
        row = {'symbol': sym, 'date': t, 'close': float(s['close'].iloc[-1])}
        row.update(pf)
        if include_events and events:
            lo_i = bisect.bisect_left(ev_ts, t - timedelta(days=max(EVENT_WINDOWS)))
            hi_i = bisect.bisect_right(ev_ts, t)
            row.update(_event_features(events[lo_i:hi_i], sym, t))
        else:
            row.update(_zero_event_features())
        row.update(_symbol_onehot(sym))
        rows.append(row)

    return pd.DataFrame(rows)


def feature_columns(df):
    """Feature columns = everything except meta + label columns."""
    return [c for c in df.columns if c not in META_COLS and not c.startswith('y_')]
