'use client'

import { useEffect, useMemo, useState } from "react"
import { fetchEvents } from "../../api/events"
import { useLanguage } from "../../contexts/LanguageContext"
import { categoryColor } from "@/components/category"
import { categoryLabel } from "../../i18n/categories"
import type { EventSummary } from "../../types"

// Panel symbols + short display labels (kept in sync with services/forecasting/routing.py).
const PANEL: { symbol: string; label: string }[] = [
  { symbol: "GC=F", label: "Gold" },
  { symbol: "CL=F", label: "Oil" },
  { symbol: "NG=F", label: "Gas" },
  { symbol: "ZW=F", label: "Wheat" },
  { symbol: "DX-Y.NYB", label: "USD" },
  { symbol: "^TNX", label: "10Y" },
  { symbol: "^VIX", label: "VIX" },
  { symbol: "SPY", label: "SPY" },
  { symbol: "BTC-USD", label: "BTC" },
  { symbol: "ETH-USD", label: "ETH" },
]
const CAT_ORDER = ["conflict", "economic", "political", "disaster", "health", "general"]
const POS = "#52c8a0"
const NEG = "#e05252"

interface Agg {
  net: Record<string, Record<string, number>>   // category -> symbol -> summed signed weight
  count: Record<string, Record<string, number>> // category -> symbol -> event count
  perSymbolAbs: Record<string, number>           // symbol -> summed |weight|
  maxAbs: number
}

function aggregate(events: EventSummary[]): Agg {
  const net: Agg["net"] = {}
  const count: Agg["count"] = {}
  const perSymbolAbs: Record<string, number> = {}
  let maxAbs = 0
  for (const e of events) {
    const cat = e.category || "general"
    for (const ind of e.affected_indicators ?? []) {
      if (!PANEL.some((p) => p.symbol === ind.symbol)) continue
      net[cat] ??= {}
      count[cat] ??= {}
      net[cat][ind.symbol] = (net[cat][ind.symbol] ?? 0) + ind.weight
      count[cat][ind.symbol] = (count[cat][ind.symbol] ?? 0) + 1
      perSymbolAbs[ind.symbol] = (perSymbolAbs[ind.symbol] ?? 0) + Math.abs(ind.weight)
      maxAbs = Math.max(maxAbs, Math.abs(net[cat][ind.symbol]))
    }
  }
  return { net, count, perSymbolAbs, maxAbs }
}

function cellColor(value: number, maxAbs: number): string {
  if (!value || maxAbs === 0) return "transparent"
  const intensity = Math.min(Math.abs(value) / maxAbs, 1)
  const base = value > 0 ? POS : NEG
  const alpha = Math.round((0.12 + 0.78 * intensity) * 255).toString(16).padStart(2, "0")
  return base + alpha
}

interface EventsHeatmapProps {
  days?: number
}

// Weighted heatmap of how recent events press on each market indicator, derived from
// Event.affected_indicators (the router output). Rows = event categories, columns = panel
// symbols; green = net upward pressure, red = net downward. Plus a "most-impacted" bar.
export default function EventsHeatmap({ days = 7 }: EventsHeatmapProps) {
  const { lang, t } = useLanguage()
  const [events, setEvents] = useState<EventSummary[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetchEvents({ start: new Date(Date.now() - days * 86400_000).toISOString(), limit: 500 })
      .then((r) => { if (!cancelled) setEvents(r.results) })
      .catch(() => { if (!cancelled) setEvents([]) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [days])

  const agg = useMemo(() => aggregate(events), [events])
  const cats = CAT_ORDER.filter((c) => agg.net[c])
  const maxSymbolAbs = Math.max(1, ...Object.values(agg.perSymbolAbs))
  const ranked = [...PANEL]
    .map((p) => ({ ...p, v: agg.perSymbolAbs[p.symbol] ?? 0 }))
    .sort((a, b) => b.v - a.v)

  if (loading) return <p className="py-6 text-center text-xs text-app-text-muted">…</p>
  if (cats.length === 0)
    return <p className="py-6 text-center text-xs text-app-text-muted">{t.heatmapEmpty}</p>

  return (
    <div className="flex flex-col gap-4">
      {/* Most-impacted indicators */}
      <div>
        <h3 className="m-0 mb-2 text-[0.8rem] font-semibold text-app-text-heading">{t.mostImpacted}</h3>
        <div className="flex flex-col gap-1">
          {ranked.filter((r) => r.v > 0).map((r) => (
            <div key={r.symbol} className="flex items-center gap-2">
              <span className="w-10 shrink-0 font-mono text-[0.7rem] text-app-text-muted">{r.label}</span>
              <div className="h-3 flex-1 overflow-hidden rounded bg-app-card">
                <div className="h-full rounded" style={{ width: `${(r.v / maxSymbolAbs) * 100}%`, background: "var(--app-accent-blue)" }} />
              </div>
              <span className="w-9 shrink-0 text-right font-mono text-[0.65rem] text-app-text-muted">{r.v.toFixed(2)}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Category x symbol net-pressure heatmap */}
      <div>
        <h3 className="m-0 mb-2 text-[0.8rem] font-semibold text-app-text-heading">{t.heatmapNet}</h3>
        <div className="overflow-x-auto">
          <table className="border-separate" style={{ borderSpacing: 2 }}>
            <thead>
              <tr>
                <th className="sticky left-0 bg-app-panel" />
                {PANEL.map((p) => (
                  <th key={p.symbol} className="px-1 pb-1 text-center font-mono text-[0.6rem] font-normal text-app-text-muted">
                    {p.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {cats.map((cat) => (
                <tr key={cat}>
                  <td className="sticky left-0 bg-app-panel pr-2 text-[0.68rem] font-medium whitespace-nowrap"
                      style={{ color: categoryColor(cat) }}>
                    {categoryLabel(lang, cat)}
                  </td>
                  {PANEL.map((p) => {
                    const v = agg.net[cat]?.[p.symbol] ?? 0
                    const n = agg.count[cat]?.[p.symbol] ?? 0
                    return (
                      <td key={p.symbol}
                          title={v ? `${categoryLabel(lang, cat)} → ${p.label}: ${v >= 0 ? "+" : ""}${v.toFixed(2)} (${n} ${t.eventCountLabel})` : ""}
                          className="h-7 w-9 rounded text-center align-middle font-mono text-[0.6rem]"
                          style={{ background: cellColor(v, agg.maxAbs), color: Math.abs(v) / (agg.maxAbs || 1) > 0.5 ? "#0f0f13" : "#888899" }}>
                        {v ? (v >= 0 ? "+" : "") + v.toFixed(1) : ""}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-[0.65rem] text-app-text-muted">
          <span style={{ color: POS }}>■</span> {t.heatmapUp} &nbsp;
          <span style={{ color: NEG }}>■</span> {t.heatmapDown}
        </p>
      </div>
    </div>
  )
}
