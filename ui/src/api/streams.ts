import type {
  PricesLatestResponse,
  PriceHistoryResponse,
  PriceBarsResponse,
  NotamZonesResponse,
  EarthquakesResponse,
  StaticPointsResponse,
  SymbolsResponse,
  ForecastsResponse,
  ForecastAccuracyResponse,
  StreamKey,
  StaticPointType,
  SymbolGroup,
} from "@/types"

import constants from "@/constants"

const BASE_URL = constants.API_BASE

export async function fetchPricesLatest(
  stream_key?: StreamKey
): Promise<PricesLatestResponse> {
  const params = new URLSearchParams()
  if (stream_key) params.set("stream_key", stream_key)
  const res = await fetch(`${BASE_URL}/prices/latest/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

export async function fetchPriceHistory(
  symbol: string,
  opts: { hours?: number; limit?: number } = {}
): Promise<PriceHistoryResponse> {
  const params = new URLSearchParams()
  if (opts.hours) {
    params.set("from", new Date(Date.now() - opts.hours * 3600 * 1000).toISOString())
  }
  if (opts.limit) params.set("limit", String(opts.limit))
  const res = await fetch(`${BASE_URL}/prices/${encodeURIComponent(symbol)}/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

export async function fetchNotamZones(
  active = true
): Promise<NotamZonesResponse> {
  const params = new URLSearchParams({ active: active ? "true" : "all" })
  const res = await fetch(`${BASE_URL}/notams/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

export async function fetchEarthquakes(
  min_magnitude = 3.0,
  hours = 24
): Promise<EarthquakesResponse> {
  const params = new URLSearchParams({
    min_magnitude: String(min_magnitude),
    hours: String(hours),
  })
  const res = await fetch(`${BASE_URL}/earthquakes/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

export async function fetchStaticPoints(
  type?: StaticPointType
): Promise<StaticPointsResponse> {
  const params = new URLSearchParams()
  if (type) params.set("type", type)
  const res = await fetch(`${BASE_URL}/static-points/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

// Curated MarketSymbol panel (drives the symbol browser + Markets sections).
export async function fetchSymbols(
  opts: { group?: SymbolGroup; stream_key?: StreamKey; forecast?: boolean; popular?: boolean } = {}
): Promise<SymbolsResponse> {
  const params = new URLSearchParams()
  if (opts.group) params.set("group", opts.group)
  if (opts.stream_key) params.set("stream_key", opts.stream_key)
  if (opts.forecast !== undefined) params.set("forecast", String(opts.forecast))
  if (opts.popular !== undefined) params.set("popular", String(opts.popular))
  const res = await fetch(`${BASE_URL}/symbols/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

// Event-fused, model-backed forecasts (one per symbol + horizon).
export async function fetchForecasts(
  opts: { symbol?: string; stream_key?: string; horizon?: number } = {}
): Promise<ForecastsResponse> {
  const params = new URLSearchParams()
  if (opts.symbol) params.set("symbol", opts.symbol)
  if (opts.stream_key) params.set("stream_key", opts.stream_key)
  if (opts.horizon) params.set("horizon", String(opts.horizon))
  const res = await fetch(`${BASE_URL}/forecasts/latest/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

export async function fetchForecastAccuracy(
  symbol?: string,
  opts: { history?: boolean; recent?: number } = {},
): Promise<ForecastAccuracyResponse> {
  const params = new URLSearchParams()
  if (symbol) params.set("symbol", symbol)
  if (opts.history) params.set("history", "1")
  if (opts.recent) params.set("recent", String(opts.recent))
  const res = await fetch(`${BASE_URL}/forecasts/accuracy/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

export async function fetchPriceBars(
  symbol: string,
  opts: { interval?: string; limit?: number } = {}
): Promise<PriceBarsResponse> {
  const params = new URLSearchParams()
  if (opts.interval) params.set("interval", opts.interval)
  if (opts.limit) params.set("limit", String(opts.limit))
  const res = await fetch(`${BASE_URL}/prices/${encodeURIComponent(symbol)}/bars/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}
