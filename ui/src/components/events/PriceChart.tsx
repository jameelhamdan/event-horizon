'use client'

import { useEffect, useState } from "react"
import {
  ResponsiveContainer, ComposedChart, Line, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
} from "recharts"
import { fetchPriceHistory } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import type { StreamKey } from "../../types"

const RANGES = [
  { hours: 24, key: "filter24h" },
  { hours: 168, key: "filter7d" },
  { hours: 720, key: "filter30d" },
] as const

function fmtValue(v: number, streamKey: StreamKey): string {
  if (streamKey === "forex") return v.toFixed(4)
  if (v >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 2 })
  if (v >= 1) return v.toFixed(2)
  return v.toFixed(4)
}

interface Point { t: number; value: number; volume: number | null }

export default function PriceChart({ symbol, streamKey }: { symbol: string; streamKey: StreamKey }) {
  const { t, lang } = useLanguage()
  const [hours, setHours] = useState(24)
  const [data, setData] = useState<Point[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchPriceHistory(symbol, { hours, limit: 1000 })
      .then((res) => {
        if (cancelled) return
        const pts = res.results
          .map((r) => ({ t: new Date(r.occurred_at).getTime(), value: r.value, volume: r.volume }))
          .sort((a, b) => a.t - b.t)
        setData(pts)
      })
      .catch(() => { if (!cancelled) setData([]) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [symbol, hours])

  const locale = lang === "ar" ? "ar" : "en"
  const fmtTime = (ms: number) => {
    const d = new Date(ms)
    return hours <= 24
      ? d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString(locale, { month: "numeric", day: "numeric" })
  }

  const hasVolume = data.some((p) => p.volume != null && p.volume > 0)

  return (
    <div style={{ padding: "8px 12px 10px", background: "#14141a", borderBottom: "1px solid #2a2a35" }}>
      <div style={{ display: "flex", gap: 4, marginBottom: 6 }}>
        {RANGES.map((r) => {
          const active = hours === r.hours
          return (
            <button
              key={r.hours}
              onClick={() => setHours(r.hours)}
              style={{
                fontSize: "0.6rem", padding: "1px 7px", borderRadius: 4, cursor: "pointer",
                border: "1px solid " + (active ? "#7c9ef8" : "#2a2a35"),
                background: active ? "rgba(124,158,248,0.18)" : "transparent",
                color: active ? "#7c9ef8" : "#888899",
              }}
            >
              {t[r.key]}
            </button>
          )
        })}
      </div>

      {loading ? (
        <div style={{ height: 130, display: "flex", alignItems: "center", justifyContent: "center", color: "#888899", fontSize: "0.7rem" }}>
          {t.loading}
        </div>
      ) : data.length === 0 ? (
        <div style={{ height: 130, display: "flex", alignItems: "center", justifyContent: "center", color: "#888899", fontSize: "0.7rem" }}>
          {t.priceNoHistory}
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={130}>
          <ComposedChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
            <CartesianGrid stroke="#20202a" vertical={false} />
            <XAxis
              dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]}
              tickFormatter={fmtTime} tick={{ fontSize: 9, fill: "#666677" }} stroke="#2a2a35" minTickGap={40}
            />
            <YAxis
              yAxisId="price" orientation="right" domain={["auto", "auto"]} width={46}
              tick={{ fontSize: 9, fill: "#666677" }} stroke="#2a2a35"
              tickFormatter={(v: number) => fmtValue(v, streamKey)}
            />
            {hasVolume && <YAxis yAxisId="vol" hide domain={[0, (dataMax: number) => dataMax * 4]} />}
            {hasVolume && <Bar yAxisId="vol" dataKey="volume" fill="#2e2e3c" isAnimationActive={false} />}
            <Line yAxisId="price" type="monotone" dataKey="value" stroke="#7c9ef8" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            <Tooltip
              contentStyle={{ background: "#0f0f13", border: "1px solid #2a2a35", borderRadius: 6, fontSize: "0.7rem" }}
              labelStyle={{ color: "#888899" }}
              itemStyle={{ color: "#e8e8f0" }}
              labelFormatter={(ms) => fmtTime(Number(ms))}
              formatter={(val, name) =>
                name === "value"
                  ? [fmtValue(Number(val), streamKey), symbol]
                  : [String(val), t.priceVolume]
              }
            />
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
