# Symbols — the `MarketSymbol` config model

`MarketSymbol` (in `api/core/models.py`) is the **single source of truth** for which
market symbols the platform fetches, forecasts, and shows. It replaces the hardcoded
symbol lists that previously lived in `services/streams/prices.py`,
`services/forecasting/history.py`, `services/forecasting/routing.py`, and
`ui/src/lib/symbols.ts`.

## Fields

| Field | Meaning |
|-------|---------|
| `symbol` | Unique ticker, e.g. `GC=F`, `BTC-USD`, `EURUSD=X` |
| `name` | Display name |
| `stream_key` | `stock` / `crypto` / `commodity` / `forex` / `bond` / `index` — the PriceTick stream bucket |
| `provider` | `yahoo` / `coingecko` / `ecb` — where prices come from |
| `provider_id` | CoinGecko coin id (blank for Yahoo/ECB) |
| `group` | `top_stock` / `top_crypto` / `resource` / `forex` / `bond` / `index` / `other` — drives Markets-UI sections |
| `is_active` | Fetched by the price streams + included in OHLC backfill |
| `is_forecast` | A forecasting **target** (the prediction panel). See [forecasting.md](forecasting.md) |
| `is_popular` + `rank` | Surfaced/ordered in "most popular" lists |
| `display_order` | Ordering within a group |
| `metadata` | Free-form JSON |

Seeded by migration `0006_marketsymbol` (idempotent `update_or_create` by `symbol`).
The forecast base panel seed is `CL=F, GC=F, BTC-USD, SPY, EURUSD=X`.

## Who reads it

All reads go through `services/market_symbols.py`, which degrades gracefully to the
historical hardcoded defaults if the table is empty/unreachable:

| Helper | Used by |
|--------|---------|
| `get_panel_symbols()` | `forecasting/routing.py`, `features.py`, `backtest.py` — the forecasting panel |
| `get_symbol_meta()` | `streams/prices.py`, `forecasting/history.py` — symbol → (stream_key, name) |
| `get_coingecko_ids()` | `streams/prices.py`, `forecasting/history.py` — crypto provider ids |
| `get_yahoo_symbols()` | `streams/prices.py` — Yahoo quote set |
| `get_backfill_symbols()` | `forecasting/history.py` — OHLC backfill set |

The Markets UI reads the panel via `GET /api/symbols/` (`fetchSymbols()` in
`ui/src/api/streams.ts`) — params `group`, `stream_key`, `forecast`, `popular`,
`active`.

## Curating symbols

Use the Django admin (`MarketSymbol` changelist):

- **Add a symbol**: create a row with `symbol`, `name`, `stream_key`, `provider`
  (+ `provider_id` for CoinGecko), and a `group`. Set `is_active=True` to start
  fetching it.
- **Add/remove a forecast target**: toggle `is_forecast` (bulk actions provided).
  This **requires a retrain** — picked up automatically by the next daily
  `train_forecast_model_task`.
- **Reorder Markets sections**: `group`, `is_popular`, `rank`, `display_order`.
- Import/export is available (import-id = `symbol`).

> ECB forex pairs (`USD/EUR`, …) are produced by `services/streams/forex.py` on their
> own schedule; they're seeded as `provider='ecb'` rows so they appear in the symbol
> browser, but the forex stream does not read `MarketSymbol`.
