'use client'

import { useMemo } from "react"
import {
  ResponsiveContainer, ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, CartesianGrid, ReferenceLine, Cell,
} from "recharts"
import { useLanguage } from "../../contexts/LanguageContext"
import { dailyPressure } from "../../lib/pressure"
import type { EventSummary } from "../../types"

const POS = "#52c8a0"
const NEG = "#e05252"
const SENTI = "#7c9ef8"

const CAP_DAYS = 90

interface PressurePaneProps {
  symbol: string
  events: EventSummary[]
  loading: boolean
  /** Page range in days; event data upstream is capped at CAP_DAYS. */
  days: number
}

// News-pressure vs price: daily net routed weight (± bars) + FinBERT sentiment (line) on the
// same timeline as the price chart above (shared recharts syncId). The visual answer to
// "does news pressure line up with the price move?".
export default function PressurePane({ symbol, events, loading, days }: PressurePaneProps) {
  const { t, lang } = useLanguage()
  const data = useMemo(() => dailyPressure(events, symbol), [events, symbol])

  const locale = lang === "ar" ? "ar" : "en"
  const fmtTime = (ms: number) =>
    new Date(ms).toLocaleDateString(locale, { month: "numeric", day: "numeric" })

  if (loading) return <p className="py-6 text-center text-xs text-app-text-muted">…</p>
  if (data.length === 0)
    return <p className="py-6 text-center text-xs text-app-text-muted">{t.pressureEmpty}</p>

  return (
    <div className="flex flex-col gap-1">
      <ResponsiveContainer width="100%" height={140}>
        <ComposedChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 8 }} syncId="market-detail" syncMethod="value">
          <CartesianGrid stroke="#20202a" vertical={false} />
          <XAxis
            dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]}
            tickFormatter={fmtTime} tick={{ fontSize: 10, fill: "#666677" }} stroke="#2a2a35" minTickGap={48}
          />
          <YAxis
            yAxisId="net" orientation="right" width={56}
            tick={{ fontSize: 10, fill: "#666677" }} stroke="#2a2a35"
            tickFormatter={(v: number) => v.toFixed(1)}
          />
          <YAxis yAxisId="senti" hide domain={[-1, 1]} />
          <ReferenceLine yAxisId="net" y={0} stroke="#2a2a35" />
          <Bar yAxisId="net" dataKey="net" isAnimationActive={false} maxBarSize={8}>
            {data.map((d) => (
              <Cell key={d.t} fill={d.net >= 0 ? POS : NEG} fillOpacity={0.75} />
            ))}
          </Bar>
          <Line
            yAxisId="senti" type="monotone" dataKey="sentiment" stroke={SENTI}
            strokeWidth={1.25} dot={false} connectNulls isAnimationActive={false}
          />
          <Tooltip
            contentStyle={{ background: "#0f0f13", border: "1px solid #2a2a35", borderRadius: 6, fontSize: "0.72rem" }}
            labelStyle={{ color: "#888899" }}
            itemStyle={{ color: "#e8e8f0" }}
            labelFormatter={(ms) => fmtTime(Number(ms))}
            formatter={(val, key) => [
              typeof val === "number" ? val.toFixed(2) : String(val),
              key === "net" ? t.pressureNet : t.pressureSentiment,
            ]}
          />
        </ComposedChart>
      </ResponsiveContainer>
      <p className="px-1 text-[0.65rem] text-app-text-muted">
        <span style={{ color: POS }}>■</span>/<span style={{ color: NEG }}>■</span> {t.pressureNet}
        {" · "}
        <span style={{ color: SENTI }}>—</span> {t.pressureSentiment}
        {days > CAP_DAYS && <> · {t.pressureRangeCap}</>}
      </p>
    </div>
  )
}
