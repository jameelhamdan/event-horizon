# Architecture

## Stack

| Layer | Technology |
|-------|------------|
| Backend | Django 6 + django-mongodb-backend |
| Storage | MongoDB 8 |
| Task queue | Celery + Redis — three queues: `default` (light I/O), `heavy` (NLP/LLM), `bulk` (long one-shot jobs) |
| Scheduling | supercronic + `api/crontab` → `manage.py run_task` (runs in `api` container) |
| Ingestion | feedparser (RSS) + requests |
| NLP | LLM (category/sub-category · geo naming · intensity) · local NER (`dslim/bert-base-NER`, entities) · VADER (sentiment, rule-based) · sentence-transformers (clustering + topic matching) · **FinBERT** (financial sentiment) · MarianMT (Arabic translation) · geonamescache (geocode) |
| LLM | Multi-provider via `services/llm/` — Groq/Cerebras/OpenRouter (free-tier cloud, primary) with Ollama (local, CPU) as last-resort fallback; per-use-case routing + fallback chains (`settings.LLM_ROUTES`) |
| Forecasting | as-of feature engineering + **LightGBM** (optional dep) |
| Frontend | React 19 + Vite + react-router-dom + react-leaflet (TypeScript) |
| Real-time | Server-Sent Events over Redis pub/sub |
| Email | AWS SES (newsletter + double opt-in confirmation) |
| Serving | uvicorn (ASGI) + nginx reverse proxy |
| Containers | Docker Compose |

## Docker services

| Service | Role |
|---------|------|
| `frontend` | builds the Vite SPA, copies `dist/` |
| `nginx` | reverse proxy (80/443) |
| `cloudflared` | optional Cloudflare Tunnel |
| `api` | uvicorn ASGI + supercronic (`api/crontab`) |
| `worker-heavy` | `celery -A app worker -Q heavy` — NLP / LLM tasks |
| `worker-light` | `celery -A app worker -Q default` — fast I/O tasks |
| `worker-bulk` | `celery -A app worker -Q bulk` — long one-shot jobs |
| `redis` | Celery broker + cache + SSE pub/sub |
| `mongo` | database (27017) |
| `static_data` | seeds static reference data (countries, airports, etc.) |

### Runtime topology

```mermaid
flowchart LR
    USER([Browser]) -->|HTTPS| NGINX[nginx]
    NGINX -->|/api, /admin| API[api · uvicorn ASGI]
    NGINX -->|static| FE[frontend dist]
    NGINX -.optional.- CF[cloudflared tunnel]

    API --> MONGO[(MongoDB)]
    API <-->|SSE pub/sub + cache| REDIS[(Redis)]

    API -->|supercronic · api/crontab| REDIS
    API -->|enqueue via run_task| REDIS
    REDIS -->|default queue| WL[worker-light]
    REDIS -->|heavy queue| WH[worker-heavy]
    REDIS -->|bulk queue| WB[worker-bulk]
    WL --> MONGO
    WH --> MONGO
    WB --> MONGO
    WH -->|LLM calls| GROQ([Groq])
    GROQ -.fallback.-> CEREBRAS([Cerebras])
    CEREBRAS -.fallback.-> OR([OpenRouter])
    OR -.fallback.-> OLLAMA[Ollama · local, last resort]
    WH -->|SES| SES([AWS SES email])
    WL -->|publish ticks| REDIS
    API -->|/api/sse relay| USER
```

## Three-queue model

Work is split by cost, not by feature:

- **`default`** — fast I/O: article fetch, price/notam/earthquake/forex streams.
  Enqueued by dispatchers (e.g. `dispatch_fetch_articles`, `dispatch_fetch_prices`).
- **`heavy`** — anything CPU- or LLM-bound: `process_articles` (LLM + local NER/VADER/
  FinBERT/translation), `aggregate_events`, topic tagging (local embeddings) /
  discovery (LLM), `run_forecast`, `score_forecasts`, `train_forecaster`, newsletter
  generation (LLM).
- **`bulk`** — long one-shot jobs: `backfill_all_sources_task`, `backfill_prices_task`,
  `bootstrap_initial_data_task`.

`enqueue(fn, queue='heavy', ...)` selects the queue. When `TASK_QUEUE_ENABLED=False`
(dev default) `enqueue()` calls the function **synchronously** — no Redis or worker
needed locally.

```mermaid
flowchart TB
    subgraph DEFAULT["default queue · worker-light · fast I/O"]
        D1[fetch_articles] --- D2[fetch_prices] --- D3[fetch_notams]
        D4[fetch_earthquakes] --- D5[fetch_forex]
    end
    subgraph HEAVY["heavy queue · worker-heavy · CPU / LLM"]
        H1[process_articles<br/>+ FinBERT] --- H2[aggregate_events]
        H3[tag_topics / discover_topics] --- H4[run_forecast / score_forecasts]
        H5[train_forecaster] --- H6[generate_newsletter]
    end
    subgraph BULK["bulk queue · worker-bulk · long one-shot jobs"]
        B1[backfill_all_sources_task] --- B2[backfill_prices_task]
        B3[bootstrap_initial_data_task]
    end
```

## Code layout (where the work happens)

