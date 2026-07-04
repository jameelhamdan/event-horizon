import type { EventsResponse, EventDetail, EventFilters } from "../types"
import constants from "@/constants";

const BASE = constants.API_BASE

export async function fetchEvents(
  filters: EventFilters = {},
): Promise<EventsResponse> {
  const params = new URLSearchParams()
  if (filters.category) params.set("category", filters.category)
  if (filters.start) params.set("start", filters.start)
  if (filters.end) params.set("end", filters.end)
  if (filters.limit) params.set("limit", String(filters.limit))
  if (filters.bbox) params.set("bbox", filters.bbox)
  if (filters.topic) params.set("topic", filters.topic)

  const res = await fetch(`${BASE}/events/?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json() as Promise<EventsResponse>
}

export async function fetchEventDetail(id: string): Promise<EventDetail> {
  const res = await fetch(`${BASE}/events/${id}/`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json() as Promise<EventDetail>
}

