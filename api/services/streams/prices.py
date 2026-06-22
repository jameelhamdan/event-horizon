"""
Price stream — Yahoo Finance (stocks, commodities, bonds) + CoinGecko (crypto).

All public APIs, no keys required.
"""
import logging
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.utils import timezone as dj_timezone

from .base import BaseStream, redis_publish

logger = logging.getLogger(__name__)

# Default symbols per category (overridable via settings/env)
DEFAULT_STOCKS = 'SPY,QQQ,^GSPC,^FTSE,^GDAXI,^N225,000001.SS'
DEFAULT_COMMODITIES = 'GC=F,CL=F,NG=F,ZW=F,ZC=F,SI=F'
DEFAULT_BONDS = '^TNX,^TYX'
# Volatility / risk-sentiment gauge — part of the indicator panel (plan §"Indicator panel").
DEFAULT_INDICES = '^VIX,DX-Y.NYB'

COINGECKO_IDS = {
    'bitcoin':  ('BTC-USD', 'Bitcoin'),
    'ethereum': ('ETH-USD', 'Ethereum'),
    'ripple':   ('XRP-USD', 'XRP'),
    'solana':   ('SOL-USD', 'Solana'),
    'binancecoin': ('BNB-USD', 'BNB'),
}

YAHOO_NAMES = {
    'SPY': 'S&P 500 ETF', 'QQQ': 'Nasdaq ETF', '^GSPC': 'S&P 500',
    '^FTSE': 'FTSE 100', '^GDAXI': 'DAX', '^N225': 'Nikkei 225',
    '000001.SS': 'Shanghai Composite',
    'GC=F': 'Gold', 'CL=F': 'Crude Oil', 'NG=F': 'Natural Gas',
    'ZW=F': 'Wheat', 'ZC=F': 'Corn', 'SI=F': 'Silver',
    '^TNX': 'US 10Y Treasury', '^TYX': 'US 30Y Treasury',
    '^VIX': 'Volatility Index', 'DX-Y.NYB': 'US Dollar Index',
}

YAHOO_STREAM_KEY = {
    'SPY': 'stock', 'QQQ': 'stock', '^GSPC': 'stock', '^FTSE': 'stock',
    '^GDAXI': 'stock', '^N225': 'stock', '000001.SS': 'stock',
    'GC=F': 'commodity', 'CL=F': 'commodity', 'NG=F': 'commodity',
    'ZW=F': 'commodity', 'ZC=F': 'commodity', 'SI=F': 'commodity',
    '^TNX': 'bond', '^TYX': 'bond',
    '^VIX': 'index', 'DX-Y.NYB': 'index',
}

HEADERS = {
    'User-Agent': f'Mozilla/5.0 (compatible; {settings.APP_NAME}/1.0)',
    'Accept': 'application/json',
}


def _yahoo_quote(symbol: str) -> dict | None:
    """Fetch a single Yahoo Finance quote. Returns None on failure."""
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
    params = {'interval': '1m', 'range': '1d'}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # A2 schema validation: log drift loudly rather than silently skipping, so a
        # format change on this undocumented API is noticed instead of going dark.
        result = data.get('chart', {}).get('result', [])
        if not result:
            err = (data.get('chart') or {}).get('error')
            logger.warning('[prices] Yahoo %s: no result (drift or error: %s)', symbol, err)
            return None
        meta = result[0].get('meta', {})
        price = meta.get('regularMarketPrice')
        prev = meta.get('previousClose')
        if price is None and prev is None:
            logger.warning('[prices] Yahoo %s: meta missing price fields — possible schema '
                           'drift (keys=%s)', symbol, sorted(meta)[:12])
            return None
        return {
            'symbol': symbol,
            'stream_key': YAHOO_STREAM_KEY.get(symbol, 'stock'),
            'name': YAHOO_NAMES.get(symbol, symbol),
            'value': price or prev,
            'change_pct': _safe_change_pct(price, prev),
            'volume': meta.get('regularMarketVolume'),
            'occurred_at': dj_timezone.now(),
        }
    except Exception as exc:
        logger.warning(f'[prices] Yahoo Finance {symbol}: {exc}')
        return None


def _safe_change_pct(current, previous) -> float | None:
    if current and previous and previous != 0:
        return round((current - previous) / previous * 100, 4)
    return None


def _coingecko_quotes() -> list[dict]:
    """Fetch multiple crypto prices from CoinGecko (free, no key)."""
    ids = ','.join(COINGECKO_IDS.keys())
    url = 'https://api.coingecko.com/api/v3/simple/price'
    params = {
        'ids': ids,
        'vs_currencies': 'usd',
        'include_24hr_change': 'true',
        'include_24hr_vol': 'true',
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f'[prices] CoinGecko: {exc}')
        return []

    records = []
    now = dj_timezone.now()
    for cg_id, (symbol, name) in COINGECKO_IDS.items():
        coin = data.get(cg_id, {})
        if not coin:
            continue
        records.append({
            'symbol': symbol,
            'stream_key': 'crypto',
            'name': name,
            'value': coin.get('usd'),
            'change_pct': coin.get('usd_24h_change'),
            'volume': coin.get('usd_24h_vol'),
            'occurred_at': now,
        })
    # A2: a non-empty response that yields no known coins signals schema drift.
    if data and not records:
        logger.warning('[prices] CoinGecko returned data but no recognized coins — '
                       'possible schema drift (keys=%s)', sorted(data)[:8])
    return records


class PriceStream(BaseStream):
    stream_type = 'price'

    def fetch(self) -> list[dict]:
        stocks_raw = getattr(settings, 'PRICE_SYMBOLS_STOCKS', DEFAULT_STOCKS)
        commodities_raw = getattr(settings, 'PRICE_SYMBOLS_COMMODITIES', DEFAULT_COMMODITIES)
        bonds_raw = getattr(settings, 'PRICE_SYMBOLS_BONDS', DEFAULT_BONDS)
        indices_raw = getattr(settings, 'PRICE_SYMBOLS_INDICES', DEFAULT_INDICES)

        yahoo_symbols = [
            s.strip() for s in (
                stocks_raw + ',' + commodities_raw + ',' + bonds_raw + ',' + indices_raw
            ).split(',') if s.strip()
        ]
        # Dedupe while preserving order (DX-Y.NYB may appear via env in multiple lists)
        yahoo_symbols = list(dict.fromkeys(yahoo_symbols))

        records = []
        for symbol in yahoo_symbols:
            quote = _yahoo_quote(symbol)
            if quote and quote.get('value') is not None:
                records.append(quote)

        records.extend(r for r in _coingecko_quotes() if r.get('value') is not None)
        return records

    def save(self, records: list[dict]) -> int:
        from core.models import PriceTick

        ticks = [
            PriceTick(
                symbol=r['symbol'],
                stream_key=r['stream_key'],
                name=r['name'],
                value=r['value'],
                change_pct=r.get('change_pct'),
                volume=r.get('volume'),
                occurred_at=r['occurred_at'],
            )
            for r in records
        ]
        PriceTick.objects.bulk_create(ticks)

        # Publish each tick individually for SSE consumers
        for r in records:
            redis_publish('sse:prices', {
                'type': 'price_tick',
                'symbol': r['symbol'],
                'stream_key': r['stream_key'],
                'name': r['name'],
                'value': r['value'],
                'change_pct': r.get('change_pct'),
                'occurred_at': r['occurred_at'].isoformat(),
            })

        return len(ticks)
