'use client'

import { useEffect, useMemo, useState } from "react"
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine,
} from "recharts"
import { fetchForecastAccuracy } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import type { ForecastAccuracyResponse } from "../../types"

const POS = "#52c8a0"
const NEG = "#e05252"
const HORIZONS = [1, 5]

// FiveThirtyEight-style published track record: rolling weekly accuracy vs the coin-flip
// baseline, plus the most recent predicted-vs-realized outcomes. Honesty as a feature.
export default function TrackRecord() {
  const { t, lang } = useLanguage()
  const [horizon, setHorizon] = useState(1)
  const [resp, setResp] = useState<ForecastAccuracyResponse | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    fetchForecastAccuracy(undefined, { history: true, recent: 8 })
      .then((r) => { if (!cancelled) setResp(r) })
      .catch(() => { if (!cancelled) setResp(null) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const summary = resp?.results.find((a) => a.horizon_days === horizon)
  const series = useMemo(
    () =>
      (resp?.history?.[String(horizon)] ?? [])
        .filter((w) => w.accuracy != null)
        .map((w) => ({ t: new Date(w.week).getTime(), accuracy: w.accuracy, scored: w.scored })),
    [resp, horizon],
  )
  const recent = useMemo(
    () => (resp?.recent ?? []).filter((f) => f.horizon_days === horizon).slice(0, 6),
    [resp, horizon],
  )

  const locale = lang === "ar" ? "ar" : "en"
  const fmtWeek = (ms: number) => new Date(ms).toLocaleDateString(locale, { month: "numeric", day: "numeric" })

  if (loading) return <p className="py-6 text-center text-xs text-app-text-muted">…</p>
  if (!resp || resp.results.length === 0)
    return <p className="py-6 text-center text-xs text-app-text-muted">{t.forecastNoModel}</p>

  return (
    <div className="flex flex-col gap-3">
      {/* Horizon toggle + headline */}
      <div className="flex items-center justify-between">
        <div className="inline-flex overflow-hidden rounded-md border border-app-border">
          {HORIZONS.map((h) => (
            <button
              key={h}
              type="button"
              onClick={() => setHorizon(h)}
              aria-pressed={h === horizon}
              className={
                "px-2 py-0.5 text-[0.7rem] font-semibold " +
                (h === horizon ? "bg-app-accent-blue text-white" : "text-app-text-muted hover:text-app-text-primary")
              }
            >
              {h}d
            </button>
          ))}
        </div>
        {summary?.accuracy != null && (
          <div className="text-right" title={summary.brier != null ? `${t.trackRecordBrier}: ${summary.brier.toFixed(3)}` : undefined}>
            <span className="font-mono text-sm font-semibold tabular-nums text-app-text-primary">
              {(summary.accuracy * 100).toFixed(0)}%
            </span>
            <span className="ms-1 text-[0.68rem] text-app-text-muted">
              ({summary.scored} {t.trackRecordScored})
            </span>
          </div>
        )}
      </div>

      {/* Weekly accuracy sparkline vs the 0.50 baseline */}
      {series.length > 1 && (
        <ResponsiveContainer width="100%" height={90}>
          <LineChart data={series} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
            <XAxis
              dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]}
              tickFormatter={fmtWeek} tick={{ fontSize: 9, fill: "#666677" }} stroke="#2a2a35" minTickGap={40}
            />
            <YAxis domain={[0, 1]} hide />
            <ReferenceLine y={0.5} stroke="#666677" strokeDasharray="4 3" />
            <Line
              type="monotone" dataKey="accuracy" stroke="#7c9ef8" strokeWidth={1.5}
              dot={false} isAnimationActive={false}
            />
            <Tooltip
              contentStyle={{ background: "#0f0f13", border: "1px solid #2a2a35", borderRadius: 6, fontSize: "0.7rem" }}
              labelStyle={{ color: "#888899" }}
              itemStyle={{ color: "#e8e8f0" }}
              labelFormatter={(ms) => fmtWeek(Number(ms))}
              formatter={(val) => [typeof val === "number" ? `${(val * 100).toFixed(0)}%` : String(val), ""]}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
      {series.length > 1 && (
        <p className="text-[0.63rem] text-app-text-muted">- - {t.trackRecordBaseline} (50%)</p>
      )}

      {/* Recent predicted vs realized */}
      {recent.length > 0 && (
        <div>
          <h4 className="mb-1 text-[0.68rem] font-semibold uppercase tracking-wide text-app-text-muted">
            {t.trackRecordRecent}
          </h4>
          <ul className="flex flex-col gap-1">
            {recent.map((f) => {
              const predUp = f.direction === "up"
              const realUp = f.realized_direction === "up"
              return (
                <li key={f.id ?? `${f.symbol}-${f.as_of_date}-${f.horizon_days}`} className="flex items-center justify-between text-[0.72rem]">
                  <span className="font-mono text-app-text-primary">{f.symbol}</span>
                  <span className="font-mono tabular-nums">
                    <span style={{ color: predUp ? POS : NEG }}>{predUp ? "▲" : "▼"}</span>
                    <span className="mx-1 text-app-text-muted">→</span>
                    <span style={{ color: realUp ? POS : NEG }}>{realUp ? "▲" : "▼"}</span>
                    <span className="ms-2">{f.is_correct ? "✓" : "✗"}</span>
                  </span>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      <p className="border-t border-app-border pt-2 text-[0.65rem] leading-snug text-app-text-muted">
        {t.trackRecordNote}
      </p>
    </div>
  )
}
