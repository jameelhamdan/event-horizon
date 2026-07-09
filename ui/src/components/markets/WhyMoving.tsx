'use client'

import { useMemo } from "react"
import { useLanguage } from "../../contexts/LanguageContext"
import { categoryColor } from "@/components/category"
import { categoryLabel } from "../../i18n/categories"
import { pressureSummary } from "../../lib/pressure"
import type { EventSummary } from "../../types"

const POS = "#52c8a0"
const NEG = "#e05252"

interface WhyMovingProps {
  symbol: string
  events: EventSummary[]
  loading: boolean
}

// Yahoo-style "why is this moving?" explainer for the selected symbol. Net pressure uses the
// same 3-day exponential decay as the forecasting feature builder (lib/pressure.ts), so the
// gauge shows exactly the signal the model sees.
export default function WhyMoving({ symbol, events, loading }: WhyMovingProps) {
  const { lang, t } = useLanguage()
  const summary = useMemo(() => pressureSummary(events, symbol), [events, symbol])

  if (loading) return <p className="py-6 text-center text-xs text-app-text-muted">…</p>
  if (summary.top.length === 0)
    return <p className="py-6 text-center text-xs text-app-text-muted">{t.whyMovingEmpty}</p>

  // Gauge scale: |net| of ~2 (a handful of strong fresh events) fills the bar.
  const frac = Math.min(Math.abs(summary.net) / 2, 1)
  const netColor = summary.net >= 0 ? POS : NEG

  return (
    <div className="flex flex-col gap-3">
      {/* Net pressure gauge: bar grows from the centre, green right / red left */}
      <div>
        <div className="mb-1 flex items-baseline justify-between text-[0.7rem]">
          <span className="font-medium uppercase tracking-wide text-app-text-muted">{t.whyMovingNet}</span>
          <span className="font-mono font-semibold tabular-nums" style={{ color: netColor }}>
            {summary.net >= 0 ? "+" : ""}{summary.net.toFixed(2)}
          </span>
        </div>
        <div className="relative h-2 overflow-hidden rounded-full bg-app-border/40">
          <div className="absolute inset-y-0 left-1/2 w-px bg-app-border" />
          <div
            className="absolute inset-y-0 rounded-full"
            style={{
              background: netColor,
              left: summary.net >= 0 ? "50%" : `${50 - frac * 50}%`,
              width: `${frac * 50}%`,
            }}
          />
        </div>
        <div className="mt-1 text-[0.68rem] text-app-text-muted">
          <span style={{ color: POS }}>{summary.up}</span> {t.whyMovingUp}
          {" · "}
          <span style={{ color: NEG }}>{summary.down}</span> {t.whyMovingDown}
        </div>
      </div>

      {/* Top contributing events */}
      <ul className="flex flex-col gap-2">
        {summary.top.map(({ event, weight }) => (
          <li key={event.id} className="rounded-md border border-app-border bg-app-bg/40 p-2">
            <div className="flex items-start justify-between gap-2">
              <p className="min-w-0 text-[0.75rem] leading-snug text-app-text-primary">
                {lang === "ar" && event.title_ar ? event.title_ar : event.title}
              </p>
              <span
                className="shrink-0 font-mono text-[0.72rem] font-semibold tabular-nums"
                style={{ color: weight >= 0 ? POS : NEG }}
              >
                {weight >= 0 ? "+" : ""}{weight.toFixed(2)}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[0.65rem] text-app-text-muted">
              <span style={{ color: categoryColor(event.category) }}>
                {categoryLabel(lang, event.category)}
              </span>
              {event.sub_categories?.[0] && (
                <span className="rounded bg-app-border/40 px-1 py-px">{event.sub_categories[0]}</span>
              )}
              <span>
                {event.article_count} {t.whyMovingArticles}
              </span>
              {lang === "ar" && event.location_name_ar ? (
                <span>{event.location_name_ar}</span>
              ) : (
                event.location_name && <span>{event.location_name}</span>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
