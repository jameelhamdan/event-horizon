"""Daily OHLC backfill for the indicator panel → ``PriceBar``.

Non-crypto symbols via **yfinance** (already a project dependency); crypto (BTC/ETH) via
**CoinGecko** (free, no key). Idempotent: only inserts dates not already stored for a symbol.

This is the training + charting substrate for the forecasting layer. Distinct from the
high-frequency ``PriceTick`` live stream.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

from services.market_symbols import (
    get_backfill_symbols,
    get_coingecko_ids,
    get_symbol_meta,
)

logger = logging.getLogger(__name__)

_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; happinga-meter/1.0)'}


def _day_anchor(dt: datetime) -> datetime:
    """Normalize any datetime to that day's UTC midnight (the bar's canonical key)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def fetch_daily_bars(symbol: str, years: int = 10, start: datetime | None = None) -> list[dict]:
    """Return a list of OHLC dicts for ``symbol``. Empty on failure.

    Crypto is fetched via **yfinance** too (BTC-USD/ETH-USD resolve natively),
    because the CoinGecko free tier caps history at ~365 days — useless for a
    10-year backfill. CoinGecko stays the live-tick source; here it's only a
    fallback if yfinance returns nothing.

    ``start`` (optional) fetches only from that date forward (incremental top-up);
    otherwise ``years`` of history is pulled.
    """
    bars = _fetch_yfinance(symbol, years, start=start)
    if not bars and start is None and symbol in get_coingecko_ids():
        # CoinGecko is a full-history fallback only: skip in incremental mode
        # (start is set) to avoid requesting 3,650 days just for a 3-day top-up.
        bars = _fetch_coingecko(symbol, years)
    return bars


def _fetch_yfinance(symbol: str, years: int, start: datetime | None = None) -> list[dict]:
    try:
        import yfinance as yf
    except ImportError:
        logger.error('[history] yfinance not installed — cannot backfill %s', symbol)
        return []

    stream_key, name = get_symbol_meta().get(symbol, ('stock', symbol))
    try:
        ticker = yf.Ticker(symbol)
        if start is not None:
            df = ticker.history(start=start.date().isoformat(), interval='1d', auto_adjust=False)
        else:
            df = ticker.history(period=f'{max(years, 1)}y', interval='1d', auto_adjust=False)
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
    cg_id = get_coingecko_ids()[symbol]
    stream_key, name = get_symbol_meta().get(symbol, ('crypto', symbol))
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


def backfill_symbol(symbol: str, years: int = 10, dry_run: bool = False, full: bool = False) -> int:
    """Fetch + upsert daily bars for one symbol. Returns count of new bars inserted.

    Incremental by default: if bars already exist, only the tail since the last
    stored date is fetched (cheap weekly top-up). ``full=True`` re-pulls the whole
    ``years`` window — use for the one-time deep seed or to repair gaps.
    """
    from core.models import PriceBar
    from django.db.models import Max

    # Incremental top-up: only fetch the tail since the last stored bar.
    # Use Max() instead of loading all dates — avoids pulling ~2500+ datetimes
    # per symbol just to find the latest one.
    start = None
    if not full:
        result = PriceBar.objects.filter(symbol=symbol, interval='1d').aggregate(Max('date'))
        latest = result['date__max']
        if latest is not None:
            start = latest - timedelta(days=3)

    bars = fetch_daily_bars(symbol, years, start=start)
    if not bars:
        return 0

    # Scope the existing-dates query to the fetched window so we never load all bars.
    bar_dates = [b['date'] for b in bars]
    existing = set(
        PriceBar.objects.filter(symbol=symbol, interval='1d', date__in=bar_dates)
        .values_list('date', flat=True)
    )
    new_bars = [b for b in bars if b['date'] not in existing]
    if dry_run:
        logger.info('[history] %s: %d fetched, %d new (dry-run)', symbol, len(bars), len(new_bars))
        return len(new_bars)
    if new_bars:
        PriceBar.objects.bulk_create([PriceBar(**b) for b in new_bars], ignore_conflicts=True)
    logger.info('[history] %s: %d fetched, %d inserted', symbol, len(bars), len(new_bars))
    return len(new_bars)


def backfill_all(
    symbols: list[str] | None = None, years: int = 10, dry_run: bool = False, full: bool = False,
) -> dict[str, int]:
    """Backfill every active symbol (or a given subset). Returns {symbol: inserted}."""
    symbols = symbols or get_backfill_symbols()
    return {s: backfill_symbol(s, years, dry_run, full) for s in symbols}
