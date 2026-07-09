// News-pressure math shared by WhyMoving, PressurePane, and the header gauge.
// The decay constant (3 days) mirrors the forecasting feature builder's
// evw_decay_7d (services/forecasting/features.py: weight * exp(-age_days / 3)),
// so what users see is the same signal the model sees.
import type { EventSummary } from "../types"

export const DECAY_DAYS = 3

/** Event time for as-of math: latest article beats the day bucket. */
export function eventTime(e: EventSummary): number {
  return new Date(e.latest_article_at ?? e.started_at).getTime()
}

export function symbolWeight(e: EventSummary, symbol: string): number {
  const hit = (e.affected_indicators ?? []).find((i) => i.symbol === symbol)
  return hit?.weight ?? 0
}

export interface EventContribution {
  event: EventSummary
  weight: number
  decayed: number
}

/** Per-event decayed contributions to one symbol, newest-first by |decayed|. */
export function contributions(events: EventSummary[], symbol: string, now = Date.now()): EventContribution[] {
  const out: EventContribution[] = []
  for (const event of events) {
    const weight = symbolWeight(event, symbol)
    if (weight === 0) continue
    const ageDays = Math.max(0, (now - eventTime(event)) / 86400_000)
    out.push({ event, weight, decayed: weight * Math.exp(-ageDays / DECAY_DAYS) })
  }
  return out.sort((a, b) => Math.abs(b.decayed) - Math.abs(a.decayed))
}

export interface PressureSummary {
  net: number
  up: number
  down: number
  top: EventContribution[]
}

export function pressureSummary(events: EventSummary[], symbol: string, topN = 3): PressureSummary {
  const contribs = contributions(events, symbol)
  let net = 0
  let up = 0
  let down = 0
  for (const c of contribs) {
    net += c.decayed
    if (c.weight > 0) up += 1
    else down += 1
  }
  return { net, up, down, top: contribs.slice(0, topN) }
}

export interface DailyPressure {
  /** ms timestamp of the day (UTC midnight) */
  t: number
  net: number
  sentiment: number | null
  count: number
}

/** Daily net routed weight + mean FinBERT sentiment for one symbol. */
export function dailyPressure(events: EventSummary[], symbol: string): DailyPressure[] {
  const days = new Map<number, { net: number; senti: number; sentiN: number; count: number }>()
  for (const e of events) {
    const weight = symbolWeight(e, symbol)
    if (weight === 0) continue
    const t = new Date(eventTime(e)).setUTCHours(0, 0, 0, 0)
    const d = days.get(t) ?? { net: 0, senti: 0, sentiN: 0, count: 0 }
    d.net += weight
    d.count += 1
    if (e.avg_finbert_sentiment != null) {
      d.senti += e.avg_finbert_sentiment
      d.sentiN += 1
    }
    days.set(t, d)
  }
  return [...days.entries()]
    .map(([t, d]) => ({ t, net: d.net, sentiment: d.sentiN ? d.senti / d.sentiN : null, count: d.count }))
    .sort((a, b) => a.t - b.t)
}
