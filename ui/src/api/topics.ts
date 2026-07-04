import type { Topic, TopicsResponse } from "../types"
import constants from "@/constants"

const BASE = constants.API_BASE

export async function fetchTopics(params: {
  active?: boolean
  current?: boolean
  top_level?: boolean
  category?: string
  month?: number
  year?: number
} = {}): Promise<Topic[]> {
  const p = new URLSearchParams()
  if (params.active !== undefined) p.set("active", params.active ? "true" : "false")
  if (params.current !== undefined) p.set("current", params.current ? "true" : "false")
  if (params.top_level !== undefined) p.set("top_level", params.top_level ? "true" : "false")
  if (params.category) p.set("category", params.category)
  if (params.month) p.set("month", String(params.month))
  if (params.year) p.set("year", String(params.year))

  const res = await fetch(`${BASE}/topics/?${p}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  const data = await res.json() as TopicsResponse
  return data.results
}
