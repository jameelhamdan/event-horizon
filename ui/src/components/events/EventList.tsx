'use client'

import { useRef, useEffect } from "react"
import EventCard from "./EventCard"
import StatusDisplay from "../StatusDisplay"
import type { EventSummary } from "../../types"
import { useLanguage } from "../../contexts/LanguageContext"

interface EventListProps {
  events: EventSummary[]
  selectedId: string | null
  onSelectEvent: (id: string) => void
  onTopicClick?: (slug: string) => void
  onSymbolClick?: (symbol: string) => void
  activeTopic?: string | null
}

function dayHeader(day: string, lang: string): string {
  if (day === "?") return "—"
  return new Date(day + "T00:00:00").toLocaleDateString(lang === "ar" ? "ar" : "en", {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
  })
}

export default function EventList({ events, selectedId, onSelectEvent, onTopicClick, onSymbolClick, activeTopic }: EventListProps) {
  const { t, lang } = useLanguage()
  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({})

  useEffect(() => {
    if (selectedId && cardRefs.current[selectedId]) {
      cardRefs.current[selectedId]!.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      })
    }
  }, [selectedId])

  if (events.length === 0) {
    return <StatusDisplay status="empty" message={t.noEventsFiltered} />
  }

  // F4 timeline: group consecutive events by day (backend orders by started_at DESC).
  const groups: { day: string; items: EventSummary[] }[] = []
  for (const ev of events) {
    const day = ev.started_at ? new Date(ev.started_at).toISOString().slice(0, 10) : "?"
    const last = groups[groups.length - 1]
    if (last && last.day === day) last.items.push(ev)
    else groups.push({ day, items: [ev] })
  }

  return (
    <div className="flex flex-col">
      {groups.map((g) => (
        <div key={g.day}>
          <div className="sticky top-0 z-[5] border-b border-app-border bg-app-surface px-3 py-1 text-[0.65rem] uppercase tracking-[0.05em] text-app-text-ghost">
            {dayHeader(g.day, lang)}
          </div>
          {g.items.map((ev) => (
            <div
              key={ev.id}
              ref={(el) => {
                cardRefs.current[ev.id] = el
              }}
            >
              <EventCard event={ev} selected={selectedId === ev.id} onSelect={onSelectEvent} onTopicClick={onTopicClick} onSymbolClick={onSymbolClick} activeTopic={activeTopic} />
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
