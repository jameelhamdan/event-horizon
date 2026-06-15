"use client"

import { useEffect, useState } from "react"
import { fetchForecasts } from "@/api/streams"
import type { Forecast, MagnitudeBucket, VolatilityBucket, Reliability } from "@/types"
import { useLanguage } from "@/contexts/LanguageContext"

const MAGNITUDE_COLOR: Record<string, string> = {
  strong_down: "#e05252",
  down:        "#e08552",
  flat:        "#888899",
  up:          "#52c8a0",
  strong_up:   "#2ea884",
}

const VOLATILITY_COLOR: Record<string, string> = {
  calm:     "#52c8a0",
  normal:   "#e0c852",
  elevated: "#e05252",
}

const RELIABILITY_COLOR: Record<string, string> = {
  high: "#52c8a0",
  med:  "#e0c852",
  low:  "#888899",
}

function Chip({ label, color }: { label: string; color: string }) {
  return (
    <span
      style={{
        fontSize:     "0.62rem",
        fontWeight:   600,
        padding:      "1px 6px",
        borderRadius: 4,
        color,
        background:   `${color}1f`,
        border:       `1px solid ${color}55`,
        whiteSpace:   "nowrap",
      }}
    >
      {label}
    </span>
  )
}

// Confidence bar — simple SVG strip
function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <svg width={32} height={6} style={{ borderRadius: 3, overflow: "hidden" }}>
        <rect x={0} y={0} width={32} height={6} fill="#2a2a35" />
        <rect x={0} y={0} width={32 * value} height={6} fill="#7c9ef8" rx={3} />
      </svg>
      <span style={{ fontSize: "0.6rem", color: "#888899" }}>{pct}%</span>
    </span>
  )
}

interface HorizonRowProps {
  fc: Forecast
  selected: boolean
  onClick: () => void
  t: ReturnType<typeof useLanguage>["t"]
}

function HorizonRow({ fc, selected, onClick, t }: HorizonRowProps) {
  const volColor = VOLATILITY_COLOR[fc.volatility_bucket] ?? "#888899"
  const magColor = MAGNITUDE_COLOR[fc.magnitude_bucket] ?? "#888899"
  const relColor = RELIABILITY_COLOR[fc.reliability] ?? "#888899"
  const volLabel = t.volatilityBuckets[fc.volatility_bucket as VolatilityBucket]
  const magLabel = t.magnitudeBuckets[fc.magnitude_bucket as MagnitudeBucket]

  return (
    <button
      onClick={onClick}
      style={{
        display:      "flex",
        alignItems:   "center",
        gap:          6,
        padding:      "5px 12px 5px 22px",
        background:   selected ? "#1e2030" : "transparent",
        border:       "none",
        borderBottom: "1px solid #20202a",
        cursor:       "pointer",
        width:        "100%",
        textAlign:    "left",
      }}
    >
      <span style={{ fontSize: "0.62rem", color: "#888899", minWidth: 18, fontFamily: "monospace" }}>
        {t.forecastHorizonShort(fc.horizon_hours)}
      </span>
      {/* Volatility head first — the headline, more-learnable target */}
      {volLabel && <Chip label={volLabel} color={volColor} />}
      {/* Magnitude head, or an abstained marker */}
      {fc.abstained
        ? <Chip label={t.forecastAbstained} color="#666677" />
        : magLabel && <Chip label={magLabel} color={magColor} />}
      <span style={{ flex: 1 }} />
      {fc.reliability && (
        <span style={{ fontSize: "0.58rem", color: relColor, textTransform: "uppercase" }}>
          {t.reliabilityLabels[fc.reliability as Reliability]}
        </span>
      )}
      <ConfidenceBar value={fc.confidence} />
    </button>
  )
}

