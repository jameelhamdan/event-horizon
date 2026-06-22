"""Daily OHLC backfill for the indicator panel → ``PriceBar``.

Non-crypto symbols via **yfinance** (already a project dependency); crypto (BTC/ETH) via
**CoinGecko** (free, no key). Idempotent: only inserts dates not already stored for a symbol.

This is the training + charting substrate for the forecasting layer. Distinct from the
high-frequency ``PriceTick`` live stream.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .routing import PANEL_SYMBOLS

logger = logging.getLogger(__name__)

# Panel symbol → (stream_key, display name). Mirrors services/streams/prices.py.
SYMBOL_META: dict[str, tuple[str, str]] = {
    'GC=F': ('commodity', 'Gold'),
    'CL=F': ('commodity', 'Crude Oil'),
    'NG=F': ('commodity', 'Natural Gas'),
    'ZW=F': ('commodity', 'Wheat'),
    'DX-Y.NYB': ('index', 'US Dollar Index'),
    '^TNX': ('bond', 'US 10Y Treasury'),
    '^VIX': ('index', 'Volatility Index'),
    'SPY': ('stock', 'S&P 500 ETF'),
    'BTC-USD': ('crypto', 'Bitcoin'),
    'ETH-USD': ('crypto', 'Ethereum'),
}

# Panel crypto → CoinGecko id.
COINGECKO_IDS: dict[str, str] = {
    'BTC-USD': 'bitcoin',
    'ETH-USD': 'ethereum',
}

_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; happinga-meter/1.0)'}


def _day_anchor(dt: datetime) -> datetime:
    """Normalize any datetime to that day's UTC midnight (the bar's canonical key)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def fetch_daily_bars(symbol: str, years: int = 5) -> list[dict]:
    """Return a list of OHLC dicts for ``symbol`` going back ``years``. Empty on failure."""
    if symbol in COINGECKO_IDS:
        return _fetch_coingecko(symbol, years)
    return _fetch_yfinance(symbol, years)


def _fetch_yfinance(symbol: str, years: int) -> list[dict]:
    try:
        import yfinance as yf
    except ImportError:
        logger.error('[history] yfinance not installed — cannot backfill %s', symbol)
        return []

    stream_key, name = SYMBOL_META.get(symbol, ('stock', symbol))
    period = f'{max(years, 1)}y'
    try:
        df = yf.Ticker(symbol).history(period=period, interval='1d', auto_adjust=False)
    except Exception as exc:  # noqa: BLE001 — yfinance raises many shapes
        logger.warning('[history] yfinance %s: %s', symbol, exc)
        return []

    if df is None or df.empty:
        logger.warning('[history] yfinance %s: empty frame', symbol)
        return []

    bars: list[dict] = []
    for idx, row in df.iterrows():
        close = row.get('Close')
        if close is None or close != close:  # NaN guard
            continue
        ts = idx.to_pydatetime() if hasattr(idx, 'to_pydatetime') else idx
        bars.append({
            'symbol': symbol, 'stream_key': stream_key, 'name': name, 'interval': '1d',
            'open': _num(row.get('Open')), 'high': _num(row.get('High')),
            'low': _num(row.get('Low')), 'close': float(close),
            'volume': _num(row.get('Volume')), 'date': _day_anchor(ts),
        })
    return bars


def _fetch_coingecko(symbol: str, years: int) -> list[dict]:
    cg_id = COINGECKO_IDS[symbol]
    stream_key, name = SYMBOL_META.get(symbol, ('crypto', symbol))
    days = min(max(years * 365, 1), 3650)
    url = f'https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart'
    params = {'vs_currency': 'usd', 'days': str(days), 'interval': 'daily'}
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning('[history] CoinGecko %s: %s', symbol, exc)
        return []

    prices = data.get('prices') or []
    vols = {int(v[0]): v[1] for v in (data.get('total_volumes') or [])}
    bars: list[dict] = []
    for ms, price in prices:
        if price is None:
            continue
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        bars.append({
            'symbol': symbol, 'stream_key': stream_key, 'name': name, 'interval': '1d',
            'open': None, 'high': None, 'low': None, 'close': float(price),
            'volume': vols.get(int(ms)), 'date': _day_anchor(dt),
        })
    return bars


def _num(value) -> float | None:
    if value is None or value != value:  # None or NaN
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def backfill_symbol(symbol: str, years: int = 5, dry_run: bool = False) -> int:
    """Fetch + upsert daily bars for one symbol. Returns count of new bars inserted."""
    from core.models import PriceBar

    bars = fetch_daily_bars(symbol, years)
    if not bars:
        return 0
    # Idempotent: skip dates already stored for this symbol+interval.
    existing = set(
        PriceBar.objects.filter(symbol=symbol, interval='1d').values_list('date', flat=True)
    )
    new_bars = [b for b in bars if b['date'] not in existing]
    if dry_run:
        logger.info('[history] %s: %d fetched, %d new (dry-run)', symbol, len(bars), len(new_bars))
        return len(new_bars)
    if new_bars:
        PriceBar.objects.bulk_create([PriceBar(**b) for b in new_bars])
    logger.info('[history] %s: %d fetched, %d inserted', symbol, len(bars), len(new_bars))
    return len(new_bars)


def backfill_all(symbols: list[str] | None = None, years: int = 5, dry_run: bool = False) -> dict[str, int]:
    """Backfill every panel symbol (or a given subset). Returns {symbol: inserted}."""
    symbols = symbols or list(PANEL_SYMBOLS)
    return {s: backfill_symbol(s, years, dry_run) for s in symbols}
