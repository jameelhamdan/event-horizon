# Architecture

## Stack

| Layer | Technology |
|-------|------------|
| Backend | Django 6 + django-mongodb-backend |
| Storage | MongoDB 8 |
| Task queue | django-rq + Redis — two queues: `default` (light I/O) and `heavy` (NLP/LLM) |
| Scheduling | supercronic + `api/crontab` → `manage.py run_task` (runs in `api` container) |
| Ingestion | feedparser (RSS) + requests |
| NLP | LLM (entities · sentiment · category/sub-category · geocode) · sentence-transformers (clustering) · **FinBERT** (financial sentiment) · geonamescache |
| LLM | Multi-provider via `services/llm.py` — `g4f` (default), `openrouter`, `ollama`; per-use-case routing + fallback chains (`settings.LLM_ROUTES`) |
| Forecasting | as-of feature engineering + LLM v1 + **LightGBM v2** (optional dep) |
| Frontend | React 19 + Vite + react-router-dom + react-leaflet (TypeScript) |
| Real-time | Server-Sent Events over Redis pub/sub |
| Email | AWS SES (newsletter + double opt-in confirmation) |
| Serving | uvicorn (ASGI) + nginx reverse proxy |
| Containers | Docker Compose |

## Docker services

| Service | Role |
|---------|------|
| `api` | uvicorn ASGI + supercronic (`api/crontab`) |
| `worker-light` | `rqworker-pool default` — fast I/O tasks |
| `worker-heavy` | `rqworker-pool heavy` — NLP / LLM tasks |
| `worker-bulk` | `rqworker-pool bulk` — long one-shot jobs |
| `frontend` | builds the Vite SPA, copies `dist/` |
| `nginx` | reverse proxy (80/443) |
| `redis` | RQ broker + cache + SSE pub/sub |
| `mongo` | database (27017) |
| `g4f` | gpt4free OpenAI-compatible LLM proxy (registration-free), reachable at `http://g4f:1337/v1` |
| `cloudflared` | optional Cloudflare Tunnel |

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
    WH -->|LLM calls| G4F[g4f proxy]
    G4F -.->|fallback| EXT([OpenRouter])
    WH -->|SES| SES([AWS SES email])
    WL -->|publish ticks| REDIS
    API -->|/api/sse relay| USER
```

## Two-queue model

Work is split by cost, not by feature:

- **`default`** — fast I/O: article fetch, price/notam/earthquake/forex streams.
- **`heavy`** — anything CPU- or LLM-bound: `process_articles`, `aggregate_events`,
  topic tagging/discovery, FinBERT scoring, `run_forecast`, `score_forecasts`,
  `train_forecaster`, newsletter generation.

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
```

## Code layout (where the work happens)

```
api/
  core/        models (Source, Article, Event, Topic, PriceTick, …, Forecast) + admin + commands
  api/         DRF views + serializers (events, forecasts, newsletter)
  services/    stateless Python — no Django models
    tasks.py            all task functions (plain Python, no decorator)
    workflow.py         orchestrates process → aggregate → tag → route
    processing/         analyzer, cleaner, clustering, finbert
    forecasting/        features, buckets, routing, calibration, service, model, metrics
    streams/            prices (+ ^VIX), notam, earthquakes, forex
    topics/             matcher, scraper, dedup, sources/current_events
    newsletter/         generator, sender
  migrations/  centralized, mapped via MIGRATION_MODULES
ui/            React 19 + Vite SPA (TypeScript)
```

## Data flow & storage

1. **Ingestion** writes raw `Article` documents.
2. **Processing** enriches each `Article` in place (entities, sentiment ×2, category,
   sub-category, translations).
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

All LLM calls go through `get_llm_service(role)` in `services/llm.py`. There is **no
single backend switch** — instead, providers are configured independently and each
use-case (*role*) is routed to one provider or an **ordered fallback chain**.

**Providers** (all OpenAI-compatible except Ollama):

| Provider | Endpoint | Registration | Notes |
|----------|----------|--------------|-------|
| `g4f` | `http://g4f:1337/v1` (Docker service) | None | **Default.** Headless gpt4free proxy; relays to free web providers. Needs outbound internet. |
| `openrouter` | `https://openrouter.ai/api/v1` | API key(s) | Comma-separated keys rotate round-robin. Fallback. |
| `ollama` | `http://localhost:11434` | None | Self-hosted local models (e.g. `qwen2.5:7b`); not in Compose by default. |

**Config split:**
- **`.env`** — per-provider settings only (keys / base URLs / model names). See
  [`api/.env.example`](../api/.env.example).
- **`settings.LLM_ROUTES`** (a dict in `settings/base.py`) — the who-uses-what routing.
  Default is `['g4f', 'openrouter']` for every role; override per role
  (`analyzer`, `topics`, `forecast`, `newsletter`, `historical`) as needed.

A multi-provider route returns a `FallbackLLMService` that tries each backend in order,
catching `LLMError`, until one succeeds. Unconfigured providers (no base URL / key) are
skipped automatically. Test a route with `python manage.py test_llm --role <role>`.

```mermaid
flowchart LR
    CALL["get_llm_service('analyzer')"] --> ROUTE{LLM_ROUTES}
    ROUTE --> G4F[g4f · Docker]
    G4F -->|fail| OR[openrouter · key]
    OR -->|all fail| ERR([LLMError])
```
</content>
