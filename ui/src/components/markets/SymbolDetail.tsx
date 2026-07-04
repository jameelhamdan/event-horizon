'use client'

import { useEffect, useMemo, useState } from "react"
import {
  ResponsiveContainer, ComposedChart, Area, Line, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
} from "recharts"
import { fetchPriceHistory, fetchPriceBars } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import type { StreamKey, PricePoint } from "../../types"

function fmtValue(v: number, streamKey: StreamKey): string {
  if (streamKey === "forex") return v.toFixed(4)
  if (v >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 2 })
  if (v >= 1) return v.toFixed(2)
  return v.toFixed(4)
}

interface SymbolDetailProps {
  symbol: string
  streamKey: StreamKey
  name?: string
  /** Page-level range in calendar days back from today. */
  days: number
}

// Master-detail chart driven by the page range selector. Short ranges (≤7d) use the high-frequency
// intraday PriceTick history; longer ranges use the daily PriceBar substrate (panel symbols). Each
// source falls back to the other when empty, so non-panel symbols still render whatever exists.
export default function SymbolDetail({ symbol, streamKey, name, days }: SymbolDetailProps) {
  const { t, lang } = useLanguage()
  const [data, setData] = useState<PricePoint[]>([])
  const [resolvedName, setResolvedName] = useState<string | undefined>(name)
  const [loading, setLoading] = useState(true)

  useEffect(() => { setResolvedName(name) }, [name, symbol])

  useEffect(() => {
    let cancelled = false
    setLoading(true)

    const fromIntraday = () =>
      fetchPriceHistory(symbol, { hours: days * 24, limit: 5000 }).then((res) => {
        if (res.results[0]?.name) setResolvedName((n) => n ?? res.results[0].name)
        return res.results
          .map((r) => ({ t: new Date(r.occurred_at).getTime(), value: r.value, volume: r.volume }))
      })

    const fromBars = () =>
      fetchPriceBars(symbol, { limit: days + 1 }).then((res) => {
        if (res.results[0]?.name) setResolvedName((n) => n ?? res.results[0].name)
        return res.results
          .map((b) => ({ t: new Date(b.date).getTime(), value: b.close, volume: b.volume }))
      })

    // Short windows favour intraday detail; long windows favour daily bars. Fall back either way.
    const primary = days <= 7 ? fromIntraday : fromBars
    const secondary = days <= 7 ? fromBars : fromIntraday

    primary()
      .then((pts) => (pts.length > 0 ? pts : secondary().catch(() => [])))
      .catch(() => secondary().catch(() => []))
      .then((pts) => {
        if (cancelled) return
        setData([...pts].sort((a, b) => a.t - b.t))
      })
      .finally(() => { if (!cancelled) setLoading(false) })

    return () => { cancelled = true }
  }, [symbol, days])

  const locale = lang === "ar" ? "ar" : "en"
  const fmtTime = (ms: number) => {
    const d = new Date(ms)
    return days <= 7
      ? d.toLocaleString(locale, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString(locale, { year: days >= 365 ? "2-digit" : undefined, month: "numeric", day: "numeric" })
  }

  // Window stats derived from the plotted series.
  const stats = useMemo(() => {
    if (data.length === 0) return null
    const first = data[0].value
    const last = data[data.length - 1].value
    const values = data.map((p) => p.value)
    const high = Math.max(...values)
    const low = Math.min(...values)
    const changePct = first !== 0 ? ((last - first) / first) * 100 : 0
    return { last, high, low, changePct }
  }, [data])

  const up = (stats?.changePct ?? 0) >= 0
  const lineColor = up ? "#52c8a0" : "#e05252"
  const hasVolume = data.some((p) => p.volume != null && p.volume > 0)
  const gradientId = `grad-${symbol.replace(/[^a-zA-Z0-9]/g, "")}`

  return (
    <section className="flex min-w-0 flex-col overflow-hidden rounded-lg border border-app-border bg-app-surface">
      <header className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1 border-b border-app-border px-4 py-3">
        <div className="min-w-0">
          <div className="font-mono text-sm font-semibold text-app-text-heading">{symbol}</div>
          {resolvedName && (
            <div className="truncate text-[0.72rem] text-app-text-muted">{resolvedName}</div>
          )}
        </div>
        {stats && (
          <div className="flex items-end gap-4 text-right">
            <div>
              <div className="font-mono text-lg font-semibold tabular-nums text-app-text-primary">
                {fmtValue(stats.last, streamKey)}
              </div>
              <div
                className="font-mono text-[0.78rem] font-medium tabular-nums"
                style={{ color: lineColor }}
              >
                {up ? "+" : ""}{stats.changePct.toFixed(2)}%
              </div>
            </div>
            <dl className="hidden gap-3 text-[0.66rem] text-app-text-muted sm:flex">
              <div>
                <dt className="uppercase tracking-wide">{t.symbolRangeHigh}</dt>
                <dd className="font-mono tabular-nums text-app-text-primary">{fmtValue(stats.high, streamKey)}</dd>
              </div>
              <div>
                <dt className="uppercase tracking-wide">{t.symbolRangeLow}</dt>
                <dd className="font-mono tabular-nums text-app-text-primary">{fmtValue(stats.low, streamKey)}</dd>
              </div>
            </dl>
          </div>
        )}
      </header>

      <div className="p-3">
        {loading ? (
          <div className="flex h-[340px] items-center justify-center text-xs text-app-text-muted">{t.loading}</div>
        ) : data.length === 0 ? (
          <div className="flex h-[340px] items-center justify-center text-xs text-app-text-muted">{t.priceNoHistory}</div>
        ) : (
          <ResponsiveContainer width="100%" height={340}>
            <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
              <defs>
                <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={lineColor} stopOpacity={0.25} />
                  <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#20202a" vertical={false} />
              <XAxis
                dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]}
                tickFormatter={fmtTime} tick={{ fontSize: 10, fill: "#666677" }} stroke="#2a2a35" minTickGap={48}
              />
              <YAxis
                yAxisId="price" orientation="right" domain={["auto", "auto"]} width={56}
                tick={{ fontSize: 10, fill: "#666677" }} stroke="#2a2a35"
                tickFormatter={(v: number) => fmtValue(v, streamKey)}
              />
              {hasVolume && <YAxis yAxisId="vol" hide domain={[0, (dataMax: number) => dataMax * 4]} />}
              {hasVolume && <Bar yAxisId="vol" dataKey="volume" fill="#23232f" isAnimationActive={false} />}
              <Area
                yAxisId="price" type="monotone" dataKey="value" stroke="none"
                fill={`url(#${gradientId})`} isAnimationActive={false}
              />
              <Line
                yAxisId="price" type="monotone" dataKey="value" stroke={lineColor}
                strokeWidth={1.75} dot={false} isAnimationActive={false}
              />
              <Tooltip
                contentStyle={{ background: "#0f0f13", border: "1px solid #2a2a35", borderRadius: 6, fontSize: "0.72rem" }}
                labelStyle={{ color: "#888899" }}
                itemStyle={{ color: "#e8e8f0" }}
                labelFormatter={(ms) => fmtTime(Number(ms))}
                formatter={(val, n) =>
                  n === "value"
                    ? [fmtValue(Number(val), streamKey), symbol]
                    : [String(val), t.priceVolume]
                }
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
    </section>
  )
}
