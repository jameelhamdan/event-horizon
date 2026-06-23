'use client'

import { useEffect, useMemo, useState } from "react"
import { fetchEvents } from "../../api/events"
import { useLanguage } from "../../contexts/LanguageContext"
import { categoryColor } from "@/components/category"
import { categoryLabel } from "../../i18n/categories"
import type { EventSummary } from "../../types"

// Panel symbols + short labels (kept in sync with services/forecasting/routing.py PANEL_SYMBOLS).
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

interface Edge { cat: string; symbol: string; weight: number }

function buildEdges(events: EventSummary[]): { edges: Edge[]; maxAbs: number } {
  const net: Record<string, Record<string, number>> = {}
  for (const e of events) {
    const cat = e.category || "general"
    for (const ind of e.affected_indicators ?? []) {
      if (!PANEL.some((p) => p.symbol === ind.symbol)) continue
      net[cat] ??= {}
      net[cat][ind.symbol] = (net[cat][ind.symbol] ?? 0) + ind.weight
    }
  }
  const edges: Edge[] = []
  let maxAbs = 0
  for (const cat of Object.keys(net)) {
    for (const symbol of Object.keys(net[cat])) {
      const weight = net[cat][symbol]
      edges.push({ cat, symbol, weight })
      maxAbs = Math.max(maxAbs, Math.abs(weight))
    }
  }
  return { edges, maxAbs }
}

interface CauseEffectGraphProps {
  days?: number
}

// Bipartite cause→effect tree built from Event.affected_indicators (the router output): event
// categories on the left flow into the market indicators they press on, on the right. Edge
// thickness encodes net |weight|, colour encodes direction (green up / red down). This is the
// graph view of the same signal the heatmap shows as a grid.
export default function CauseEffectGraph({ days = 7 }: CauseEffectGraphProps) {
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

  const { edges, maxAbs } = useMemo(() => buildEdges(events), [events])

  // Drop near-zero edges so the graph stays legible.
  const shown = useMemo(
    () => edges.filter((e) => maxAbs > 0 && Math.abs(e.weight) >= maxAbs * 0.08),
    [edges, maxAbs],
  )

  if (loading) return <p className="py-6 text-center text-xs text-app-text-muted">…</p>
  if (shown.length === 0)
    return <p className="py-6 text-center text-xs text-app-text-muted">{t.causeEffectEmpty}</p>

  const cats = CAT_ORDER.filter((c) => shown.some((e) => e.cat === c))
  const syms = PANEL.filter((p) => shown.some((e) => e.symbol === p.symbol))

  // Layout. Fixed-size SVG, horizontally scrollable on narrow screens.
  const W = 520
  const padY = 24
  const rowH = 46
  const H = Math.max(cats.length, syms.length) * rowH + padY * 2
  const leftX = 96
  const rightX = W - 96
  // Evenly distribute n node centers between the top and bottom padding; a lone node is centered.
  const slot = (i: number, n: number) => {
    const top = padY + rowH / 2
    const bottom = H - padY - rowH / 2
    return n <= 1 ? (top + bottom) / 2 : top + (i * (bottom - top)) / (n - 1)
  }
  const catYs = Object.fromEntries(cats.map((c, i) => [c, slot(i, cats.length)]))
  const symYs = Object.fromEntries(syms.map((s, i) => [s.symbol, slot(i, syms.length)]))

  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-between px-2 text-[0.62rem] font-semibold uppercase tracking-wide text-app-text-muted">
        <span>{t.causeLabel}</span>
        <span>{t.effectLabel}</span>
      </div>
      <div className="overflow-x-auto">
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="max-w-full" role="img">
          {/* Edges first so nodes sit on top */}
          {shown.map((e) => {
            const y1 = catYs[e.cat]
            const y2 = symYs[e.symbol]
            const intensity = Math.min(Math.abs(e.weight) / maxAbs, 1)
            const sw = 1 + intensity * 6
            const mx = (leftX + rightX) / 2
            return (
              <path
                key={`${e.cat}-${e.symbol}`}
                d={`M ${leftX} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${rightX} ${y2}`}
                fill="none"
                stroke={e.weight >= 0 ? POS : NEG}
                strokeWidth={sw}
                strokeOpacity={0.2 + intensity * 0.6}
                strokeLinecap="round"
              >
                <title>
                  {`${categoryLabel(lang, e.cat)} → ${PANEL.find((p) => p.symbol === e.symbol)?.label ?? e.symbol}: ${e.weight >= 0 ? "+" : ""}${e.weight.toFixed(2)}`}
                </title>
              </path>
            )
          })}

          {/* Cause nodes (left) */}
          {cats.map((c) => (
            <g key={c}>
              <circle cx={leftX} cy={catYs[c]} r={6} fill={categoryColor(c)} />
              <text
                x={leftX - 12} y={catYs[c]} textAnchor="end" dominantBaseline="middle"
                fontSize={11} fill={categoryColor(c)} fontWeight={600}
              >
                {categoryLabel(lang, c)}
              </text>
            </g>
          ))}

          {/* Effect nodes (right) */}
          {syms.map((s) => (
            <g key={s.symbol}>
              <circle cx={rightX} cy={symYs[s.symbol]} r={6} fill="#7c9ef8" />
              <text
                x={rightX + 12} y={symYs[s.symbol]} textAnchor="start" dominantBaseline="middle"
                fontSize={11} fill="#e8e8f0" fontFamily="monospace"
              >
                {s.label}
              </text>
            </g>
          ))}
        </svg>
      </div>
      <p className="px-2 text-[0.65rem] text-app-text-muted">
        <span style={{ color: POS }}>■</span> {t.heatmapUp} &nbsp;
        <span style={{ color: NEG }}>■</span> {t.heatmapDown}
      </p>
    </div>
  )
}
