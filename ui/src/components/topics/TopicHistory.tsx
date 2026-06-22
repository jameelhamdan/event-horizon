'use client'

import { useEffect, useState } from "react"
import { fetchTopics } from "../../api/topics"
import { useLanguage } from "../../contexts/LanguageContext"
import type { Topic } from "../../types"
import { cn } from "@/lib/utils"

const NOW = new Date()
const YEARS = Array.from({ length: 6 }, (_, i) => NOW.getFullYear() - i)

interface TopicHistoryProps {
  onTopicClick?: (slug: string) => void
  activeTopic?: string | null
}

// F4: browse historical topics by month/year via /api/topics/?year=&month=.
export default function TopicHistory({ onTopicClick, activeTopic }: TopicHistoryProps) {
  const { t, lang } = useLanguage()
  const [open, setOpen] = useState(false)
  const [year, setYear] = useState(NOW.getFullYear())
  const [month, setMonth] = useState(0) // 0 = all months
  const [topics, setTopics] = useState<Topic[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    fetchTopics({ year, ...(month ? { month } : {}) })
      .then((data) => { if (!cancelled) setTopics(data) })
      .catch(() => { if (!cancelled) setTopics([]) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [open, year, month])

  const monthNames = Array.from({ length: 12 }, (_, i) =>
    new Date(2000, i, 1).toLocaleDateString(lang === "ar" ? "ar" : "en", { month: "short" }))

  const selectCls =
    "rounded border border-app-border bg-transparent px-1 py-[0.1rem] text-[0.7rem] text-app-text-muted [color-scheme:dark]"

  return (
    <div className="border-b border-app-border">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full cursor-pointer items-center gap-2 border-none bg-transparent px-3 py-2 text-[0.68rem] uppercase tracking-[0.06em] text-app-text-ghost"
      >
        <span>🕘</span>
        <span className="flex-1 text-left">{t.topicHistoryTitle}</span>
        <span>{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div className="px-3 pb-2">
          <div className="mb-2 flex gap-2">
            <select value={year} onChange={(e) => setYear(Number(e.target.value))} className={selectCls}>
              {YEARS.map((y) => <option key={y} value={y}>{y}</option>)}
            </select>
            <select value={month} onChange={(e) => setMonth(Number(e.target.value))} className={selectCls}>
              <option value={0}>{t.allMonths}</option>
              {monthNames.map((m, i) => <option key={i} value={i + 1}>{m}</option>)}
            </select>
          </div>

          {loading ? (
            <div className="text-[0.72rem] text-app-text-dim">{t.loading}</div>
          ) : topics.length === 0 ? (
            <div className="text-[0.72rem] text-app-text-dim">{t.topicHistoryEmpty}</div>
          ) : (
            <div className="flex flex-wrap gap-1">
              {topics.slice(0, 40).map((tp) => {
                const active = activeTopic === tp.slug
                return (
                  <button
                    key={tp.slug}
                    onClick={() => onTopicClick?.(tp.slug)}
                    title={tp.description ?? tp.name}
                    className={cn(
                      "cursor-pointer rounded-full border px-[0.45rem] py-[0.1rem] text-[0.66rem] leading-[1.4] transition-colors",
                      active
                        ? "border-[#7c9ef8] bg-[#1e2540] text-[#7c9ef8]"
                        : "border-[#2a2a44] bg-[#1a1a2e] text-[#8888aa] hover:border-[#7c9ef8] hover:text-[#7c9ef8]",
                    )}
                  >
                    {tp.name}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
