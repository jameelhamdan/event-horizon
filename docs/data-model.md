# Data model

All core models live in `api/core/models.py` and use `MongoManager` from
django-mongodb-backend. Migrations are centralized under `api/migrations/` and mapped
via `MIGRATION_MODULES`.

**Two MongoDB rules apply everywhere:**

| Rule | Wrong | Right |
|------|-------|-------|
| Never use the `__date` lookup | `filter(published_on__date=today)` | `filter(published_on__gte=start, published_on__lt=end)` |
| `Article.article_ids` holds **string** UUIDs | `filter(id__in=event.article_ids)` | `filter(id__in=[uuid.UUID(a) for a in event.article_ids])` |

Legend for the type column: `→` = enum/choices, `[]` = list, `{}` = dict/subdocument.

## Relationships

There are no hard foreign keys between the analytics collections (MongoDB) — links are
**logical**, made by string id-lists or shared keys (`source_code`, `symbol`,
`topic_slugs`). Solid lines below are id-list references; the dashed line is the only
real Django FK (`Article.related → Article`).

```mermaid
erDiagram
    SOURCE  ||--o{ ARTICLE : "source_code"
    ARTICLE ||--o| ARTICLE : "related (FK)"
    EVENT   }o--o{ ARTICLE : "article_ids[]"
    EVENT   }o--o{ TOPIC : "topic_slugs[]"
    EVENT   }o--o{ FORECAST : "event_ids[] · affected_indicators.symbol"
    PRICETICK }o--|| FORECAST : "symbol"
    NEWSLETTER }o--o{ ARTICLE : "articles[] snapshot"

    ARTICLE {
        uuid id PK
        string source_code
        float sentiment
        float finbert_sentiment
        string category
    }
    EVENT {
        string location_name
        datetime latest_article_at
        json affected_indicators
    }
    FORECAST {
        string symbol
        int horizon_days
        float proba_up
        string direction
        bool is_correct
    }
    TOPIC {
        string slug PK
        float topic_score
        bool is_top_level
    }
    PRICETICK {
        string symbol
        float value
        datetime occurred_at
    }
```

---

## Article

