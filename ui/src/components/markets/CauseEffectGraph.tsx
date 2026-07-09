'use client'

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { fetchSymbols } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import { categoryColor } from "@/components/category"
import { categoryLabel } from "../../i18n/categories"
import { symbolStreamKey } from "../../lib/symbols"
import type { EventSummary, StreamKey } from "../../types"

// Short display labels for well-known symbols; anything else falls back to its
// MarketSymbol name. The panel itself is DB-driven (fetched from /api/symbols/),
// so admin panel changes show up here without a code edit.
const SHORT_LABELS: Record<string, string> = {
  "GC=F": "Gold",
  "CL=F": "Oil",
  "NG=F": "Gas",
  "ZW=F": "Wheat",
  "DX-Y.NYB": "USD",
  "^TNX": "10Y",
  "^VIX": "VIX",
  "SPY": "SPY",
  "BTC-USD": "BTC",
  "ETH-USD": "ETH",
}
// Fallback when /api/symbols/ is unreachable — the seeded default panel.
const FALLBACK_PANEL: { symbol: string; label: string }[] =
  Object.entries(SHORT_LABELS).map(([symbol, label]) => ({ symbol, label }))
const CAT_ORDER = ["conflict", "economic", "political", "disaster", "health", "general"]
const POS = "#52c8a0"
const NEG = "#e05252"
const MAX_TOPICS = 8

type Mode = "categories" | "topics"

interface Edge { cause: string; symbol: string; weight: number }

function buildEdges(
  events: EventSummary[],
  mode: Mode,
  panel: { symbol: string; label: string }[],
): { edges: Edge[]; maxAbs: number; causes: string[] } {
  const panelSet = new Set(panel.map((p) => p.symbol))
  const net: Record<string, Record<string, number>> = {}
  for (const e of events) {
    const causes = mode === "categories" ? [e.category || "general"] : (e.topic_slugs ?? [])
    if (causes.length === 0) continue
    for (const ind of e.affected_indicators ?? []) {
      if (!panelSet.has(ind.symbol)) continue
      for (const cause of causes) {
        net[cause] ??= {}
        net[cause][ind.symbol] = (net[cause][ind.symbol] ?? 0) + ind.weight
      }
    }
  }
  const edges: Edge[] = []
  let maxAbs = 0
  for (const cause of Object.keys(net)) {
    for (const symbol of Object.keys(net[cause])) {
      const weight = net[cause][symbol]
      edges.push({ cause, symbol, weight })
      maxAbs = Math.max(maxAbs, Math.abs(weight))
    }
  }

  let causes: string[]
  if (mode === "categories") {
    causes = CAT_ORDER.filter((c) => edges.some((e) => e.cause === c))
  } else {
    // Topics ranked by total |weight|, capped so the graph stays legible.
    const totals = new Map<string, number>()
    for (const e of edges) totals.set(e.cause, (totals.get(e.cause) ?? 0) + Math.abs(e.weight))
    causes = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, MAX_TOPICS).map(([slug]) => slug)
  }
  return { edges, maxAbs, causes }
}

interface CauseEffectGraphProps {
  mode: Mode
  events: EventSummary[]
  loading: boolean
  onSelectSymbol?: (symbol: string, streamKey: StreamKey, name: string) => void
}

