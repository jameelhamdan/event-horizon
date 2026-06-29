# Pipeline — phase by phase

The system is a chain of stages. Each stage has a task function in
`services/tasks.py` (plain Python, no decorator), a management command for manual
runs, and a scheduled cadence in `api/crontab`. Stages communicate
only through MongoDB documents — there is no in-memory hand-off — so any stage can be
re-run independently.

> Scheduling note: periodic jobs run via supercronic in the `api` container. Edit
> `api/crontab` to change cadence. Heavy NLP/LLM work is fanned out to the `heavy`
> queue by dispatcher tasks that run on the `default` queue.

## Stage chain & cadences

```mermaid
flowchart TD
    F["dispatch_fetch_task<br/><i>default · every 10m</i>"] --> A1[(Article)]
    A1 --> P["dispatch_process_articles_task<br/><i>default · every 4h</i>"]
    P --> A2[(Article · enriched)]
    A2 --> AG["aggregate_events_task<br/><i>heavy · every 4h+30m</i>"]
    AG --> E[(Event)]
    E --> RE["dispatch_route_events_task<br/><i>default · every 6h</i>"]
    RE --> EI[(Event · affected_indicators)]
    EI --> DT["discover_topics_task<br/><i>heavy · daily 05:00</i>"]
    DT --> T[(Topic links)]
    RT["refresh_topics_task<br/><i>heavy · daily 04:00</i>"] --> T
    EI --> RF["run_forecast_task<br/><i>heavy · daily 05:30</i>"]
    PT[(PriceTick)] --> RF
    RF --> FC[(Forecast)]
    FC --> SF["score_forecasts_task<br/><i>heavy · daily 07:00</i>"]
    EI --> GN["generate_newsletter_task<br/><i>heavy · daily 06:00</i>"]

    classDef q fill:#1a1a22,stroke:#7c9ef8,color:#e8e8f0;
    class F,P,AG,RE,DT,RT,RF,SF,GN q;
```

Streams run independently on the `default` queue and feed `PriceTick` (and the SSE
channels), which the forecaster reads:

```mermaid
flowchart LR
    YF([Yahoo + CoinGecko]) --> FP["fetch_prices_task · 5m"] --> PT[(PriceTick)]
    ECB([ECB]) --> FX["fetch_forex_task · 15m"] --> PT
    AW([aviationweather]) --> NO["fetch_notams_task · 15m"] --> NZ[(Notam*)]
    USGS([USGS]) --> EQ["fetch_earthquakes_task · 5m"] --> EQR[(EarthquakeRecord)]
    PT & NZ & EQR -.->|Redis pub/sub| SSE([/api/sse → browser])
```

---

## Stage 1 — Fetch

**Goal:** get raw news into the system as `Article` documents.

Two modes:

### 1a. Live (`dispatch_fetch_task`, default queue, every 10m)
- `dispatch_fetch_task` enqueues one `fetch_source_task(source_code, start_date)` per
  enabled `Source` on the default queue.
- Each `fetch_source_task` pulls latest items for its source, dedupes on
  `(source_code, source_type, source_url)`, and stores `Article` with raw fields only
  (`title, content, url, author, source, published_on, banner_image_url`).
- RSS via feedparser today; website/API adapters are the growth path.

### 1b. Historical backfill (`backfill_history` command)
- Top-N articles **per ISO week** per source, ranked by LLM significance
  (`RSSHistoricalService`), saved idempotently. This produces the **training corpus**
  for the forecasting subsystem.
- ⚠️ **Point-in-time correctness:** ranking uses only information available *as of the
  publish week* — never present-day popularity — so no future information leaks into
  training features. The ranking signal + score are recorded on each `Article` so
  leakage is auditable.

```bash
python manage.py fetch_data <source> --hours 6
python manage.py backfill_history <source> --start-date 2022-01-01 --end-date 2025-01-01 --top-n 10
```

---

## Stage 2 — Process articles

**Task:** `dispatch_process_articles_task` (default queue, every 4h). **Code:**
`services/processing/` (`cleaner.py` drives it; `analyzer.py`, `finbert.py` are
called within).

`dispatch_process_articles_task` fans out to one `process_article_task(id)` per
unprocessed article on the heavy queue. A recovery pass with `only_failed=true` runs
every 12h to retry articles that previously errored.

Per article, enrich in place:

| Field | How |
|-------|-----|
| Entities / locations | LLM entities + geonamescache geocode |
| Category + **sub-category** | LLM, two-level taxonomy (see below) |
| Sentiment | `Article.sentiment` — LLM-extracted polarity [-1, 1] |
| Sentiment (**FinBERT**) | `Article.finbert_sentiment` — news-domain, batched on the heavy queue, computed **once at process time** |
| i18n (en/ar) | LLM translations → `Article.translations` |

**Two-level category taxonomy** (`EventCategory`): top-level stays small
(`conflict, disaster, economic, political, health, general`); the LLM-produced
`sub_category` does the work (e.g. `monetary-policy`, `airstrike`, `earthquake`).
Legacy flat values (`protest`, `crime`) still validate for old data but are never
assigned to new data.

