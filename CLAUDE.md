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
python manage.py run_task pipeline_tick_task --sync
python manage.py run_task dispatch_stage_task stage_name=aggregate --sync

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
python -m tests.tests_stages
python -m tests.tests_topics_matcher
python -m tests.tests_forecasting_routing
DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_forecast   # slower (LightGBM roundtrip)

# Lint (dev-only tooling, not in the Docker image — pip install -r requirements-dev.txt)
ruff check .
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
    topics/                matcher.py (EmbeddingTopicMatcher — local, default, semantic;
                           TopicMatcher — keyword fallback, used when the embedding model
                           can't load and for retroactive tagging), scraper.py, dedup.py,
                           sources/ (WikipediaCurrentEventsAdapter)
    streams/               prices.py, notam.py, earthquakes.py, forex.py — BaseStream.run()
                           re-raises fetch/save failures so a broken stream surfaces as a
                           FAILED TaskRun instead of a silent success-with-0
    data/                  DataService, rss.py (feedparser), telegram.py (Telethon)
    scoring/               LLM importance scoring (batches of 30 titles) for the score stage
    workflow/              articles.py, events.py, topics.py — per-stage orchestration glue
                           (fetch/process article flow, event aggregation, topic discover/
                           refresh/enrich) that doesn't belong in a single stateless module
    newsletter/            generator.py (LLM), sender.py (Markdown→HTML→SES)
    email/                 mailer.py, providers.py — SES wrapper used by newsletter + accounts
    forecasting/           routing.py — deterministic (rules-based) event→symbol routing + LightGBM clf+reg
    routing/               __init__.py — thin wrapper around forecasting/routing.py that
                           persists Event.affected_indicators (route_events()); no LLM path
  migrations/              Centralized — all apps map here via MIGRATION_MODULES
  tests/                   Management-command e2e tests + offline unit tests
  requirements-dev.txt     Dev-only deps (currently just ruff) — not installed in Docker images
  ruff.toml                Lint config (F + E9/W6 rules only — real defects, no style rules)
```

### Task execution model

Tasks are Celery tasks (`@shared_task`) in `services/tasks.py` and `newsletter/tasks.py` — calling one directly as a plain function still runs it synchronously. The `enqueue()` helper in `services/queue.py` wraps Celery (Redis broker); when `TASK_QUEUE_ENABLED=False` it calls the function directly instead of `apply_async()`.

Three queues:
- `default` — light I/O (fetch, stream collectors, stage dispatch) — 4 workers
- `heavy` — NLP/LLM work (scoring, processing, clustering, topic matching, newsletters) — 4 workers (`celery -A app worker -Q heavy`); ML models load lazily per job, no preloading
- `bulk` — long one-shot jobs and pure dispatchers (price backfills, model training, the historical-article backfill dispatcher — its actual per-day-chunk fetch/save/process work runs on `heavy`, bounded to ~10min per chunk) — 1 worker

**The pipeline is a stage registry, not a set of per-step tasks.** `services/stages.py` declares each pull-based stage (fetch → score → process → geocode-repair → aggregate → tag → route) with its selection predicate, handler, chunk size, queue, and cadence. Exactly two Celery tasks execute all of them:
- `pipeline_tick_task` (cron, every 10 min) — dispatches every enabled stage that is due and has pending work
- `run_stage_chunk_task(stage_name, ids)` — the only fan-out worker

`dispatch_stage_task(stage_name)` force-dispatches one stage (admin buttons, manual repair). The dashboard's coverage table and the dispatcher read the same `pending_*` callables, so counts and behavior can't drift. Time-of-day jobs (topics, newsletter, forecast, maintenance) remain standalone crontab tasks.

The crontab (`api/crontab`, run by supercronic in the `api` container) dispatches everything via `manage.py run_task <task_name>`. To manually trigger any cron job locally: `python manage.py run_task <task_name> --sync`; to force one stage: `python manage.py run_task dispatch_stage_task stage_name=process --sync`.

Manual/CLI entry points (`manage.py fetch_data`, `manage.py process_articles`) select work through the SAME predicates the dispatcher uses (`services/stages.py::select_ids` / `fetch_source`) rather than keeping their own selection logic — so a manual run can't select a different set of records than the pipeline would.

### Pipeline flow

```
pipeline_tick_task (every 10m) — dispatches due stages from services/stages.py:
  fetch     (10m, default)  — per-source cursor (Source.last_fetched_at), RSS + title dedup
  score     (60m, heavy)    — LLM importance scoring, batches of 30 titles
  process   (30m, heavy)    — chunks of 8 = one batched LLM call
                              — LLM: category/sub-category/geo/intensity + EN translation
                              — local: NER (entities), VADER (sentiment), FinBERT (financial
                                sentiment), MarianMT (EN→AR translation)
  geocode   (12h, heavy)    — repair: reprocess processed-but-unlocated articles
  aggregate (30m, heavy)    — singleton: cluster + upsert Events, routes inline
  tag       (60m, heavy)    — EmbeddingTopicMatcher chunks of 10 (local — no LLM)
  route     (6h, heavy)     — repair only: events that missed inline routing
discover_topics_task (daily 05:00, LLM)
refresh_topics_task (daily 04:00, WikipediaCurrentEventsAdapter + LLM enrichment)
generate_newsletter_task (daily 06:00, LLM)
pipeline_health_task (every 30m) — freshness/staleness report, persisted to Redis and
  rendered on /admin/dashboard/'s Health section (see Admin below)
```

Stream tasks run independently: prices (5m), NOTAMs (15m), earthquakes (5m), forex (15m). Each saves to MongoDB and publishes to a Redis SSE channel (`sse:prices`, `sse:notams`, `sse:earthquakes`). A stream's `fetch()`/`save()` failure propagates out of `BaseStream.run()` so the task fails visibly (TaskRun status) instead of reporting a silent 0.

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

- Django admin at `/admin/`; custom operations dashboard at `/admin/dashboard/` (pipeline status, manual triggers, per-queue queued/running/failed counts + broker depth from `TaskRun`, a Health section rendering the last `pipeline_health_task` report — per-stage staleness, stream freshness, current-topics count). Individual tasks — args, result, status, retries, error/traceback — are browsable at `/admin/core/taskrun/`.
