"use client";

import { useState, useEffect, useCallback, lazy, Suspense } from "react";
import EventList from "../components/events/EventList";
import { fetchEvents } from "../api/events";
import { useSSE } from "../hooks/useSSE";
import { useNavigate } from "react-router-dom";
import { SiteHeader } from "../components/layout";
import { categoryColor, categoryShapeComponent } from "@/components/category";
import { useLanguage } from "../contexts/LanguageContext";
import { categoryLabel } from "../i18n/categories";
import { fetchTopics } from "../api/topics";
import TopicHistory from "../components/topics/TopicHistory";
import type { EventSummary, EventFilters, Topic } from "../types";
import { cn } from "@/lib/utils";
import { useDocumentTitle } from "../hooks/useDocumentTitle";

const MapView = lazy(() => import("../components/events/MapView"));
const POLL_INTERVAL_MS = 60_000;

const CATEGORY_TABS = [
  { value: "", label: "All" },
  { value: "conflict", label: "Conflict" },
  { value: "protest", label: "Protest" },
  { value: "disaster", label: "Disaster" },
  { value: "political", label: "Political" },
  { value: "economic", label: "Economic" },
  { value: "crime", label: "Crime" },
  { value: "general", label: "General" },
] as const;

const QUICK_FILTERS = [
  { value: "6h", label: "6h", ms: 6 * 60 * 60 * 1000 },
  { value: "24h", label: "24h", ms: 24 * 60 * 60 * 1000 },
  { value: "7d", label: "7d", ms: 7 * 24 * 60 * 60 * 1000 },
  { value: "30d", label: "30d", ms: 30 * 24 * 60 * 60 * 1000 },
] as const;

const OVERLAY_CONTROLS = [
  { key: "notams", color: "#ff6644" },
  { key: "earthquakes", color: "#7c6ef8" },
  { key: "staticPoints", color: "#4fc3f7" },
] as const;

type QuickFilter = (typeof QUICK_FILTERS)[number]["value"] | "";

