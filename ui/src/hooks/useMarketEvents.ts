import { useEffect, useState } from "react"
import { fetchEvents } from "../api/events"
import type { EventSummary } from "../types"

interface MarketEventsState {
  events: EventSummary[]
  loading: boolean
}

function useEventsQuery(days: number, symbol?: string): MarketEventsState {
  // loading covers the initial fetch; on query change the previous events remain
  // visible until the new response lands (state is only set from the callbacks).
  const [state, setState] = useState<MarketEventsState>({ events: [], loading: true })

  useEffect(() => {
    let cancelled = false
    fetchEvents({
      start: new Date(Date.now() - days * 86400_000).toISOString(),
      limit: 500,
      ...(symbol ? { symbol } : {}),
    })
      .then((r) => { if (!cancelled) setState({ events: r.results, loading: false }) })
      .catch(() => { if (!cancelled) setState({ events: [], loading: false }) })
    return () => { cancelled = true }
  }, [days, symbol])

  return state
}

/** Recent routed events for one market symbol (drives WhyMoving, PressurePane, chart markers). */
export function useSymbolEvents(symbol: string, days: number): MarketEventsState {
  return useEventsQuery(days, symbol)
}

/** Recent events across the whole panel (drives CauseEffectGraph + the news-pressure gauge). */
export function usePanelEvents(days: number): MarketEventsState {
  return useEventsQuery(days)
}
