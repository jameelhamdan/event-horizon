'use client'

import { useState, useEffect, useCallback } from "react"
import { fetchForecasts, fetchForecastAccuracy } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import type { Forecast, ForecastAccuracy } from "../../types"
import ForecastChart from "./ForecastChart"
import { cn } from "@/lib/utils"

const POLL_INTERVAL_MS = 60_000
const HORIZONS = [1, 5] as const

interface ForecastPanelProps {
  embedded?: boolean
  onCount?: (n: number) => void
}

const DIR_COLOR: Record<string, string> = {
  up: "#52c8a0", down: "#e05252", neutral: "#888899",
}
const DIR_ARROW: Record<string, string> = { up: "▲", down: "▼", neutral: "→" }

// Event-fused forecasts: direction + calibrated P(up) + predicted Δ% per symbol, with a 1d/5d
// horizon toggle, a rolling-accuracy badge, and an expandable price chart with forward projection.
export default function ForecastPanel({ embedded = false, onCount }: ForecastPanelProps) {
  const { t } = useLanguage()
  const [horizon, setHorizon] = useState<number>(1)
  const [forecasts, setForecasts] = useState<Forecast[]>([])
  const [accuracy, setAccuracy] = useState<ForecastAccuracy[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [fc, acc] = await Promise.all([
        fetchForecasts({ horizon }),
        fetchForecastAccuracy().catch(() => ({ results: [] as ForecastAccuracy[], count: 0 })),
      ])
      setForecasts(fc.results)
      setAccuracy(acc.results)
      onCount?.(fc.count)
    } catch {
      setForecasts([])
      onCount?.(0)
    }
  }, [horizon, onCount])

  useEffect(() => {
    load()
    const id = setInterval(load, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [load])

  const acc = accuracy.find((a) => a.horizon_days === horizon)

  return (
    <div className={cn("flex flex-col gap-2 p-3", embedded && "p-2")}>
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between">
          <h2 className="m-0 text-sm font-semibold text-app-text-heading">{t.marketForecasts}</h2>
          <div className="flex gap-1">
            {HORIZONS.map((h) => (
              <button
                key={h}
                onClick={() => setHorizon(h)}
                className={cn(
                  "rounded border px-2 py-0.5 text-[0.65rem]",
                  horizon === h
                    ? "border-app-accent-blue bg-app-accent-blue/15 text-app-accent-blue"
                    : "border-app-border text-app-text-muted",
                )}
              >
                {h === 1 ? t.forecastHorizon1d : t.forecastHorizon5d}
              </button>
            ))}
          </div>
        </div>
        <p className="m-0 text-[0.7rem] leading-snug text-app-text-muted">{t.forecastNote}</p>
        {acc && acc.accuracy != null && (
          <p className="m-0 text-[0.7rem] text-app-text-muted">
            {t.forecastAccuracy}: {(acc.accuracy * 100).toFixed(0)}% ({acc.scored})
            {acc.brier != null && ` · Brier ${acc.brier.toFixed(3)}`}
          </p>
        )}
      </div>

      {forecasts.length === 0 ? (
        <p className="m-0 py-4 text-center text-xs text-app-text-muted">{t.forecastNoModel}</p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-px p-0">
          {forecasts.map((f) => {
            const open = expanded === f.symbol
            return (
              <li key={`${f.symbol}-${f.horizon_days}`} className="border-b border-app-border last:border-b-0">
                <button
                  onClick={() => setExpanded(open ? null : f.symbol)}
                  className="flex w-full items-center justify-between py-1.5 text-left"
                >
                  <span className="font-mono text-xs text-app-text-heading">{f.symbol}</span>
                  <span className="flex items-center gap-2 text-xs">
                    <span style={{ color: DIR_COLOR[f.direction] }}>{DIR_ARROW[f.direction]}</span>
                    <span className="font-mono" style={{ color: DIR_COLOR[f.direction] }}>
                      {f.predicted_change_pct >= 0 ? "+" : ""}{f.predicted_change_pct.toFixed(2)}%
                    </span>
                    <span className="text-app-text-muted">{(f.proba_up * 100).toFixed(0)}%</span>
                  </span>
                </button>
                {open && (
                  <div className="pb-2">
                    <ForecastChart symbol={f.symbol} forecast={f} />
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
