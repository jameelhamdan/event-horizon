'use client'

import { useMemo } from "react"
import { useLanguage } from "../../contexts/LanguageContext"
import type { EventSummary } from "../../types"

// CNN Fear&Greed-style one-glance dial: today's total routed |weight| across the panel,
// placed against the whole window's daily distribution → Quiet / Normal / Elevated / Intense.
export default function PressureGauge({ events }: { events: EventSummary[] }) {
  const { t } = useLanguage()

  const { level, frac } = useMemo(() => {
    const byDay = new Map<number, number>()
    for (const e of events) {
      let total = 0
      for (const ind of e.affected_indicators ?? []) total += Math.abs(ind.weight)
      if (total === 0) continue
      const day = new Date(e.latest_article_at ?? e.started_at).setUTCHours(0, 0, 0, 0)
      byDay.set(day, (byDay.get(day) ?? 0) + total)
    }
    if (byDay.size === 0) return { level: null as string | null, frac: 0 }
    const today = new Date().setUTCHours(0, 0, 0, 0)
    // "Today" falls back to the newest day with data (timezones, quiet mornings).
    const current = byDay.get(today) ?? byDay.get(Math.max(...byDay.keys())) ?? 0
    const sorted = [...byDay.values()].sort((a, b) => a - b)
    const rank = sorted.filter((v) => v <= current).length / sorted.length
    const level = rank < 0.25 ? t.gaugeQuiet : rank < 0.75 ? t.gaugeNormal : rank < 0.92 ? t.gaugeElevated : t.gaugeIntense
    return { level, frac: rank }
  }, [events, t])

  if (level == null) return null

  const color = frac < 0.25 ? "#52c8a0" : frac < 0.75 ? "#7c9ef8" : frac < 0.92 ? "#e0a052" : "#e05252"

  return (
    <div className="flex items-center gap-2 rounded-md border border-app-border bg-app-surface px-3 py-1.5">
      <span className="text-[0.68rem] font-medium uppercase tracking-wide text-app-text-muted">{t.gaugeTitle}</span>
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-app-border/40">
        <div className="h-full rounded-full" style={{ width: `${Math.max(frac * 100, 6)}%`, background: color }} />
      </div>
      <span className="text-[0.72rem] font-semibold" style={{ color }}>{level}</span>
    </div>
  )
}
