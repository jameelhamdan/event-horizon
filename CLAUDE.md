# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Event Horizon** (`eventhorizonai.dev`) — a live global-event map that ingests news from RSS/Telegram sources, runs NLP and LLM analysis on each article, clusters articles into geographic events, streams real-time market/NOTAM/earthquake data, and serves it all via a DRF API to a React + Leaflet SPA.

## Commands

### Backend (Django — run from `api/`)

```bash
# Run the dev server (requires Mongo + Redis or TASK_QUEUE_ENABLED=false)
python manage.py runserver

# Run all migrations
python manage.py migrate

# Trigger a pipeline task manually (--sync runs in-process, no worker needed)
python manage.py run_task dispatch_fetch_task --sync
python manage.py run_task aggregate_events_task --sync

# Smoke-test LLM routing for a given role
python manage.py test_llm --role analyzer_lite --prompt "your prompt"

# End-to-end pipeline test (writes JSON report)
python manage.py e2e_pipeline
python manage.py e2e_pipeline --skip-fetch --skip-process

# Live LLM-provider connectivity test (reads .env.app, makes real calls)
python api/tests/tests_llm_providers.py
python api/tests/tests_llm_providers.py --provider groq

# Unit tests — dependency-light, no Mongo/network needed (plain assert functions,
# not unittest.TestCase, so `manage.py test` won't discover them — run as modules):
python -m tests.tests_scoring
python -m tests.tests_utils
python -m tests.tests_queue
python -m tests.tests_cache
python -m tests.tests_models
python -m tests.tests_processing
python -m tests.tests_topics_matcher
python -m tests.tests_forecasting_routing
DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_forecast   # slower (LightGBM roundtrip)
```

### Frontend (from `ui/`)

```bash
npm run dev          # Vite dev server
npm run build        # tsc + Vite production build
npm run typecheck    # tsc --noEmit
npm run lint         # ESLint
npm run format       # Prettier
```

### Docker (full stack)

```bash
docker compose up -d                    # start everything
docker compose up -d api worker-heavy  # restart specific services
```

## Environment

Copy `api/.env.example` to `api/.env` (or `.env.app` at the project root). Key settings:

- `TASK_QUEUE_ENABLED=false` — tasks run synchronously; no Redis/worker required locally.
- `DATABASE_URL` — MongoDB connection string (default `mongodb://root:1234@localhost:27017/radar-live?authSource=admin`).
- LLM credentials go in env (`GROQ_API_KEYS`, `CEREBRAS_API_KEYS`, `OPENROUTER_API_KEYS`, `OLLAMA_BASE_URL`); routing logic is in `settings/base.py` `LLM_ROUTES`. The LLM only handles category/sub-category/geo/intensity classification and newsletter/topic-enrichment prose now — entities, sentiment, translation, topic tagging, and event routing all run on local models (see LLM routing below).

## Architecture

### Backend layout

```
api/                       PYTHONPATH root inside Docker (/app)
  app/                     WSGI/ASGI entry, root URLconf, middleware
  core/                    Django app — Source, Article, Event, Topic, PriceTick,
                           NotamZone/Record, EarthquakeRecord, StaticPoint, MarketSymbol
  accounts/                Custom User model (AUTH_USER_MODEL = 'accounts.User')
  api/                     DRF views + serializers — all public endpoints live here
  newsletter/              DailyNewsletter + Subscriber models, newsletter tasks
  misc/                    EmailLog model
  services/                Stateless Python services (no models)
    tasks.py               All task functions — plain Python, no decorator
    queue.py               enqueue() helper — sync fallback when TASK_QUEUE_ENABLED=False
    llm/                   LLM client — provider abstraction + round-robin key rotation
    processing/            analyzer.py (LLM: category/sub-category/geo/intensity), ner.py (local NER,
                           entities), vader.py (local, sentiment), finbert.py (local, financial
                           sentiment), cleaner.py (orchestrates all of the above), clustering.py
    translation/           Local EN→AR translation (MarianMT) — replaces LLM-generated translations
    topics/                matcher.py (EmbeddingTopicMatcher — local, default; LLMTopicMatcher — kept,
                           unused by default; TopicMatcher — keyword fallback), scraper.py, dedup.py,
                           WikipediaCurrentEventsAdapter
    streams/               prices.py, notam.py, earthquakes.py, forex.py
    data/                  DataService, rss.py (feedparser), telegram.py (Telethon)
    newsletter/            generator.py (LLM), sender.py (Markdown→HTML→SES)
    forecasting/           routing.py — deterministic (rules-based) event→symbol routing + LightGBM clf+reg
    routing/               llm_router.py — LLM event→symbol routing, kept as an alternative
                           (FORECAST_ROUTER='llm'), unused while the default is 'rules'
  migrations/              Centralized — all apps map here via MIGRATION_MODULES
  tests/                   Management-command e2e tests + offline unit tests
```

### Task execution model

Tasks are Celery tasks (`@shared_task`) in `services/tasks.py` and `newsletter/tasks.py` — calling one directly as a plain function still runs it synchronously. The `enqueue()` helper in `services/queue.py` wraps Celery (Redis broker); when `TASK_QUEUE_ENABLED=False` it calls the function directly instead of `apply_async()`.