export default function IndexPage() {
  const { lang, t } = useLanguage();
  const navigate = useNavigate();
  useDocumentTitle();

  const [events, setEvents] = useState<EventSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filters, setFilters] = useState<EventFilters>({ category: "" });
  const [activeTopic, setActiveTopic] = useState<string | null>(null);
  const [topics, setTopics] = useState<Topic[]>([]);
  const [quickFilter, setQuickFilter] = useState<QuickFilter>("");
  const [mounted, setMounted] = useState(false);

  const [isMobile, setIsMobile] = useState(false);
  const [mobileTab, setMobileTab] = useState<"map" | "list">("map");
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const [showNotams, setShowNotams] = useState(true);
  const [showEarthquakes, setShowEarthquakes] = useState(true);
  const [showStaticPoints, setShowStaticPoints] = useState(true);
  const [streamRefresh, setStreamRefresh] = useState(0);

  const overlayState = { notams: showNotams, earthquakes: showEarthquakes, staticPoints: showStaticPoints };
  const overlaySetters = {
    notams: setShowNotams,
    earthquakes: setShowEarthquakes,
    staticPoints: setShowStaticPoints,
  };
  const overlayLabels = { notams: t.notams, earthquakes: t.earthquakes, staticPoints: t.locations };

  useSSE((event) => {
    if (event.type === "notam_update" || event.type === "earthquake_update") {
      setStreamRefresh((n) => n + 1);
    }
  });

  const load = useCallback(async () => {
    try {
      let effectiveFilters = filters;
      if (quickFilter) {
        const offsetMs = QUICK_FILTERS.find((q) => q.value === quickFilter)!.ms;
        effectiveFilters = { ...filters, start: new Date(Date.now() - offsetMs).toISOString() };
      }
      if (activeTopic) effectiveFilters = { ...effectiveFilters, topic: activeTopic };
      const data = await fetchEvents(effectiveFilters);
      setEvents(data.results);
    } catch (e) {
      console.error(e);
    }
  }, [filters, quickFilter, activeTopic]);

  function handleTopicClick(slug: string) {
    setActiveTopic((prev) => (prev === slug ? null : slug));
  }

  // Cross-link: clicking an event's affected-indicator chip opens the Markets page
  // focused on that symbol.
  function handleSymbolClick(symbol: string) {
    navigate(`/markets?symbol=${encodeURIComponent(symbol)}`);
  }

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    fetchTopics({ active: true, top_level: true })
      .then((data) =>
        setTopics(
          [...data]
            .sort((a, b) => {
              if (a.is_pinned && !b.is_pinned) return -1;
              if (!a.is_pinned && b.is_pinned) return 1;
              return (b.topic_score ?? b.event_count) - (a.topic_score ?? a.event_count);
            })
            .slice(0, 8),
        ),
      )
      .catch(() => {});
  }, []);

  useEffect(() => {
    function check() {
      setIsMobile(window.innerWidth < 768);
    }
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (isMobile) {
      setTimeout(() => {
        window.dispatchEvent(new Event("resize"));
      }, 50);
    }
  }, [sidebarOpen, isMobile]);

  function clearQuickFilter() {
    setQuickFilter("");
  }

  // Date-range picker (F4): sets an explicit start/end ISO bound and clears the
  // relative quick filter so the two controls don't fight each other.
  function setDateBound(which: "start" | "end", value: string) {
    setQuickFilter("");
    setFilters((f) => {
      if (!value) return { ...f, [which]: "" };
      const iso =
        which === "start"
          ? new Date(value + "T00:00:00").toISOString()
          : new Date(value + "T23:59:59").toISOString();
      return { ...f, [which]: iso };
    });
  }

  function handleSelectEvent(id: string) {
    setSelectedId(id);
    if (isMobile) {
      setMobileTab("list");
      setSidebarOpen(true);
    }
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-app-bg text-app-text-primary">
      <header className="shrink-0 border-b border-app-border bg-app-surface">
        <SiteHeader showNav={!isMobile}>
          {isMobile && (
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              title={sidebarOpen ? t.hideSidebar : t.showSidebar}
              className={cn(
                "flex h-[1.6rem] w-[1.6rem] shrink-0 cursor-pointer items-center justify-center rounded border-none bg-transparent p-[0.2rem] text-[1.2rem] transition-colors duration-[120ms]",
                sidebarOpen ? "text-app-accent-blue" : "text-app-text-muted",
              )}
            >
              {sidebarOpen ? "✕" : "☰"}
            </button>
          )}
          <div className="h-[18px] w-px shrink-0 bg-app-border" />
          <div className="flex flex-1 items-center gap-[0.2rem] overflow-x-auto [scrollbar-width:none]">
            {QUICK_FILTERS.map((qf) => {
              const active = quickFilter === qf.value;
              return (
                <button
                  key={qf.value}
                  onClick={() => {
                    setQuickFilter(active ? "" : qf.value);
                    setFilters((f) => ({ ...f, start: "", end: "" }));
                  }}
                  className={cn("qf-btn", active ? "qf-btn-active" : "qf-btn-inactive")}
                >
                  {({ "6h": t.filter6h, "24h": t.filter24h, "7d": t.filter7d, "30d": t.filter30d } as Record<string, string>)[qf.value] ?? qf.value}
                </button>
              );
            })}
            {quickFilter && (
              <button
                onClick={clearQuickFilter}
                title={t.clearTimeFilter}
                className="shrink-0 cursor-pointer rounded border-none bg-transparent px-[0.25rem] py-[0.1rem] text-[0.7rem] leading-none text-app-text-ghost"
              >
                ✕
              </button>
            )}
            <div className="mx-1 h-[16px] w-px shrink-0 bg-app-border" />
            <input
              type="date"
              aria-label={t.dateFrom}
              title={t.dateFrom}
              value={filters.start ? filters.start.slice(0, 10) : ""}
              onChange={(e) => setDateBound("start", e.target.value)}
              className="shrink-0 rounded border border-app-border bg-transparent px-1 py-[0.1rem] text-[0.68rem] text-app-text-muted [color-scheme:dark]"
            />
            <input
              type="date"
              aria-label={t.dateTo}
              title={t.dateTo}
              value={filters.end ? filters.end.slice(0, 10) : ""}
              onChange={(e) => setDateBound("end", e.target.value)}
              className="shrink-0 rounded border border-app-border bg-transparent px-1 py-[0.1rem] text-[0.68rem] text-app-text-muted [color-scheme:dark]"
            />
            {(filters.start || filters.end) && (
              <button
                onClick={() => setFilters((f) => ({ ...f, start: "", end: "" }))}
                title={t.clearDateRange}
                className="shrink-0 cursor-pointer rounded border-none bg-transparent px-[0.25rem] py-[0.1rem] text-[0.7rem] leading-none text-app-text-ghost"
              >
                ✕
              </button>
            )}
          </div>
        </SiteHeader>

        <div className="flex h-[34px] items-center gap-[0.2rem] overflow-x-auto px-3 [scrollbar-width:none]">
          {CATEGORY_TABS.map((tab) => {
            const active = filters.category === tab.value;
            const color = tab.value ? categoryColor(tab.value) : "var(--app-accent-blue)";
            const Shape = tab.value ? categoryShapeComponent(tab.value) : null;
            return (
              <button
                key={tab.value}
                onClick={() =>
                  setFilters((f) => ({
                    ...f,
                    category: tab.value !== "" && f.category === tab.value ? "" : tab.value,
                  }))
                }
                className={cn("cat-tab", active ? "cat-tab-active" : "cat-tab-inactive")}
                style={{ "--cat-color": color } as React.CSSProperties}
              >
                {Shape ? (
                  <Shape size={10} color={active ? color : color + "bb"} />
                ) : (
                  <span className={cn("text-[0.62rem]", active ? "opacity-100" : "opacity-55")}>◉</span>
                )}
                {categoryLabel(lang, tab.value || "all")}
              </button>
            );
          })}
        </div>

        {topics.length > 0 && (
          <div className="flex h-[34px] items-center gap-[0.2rem] overflow-x-auto border-t border-app-border px-3 [scrollbar-width:none]">
            {topics.map((topic) => {
              const isActive = activeTopic === topic.slug;
              const color = topic.category ? categoryColor(topic.category) : "#7c9ef8";
              return (
                <button
                  key={topic.slug}
                  onClick={() => handleTopicClick(topic.slug)}
                  title={topic.description ?? topic.name}
                  className={cn("cat-tab", isActive ? "cat-tab-active" : "cat-tab-inactive")}
                  style={{ "--cat-color": color } as React.CSSProperties}
                >
                  <span
                    className="inline-block h-[5px] w-[5px] shrink-0 rounded-full"
                    style={{ backgroundColor: isActive ? color : color + "99", flexShrink: 0 }}
                  />
                  {topic.name}
                </button>
              );
            })}
            {activeTopic && (
              <button
                onClick={() => setActiveTopic(null)}
                title={t.clearTopicFilter}
                className="shrink-0 cursor-pointer rounded border-none bg-transparent px-[0.25rem] py-[0.1rem] text-[0.7rem] leading-none text-app-text-ghost"
              >
                ✕
              </button>
            )}
          </div>
        )}
      </header>

      <main className="relative flex flex-1 overflow-hidden">
        <section
          className={cn(
            "relative min-w-0",
            isMobile ? (sidebarOpen ? "hidden" : "block flex-1") : "block flex-[1_1_60%]",
          )}
        >
          {mounted && (
            <Suspense fallback={<div className="h-full bg-app-panel" />}>
              <MapView
                events={events}
                selectedId={selectedId}
                onSelectEvent={handleSelectEvent}
                streamRefresh={streamRefresh}
                showNotams={showNotams}
                showEarthquakes={showEarthquakes}
                showStaticPoints={showStaticPoints}
              />
            </Suspense>
          )}

          {(!isMobile || mobileTab === "map") && (
            <div className="absolute left-[10px] z-[1000] flex flex-col gap-1" style={{ bottom: isMobile ? 16 : 28 }}>
              {OVERLAY_CONTROLS.map(({ key, color }) => {
                const active = overlayState[key];
                return (
                  <button
                    key={key}
                    onClick={() => overlaySetters[key]((v) => !v)}
                    className={cn("overlay-btn", active ? "overlay-btn-active" : "overlay-btn-inactive")}
                    style={{ "--overlay-color": color } as React.CSSProperties}
                  >
                    {overlayLabels[key]}
                  </button>
                );
              })}
            </div>
          )}
        </section>

        <section
          className={cn(
            "flex min-w-0 flex-col overflow-hidden bg-app-panel",
            isMobile
              ? cn("absolute inset-0 z-[500]", !sidebarOpen && "hidden")
              : "flex-[0_0_380px] border-l border-app-border",
          )}
        >
          {/* Events list — markets & forecasts now live on the dedicated /markets page */}
          <div className="min-h-0 flex-1 overflow-y-auto">
            <TopicHistory onTopicClick={handleTopicClick} activeTopic={activeTopic} />
            <EventList events={events} selectedId={selectedId} onSelectEvent={handleSelectEvent} onTopicClick={handleTopicClick} onSymbolClick={handleSymbolClick} activeTopic={activeTopic} />
          </div>
        </section>
      </main>

      {isMobile && (
        <nav className="flex h-[52px] shrink-0 border-t border-app-border bg-app-surface">
          {(["map", "list"] as const).map((tab) => {
            const active = mobileTab === tab;
            return (
              <button
                key={tab}
                onClick={() => {
                  setMobileTab(tab);
                  setSidebarOpen(tab === "list");
                }}
                className={cn("mobile-nav-btn", active ? "mobile-nav-btn-active" : "mobile-nav-btn-inactive")}
              >
                <span className="text-[1.05rem] leading-none">{tab === "map" ? "⬡" : "☰"}</span>
                {tab === "map" ? t.mapTab : t.listTab}
              </button>
            );
          })}
          <a href="/markets" className="mobile-nav-link">
            <span className="text-[1.05rem] leading-none">$</span>
            {t.navMarkets}
          </a>
          <a href="/newsletter" className="mobile-nav-link">
            <span className="text-[1.05rem] leading-none">✉</span>
            {t.briefingsTab}
          </a>
        </nav>
      )}
    </div>
  );
}
