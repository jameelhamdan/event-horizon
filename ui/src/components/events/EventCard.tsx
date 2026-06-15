"use client"

import { useState } from "react"
import { fetchEventDetail } from "../../api/events"
import { categoryColor } from "@/components/category"
import { timeAgo, CategoryBadge, EventMeta, useLocalizedField } from "./EventUI"
import { useLanguage } from "../../contexts/LanguageContext"
import { subCategoryLabel } from "../../i18n/categories"
import type { EventSummary, Article } from "../../types"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"

interface EventCardProps {
  event: EventSummary
  selected: boolean
  onSelect: (id: string) => void
  onTopicClick?: (slug: string) => void
  activeTopic?: string | null
}

export default function EventCard({ event, selected, onSelect, onTopicClick, activeTopic }: EventCardProps) {
  const { lang, t } = useLanguage()
  const pick = useLocalizedField()
  const [articles, setArticles] = useState<Article[] | null>(null)
  const [loadingArticles, setLoadingArticles] = useState(false)

  const color = categoryColor(event.category)
  const sourceNameMap = Object.fromEntries(
    (event.source_codes ?? []).map((code, i) => [code, event.source_names?.[i] ?? code])
  )

  async function toggleArticles(e: React.MouseEvent) {
    e.stopPropagation()
    if (articles) {
      setArticles(null)
      return
    }
    setLoadingArticles(true)
    try {
      const detail = await fetchEventDetail(event.id)
      setArticles(detail.articles ?? [])
    } finally {
      setLoadingArticles(false)
    }
  }

  return (
    <div
      onClick={() => onSelect(event.id)}
      className={cn("event-card", selected ? "bg-app-card-selected" : "bg-app-card")}
      style={{ "--cat-color": color } as React.CSSProperties}
    >
      <div className="mb-[0.35rem] flex items-center justify-between">
        <div className="flex flex-wrap items-center gap-[0.35rem]">
          <CategoryBadge category={event.category} />
          {event.sub_categories?.map((sub) => (
            <span key={sub} className="sub-cat-tag">
              {subCategoryLabel(lang, sub)}
            </span>
          ))}
        </div>
        <span className="text-[0.75rem] text-app-text-ghost">
          {timeAgo(event.started_at, lang)}
        </span>
      </div>

      <div className="mb-[0.4rem] text-[0.9rem] font-medium leading-[1.35] text-app-text-body">
        {pick(event as unknown as Record<string, unknown>, "title")}
      </div>

      <div className="mb-[0.4rem]">
        <EventMeta event={event} />
      </div>

      {event.topic_slugs && event.topic_slugs.length > 0 && (
        <div className="mb-[0.4rem] flex flex-wrap gap-[0.25rem]">
          {event.topic_slugs.slice(0, 3).map((slug) => (
            <button
              key={slug}
              onClick={(e) => {
                e.stopPropagation()
                onTopicClick?.(slug)
              }}
              className={cn(
                "rounded-full border px-[0.45rem] py-[0.1rem] text-[0.68rem] leading-[1.4] transition-colors duration-100",
                activeTopic === slug
                  ? "border-[#7c9ef8] bg-[#1e2540] text-[#7c9ef8]"
                  : "border-[#2a2a44] bg-[#1a1a2e] text-[#8888aa] hover:border-[#7c9ef8] hover:text-[#7c9ef8]",
              )}
            >
              {slug}
            </button>
          ))}
          {event.topic_slugs.length > 3 && (
            <span className="rounded-full border border-[#2a2a44] px-[0.45rem] py-[0.1rem] text-[0.68rem] leading-[1.4] text-[#666677]">
              +{event.topic_slugs.length - 3}
            </span>
          )}
        </div>
      )}

      {event.affected_indicators && event.affected_indicators.length > 0 && (
        <div className="mb-[0.4rem] flex flex-wrap items-center gap-[0.3rem]">
          <span className="text-[0.62rem] uppercase tracking-[0.04em] text-app-text-ghost">
            {t.affectedIndicators}
          </span>
          {event.affected_indicators
            .slice()
            .sort((a, b) => Math.abs(b.weight) - Math.abs(a.weight))
            .slice(0, 4)
            .map((ind) => {
              const positive = ind.weight >= 0
              return (
                <span
                  key={ind.symbol}
                  title={`${ind.symbol}: ${ind.weight.toFixed(2)}`}
                  className="rounded border px-[0.4rem] py-[0.05rem] font-mono text-[0.64rem] leading-[1.4]"
                  style={{
                    color: positive ? "#52c8a0" : "#e05252",
                    borderColor: positive ? "#2a4a3e" : "#4a2a2e",
                    background: positive ? "#16241e" : "#241616",
                  }}
                >
                  {ind.symbol} {positive ? "+" : ""}{ind.weight.toFixed(2)}
                </span>
              )
            })}
        </div>
      )}

      <Button
        onClick={toggleArticles}
        variant="link"
        className="h-auto p-0 text-[0.75rem] text-app-accent-blue"
      >
        {loadingArticles ? t.loading : articles ? t.hideArticles : t.showArticles}
      </Button>

      {articles && (
        <ul className="mt-2 list-none border-l-2 border-app-border-subtle pl-2">
          {articles.map((a) => (
            <li key={a.id} className="mb-2 flex flex-col">
              <a
                href={a.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[0.82rem] text-app-accent-blue no-underline"
              >
                {pick(a as unknown as Record<string, unknown>, "title")}
              </a>
              <span className="text-[0.73rem] text-app-text-ghost">
                {sourceNameMap[a.source_code] ?? a.source_code} · {new Date(a.published_on).toLocaleString()}
              </span>
            </li>
          ))}
          {articles.length === 0 && (
            <li className="text-[0.8rem] text-app-text-ghost">{t.noEvents}</li>
          )}
        </ul>
      )}
    </div>
  )
}
