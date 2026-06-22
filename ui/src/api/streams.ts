import type {
  PricesLatestResponse,
  PriceHistoryResponse,
  NotamZonesResponse,
  EarthquakesResponse,
  StaticPointsResponse,
  ForecastsResponse,
  StreamKey,
  StaticPointType,
} from "@/types"

import constants from "@/constants"

const BASE_URL = `${constants.BASE_URL}/api`

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

// Placeholder: the backend returns a neutral / 0% forecast per symbol until the
// prediction layer is reworked.
export async function fetchForecasts(
  symbol?: string,
  stream_key?: string
): Promise<ForecastsResponse> {
  const params = new URLSearchParams()
  if (symbol) params.set("symbol", symbol)
  if (stream_key) params.set("stream_key", stream_key)
  const res = await fetch(`${BASE_URL}/forecasts/latest/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}
