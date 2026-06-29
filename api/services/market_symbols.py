"""Central accessors for the configurable market-symbol panel (``MarketSymbol``).

This is the single read path for "what symbols exist / are fetched / are forecast
targets". Every consumer (price stream, OHLC backfill, event router, Markets API)
reads here so symbols can be curated in the admin without code changes.

All accessors degrade gracefully: if the ``MarketSymbol`` table is empty or the DB
is unavailable (e.g. during a migration or in a unit test with no Mongo), they fall
back to the historical hardcoded defaults so nothing breaks.
"""

import logging

logger = logging.getLogger(__name__)

# ── Hardcoded fallbacks (mirror the pre-MarketSymbol defaults) ────────────────
# Used only when the MarketSymbol table is empty / unreachable.

_FALLBACK_PANEL: list[str] = ['CL=F', 'GC=F', 'BTC-USD', 'SPY', 'EURUSD=X']

# symbol → (stream_key, name) for all historically-known symbols
_FALLBACK_META: dict[str, tuple[str, str]] = {
    'GC=F': ('commodity', 'Gold'), 'CL=F': ('commodity', 'Crude Oil'),
    'NG=F': ('commodity', 'Natural Gas'), 'ZW=F': ('commodity', 'Wheat'),
    'ZC=F': ('commodity', 'Corn'), 'SI=F': ('commodity', 'Silver'),
    'SPY': ('stock', 'S&P 500 ETF'), 'QQQ': ('stock', 'Nasdaq ETF'),
    '^GSPC': ('stock', 'S&P 500'), '^FTSE': ('stock', 'FTSE 100'),
    '^GDAXI': ('stock', 'DAX'), '^N225': ('stock', 'Nikkei 225'),
    '000001.SS': ('stock', 'Shanghai Composite'),
    '^TNX': ('bond', 'US 10Y Treasury'), '^TYX': ('bond', 'US 30Y Treasury'),
    '^VIX': ('index', 'Volatility Index'), 'DX-Y.NYB': ('index', 'US Dollar Index'),
    'EURUSD=X': ('forex', 'EUR/USD'),
    'BTC-USD': ('crypto', 'Bitcoin'), 'ETH-USD': ('crypto', 'Ethereum'),
    'XRP-USD': ('crypto', 'XRP'), 'SOL-USD': ('crypto', 'Solana'),
    'BNB-USD': ('crypto', 'BNB'),
}

_FALLBACK_COINGECKO: dict[str, str] = {
    'BTC-USD': 'bitcoin', 'ETH-USD': 'ethereum', 'XRP-USD': 'ripple',
    'SOL-USD': 'solana', 'BNB-USD': 'binancecoin',
}


def _active_rows() -> list:
    """Return active MarketSymbol rows, or [] if the table is empty/unreachable."""
    try:
        from core.models import MarketSymbol
        return list(MarketSymbol.objects.filter(is_active=True))
    except Exception as exc:  # noqa: BLE001 — DB may be unavailable (migration/test)
        logger.debug('[market_symbols] active rows unavailable, using fallback: %s', exc)
        return []


def get_panel_symbols() -> list[str]:
    """Forecasting target symbols (``is_forecast=True``). Falls back to the 5 base symbols."""
    try:
        from core.models import MarketSymbol
        syms = list(
            MarketSymbol.objects.filter(is_forecast=True, is_active=True)
            .values_list('symbol', flat=True)
        )
        if syms:
            return syms
    except Exception as exc:  # noqa: BLE001
        logger.debug('[market_symbols] panel unavailable, using fallback: %s', exc)
    return list(_FALLBACK_PANEL)


def get_symbol_meta() -> dict[str, tuple[str, str]]:
    """symbol → (stream_key, name) for all active symbols. Falls back to hardcoded meta."""
    rows = _active_rows()
    if not rows:
        return dict(_FALLBACK_META)
    return {r.symbol: (r.stream_key, r.name) for r in rows}


def get_coingecko_ids() -> dict[str, str]:
    """symbol → CoinGecko id for active crypto symbols. Falls back to hardcoded ids."""
    rows = _active_rows()
    if not rows:
        return dict(_FALLBACK_COINGECKO)
    out = {r.symbol: r.provider_id for r in rows if r.provider == 'coingecko' and r.provider_id}
    return out or dict(_FALLBACK_COINGECKO)


def get_yahoo_symbols() -> list[str]:
    """Active Yahoo-provider symbols (for the price stream). Falls back to hardcoded meta keys."""
    rows = _active_rows()
    if not rows:
        return [s for s, (sk, _n) in _FALLBACK_META.items() if s not in _FALLBACK_COINGECKO]
    return [r.symbol for r in rows if r.provider == 'yahoo']


def get_backfill_symbols() -> list[str]:
    """All active symbols that have OHLC history (yahoo + coingecko, not ECB forex)."""
    rows = _active_rows()
    if not rows:
        return list(_FALLBACK_META)
    return [r.symbol for r in rows if r.provider in ('yahoo', 'coingecko')]