Three queues:
- `default` — light I/O (fetchers, stream collectors, fan-out dispatchers) — 4 workers
- `heavy` — NLP/LLM work (processing, clustering, topic matching, newsletters) — 2 workers (`celery -A app worker -Q heavy`); ML models load lazily per job, no preloading
- `bulk` — long one-shot jobs and pure dispatchers (price backfills, model training, the historical-article backfill dispatcher — its actual per-day-chunk fetch/save/process work runs on `heavy`, bounded to ~10min per chunk) — 1 worker

The crontab (`api/crontab`, run by supercronic in the `api` container) dispatches everything via `manage.py run_task <task_name>`. To manually trigger any cron job locally: `python manage.py run_task <task_name> --sync`.

### Pipeline flow

```
dispatch_fetch_task (every 10m)
  → fetch_source_task × N (one per enabled Source, default queue)
    → dispatch_process_articles_task (every 4h, heavy)
      → process_articles_chunk_task × N
          — LLM: category/sub-category/geo/intensity + EN translation
          — local: NER (entities), VADER (sentiment), FinBERT (financial sentiment),
            MarianMT (EN→AR translation)
        → aggregate_events_task (every 4h+30m, heavy)
          → dispatch_tag_topics_task → tag_events_chunk_task × N (EmbeddingTopicMatcher, local — no LLM)
dispatch_route_events_task (every 6h, heavy) — deterministic rules router (no LLM)
discover_topics_task (daily 05:00, LLM)
refresh_topics_task (daily 04:00, WikipediaCurrentEventsAdapter + LLM enrichment)
generate_newsletter_task (daily 06:00, LLM)
```

Stream tasks run independently: prices (5m), NOTAMs (15m), earthquakes (5m), forex (15m). Each saves to MongoDB and publishes to a Redis SSE channel (`sse:prices`, `sse:notams`, `sse:earthquakes`).

### LLM routing

`get_llm_service(role)` in `services/llm/__init__.py` reads `settings.LLM_ROUTES[role]` (a list of provider names) and tries each in order on failure. Providers: `groq`, `cerebras`, `openrouter`, `ollama_small/medium/large`. Strip code fences before `json.loads()` — always use `services.llm.strip_code_fences()`.

Several tasks that used to go through the LLM now run on local CPU models instead — cheaper, faster, and no rate limits. When touching these areas, prefer extending the local model rather than adding LLM calls back:

| Task | Local replacement | Module |
| ---- | ------------------ | ------ |
| Named entities | `dslim/bert-base-NER` (transformers) | `services/processing/ner.py` |
| Article sentiment | VADER (rule-based) | `services/processing/vader.py` |
| Arabic translation | `Helsinki-NLP/opus-mt-en-ar` (MarianMT) | `services/translation/` |
| Event → topic tagging | sentence-transformer cosine similarity (`paraphrase-multilingual-MiniLM-L12-v2`, same model as clustering) | `services/topics/matcher.py::EmbeddingTopicMatcher` |
| Event → market-symbol routing | Deterministic weighted rules (category/sub-category/country/sentiment) | `services/forecasting/routing.py` |

Still LLM-driven (needs real judgment or free-form generation): article `category`/`sub_category`/`country`/`city`/`intensity` classification (`services/processing/analyzer.py`), article importance scoring (`services/scoring/`), topic description/keyword enrichment + discovery (`services/workflow/topics.py`), and the daily newsletter (`services/newsletter/generator.py`).

`LLMTopicMatcher` (`services/topics/matcher.py`) and `LLMEventRouter` (`services/routing/llm_router.py`) are kept as opt-in alternatives (`FORECAST_ROUTER='llm'` for routing) but are not used by default.

### Django/MongoDB notes

- All models use `django_mongodb_backend`; `DEFAULT_AUTO_FIELD = ObjectIdAutoField`.
- Never use `__date` ORM lookup — use explicit datetime range filters.
- `Article.article_ids` stores string UUIDs; convert with `uuid.UUID()` before ORM filter.
- Migrations are centralized in `api/migrations/`; all apps map to it via `MIGRATION_MODULES`.

### Frontend

React 19 + Vite SPA at `ui/`. All files are `.tsx`/`.ts` — never `.jsx`/`.js`.

- All user-visible strings go through `useLanguage()` → `t.key`; strings defined in `src/i18n/strings.ts` (en + ar). Never hardcode English text in components.
- API base URLs are relative (no hardcoded host) to avoid mixed-content SSE issues.
- SSE handled by `src/hooks/useSSE.ts` (auto-reconnects).
- Every page component calls `useDocumentTitle()`.
- UI components use shadcn/ui + Tailwind CSS v4.

### Admin

- Django admin at `/admin/`; custom operations dashboard at `/admin/dashboard/` (pipeline status, manual triggers, per-queue queued/running/failed counts from `TaskRun`). Individual tasks — args, result, status, retries, error/traceback — are browsable at `/admin/core/taskrun/`.