function DetailPanel({ fc, t }: { fc: Forecast; t: ReturnType<typeof useLanguage>["t"] }) {
  const fv = fc.feature_vector as Record<string, unknown>
  const num = (k: string, mult = 1, digits = 3) =>
    typeof fv[k] === "number" ? ((fv[k] as number) * mult).toFixed(digits) : "N/A"

  const finbert = num("news_finbert_mean")
  const vader = num("news_vader_mean")
  const momentum1h = typeof fv.price_momentum_1h === "number"
    ? (fv.price_momentum_1h * 100).toFixed(2) + "%"
    : "N/A"
  const routedCount = typeof fv.routed_event_count === "number" ? fv.routed_event_count : "N/A"
  const generated = new Date(fc.generated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })

  return (
    <div style={{ padding: "10px 12px 10px 22px", background: "#14141a", borderBottom: "1px solid #2a2a35", fontSize: "0.72rem", color: "#888899" }}>
      <div style={{ color: "#e8e8f0", marginBottom: 4, fontSize: "0.75rem" }}>
        {fc.symbol} · {t.forecastHorizon(fc.horizon_hours)}
        {" · "}<span style={{ color: "#444458" }}>{generated}</span>
        {fc.model_name && <span style={{ color: "#444458" }}> · {fc.model_name}</span>}
      </div>
      {fc.reasoning && (
        <div style={{ marginBottom: 6, color: "#aaaacc", fontStyle: "italic" }}>"{fc.reasoning}"</div>
      )}
      {/* Predicted vs actual buckets, once scored */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 6 }}>
        <span>{t.forecastVolatility}: <span style={{ color: VOLATILITY_COLOR[fc.volatility_bucket] ?? "#e8e8f0" }}>
          {t.volatilityBuckets[fc.volatility_bucket as VolatilityBucket] ?? "—"}
        </span>
        {fc.actual_volatility_bucket && (
          <span> → <span style={{ color: "#52c8a0" }}>{t.volatilityBuckets[fc.actual_volatility_bucket as VolatilityBucket]}</span></span>
        )}</span>
        <span>{t.forecastMagnitude}: <span style={{ color: MAGNITUDE_COLOR[fc.magnitude_bucket] ?? "#e8e8f0" }}>
          {fc.abstained ? t.forecastAbstained : (t.magnitudeBuckets[fc.magnitude_bucket as MagnitudeBucket] ?? "—")}
        </span>
        {fc.actual_bucket && (
          <span> → <span style={{ color: "#52c8a0" }}>{t.magnitudeBuckets[fc.actual_bucket as MagnitudeBucket]}</span></span>
        )}</span>
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <span>{t.forecastFinbert} <span style={{ color: "#e8e8f0" }}>{finbert}</span></span>
        <span>{t.forecastSentiment} <span style={{ color: "#e8e8f0" }}>{vader}</span></span>
        <span>{t.forecastMomentum1h} <span style={{ color: "#e8e8f0" }}>{momentum1h}</span></span>
        <span>{t.forecastRelatedEvents} <span style={{ color: "#e8e8f0" }}>{routedCount}</span></span>
        {fc.actual_value !== null && (
          <span>{t.forecastActual} <span style={{ color: "#52c8a0" }}>{fc.actual_value?.toFixed(2)}</span></span>
        )}
      </div>
    </div>
  )
}

export default function ForecastPanel() {
  const { t } = useLanguage()
  const [forecasts, setForecasts] = useState<Forecast[]>([])
  const [selected, setSelected]   = useState<string | null>(null)
  const [loading, setLoading]     = useState(true)
  const [expanded, setExpanded]   = useState(true)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const data = await fetchForecasts()
        if (!cancelled) {
          setForecasts(data.results)
          setLoading(false)
        }
      } catch {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    const timer = setInterval(load, 5 * 60 * 1000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [])

  if (loading) return null
  if (forecasts.length === 0) return null

  // Group by stream_key → symbol → horizon-sorted rows.
  const grouped: Record<string, Record<string, Forecast[]>> = {}
  for (const fc of forecasts) {
    ;(grouped[fc.stream_key] ??= {})[fc.symbol] ??= []
    grouped[fc.stream_key][fc.symbol].push(fc)
  }
  for (const streamGroup of Object.values(grouped)) {
    for (const rows of Object.values(streamGroup)) {
      rows.sort((a, b) => a.horizon_hours - b.horizon_hours)
    }
  }

  return (
    <div style={{ borderBottom: "1px solid #2a2a35", background: "#0f0f13" }}>
      <button
        onClick={() => setExpanded((v) => !v)}
        style={{
          display: "flex", alignItems: "center", gap: 6, width: "100%", padding: "6px 12px",
          background: "transparent", border: "none",
          borderBottom: expanded ? "1px solid #2a2a35" : "none",
          cursor: "pointer", color: "#888899", fontSize: "0.7rem",
          letterSpacing: "0.06em", textTransform: "uppercase",
        }}
      >
        <span>⬡</span>
        <span style={{ flex: 1, textAlign: "left" }}>{t.marketForecasts}</span>
        <span>{expanded ? "▲" : "▼"}</span>
      </button>

      {expanded && Object.entries(grouped).map(([key, symbolGroup]) => (
        <div key={key}>
          <div style={{ padding: "3px 12px", fontSize: "0.65rem", color: "#444458", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            {t.streamKeys[key as keyof typeof t.streamKeys] ?? key}
          </div>
          {Object.entries(symbolGroup).map(([symbol, rows]) => (
            <div key={symbol}>
              <div style={{ padding: "2px 12px", fontSize: "0.74rem", color: "#e8e8f0", fontFamily: "monospace" }}>
                {symbol}
              </div>
              {rows.map((fc) => (
                <div key={fc.id}>
                  <HorizonRow
                    fc={fc}
                    t={t}
                    selected={selected === fc.id}
                    onClick={() => setSelected((prev) => (prev === fc.id ? null : fc.id))}
                  />
                  {selected === fc.id && <DetailPanel fc={fc} t={t} />}
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
