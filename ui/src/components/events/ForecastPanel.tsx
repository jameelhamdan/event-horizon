'use client'

import { useState, useEffect } from "react"
import { fetchForecasts } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import type { Forecast } from "../../types"
import { cn } from "@/lib/utils"

const POLL_INTERVAL_MS = 60_000

interface ForecastPanelProps {
  embedded?: boolean
  onCount?: (n: number) => void
}

// Placeholder panel: the prediction layer is being reworked, so the API returns a
// neutral / 0% forecast per symbol. This surface stays functional in the meantime.
export default function ForecastPanel({ embedded = false, onCount }: ForecastPanelProps) {
  const { t } = useLanguage()
  const [forecasts, setForecasts] = useState<Forecast[]>([])

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetchForecasts()
        if (cancelled) return
        setForecasts(res.results)
        onCount?.(res.count)
      } catch {
        if (!cancelled) {
          setForecasts([])
          onCount?.(0)
        }
      }
    }
    load()
    const id = setInterval(load, POLL_INTERVAL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [onCount])

  return (
    <div className={cn("flex flex-col gap-2 p-3", embedded && "p-2")}>
      <div className="flex flex-col gap-1">
        <h2 className="m-0 text-sm font-semibold text-app-text">{t.marketForecasts}</h2>
        <p className="m-0 text-[0.7rem] leading-snug text-app-text-muted">{t.forecastPlaceholderNote}</p>
      </div>

      {forecasts.length === 0 ? (
        <p className="m-0 py-4 text-center text-xs text-app-text-muted">{t.noForecasts}</p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-px p-0">
          {forecasts.map((f) => (
            <li
              key={`${f.symbol}-${f.horizon_hours}`}
              className="flex items-center justify-between border-b border-app-border py-1.5 last:border-b-0"
            >
              <span className="font-mono text-xs text-app-text">{f.symbol}</span>
              <span className="flex items-center gap-2 text-xs">
                <span className="text-app-text-muted">{t.sentNeutral}</span>
                <span className="font-mono text-app-text-muted">
                  {f.predicted_change_pct >= 0 ? "+" : ""}
                  {f.predicted_change_pct.toFixed(1)}%
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
