import type { StreamKey } from "../types"

// Maps a market symbol (as emitted by the backend router's PANEL_SYMBOLS) to the
// stream_key tab it lives under in the PriceTicker. Used for Event → Market
// cross-linking (F5): clicking an affected-indicator chip opens that symbol's chart.
const SYMBOL_STREAM_KEY: Record<string, StreamKey> = {
  "GC=F": "commodity",
  "CL=F": "commodity",
  "NG=F": "commodity",
  "ZW=F": "commodity",
  "^VIX": "index",
  "DX-Y.NYB": "index",
  "^TNX": "bond",
  "SPY": "stock",
  "BTC-USD": "crypto",
  "ETH-USD": "crypto",
}

export function symbolStreamKey(symbol: string): StreamKey | null {
  return SYMBOL_STREAM_KEY[symbol] ?? null
}