// Bipartite event→effect flow built from Event.affected_indicators (the router output):
// events (by category or by topic, per the `mode` prop) on the left press on market
// indicators on the right. Edge thickness = net |weight|, colour = direction. Symbol
// nodes select the master chart; event nodes toggle-highlight their edges. Fills the
// card width (ResizeObserver). Render one instance per mode — no in-component toggle.
export default function CauseEffectGraph({ mode, events, loading, onSelectSymbol }: CauseEffectGraphProps) {
  const { lang, t } = useLanguage()
  const [focusCause, setFocusCause] = useState<string | null>(null)

  // DB-driven indicator panel (MarketSymbol.is_forecast); hardcoded fallback offline.
  const [panel, setPanel] = useState(FALLBACK_PANEL)
  useEffect(() => {
    let cancelled = false
    fetchSymbols({ forecast: true })
      .then((r) => {
        if (cancelled || r.results.length === 0) return
        setPanel(r.results.map((s) => ({ symbol: s.symbol, label: SHORT_LABELS[s.symbol] ?? s.name })))
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  // A focused cause can vanish when the underlying events change (range switch,
  // refresh) — clear the focus instead of stranding a blank graph. State is
  // adjusted during render (not in an effect) per the React docs pattern.
  const [prevEvents, setPrevEvents] = useState(events)
  if (prevEvents !== events) {
    setPrevEvents(events)
    setFocusCause(null)
  }

  const containerRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(520)
  useLayoutEffect(() => {
    const el = containerRef.current
    if (!el) return
    const measure = () => setWidth(Math.max(el.clientWidth, 360))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const { edges, maxAbs, causes } = useMemo(() => buildEdges(events, mode, panel), [events, mode, panel])

  // Drop near-zero edges so the graph stays legible; apply the cause focus if set.
  const shown = useMemo(() => {
    let out = edges.filter((e) => maxAbs > 0 && Math.abs(e.weight) >= maxAbs * 0.08 && causes.includes(e.cause))
    if (focusCause) out = out.filter((e) => e.cause === focusCause)
    return out
  }, [edges, maxAbs, causes, focusCause])

  const causeName = (cause: string) =>
    mode === "categories" ? categoryLabel(lang, cause) : cause.replace(/-/g, " ")
  const causeCol = (cause: string) => (mode === "categories" ? categoryColor(cause) : "#c9a86a")

  if (loading) return <p className="py-6 text-center text-xs text-app-text-muted">…</p>
  if (shown.length === 0)
    return <p className="py-6 text-center text-xs text-app-text-muted">{t.causeEffectEmpty}</p>

  const visCauses = causes.filter((c) => shown.some((e) => e.cause === c))
  const syms = panel.filter((p) => shown.some((e) => e.symbol === p.symbol))

  // Layout: node columns hug the measured container width.
  const W = width
  const padY = 24
  const rowH = 46
  const H = Math.max(visCauses.length, syms.length, 1) * rowH + padY * 2
  const labelW = Math.min(150, Math.max(96, W * 0.2))
  const leftX = labelW
  const rightX = W - labelW
  const slot = (i: number, n: number) => {
    const top = padY + rowH / 2
    const bottom = H - padY - rowH / 2
    return n <= 1 ? (top + bottom) / 2 : top + (i * (bottom - top)) / (n - 1)
  }
  const causeYs = Object.fromEntries(visCauses.map((c, i) => [c, slot(i, visCauses.length)]))
  const symYs = Object.fromEntries(syms.map((s, i) => [s.symbol, slot(i, syms.length)]))

  return (
    <div className="flex flex-col gap-2" ref={containerRef}>
      <div className="flex items-center justify-between px-2">
        <span className="text-[0.62rem] font-semibold uppercase tracking-wide text-app-text-muted">{t.causeLabel}</span>
        <span className="text-[0.62rem] font-semibold uppercase tracking-wide text-app-text-muted">{t.effectLabel}</span>
      </div>
      <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} role="img">
        {/* Edges first so nodes sit on top */}
        {shown.map((e) => {
          const y1 = causeYs[e.cause]
          const y2 = symYs[e.symbol]
          if (y1 == null || y2 == null) return null
          const intensity = Math.min(Math.abs(e.weight) / maxAbs, 1)
          const sw = 1 + intensity * 6
          const mx = (leftX + rightX) / 2
          return (
            <path
              key={`${e.cause}-${e.symbol}`}
              d={`M ${leftX} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${rightX} ${y2}`}
              fill="none"
              stroke={e.weight >= 0 ? POS : NEG}
              strokeWidth={sw}
              strokeOpacity={0.2 + intensity * 0.6}
              strokeLinecap="round"
            >
              <title>
                {`${causeName(e.cause)} → ${panel.find((p) => p.symbol === e.symbol)?.label ?? e.symbol}: ${e.weight >= 0 ? "+" : ""}${e.weight.toFixed(2)}`}
              </title>
            </path>
          )
        })}

        {/* Cause nodes (left) — click to isolate that cause's edges */}
        {visCauses.map((c) => (
          <g
            key={c}
            style={{ cursor: "pointer" }}
            opacity={focusCause && focusCause !== c ? 0.35 : 1}
            onClick={() => setFocusCause((cur) => (cur === c ? null : c))}
          >
            <circle cx={leftX} cy={causeYs[c]} r={6} fill={causeCol(c)} />
            <text
              x={leftX - 12} y={causeYs[c]} textAnchor="end" dominantBaseline="middle"
              fontSize={11} fill={causeCol(c)} fontWeight={600}
            >
              {causeName(c)}
            </text>
          </g>
        ))}

        {/* Effect nodes (right) — click to select in the master chart */}
        {syms.map((s) => (
          <g
            key={s.symbol}
            style={{ cursor: onSelectSymbol ? "pointer" : undefined }}
            onClick={() => {
              const sk = symbolStreamKey(s.symbol)
              if (onSelectSymbol && sk) onSelectSymbol(s.symbol, sk, s.label)
            }}
          >
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
      <p className="px-2 text-[0.65rem] text-app-text-muted">
        <span style={{ color: POS }}>■</span> {t.heatmapUp} &nbsp;
        <span style={{ color: NEG }}>■</span> {t.heatmapDown}
      </p>
    </div>
  )
}
