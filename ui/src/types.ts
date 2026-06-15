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

export type MagnitudeBucket =
  | "strong_down" | "down" | "flat" | "up" | "strong_up" | ""

export type VolatilityBucket = "calm" | "normal" | "elevated" | ""

export type Reliability = "high" | "med" | "low" | ""

export interface Forecast {
  id: string
  symbol: string
  stream_key: string
  generated_at: string
  horizon_hours: number
  direction: ForecastDirection
  confidence: number
  // Two-head bucketed prediction + scored actuals
  magnitude_bucket: MagnitudeBucket
  actual_bucket: MagnitudeBucket
  volatility_bucket: VolatilityBucket
  actual_volatility_bucket: VolatilityBucket
  reliability: Reliability
  abstained: boolean
  predicted_value: number | null
  actual_value: number | null
  model_name: string
  reasoning: string
  event_ids: string[]
  feature_vector: Record<string, unknown>
}

export interface ForecastsResponse {
  results: Forecast[]
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

