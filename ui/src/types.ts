export type Category =
  | "conflict"
  | "disaster"
  | "economic"
  | "political"
  | "health"
  | "general"
  // legacy flat categories — still present in pre-redesign data
  | "protest"
  | "crime"

export interface Topic {
  id: string
  slug: string
  name: string
  keywords: string[]
  description?: string
  category?: string
  source_url?: string
  is_current: boolean
  is_active: boolean
  is_pinned?: boolean
  is_top_level?: boolean
  started_at?: string
  ended_at?: string
  event_count: number
  topic_score?: number
}

export interface TopicsResponse {
  results: Topic[]
  count: number
}

export interface EventSummary {
  id: string
  latitude: number | null
  longitude: number | null
  category: Category
  sub_categories: string[]
  avg_intensity: number | null
  title: string
  title_ar: string
  location_name: string
  location_name_ar: string
  article_count: number
  source_codes: string[]
  source_names: string[]
  started_at: string
  topics?: Record<string, number>
  topic_slugs?: string[]
  avg_finbert_sentiment?: number | null
  affected_indicators?: AffectedIndicator[]
}

export interface Article {
  id: string
  title: string
  title_ar: string
  source_url: string
  source_code: string
  published_on: string
}

export interface EventDetail extends EventSummary {
  articles: Article[]
}

export interface EventFilters {
  category?: string
  start?: string
  end?: string
  limit?: number
  bbox?: string
  topic?: string
}

export interface EventsResponse {
  results: EventSummary[]
  count: number
}

export type StreamKey = "stock" | "crypto" | "commodity" | "forex" | "bond" | "index"

export interface AffectedIndicator {
  symbol: string
  weight: number
}

export interface PriceTick {
  id: string
  symbol: string
  stream_key: StreamKey
  name: string
  value: number
  change_pct: number | null
  volume: number | null
  occurred_at: string
}

export interface PricesLatestResponse {
  results: PriceTick[]
}

export interface PriceHistoryResponse {
  symbol: string
  results: PriceTick[]
  count: number
}

/** A single chart-ready point derived from PriceTick/PriceBar history. */
export interface PricePoint {
  t: number
  value: number
  volume: number | null
}

export interface GeoJSONFeature {
  type: "Feature"
  geometry: { type: string; coordinates: unknown } | null
  properties: Record<string, unknown>
}

export interface NotamZone {
  id: string
  notam_id: string
  notam_type: string
  geometry: GeoJSONFeature
  is_active: boolean
  effective_from: string
  effective_to: string | null
  altitude_min_ft: number | null
  altitude_max_ft: number | null
  location_name: string
  country_code: string
  updated_at: string
}

export interface NotamZonesResponse {
  results: NotamZone[]
  count: number
}

export interface EarthquakeRecord {
  id: string
  usgs_id: string
  magnitude: number
  magnitude_type: string
  depth_km: number | null
  location_name: string
  latitude: number
  longitude: number
  occurred_at: string
  tsunami_alert: boolean
  alert_level: string
}

export interface EarthquakesResponse {
  results: EarthquakeRecord[]
  count: number
}

export type StaticPointType =
  | "exchange"
  | "commodity_exchange"
  | "port"
  | "central_bank"

export interface StaticPoint {
  id: string
  code: string
  point_type: StaticPointType
  name: string
  country: string
  country_code: string
  latitude: number
  longitude: number
  metadata: Record<string, unknown>
  is_active: boolean
}

export interface StaticPointsResponse {
  results: StaticPoint[]
  count: number
}

export type SymbolGroup =
  | "top_stock" | "top_crypto" | "resource" | "forex" | "bond" | "index" | "other"

export interface MarketSymbol {
  id: string
  symbol: string
  name: string
  stream_key: StreamKey
  provider: string
  group: SymbolGroup
  is_active: boolean
  is_forecast: boolean
  is_popular: boolean
  rank: number
  display_order: number
}

export interface SymbolsResponse {
  results: MarketSymbol[]
  count: number
}

export interface NewsletterSummary {
  id: string
  date: string
  subject: string
  sent_at: string | null
  event_count: number
  status: string
}

export interface NewsletterDetail extends NewsletterSummary {
  html_body: string
  text_body: string
  generated_at: string
  sent_count: number
}

export type ForecastDirection = "up" | "down" | "neutral"

// Model-backed, event-fused forecast (one per symbol + horizon).
export interface Forecast {
  symbol: string
  stream_key: string
  generated_at: string
  as_of_date: string
  horizon_days: number
  direction: ForecastDirection
  proba_up: number
  predicted_change_pct: number
  predicted_price: number | null
  band_low: number | null
  band_high: number | null
  confidence: number
  current_value: number | null
  router_source: string
  model_version: string
  realized_direction: ForecastDirection | null
  realized_change_pct: number | null
  is_correct: boolean | null
  scored_at: string | null
}

export interface ForecastsResponse {
  results: Forecast[]
  count: number
}

export interface ForecastAccuracy {
  horizon_days: number
  scored: number
  correct: number
  accuracy: number | null
  brier: number | null
}

export interface ForecastAccuracyResponse {
  results: ForecastAccuracy[]
  count: number
}

export interface PriceBar {
  id: string
  symbol: string
  stream_key: StreamKey
  name: string
  interval: string
  open: number | null
  high: number | null
  low: number | null
  close: number
  volume: number | null
  date: string
}

export interface PriceBarsResponse {
  symbol: string
  interval: string
  results: PriceBar[]
  count: number
}


export type SSEEvent =
  | { type: "connected" }
  | {
      type: "price_tick"
      symbol: string
      stream_key: StreamKey
      name: string
      value: number
      change_pct: number | null
      occurred_at: string
    }
  | { type: "notam_update"; active_count: number; new_count: number }
  | { type: "earthquake_update"; new_count: number; max_magnitude: number }
  | { type: string; [key: string]: unknown }

