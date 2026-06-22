'use client'

import { useEffect, useMemo, useState } from "react"
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from "recharts"
import { fetchPriceBars } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"

// A small, representative slice of the forecast panel symbols. Overlaying every symbol would be
// unreadable, so we pick cross-asset bellwethers (metal / energy / dollar / equity / crypto / vol).
const SERIES: { symbol: string; label: string; color: string }[] = [
  { symbol: "GC=F", label: "Gold", color: "#e0c852" },
  { symbol: "CL=F", label: "Oil", color: "#e09652" },
  { symbol: "DX-Y.NYB", label: "USD", color: "#52c8a0" },
  { symbol: "SPY", label: "SPY", color: "#7c9ef8" },
  { symbol: "BTC-USD", label: "BTC", color: "#c852c8" },
  { symbol: "^VIX", label: "VIX", color: "#e05252" },
]

interface Row {
  date: string
  [symbol: string]: number | string | undefined
}

interface IndicatorsLineChartProps {
  limit?: number
  height?: number
}

// Multi-indicator overlay: each indicator's daily close is rebased to 0% at the window start so
// they share one axis and you can read how they move *relative to each other* (cause/effect over
// time). Data comes from the same daily PriceBar substrate the forecast charts use.
export default function IndicatorsLineChart({ limit = 120, height = 260 }: IndicatorsLineChartProps) {
  const { t } = useLanguage()
  const [series, setSeries] = useState<Record<string, { date: string; close: number }[]>>({})
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all(
      SERIES.map((s) =>
        fetchPriceBars(s.symbol, { limit })
          .then((r) => [s.symbol, r.results.map((b) => ({ date: b.date.slice(0, 10), close: b.close }))] as const)
          .catch(() => [s.symbol, [] as { date: string; close: number }[]] as const),
      ),
    )
      .then((entries) => { if (!cancelled) setSeries(Object.fromEntries(entries)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [limit])

  // Merge all series into one date-keyed table, rebasing each to % change from its first close.
  const { data, present } = useMemo(() => {
    const byDate = new Map<string, Row>()
    const present: typeof SERIES = []
    for (const s of SERIES) {
      const bars = series[s.symbol] ?? []
      if (bars.length === 0) continue
      present.push(s)
      const base = bars[0].close
      if (!base) continue
      for (const b of bars) {
        const row = byDate.get(b.date) ?? { date: b.date }
        row[s.symbol] = ((b.close - base) / base) * 100
        byDate.set(b.date, row)
      }
    }
    const data = [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date))
    return { data, present }
  }, [series])

  if (loading) return <p className="py-6 text-center text-xs text-app-text-muted">…</p>
  if (data.length === 0)
    return <p className="py-6 text-center text-xs text-app-text-muted">{t.priceNoHistory}</p>

  return (
    <div className="w-full">
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#2a2a35" strokeDasharray="3 3" />
          <XAxis dataKey="date" tick={{ fill: "#888899", fontSize: 10 }} minTickGap={40} stroke="#2a2a35" />
          <YAxis
            tick={{ fill: "#888899", fontSize: 10 }} width={44} stroke="#2a2a35"
            tickFormatter={(v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(0)}%`}
          />
          <Tooltip
            contentStyle={{ background: "#1a1a22", border: "1px solid #2a2a35", color: "#e8e8f0", fontSize: 12 }}
            labelStyle={{ color: "#888899" }}
            formatter={(v, name) => {
              const n = typeof v === "number" ? v : Number(v)
              return [`${n >= 0 ? "+" : ""}${n.toFixed(2)}%`, name]
            }}
          />
          <Legend wrapperStyle={{ fontSize: 11, color: "#888899" }} />
          {present.map((s) => (
            <Line
              key={s.symbol} type="monotone" dataKey={s.symbol} name={s.label}
              stroke={s.color} dot={false} strokeWidth={1.5} connectNulls isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <p className="mt-1 text-[0.65rem] text-app-text-muted">{t.indicatorsCompareNote}</p>
    </div>
  )
}