```
api/
  core/        models (Source, Article, Event, Topic, PriceTick, …, Forecast) + admin + commands
  api/         DRF views + serializers (events, forecasts, newsletter)
  services/    stateless Python — no Django models
    tasks.py            all task functions (plain Python, no decorator)
    workflow.py         orchestrates process → aggregate → tag → route
    processing/         analyzer (LLM), ner (local), vader (local), finbert (local), cleaner, clustering
    translation/        local EN→AR (MarianMT)
    forecasting/        features, buckets, routing (deterministic), calibration, service, model, metrics
    routing/            llm_router — LLM alternative to forecasting/routing.py, unused by default
    streams/            prices (+ ^VIX), notam, earthquakes, forex
    topics/             matcher (EmbeddingTopicMatcher default, LLMTopicMatcher alt, TopicMatcher fallback), scraper, dedup, sources/current_events
    newsletter/         generator, sender
  migrations/  centralized, mapped via MIGRATION_MODULES
ui/            React 19 + Vite SPA (TypeScript)
```

## Data flow & storage

1. **Ingestion** writes raw `Article` documents.
2. **Processing** enriches each `Article` in place: LLM (category, sub-category, geo,
   intensity, English title/summary), local NER (entities), local VADER + FinBERT
   (sentiment ×2), local MarianMT (Arabic translation).
3. **Aggregation** rolls articles up into `Event` documents and attaches
   `affected_indicators`.
4. **Streams** write `PriceTick` / `NotamZone` / `NotamRecord` / `EarthquakeRecord`
   independently and publish to Redis SSE channels.
5. **Forecasting** reads `Event` + `PriceTick` (strictly as-of) and writes `Forecast`
   rows, later filling actuals during scoring.

All time-based filtering on MongoDB uses explicit datetime ranges (never `__date`),
and the forecasting subsystem enforces point-in-time (as-of) cuts everywhere — see
[forecasting.md](forecasting.md).

## Real-time (SSE)

`GET /api/sse/` is an async ASGI view subscribed to Redis channels (`sse:prices`,
`sse:notams`, `sse:earthquakes`). Each stream task publishes after saving; the browser
`useSSE` hook auto-reconnects and dispatches per event type (`price_tick`,
`notam_update`, `earthquake_update`).

## LLM providers & routing

All LLM calls go through `get_llm_service(role)` in `services/llm/__init__.py`. There is
**no single backend switch** — instead, providers are configured independently and each
use-case (*role*) is routed to one provider or an **ordered fallback chain**. Free-tier
cloud providers lead every chain; local Ollama is always the last-resort fallback (it's
the only one with no rate limit, but it's slow on CPU-only hardware).

**Providers:**

| Provider | Endpoint | Key required | Notes |
|----------|----------|--------------|-------|
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEYS` | Free tier, high headroom — leads most chains. |
| `cerebras` | `https://api.cerebras.ai/v1` | `CEREBRAS_API_KEYS` | Free tier, tiny 5 req/min quota — only *leads* the low-volume newsletter role. |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEYS` | Mid fallback. Comma-separated keys rotate round-robin. |
| `ollama_small` | `OLLAMA_BASE_URL` (default `http://localhost:11434`) | None | `qwen3:4b` — last resort, fast/simple tasks |
| `ollama_medium` | same | None | `qwen3:8b` — last resort, default tier |
| `ollama_large` | same | None | `qwen3:14b` — last resort, complex analysis & newsletters |

Model overrides: set `OLLAMA_MODEL_SMALL`, `OLLAMA_MODEL_MEDIUM`, `OLLAMA_MODEL_LARGE` in `.env`.

**Default `LLM_ROUTES` (in `settings/base.py`):**

| Role | Chain | Used for |
|------|-------|----------|
| `default` | groq → cerebras → openrouter → ollama_medium | fallback for unlisted roles |
| `analyzer_lite` | groq → cerebras → openrouter → ollama_medium | article category/sub-category/geo/intensity + EN translation (entities/sentiment are local — see below) |
| `newsletter` | cerebras → openrouter → ollama_large | daily newsletter prose |
| `scoring` | groq → cerebras → openrouter → ollama_small | article importance rating |
| `historical` | groq → cerebras → openrouter → ollama_small | backfill importance rating |
| `topics` | groq → cerebras → openrouter → ollama_medium | topic description/keyword enrichment + discovery (tagging itself is local — see below) |
| `routing` | groq → cerebras → openrouter → ollama_small | unused by default (`FORECAST_ROUTER='rules'`); opt-in alternative to the deterministic router |

**Local-model replacements** (no LLM call at all): entities (`services/processing/ner.py`,
`dslim/bert-base-NER`), sentiment (`services/processing/vader.py`, VADER), Arabic
translation (`services/translation/`, MarianMT), event→topic tagging
(`services/topics/matcher.py::EmbeddingTopicMatcher`, sentence-transformer cosine
similarity), and event→symbol routing (`services/forecasting/routing.py`, deterministic
rules). See [CLAUDE.md → LLM routing](../CLAUDE.md) for the full rationale.

**Config split:**
- **`.env`** — per-provider settings only (keys / base URLs / model names). See
  [`api/.env.example`](../api/.env.example).
- **`settings.LLM_ROUTES`** (a dict in `settings/base.py`) — the who-uses-what routing.
  Override per role as needed.

A multi-provider route returns a `FallbackLLMService` that tries each backend in order,
catching `LLMError`, until one succeeds. Unconfigured providers (no base URL / key) are
skipped automatically. Test a route with `python manage.py test_llm --role <role>`.

```mermaid
flowchart LR
    CALL["get_llm_service('analyzer_lite')"] --> ROUTE{LLM_ROUTES}
    ROUTE --> GROQ[groq · key]
    GROQ -->|fail| CEREBRAS[cerebras · key]
    CEREBRAS -->|fail| OR[openrouter · key]
    OR -->|fail| OLLAMA[ollama_medium · local, last resort]
    OLLAMA -->|all fail| ERR([LLMError])
```
</content>