Raw news item from one source, enriched **in place** by either the `analyze`
stage (fresh live articles — full cloud LLM) or the `annotate` stage
(historical/backfill — on-prem NLP; and, for low-confidence classifications,
re-judged by the `refine` stage — see `stage`/`refined_on`/`refined_by`). A
re-refine (admin action or CLI) overwrites `refined_by` and `refined_on`
regardless of the article's current stage — the field always reflects
whichever judge most recently decided it.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID (PK) | Primary key, generated at fetch time |
| `source_code` | str(64) | Code of the originating `Source` |
| `source_type` | str → `SourceType` | website / api / rss / social / email / newsletter / database |
| `source_url` | URL(512) | Canonical item URL (dedupe key with code+type) |
| `author` / `author_slug` | str(100) | Byline + slug |
| `title` | str(200) | Headline |
| `content` | text | Body text |
| `published_on` | datetime | Publish timestamp (UTC); drives all as-of cuts |
| `related` | FK→self | Optional link to a related article |
| `entities` | `[]` | Unused — retained for schema stability; not populated |
| `sentiment` | float \| null | Local VADER polarity [-1, 1] (rule-based, no LLM call) |
| `finbert_sentiment` | float \| null | **FinBERT** signed sentiment [-1, 1] — news-domain (new) |
| `location` | str(255) \| null | `City, Country` — NER + gazetteer (`annotate`) or LLM-named + geocoded (`analyze`) |
| `event_intensity` | float \| null | Rule-rated (`annotate`) or LLM-rated (`analyze`) newsworthiness/severity [0, 1] |
| `category` | str → `EventCategory` | Top-level category (prototype classifier / LLM / judge) |
| `sub_category` | str(64) \| null | Sub-category within the top-level |
| `processed_on` | datetime \| null | Set when analysis/annotation completes |
| `stage` | str(16) | Pipeline position: `fetched → annotated \| refine → refined` (see [pipeline-state.md](pipeline-state.md)) — `analyze` and `annotate` both terminate at `annotated` |
| `refined_on` | datetime \| null | Set when the refine stage re-judged the article |
| `refined_by` | str \| null | Judge that produced the current verdict: `zeroshot` \| `ollama` \| `cloud` (see `REFINE_PROVIDER`) |
| `importance_score` / `importance_source` | float \| str | 1–10 significance (rules post-processing over either analyzer's intensity) |
| `banner_image_url` | URL(512) \| null | RSS media or OG-image fallback |
| `latitude` / `longitude` | float \| null | Geocoded coordinates |
| `translations` | `{}` | Per-language `{en:{...}, ar:{...}}` — `en` is `annotate`'s extractive summary, or an abstractive one from `analyze`/the cloud refine provider; `ar` always generated locally (MarianMT, `services/translation/`) |
| `extra_data` | `{}` | Annotation payload (`llm` block: labels, `annotator`/`confidence` from `annotate`, or the raw LLM fields from `analyze`) etc. |
| `updated_on` / `created_on` | datetime | Audit timestamps |

**Indexes:** `created_on`, `source_code`, `author_slug`, `category`, `processed_on`,
`location`, `stage`. (`refined_by` is not indexed — low cardinality, filtered via admin only.)

---

## Event

An aggregated real-world happening; **one event, many source articles**. Built by
`aggregate_events`.

| Field | Type | Description |
|-------|------|-------------|
| `title` | str(512) | Representative title |
| `content` | text | Aggregated/representative body |
| `category` | str → `EventCategory` | Default `general` |
| `location_name` | str(255) | Bucketing location |
| `latitude` / `longitude` | float \| null | Coordinates |
| `started_at` | datetime | Timestamp of the **earliest** article |
| `latest_article_at` | datetime \| null | **= max(`published_on`)** over members — the **event-time** used for all as-of forecasting cuts (not the day bucket) ★new |
| `article_count` | int | Number of constituent articles |
| `avg_sentiment` | float \| null | Mean article sentiment |
| `avg_finbert_sentiment` | float \| null | FinBERT mean over articles ★new |
| `avg_intensity` | float \| null | Mean event intensity |
| `affected_indicators` | `[]` | Deterministically routed `[{symbol, weight}]` (weight signed) ★new |
| `article_ids` | `[]` | String UUIDs of member articles |
| `source_codes` | `[]` | Distinct source codes |
| `sub_categories` | `[]` | Distinct sub-categories present |
| `translations` | `{}` | Per-language `{en:{...}, ar:{...}}` |
| `topics` | `{}` | `{slug: confidence}` (float 0–1) |
| `topic_slugs` | `[]` | Flat slug list parallel to `topics` (queryable) |
| `topics_source` | str(8) | Provenance: `embed` (`EmbeddingTopicMatcher`, local) or `keyword` (fallback — re-evaluated on a later run) |
| `updated_on` / `created_on` | datetime | Audit timestamps |

**Indexes:** `started_at`, `latest_article_at`, `category`, `location_name`.
★new = added by the pipeline redesign (migration `0004`).

---

## Topic

An ongoing storyline grouping many events (e.g. *2023 Turkey–Syria earthquakes*).

| Field | Type | Description |
|-------|------|-------------|
| `slug` | str(128), unique | Stable identifier |
| `name` | str(255) | Display name |
| `keywords` | `[]` | Matching keywords (LLM-expanded) |
| `description` | text | LLM-enriched summary |
| `category` | str → `EventCategory` | Optional category |
| `source_url` | URL(512) | Origin link |
| `source_ids` | `[]` | Adapters that confirmed the topic |
| `is_current` | bool | In today's news cycle |
| `is_active` | bool | Shown in UI / not soft-deleted |
| `started_at` / `ended_at` | datetime \| null | Lifecycle; null `ended_at` = ongoing |
| `fetched_at` | datetime | Last refresh |
| `parent_slug` | str(128) \| null | Optional hierarchy parent |
| `historical_month/day/year` | int \| null | Calendar anchor for historical topics |
| `event_count` | int | Denormalized tagged-event count |
| `topic_score` | float | Composite ranking score |
| `is_pinned` | bool | Admin override: always top-level |
| `is_top_level` | bool | Auto-promoted when score passes threshold |

**Indexes:** `is_current`, `is_active`, `is_top_level`, `category`, `started_at`,
`ended_at`, `parent_slug`, `(historical_month, historical_day)`.
Helper: `is_live_at(dt)` → bool.

---

## Forecast

One prediction for one `(symbol, horizon_days, generated_at)`. See
[forecasting.md](forecasting.md).

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | str(32) | Indicator symbol (e.g. `GC=F`, `^VIX`) |
| `stream_key` | str(32) | crypto / stock / commodity / forex / bond / index |
| `generated_at` | datetime | Wall-clock time the forecast was produced |
| `as_of_date` | datetime | Feature cut time `t` |
| `horizon_days` | int | 1 (short-term) or 5 (weekly) |
| `direction` | str → `ForecastDirection` | up / down / neutral |
| `proba_up` | float | Calibrated P(up) [0, 1] |
| `predicted_change_pct` | float | Regressor output (%) |
| `predicted_price` | float \| null | Predicted price at horizon |
| `band_low` | float \| null | Lower bound of prediction band |
| `band_high` | float \| null | Upper bound of prediction band |
| `confidence` | float | `\|proba_up − 0.5\| × 2` — scalar [0, 1] |
| `current_value` | float \| null | Last close at feature cut time `t` |
| `router_source` | str(8) | Provenance: `rules` (only router; historical rows may carry `llm`) |
| `model_version` | str(64) | Model identifier / version string |
| `realized_direction` | str(8) \| null | Actual direction after horizon (scoring) |
| `realized_change_pct` | float \| null | Actual % change after horizon (scoring) |
| `is_correct` | bool \| null | Whether direction prediction was correct (scoring) |
| `scored_at` | datetime \| null | When scoring was applied |
| `created_on` | datetime | Row creation timestamp |

**Indexes:** `(symbol, horizon_days, generated_at)`, `as_of_date`, `generated_at`.

### Enums

| Enum | Values |
|------|--------|
| `EventCategory` | conflict, disaster, economic, political, health, general *(+ legacy protest, crime)* |
| `ForecastDirection` | up, down, neutral |
| `SourceType` | website, api, rss, social, email, newsletter, database |
| `StaticPointType` | exchange, commodity_exchange, port, central_bank |

Horizons: 1 day (short-term), 5 days (weekly). Set via `FORECAST_HORIZON_DAYS` in settings.

---

## Stream models

### PriceTick
One price sample. 1-year TTL in production. Now includes `^VIX` and `DX-Y.NYB`.

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | str(32) | e.g. `BTC-USD`, `GC=F`, `^VIX` |
| `stream_key` | str(32) | crypto / stock / commodity / forex / bond / index |
| `name` | str(64) | Display name |
| `value` | float | Price/level |
| `change_pct` | float \| null | % vs previous close |
| `volume` | float \| null | Volume |
| `occurred_at` | datetime (indexed) | Sample time |

**Indexes:** `(symbol, occurred_at)`, `stream_key`.

### NotamZone — current live state (upserted by `notam_id`)
| Field | Type | Description |
|-------|------|-------------|
| `notam_id` | str(128), unique | NOTAM identifier |
| `notam_type` | str(32) | TFR / prohibited / restricted / danger |
| `geometry` | `{}` | GeoJSON Feature |
| `effective_from` / `effective_to` | datetime | Validity window |
| `is_active` | bool (indexed) | Currently live |
| `location_name` / `country_code` | str | Location |
| `altitude_min_ft` / `altitude_max_ft` | int \| null | Altitude band |
| `updated_at` | datetime | Last upsert |

### NotamRecord — append-only history
Same descriptive fields as `NotamZone` plus `source_region`, `status`
(active/expired/cancelled), `raw_text`, and `fetched_at` (append timestamp).
**Indexes:** `(effective_from, effective_to)`, `status`, `country_code`.

### EarthquakeRecord — USGS events
| Field | Type | Description |
|-------|------|-------------|
| `usgs_id` | str(32), unique | USGS event id |
| `magnitude` | float (indexed) | Magnitude |
| `magnitude_type` | str(8) | ml / mb / mw |
| `depth_km` | float \| null | Depth |
| `location_name` | str(255) | Place description |
| `latitude` / `longitude` | float | Epicenter |
| `occurred_at` | datetime (indexed) | Event time |
| `tsunami_alert` | bool | Tsunami flag |
| `alert_level` | str(16) | green / yellow / orange / red |
| `fetched_at` | datetime | Ingest time |

### StaticPoint — reference geography
Seed with `bootstrap_static_points`. Fields: `code` (unique), `point_type`
(→ `StaticPointType`), `name`, `country`, `country_code`, `latitude`, `longitude`,
`metadata` (`{}`), `is_active`. **Indexes:** `point_type`, `country_code`.

---

## Newsletter app (`api/newsletter/models.py`)

### DailyNewsletter
Body is stored as **Markdown** and converted to HTML only at send time.

| Field | Type | Description |
|-------|------|-------------|
| `date` | date, unique | One newsletter per day |
| `subject` | str(255) | Email subject |
| `body` | text | **Markdown** content |
| `articles` | `[]` | Snapshot of referenced articles |
| `cover_image_url` / `cover_image_credit` | str \| null | Cover art + attribution |
| `generated_at` | datetime | Generation time |
| `sent_at` | datetime \| null | Send time |
| `sent_count` | int | Recipients delivered |
| `status` | str → draft/sending/sent/error | Lifecycle |
| `event_count` | int | Events summarized |

### Subscriber — double opt-in
| Field | Type | Description |
|-------|------|-------------|
| `email` | str(254), unique | Subscriber email |
| `token` | UUID, unique | Confirm/unsubscribe token |
| `subscribed_at` | datetime | Sign-up time |
| `confirmed_at` | datetime \| null | Opt-in confirmation time |
| `is_active` | bool | True only after confirmation |
| `unsubscribed_at` | datetime \| null | Opt-out time |

Lifecycle: **pending → active (confirmed) → unsubscribed**.

```mermaid
stateDiagram-v2
    [*] --> pending: subscribe (confirmation email sent)
    pending --> active: confirm token (sets confirmed_at, is_active=true)
    active --> unsubscribed: unsubscribe token (sets unsubscribed_at, is_active=false)
    pending --> unsubscribed: unsubscribe token
    unsubscribed --> active: re-subscribe + confirm
```

---

## Other apps

| App | Model | Purpose |
|-----|-------|---------|
| `accounts` | `User` (+ `UserManager`) | Email-based custom user; `AUTH_USER_MODEL='accounts.User'` |
| `misc` | `EmailLog` | Admin monitoring of sent emails |

---

## NLP DTOs (not persisted)

Defined in `core/models.py` for the processing pipeline:

| DTO | Key fields |
|-----|-----------|
| `ArticleDocument` | `id, title, content, source_code, published_on` + `full_text` property |
| `ArticleFeatures` | `sentiment, finbert_sentiment, location, lat/lon, event_intensity, category, sub_category, llm_data, translations` |
</content>
