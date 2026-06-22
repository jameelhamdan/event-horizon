'use client'

import { useEffect, useState } from "react"
import {
  ComposedChart, Area, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts"
import { fetchPriceBars } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import type { Forecast, PriceBar } from "../../types"

interface ForecastChartProps {
  symbol: string
  forecast?: Forecast | null
  height?: number
}

interface ChartPoint {
  date: string
  close?: number
  projection?: number
  band?: [number, number]
}

// Daily close line (from PriceBar) + a dashed forward projection to the forecast horizon, with
// a shaded confidence band. The projection segment anchors to the last real close so it connects.
export default function ForecastChart({ symbol, forecast, height = 220 }: ForecastChartProps) {
  const { t } = useLanguage()
  const [bars, setBars] = useState<PriceBar[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchPriceBars(symbol, { limit: 180 })
      .then((res) => { if (!cancelled) setBars(res.results) })
      .catch(() => { if (!cancelled) setBars([]) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [symbol])

  if (loading) return <p className="py-6 text-center text-xs text-app-text-muted">{symbol}…</p>
  if (bars.length === 0)
    return <p className="py-6 text-center text-xs text-app-text-muted">{t.priceNoHistory}</p>

  const data: ChartPoint[] = bars.map((b) => ({ date: b.date.slice(0, 10), close: b.close }))

  if (forecast && forecast.predicted_price != null && data.length > 0) {
    const last = data[data.length - 1]
    last.projection = last.close
    const future = new Date(forecast.as_of_date)
    future.setDate(future.getDate() + forecast.horizon_days)
    data.push({
      date: future.toISOString().slice(0, 10),
      projection: forecast.predicted_price,
      band: forecast.band_low != null && forecast.band_high != null
        ? [forecast.band_low, forecast.band_high]
        : undefined,
    })
  }

  const projColor = forecast?.direction === "up" ? "#52c8a0"
    : forecast?.direction === "down" ? "#e05252" : "#888899"

  return (
    <div className="w-full">
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#2a2a35" strokeDasharray="3 3" />
          <XAxis dataKey="date" tick={{ fill: "#888899", fontSize: 10 }} minTickGap={40} stroke="#2a2a35" />
          <YAxis domain={["auto", "auto"]} tick={{ fill: "#888899", fontSize: 10 }} width={48} stroke="#2a2a35" />
          <Tooltip
            contentStyle={{ background: "#1a1a22", border: "1px solid #2a2a35", color: "#e8e8f0", fontSize: 12 }}
            labelStyle={{ color: "#888899" }}
          />
          {forecast?.band_low != null && (
            <Area dataKey="band" stroke="none" fill={projColor} fillOpacity={0.12} isAnimationActive={false} />
          )}
          <Line type="monotone" dataKey="close" stroke="#7c9ef8" dot={false} strokeWidth={1.5} isAnimationActive={false} />
          <Line
            type="monotone" dataKey="projection" stroke={projColor} strokeDasharray="5 4"
            strokeWidth={1.5} dot={{ r: 2 }} connectNulls isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
      {forecast && (
        <p className="mt-1 text-center text-[0.7rem] text-app-text-muted">
          {t.forecastProjection}:{" "}
          {forecast.direction === "up" ? "▲" : forecast.direction === "down" ? "▼" : "→"}{" "}
          {forecast.predicted_change_pct >= 0 ? "+" : ""}
          {forecast.predicted_change_pct.toFixed(2)}% · {t.forecastProbUp} {(forecast.proba_up * 100).toFixed(0)}%
        </p>
      )}
    </div>
  )
}
