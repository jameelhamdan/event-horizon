'use client'

import { useEffect, useMemo, useState } from "react"
import {
  ResponsiveContainer, ScatterChart, Scatter, Line, XAxis, YAxis, Tooltip, CartesianGrid, ReferenceLine,
} from "recharts"
import { fetchPriceBars } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import { eventTime, symbolWeight } from "../../lib/pressure"
import type { EventSummary } from "../../types"

const MIN_WEEKS = 8

interface Point { senti: number; ret: number; week: string }

function weekStart(ms: number): number {
  const d = new Date(ms)
  d.setUTCHours(0, 0, 0, 0)
  d.setUTCDate(d.getUTCDate() - d.getUTCDay())
  return d.getTime()
}

// Research corner: weekly mean FinBERT sentiment of routed events (x) vs realized weekly
// return (y), with an OLS fit + R². Deliberately shows how weak the raw correlation is.
export default function SentimentScatter({ symbol, events, days }: {
  symbol: string
  events: EventSummary[]
  days: number
}) {
  const { t } = useLanguage()
  const [bars, setBars] = useState<{ t: number; close: number }[]>([])

  useEffect(() => {
    let cancelled = false
    fetchPriceBars(symbol, { limit: Math.max(days + 1, 90) })
      .then((r) => {
        if (cancelled) return
        setBars(
          r.results
            .map((b) => ({ t: new Date(b.date).getTime(), close: b.close }))
            .sort((a, b) => a.t - b.t),
        )
      })
      .catch(() => { if (!cancelled) setBars([]) })
    return () => { cancelled = true }
  }, [symbol, days])

  const { points, fit } = useMemo(() => {
    // Weekly close: last bar of each week.
    const weekly = new Map<number, number>()
    for (const b of bars) weekly.set(weekStart(b.t), b.close)
    const weeks = [...weekly.keys()].sort((a, b) => a - b)

    // Weekly mean sentiment of events routed to this symbol.
    const senti = new Map<number, { sum: number; n: number }>()
    for (const e of events) {
      if (symbolWeight(e, symbol) === 0 || e.avg_finbert_sentiment == null) continue
      const w = weekStart(eventTime(e))
      const s = senti.get(w) ?? { sum: 0, n: 0 }
      s.sum += e.avg_finbert_sentiment
      s.n += 1
      senti.set(w, s)
    }

    const points: Point[] = []
    for (let i = 0; i + 1 < weeks.length; i++) {
      const w = weeks[i]
      const s = senti.get(w)
      const c0 = weekly.get(w)
      const c1 = weekly.get(weeks[i + 1])
      if (!s || c0 == null || c1 == null || c0 === 0) continue
      points.push({
        senti: s.sum / s.n,
        ret: ((c1 - c0) / c0) * 100,
        week: new Date(w).toISOString().slice(0, 10),
      })
    }

    // OLS y = a + bx and R².
    let fit: { a: number; b: number; r2: number } | null = null
    if (points.length >= MIN_WEEKS) {
      const n = points.length
      const mx = points.reduce((s, p) => s + p.senti, 0) / n
      const my = points.reduce((s, p) => s + p.ret, 0) / n
      let sxy = 0
      let sxx = 0
      let syy = 0
      for (const p of points) {
        sxy += (p.senti - mx) * (p.ret - my)
        sxx += (p.senti - mx) ** 2
        syy += (p.ret - my) ** 2
      }
      if (sxx > 0 && syy > 0) {
        const b = sxy / sxx
        fit = { a: my - b * mx, b, r2: (sxy * sxy) / (sxx * syy) }
      }
    }
    return { points, fit }
  }, [bars, events, symbol])

  if (points.length < MIN_WEEKS)
    return <p className="py-4 text-center text-[0.7rem] text-app-text-muted">{t.scatterNotEnough}</p>

  const xs = points.map((p) => p.senti)
  const fitLine = fit
    ? [
        { senti: Math.min(...xs), ret: fit.a + fit.b * Math.min(...xs) },
        { senti: Math.max(...xs), ret: fit.a + fit.b * Math.max(...xs) },
      ]
    : []

  return (
    <div className="flex flex-col gap-1">
      <ResponsiveContainer width="100%" height={200}>
        <ScatterChart margin={{ top: 8, right: 8, bottom: 4, left: 8 }}>
          <CartesianGrid stroke="#20202a" />
          <XAxis
            dataKey="senti" type="number" domain={["auto", "auto"]} name={t.pressureSentiment}
            tick={{ fontSize: 10, fill: "#666677" }} stroke="#2a2a35"
            tickFormatter={(v: number) => v.toFixed(1)}
          />
          <YAxis
            dataKey="ret" type="number" domain={["auto", "auto"]} width={44}
            tick={{ fontSize: 10, fill: "#666677" }} stroke="#2a2a35"
            tickFormatter={(v: number) => `${v.toFixed(0)}%`}
          />
          <ReferenceLine y={0} stroke="#2a2a35" />
          <Scatter data={points} fill="#7c9ef8" fillOpacity={0.75} isAnimationActive={false} />
          {fitLine.length === 2 && (
            <Line
              data={fitLine} dataKey="ret" type="linear" stroke="#c9a86a"
              strokeDasharray="5 4" dot={false} isAnimationActive={false}
            />
          )}
          <Tooltip
            contentStyle={{ background: "#0f0f13", border: "1px solid #2a2a35", borderRadius: 6, fontSize: "0.7rem" }}
            labelStyle={{ color: "#888899" }}
            itemStyle={{ color: "#e8e8f0" }}
            formatter={(val, key) => [
              typeof val === "number" ? (key === "ret" ? `${val.toFixed(2)}%` : val.toFixed(2)) : String(val),
              key === "ret" ? "%" : t.pressureSentiment,
            ]}
          />
        </ScatterChart>
      </ResponsiveContainer>
      <p className="px-1 text-[0.65rem] leading-snug text-app-text-muted">
        {t.scatterNote}
        {fit && <> · n={points.length}, R²={fit.r2.toFixed(2)}</>}
      </p>
    </div>
  )
}
