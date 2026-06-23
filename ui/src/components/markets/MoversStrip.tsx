'use client'

import { useEffect, useMemo, useState } from "react"
import { fetchPricesLatest } from "../../api/streams"
import { useLanguage } from "../../contexts/LanguageContext"
import type { PriceTick, StreamKey } from "../../types"

const POLL_INTERVAL_MS = 60_000

interface MoversStripProps {
  onSelect: (symbol: string, streamKey: StreamKey, name: string) => void
  selectedSymbol?: string | null
}

interface Group {
  label: string
  ticks: PriceTick[]
}

// Top-of-dashboard summary strip: biggest gainers / losers / most volatile across every stream,
// from the latest tick per symbol. Each chip cross-selects the symbol into the central chart.
export default function MoversStrip({ onSelect, selectedSymbol }: MoversStripProps) {
  const { t } = useLanguage()
  const [ticks, setTicks] = useState<PriceTick[]>([])

  useEffect(() => {
    let cancelled = false
    const load = () =>
      fetchPricesLatest()
        .then((d) => { if (!cancelled) setTicks(d.results) })
        .catch(() => {})
    load()
    const id = setInterval(load, POLL_INTERVAL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  const groups = useMemo<Group[]>(() => {
    const withChange = ticks.filter((tk) => tk.change_pct != null)
    const byChangeDesc = [...withChange].sort((a, b) => (b.change_pct ?? 0) - (a.change_pct ?? 0))
    const byVolatility = [...withChange].sort(
      (a, b) => Math.abs(b.change_pct ?? 0) - Math.abs(a.change_pct ?? 0),
    )
    return [
      { label: t.moversGainers, ticks: byChangeDesc.slice(0, 4) },
      { label: t.moversLosers, ticks: byChangeDesc.slice(-4).reverse() },
      { label: t.moversVolatile, ticks: byVolatility.slice(0, 4) },
    ]
  }, [ticks, t])

  if (ticks.length === 0) return null

  return (
    <div className="flex flex-wrap gap-x-6 gap-y-2 rounded-lg border border-app-border bg-app-surface px-4 py-2.5">
      {groups.map((g) => (
        <div key={g.label} className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 text-[0.62rem] font-semibold uppercase tracking-wide text-app-text-muted">
            {g.label}
          </span>
          <div className="flex flex-wrap gap-1.5">
            {g.ticks.map((tk) => {
              const pct = tk.change_pct ?? 0
              const up = pct >= 0
              const active = selectedSymbol === tk.symbol
              return (
                <button
                  key={`${g.label}-${tk.symbol}`}
                  type="button"
                  onClick={() => onSelect(tk.symbol, tk.stream_key, tk.name)}
                  className={
                    "flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[0.7rem] transition-colors " +
                    (active
                      ? "border-app-accent-blue bg-app-accent-blue/10"
                      : "border-app-border hover:border-app-border-subtle")
                  }
                >
                  <span className="font-mono font-semibold text-app-text-primary">{tk.symbol}</span>
                  <span
                    className="font-mono tabular-nums"
                    style={{ color: up ? "#52c8a0" : "#e05252" }}
                  >
                    {up ? "+" : ""}{pct.toFixed(2)}%
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}