Both sentiment scores are stored so downstream features can use either; sentiment is
always a **feature**, never the predictor.

---

## Stage 2b — Aggregate into events

**Task:** `aggregate_events_task` (heavy queue, every 4h+30m). **Code:** `services/workflow.py`.

1. Bucket processed articles by `(city, country, category, day)`.
2. Semantically sub-cluster within a bucket (`SemanticClusterer`,
   cosine ≥ 0.55, multilingual MiniLM).
3. Upsert an `Event` keyed on `(location_name, category, day)`, aggregating:
   - `avg_sentiment` (mean article sentiment), `avg_finbert_sentiment` (FinBERT mean), `avg_intensity`
   - **`latest_article_at` = max(published_on)** over constituent articles — this is
     the **event-time** used for all as-of forecasting cuts (not the day bucket).

One event = many source articles. This is the "relationship between articles of the
same time/type" the system is built around.

---

## Stage 2c — Topic tagging & discovery

| Task | Cadence | Role |
|------|---------|------|
| `dispatch_tag_topics_task` | on demand (admin) | `LLMTopicMatcher` (batch 10 events/call) → `Event.topic_slugs` + `Event.topics`. Re-routes `affected_indicators` once topics are known (topic routing is higher-signal). Falls back to keyword `TopicMatcher` on LLM error. |
| `discover_topics_task` | daily 05:00 | LLM discovers new `Topic`s from recent events. |
| `refresh_topics_task` | daily 04:00 | Scrape Wikipedia `Portal:Current_events` (last `TOPIC_SOURCES_DAYS`) → dedupe → semantic merge (≥0.85) → LLM enrich descriptions/keywords → upsert; age-off stale topics. |

A **Topic** is an ongoing storyline grouping many events (e.g. "2023 Turkey–Syria
earthquakes"). `is_current` = in today's cycle; `is_active` = shown in UI;
`is_top_level` = promoted by score or pin.

---

## Stage 2d — Route events to indicators

**Task:** `dispatch_route_events_task` (default queue, every 6h).

`dispatch_route_events_task` fans out to `route_events_chunk_task(event_ids)` on the
heavy queue. Each chunk calls `route_event_to_weighted_symbols()` per event, producing
`affected_indicators = [{symbol, weight}]` stored on the `Event`. This deterministic
routing is the bridge between news events and the forecasting subsystem (see
[forecasting.md](forecasting.md)).

---

## Stage 3 — Prediction (AI)

**Tasks:** `train_forecast_model_task` (daily 05:00), `run_forecast_task` (daily 05:30),
`score_forecasts_task` (daily 07:00). Fully documented in
**[forecasting.md](forecasting.md)**. In brief:

- For each `(indicator symbol, time t)` build an **as-of, volume-normalized** feature
  vector from `PriceTick`s ≤ t and `Event`s with event-time ≤ t.
- Forecast output per horizon (1 day, 5 days):
  - `direction` — up / down / neutral
  - `proba_up` — calibrated probability of an upward move
  - `predicted_change_pct` — point estimate of percentage change
  - `band_low` / `band_high` — prediction interval
- **Scoring** (`score_forecasts_task`) fills `realized_direction`,
  `realized_change_pct`, and `is_correct` once the horizon closes.

---

## Streams (independent of the news pipeline)

Default queue; each saves to MongoDB and publishes to a Redis SSE channel:

| Task | Cadence | Writes | Source |
|------|---------|--------|--------|
| `fetch_prices_task` | 5m | `PriceTick` | Yahoo Finance + CoinGecko (incl. **^VIX**, DX-Y.NYB) |
| `fetch_notams_task` | 15m | `NotamZone` (upsert) + `NotamRecord` (append) | aviationweather.gov |
| `fetch_earthquakes_task` | 5m | `EarthquakeRecord` | USGS FDSN |
| `fetch_forex_task` | 15m | `PriceTick` (`stream_key='forex'`) | ECB |

---

## Stage 4 — Newsletter

`generate_newsletter_task` (daily 06:00) groups the day's events by category, writes
per-category LLM sections into `DailyNewsletter.body` (**Markdown**), and snapshots the
articles + cover image (idempotent). `send_newsletter` converts Markdown→HTML at send
time and delivers to confirmed subscribers via AWS SES (double opt-in; token
unsubscribe). See [`../CLAUDE.md` → Newsletter](../CLAUDE.md).

---

## Maintenance tasks

| Task | Cadence | Purpose |
|------|---------|---------|
| `score_articles_task` | hourly | LLM-rate article significance (1–10); requires `ARTICLE_IMPORTANCE_SCORING_ENABLED` |
| `cleanup_low_importance_articles_task` | daily 03:00 | Delete articles below `ARTICLE_MIN_IMPORTANCE` after grace period |
| `prune_stale_articles_task` | daily 03:30 | Remove old unprocessed articles |
| `adjust_source_weights_task` | weekly (Sun 02:00) | Adjust source reliability weights based on signal quality |
| `pipeline_health_task` | every 30m | Emit pipeline health metrics |
| `backfill_prices_task` | weekly (Sun 00:00) | Backfill daily OHLC for active symbols (bulk queue) |
</content>
