"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { SiteHeader } from "../components/layout";
import PriceTicker from "../components/events/PriceTicker";
import ForecastPanel from "../components/events/ForecastPanel";
import IndicatorsLineChart from "../components/markets/IndicatorsLineChart";
import CauseEffectGraph from "../components/markets/CauseEffectGraph";
import SymbolDetail from "../components/markets/SymbolDetail";
import MoversStrip from "../components/markets/MoversStrip";
import { useSSE } from "../hooks/useSSE";
import { useLanguage } from "../contexts/LanguageContext";
import type { UIStrings } from "../i18n/strings";
import type { StreamKey } from "../types";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { symbolStreamKey } from "../lib/symbols";

interface Card {
  title: string;
  children: React.ReactNode;
}

function Panel({ title, children }: Card) {
  return (
    <section className="flex min-w-0 flex-col overflow-hidden rounded-lg border border-app-border bg-app-surface">
      <header className="shrink-0 border-b border-app-border px-3 py-2 text-[0.8rem] font-semibold text-app-text-heading">
        {title}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">{children}</div>
    </section>
  );
}

// Date-range options for the time-based panels: number of calendar days back from today.
const RANGES: { key: keyof UIStrings; days: number }[] = [
  { key: "range1d", days: 1 },
  { key: "range1w", days: 7 },
  { key: "range1m", days: 30 },
  { key: "range3m", days: 90 },
  { key: "range1y", days: 365 },
  { key: "range5y", days: 1825 },
];

function RangeSelector({
  value,
  onChange,
}: {
  value: number;
  onChange: (days: number) => void;
}) {
  const { t } = useLanguage();
  return (
    <div className="flex items-center gap-2">
      <span className="text-[0.7rem] font-medium uppercase tracking-wide text-app-text-muted">
        {t.rangeLabel}
      </span>
      <div className="inline-flex overflow-hidden rounded-md border border-app-border bg-app-surface">
        {RANGES.map((r) => {
          const active = r.days === value;
          return (
            <button
              key={r.days}
              type="button"
              onClick={() => onChange(r.days)}
              aria-pressed={active}
              className={
                "px-2.5 py-1 text-[0.72rem] font-semibold transition-colors " +
                (active
                  ? "bg-app-accent-blue text-white"
                  : "text-app-text-muted hover:bg-app-border/40 hover:text-app-text-primary")
              }
            >
              {t[r.key] as string}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function MarketsPage() {
  const { t } = useLanguage();
  useDocumentTitle(t.navMarkets);

  // Cross-link from an event's affected-indicator chip: /markets?symbol=GC=F
  const [params] = useSearchParams();
  const symbolParam = params.get("symbol");
  const sk = symbolParam ? symbolStreamKey(symbolParam) : null;
  const focusSymbol = symbolParam && sk ? { symbol: symbolParam, streamKey: sk } : null;

  // Page-level date range (calendar days back from today) driving the time-based panels.
  const [rangeDays, setRangeDays] = useState(90);

  // Master-detail selection: the watchlist / movers strip drive the central chart.
  const [selected, setSelected] = useState<{ symbol: string; streamKey: StreamKey; name?: string }>(
    symbolParam && sk
      ? { symbol: symbolParam, streamKey: sk }
      : { symbol: "BTC-USD", streamKey: "crypto", name: "Bitcoin" },
  );

  // Honour a later ?symbol= cross-link navigation.
  useEffect(() => {
    if (symbolParam && sk) setSelected({ symbol: symbolParam, streamKey: sk });
  }, [symbolParam, sk]);

  const handleSelect = (symbol: string, streamKey: StreamKey, name: string) =>
    setSelected({ symbol, streamKey, name });

  const [latestPriceTick, setLatestPriceTick] = useState<{
    symbol: string;
    value: number;
    change_pct: number | null;
    occurred_at: string;
  } | null>(null);

  useSSE((event) => {
    if (event.type === "price_tick") {
      setLatestPriceTick({
        symbol: event.symbol as string,
        value: event.value as number,
        change_pct: event.change_pct as number | null,
        occurred_at: event.occurred_at as string,
      });
    }
  });

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-app-bg text-app-text-primary">
      <SiteHeader activePage="markets" />

      <main className="min-h-0 flex-1 overflow-y-auto p-4 lg:p-6">
        <div className="mx-auto flex max-w-[1800px] flex-col gap-4">
          {/* Page toolbar: title + global date-range selector */}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h1 className="text-lg font-semibold text-app-text-heading">{t.navMarkets}</h1>
            <RangeSelector value={rangeDays} onChange={setRangeDays} />
          </div>

          {/* Movers summary strip */}
          <MoversStrip onSelect={handleSelect} selectedSymbol={selected.symbol} />

          {/* Master-detail dashboard: watchlist | chart + insights | forecasts */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[320px_minmax(0,1fr)] xl:grid-cols-[320px_minmax(0,1fr)_360px]">
            {/* Watchlist */}
            <div className="flex min-w-0 flex-col">
              <Panel title={t.watchlist}>
                <PriceTicker
                  latestTick={latestPriceTick}
                  focusSymbol={focusSymbol}
                  selectedSymbol={selected.symbol}
                  onSelectSymbol={handleSelect}
                />
              </Panel>
            </div>

            {/* Center: focused symbol chart + indicator relationships */}
            <div className="flex min-w-0 flex-col gap-4">
              <SymbolDetail
                symbol={selected.symbol}
                streamKey={selected.streamKey}
                name={selected.name}
                days={rangeDays}
              />
              <Panel title={t.indicatorsCompare}>
                <IndicatorsLineChart days={rangeDays} />
              </Panel>
              <Panel title={t.causeEffectTitle}>
                <p className="mb-3 text-[0.7rem] leading-snug text-app-text-muted">{t.causeEffectNote}</p>
                <CauseEffectGraph days={rangeDays} />
              </Panel>
            </div>

            {/* Forecasts & insights — drops below center on md, sidebar on xl */}
            <div className="flex min-w-0 flex-col lg:col-span-2 xl:col-span-1">
              <Panel title={t.marketForecasts}>
                <ForecastPanel embedded />
              </Panel>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
