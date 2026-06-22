"use client";

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { SiteHeader } from "../components/layout";
import PriceTicker from "../components/events/PriceTicker";
import ForecastPanel from "../components/events/ForecastPanel";
import EventsHeatmap from "../components/markets/EventsHeatmap";
import IndicatorsLineChart from "../components/markets/IndicatorsLineChart";
import CauseEffectGraph from "../components/markets/CauseEffectGraph";
import { useSSE } from "../hooks/useSSE";
import { useLanguage } from "../contexts/LanguageContext";
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

export default function MarketsPage() {
  const { t } = useLanguage();
  useDocumentTitle(t.navMarkets);

  // Cross-link from an event's affected-indicator chip: /markets?symbol=GC=F
  const [params] = useSearchParams();
  const symbolParam = params.get("symbol");
  const sk = symbolParam ? symbolStreamKey(symbolParam) : null;
  const focusSymbol = symbolParam && sk ? { symbol: symbolParam, streamKey: sk } : null;

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

      <main className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto grid max-w-6xl grid-cols-1 gap-4 lg:grid-cols-[minmax(0,400px)_1fr]">
          {/* Left column: live markets + forecasts */}
          <div className="flex flex-col gap-4">
            <Panel title={t.tabMarkets}>
              <PriceTicker latestTick={latestPriceTick} focusSymbol={focusSymbol} />
            </Panel>
            <Panel title={t.marketForecasts}>
              <ForecastPanel embedded />
            </Panel>
          </div>

          {/* Right column: indicator relationships + weighted event→market views */}
          <div className="flex flex-col gap-4">
            <Panel title={t.indicatorsCompare}>
              <IndicatorsLineChart />
            </Panel>
            <Panel title={t.causeEffectTitle}>
              <p className="mb-3 text-[0.7rem] leading-snug text-app-text-muted">{t.causeEffectNote}</p>
              <CauseEffectGraph days={7} />
            </Panel>
            <Panel title={t.eventsImpactHeatmap}>
              <EventsHeatmap days={7} />
            </Panel>
          </div>
        </div>
      </main>
    </div>
  );
}
