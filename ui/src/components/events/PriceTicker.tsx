'use client'

import { useState, useEffect } from "react"
import { fetchPricesLatest } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import type { PriceTick, StreamKey } from "../../types"
import { cn } from "@/lib/utils"

const STREAM_KEYS: StreamKey[] = ["stock", "crypto", "commodity", "forex", "bond", "index"]

function changeColorClass(pct: number | null): string {
  if (pct == null) return "text-app-text-muted"
  return pct >= 0 ? "text-app-accent-green" : "text-app-accent-red"
}

function formatValue(value: number, streamKey: StreamKey): string {
  if (streamKey === "forex") return value.toFixed(4)
  if (value >= 1000) return value.toLocaleString("en-US", { maximumFractionDigits: 2 })
  if (value >= 1) return value.toFixed(2)
  return value.toFixed(4)
}

interface PriceRowProps {
  tick: PriceTick
  flash: boolean
}

function PriceRow({ tick, flash }: PriceRowProps) {
  return (
    <div className={cn("price-row", flash && "price-row-flash")}>
      <span className="w-[72px] shrink-0 overflow-hidden text-ellipsis whitespace-nowrap text-[0.72rem] font-semibold tracking-[0.01em] text-app-text-body-dim">
        {tick.symbol}
      </span>
      <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[0.68rem] text-app-text-muted">
        {tick.name}
      </span>
      <span className="w-[80px] shrink-0 text-right font-mono text-[0.76rem] font-semibold tabular-nums text-app-text-primary">
        {formatValue(tick.value, tick.stream_key)}
      </span>
      <span className={cn("w-[56px] shrink-0 text-right font-mono text-[0.7rem] font-medium tabular-nums", changeColorClass(tick.change_pct))}>
        {tick.change_pct != null
          ? `${tick.change_pct >= 0 ? "+" : ""}${tick.change_pct.toFixed(2)}%`
          : "—"}
      </span>
    </div>
  )
}

interface PriceTickerProps {
  latestTick: {
    symbol: string
    value: number
    change_pct: number | null
    occurred_at: string
  } | null
}

export default function PriceTicker({ latestTick }: PriceTickerProps) {
  const { t } = useLanguage()
  const [activeKey, setActiveKey] = useState<StreamKey>("crypto")
  const [ticks, setTicks] = useState<PriceTick[]>([])
  const [loading, setLoading] = useState(true)
  const [flashedSymbols, setFlashedSymbols] = useState<Set<string>>(new Set())

  useEffect(() => {
    setLoading(true)
    fetchPricesLatest(activeKey)
      .then((data) => setTicks(data.results))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [activeKey])

  useEffect(() => {
    if (!latestTick) return
    setTicks((prev) =>
      prev.map((tk) =>
        tk.symbol === latestTick.symbol
          ? { ...tk, value: latestTick.value, change_pct: latestTick.change_pct, occurred_at: latestTick.occurred_at }
          : tk,
      ),
    )
    setFlashedSymbols((prev) => new Set([...prev, latestTick.symbol]))
    const timer = setTimeout(() => {
      setFlashedSymbols((prev) => {
        const next = new Set(prev)
        next.delete(latestTick.symbol)
        return next
      })
    }, 600)
    return () => clearTimeout(timer)
  }, [latestTick])

  return (
    <div className="flex flex-col border-b border-app-border">
      <div className="border-b border-app-border-mid px-3 pt-2">
        <div className="mb-[0.35rem] text-[0.68rem] font-semibold uppercase tracking-[0.06em] text-app-text-ghost">
          {t.markets}
        </div>
        <div className="flex gap-[0.2rem] overflow-x-auto [scrollbar-width:none]">
          {STREAM_KEYS.map((key) => (
            <button
              key={key}
              onClick={() => setActiveKey(key)}
              className={cn("price-tab", activeKey === key ? "price-tab-active" : "price-tab-inactive")}
            >
              {t.streamKeys[key]}
            </button>
          ))}
        </div>
      </div>

      <div className="flex gap-2 border-b border-app-border-dim px-3 py-[0.22rem]">
        <span className="w-[72px] shrink-0 text-[0.62rem] uppercase tracking-[0.04em] text-app-text-dim">
          {t.symbolCol}
        </span>
        <span className="flex-1" />
        <span className="w-[80px] shrink-0 text-right text-[0.62rem] uppercase tracking-[0.04em] text-app-text-dim">
          {t.priceCol}
        </span>
        <span className="w-[56px] shrink-0 text-right text-[0.62rem] uppercase tracking-[0.04em] text-app-text-dim">
          {t.changeCol}
        </span>
      </div>

      <div className="max-h-[200px] overflow-y-auto">
        {loading ? (
          <div className="min-h-[200px] px-3 py-3 text-[0.72rem] text-app-text-dim">
            {t.loading}
          </div>
        ) : ticks.length === 0 ? (
          <div className="px-3 py-3 text-[0.72rem] text-app-text-dim">
            {t.noDataYet}
          </div>
        ) : (
          ticks.map((tk) => (
            <PriceRow key={tk.id} tick={tk} flash={flashedSymbols.has(tk.symbol)} />
          ))
        )}
      </div>
    </div>
  )
}
