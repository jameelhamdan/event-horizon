# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Event Horizon** (`eventhorizonai.dev`) — a live global-event map that ingests news from RSS sources, runs NLP and LLM analysis on each article, clusters articles into geographic events, streams real-time market/NOTAM/earthquake data, and serves it all via a DRF API to a React + Leaflet SPA.

## Commands

### Backend (Django — run from `api/`)

```bash
# Run the dev server (requires Mongo + Redis or TASK_QUEUE_ENABLED=false)
python manage.py runserver

# Run all migrations
python manage.py migrate

# Trigger a pipeline task manually (--sync runs in-process, no worker needed)
python manage.py run_task pipeline_tick_task --sync
python manage.py run_task dispatch_stage_task stage_name=annotate --sync

# Smoke-test LLM routing for a given role
python manage.py test_llm --role analyzer_lite --prompt "your prompt"

# End-to-end pipeline test (writes JSON report to results/e2e_pipeline/)
python manage.py e2e_pipeline
python manage.py e2e_pipeline --skip-fetch --skip-process   # --skip-process skips the annotate step

# Capstone evaluation (needs Mongo with real data; writes JSON reports to results/<command>/)
python manage.py evaluate_forecasting   # routing Precision@k + walk-forward 24h return MAE
python manage.py evaluate_freshness     # fetch→map latency P50/P95/P99

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
python -m tests.tests_annotator
python -m tests.tests_refiner
python -m tests.tests_stages
python -m tests.tests_topics_matcher
python -m tests.tests_historical
python -m tests.tests_wikipedia_history
python -m tests.tests_wayback_history
python -m tests.tests_forecasting_routing
python -m tests.tests_forecast_evaluate
DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_forecast   # slower (LightGBM roundtrip)

# Lint (dev-only tooling, not in the Docker image — uv pip install -e '.[dev]' or pip install -e '.[dev]')
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
- LLM credentials go in env (`GROQ_API_KEYS`, `CEREBRAS_API_KEYS`, `MISTRAL_API_KEYS`, `OPENROUTER_API_KEYS`, `OLLAMA_BASE_URL`); routing logic is in `settings/base.py` `LLM_ROUTES`. The LLM only handles category/sub-category/geo/intensity classification and newsletter/topic-enrichment prose — sentiment, translation, topic tagging, and event routing all run on local models (see LLM routing below).

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
    processing/            annotator.py (annotate stage: LLM-free NLP — prototype
                           embeddings + NER + rules + VADER/FinBERT/translation),
                           refiner.py (refine stage: zeroshot/ollama/cloud judge over
                           historical/backfill low-confidence output, REFINE_PROVIDER),
                           analyzer.py (cloud LLM prompt client — primary analyzer
                           for the 'analyze' stage's live traffic, and refiner's
                           'cloud' provider for historical),
                           taxonomy.py (category tree + prototypes + intensity priors),
                           geocode.py (gazetteer + aliases),
                           vader.py (local, sentiment), finbert.py (local, financial
                           sentiment), clustering.py
    translation/           Local EN→AR translation (MarianMT) — replaces LLM-generated translations
    topics/                matcher.py (EmbeddingTopicMatcher — local, default, semantic;
                           TopicMatcher — keyword fallback, used when the embedding model
                           can't load and for retroactive tagging), scraper.py, dedup.py,
                           sources/ (WikipediaCurrentEventsAdapter)
    streams/               prices.py, notam.py, earthquakes.py, forex.py — BaseStream.run()
                           re-raises fetch/save failures so a broken stream surfaces as a
                           FAILED TaskRun instead of a silent success-with-0
    data/                  DataService, rss.py (feedparser),
                           historical.py + wikipedia.py + wayback.py (historical backfill
                           discovery: Wikipedia Current Events monthly pages are the primary
                           path — curated per-day events with citations; per-publisher
                           supplements are Wayback front-page mining for recency-only-sitemap
                           publishers (wayback.py FRONTPAGES registry; paced client, optional
                           WAYBACK_PROXY_URL) and sitemap discovery for deep-archive ones;
                           bodies fetched live with Wayback capture fallback)
    scoring/               ImportanceScorer — source-weight/corroboration/floor
                           post-processing over the annotate stage's rules base (no LLM)
    workflow/              articles.py, events.py, topics.py — per-stage orchestration glue
                           (fetch/annotate/refine article flow, event aggregation, topic discover/
                           refresh/enrich) that doesn't belong in a single stateless module
    newsletter/            generator.py (LLM), sender.py (Markdown→HTML→SES)
    email/                 mailer.py, providers.py — SES wrapper used by newsletter + accounts
    forecasting/           routing.py (deterministic rules-based event→symbol routing),
                           features.py/model.py (as-of feature frame + LightGBM clf+reg),
                           backtest.py (walk-forward, 3 ablation arms), history.py (PriceBar
                           OHLC backfill), evaluate.py (capstone eval: routing Precision@k +
                           return MAE vs zero baseline)
    routing/               __init__.py — thin wrapper around forecasting/routing.py that
                           persists Event.affected_indicators (route_events()); no LLM path
  migrations/              Centralized — all apps map here via MIGRATION_MODULES
  tests/                   Management-command e2e tests + offline unit tests
  pyproject.toml           Runtime deps ([project.dependencies]), dev extra ([project.optional-dependencies].dev — currently just ruff, not installed in Docker), torch CPU index (tool.uv.sources), and ruff lint config (tool.ruff — F + E9/W6/E502 rules only, no style rules)
```

### Task execution model

Tasks are Celery tasks (`@shared_task`) in `services/tasks.py` and `newsletter/tasks.py` — calling one directly as a plain function still runs it synchronously. The `enqueue()` helper in `services/queue.py` wraps Celery (Redis broker); when `TASK_QUEUE_ENABLED=False` it calls the function directly instead of `apply_async()`.

Three queues:
- `default` — light I/O (fetch, stream collectors, stage dispatch) — 4 workers
- `heavy` — NLP/LLM work (scoring, processing, clustering, topic matching, newsletters) — 4 workers (`celery -A app worker -Q heavy`); ML models load lazily per job, no preloading
- `bulk` — long one-shot jobs and pure dispatchers (price backfills, model training, the historical-article backfill dispatcher — its actual per-day-chunk fetch/save/process work runs on `heavy`, bounded to ~10min per chunk) — 1 worker

**The pipeline is a stage registry, not a set of per-step tasks.** `services/stages.py` declares each pull-based stage (fetch → {analyze | annotate → refine} → aggregate → tag → route) with its selection predicate, handler, chunk size, queue, and cadence. `analyze` and `annotate` both consume `stage='fetched'` articles but partition disjointly — `analyze` claims only articles fetched within `LIVE_ANALYZE_FRESHNESS_HOURS`, `annotate` claims everything else (backfill-tagged, or aged out of that window) — so an article always lands in exactly one, never both, and never neither. `Article.stage` (`fetched → annotated | refine → refined`) is the stored pipeline-position field the predicates filter on; `analyze` and `annotate` both terminate confident output at `'annotated'`. Geocoding is not a stage — it runs inline in `analyze`/`annotate` (a local `geonamescache` lookup in `services/processing/geocode.py`). Exactly two Celery tasks execute all of them:
- `pipeline_tick_task` (cron, every 10 min) — dispatches every enabled stage that is due and has pending work
- `run_stage_chunk_task(stage_name, ids)` — the only fan-out worker

`dispatch_stage_task(stage_name)` force-dispatches one stage (admin buttons, manual repair). The dashboard's coverage table and the dispatcher read the same `pending_*` callables, so counts and behavior can't drift. Time-of-day jobs (topics, newsletter, forecast, maintenance) remain standalone crontab tasks.

The crontab (`api/crontab`, run by supercronic in the `api` container) dispatches everything via `manage.py run_task <task_name>`. To manually trigger any cron job locally: `python manage.py run_task <task_name> --sync`; to force one stage: `python manage.py run_task dispatch_stage_task stage_name=annotate --sync`.

Manual/CLI entry points (`manage.py fetch_data`, `manage.py process_articles`) select work through the SAME predicates the dispatcher uses (`services/stages.py::select_ids` / `fetch_source`) rather than keeping their own selection logic — so a manual run can't select a different set of records than the pipeline would.

### Pipeline flow

```
pipeline_tick_task (every 10m) — dispatches due stages from services/stages.py:
  fetch     (10m, default)  — per-source cursor (Source.last_fetched_at), RSS + title dedup
  analyze   (3h, heavy)     — chunks of 8; full cloud-LLM analysis (analyzer.py via
                              LLM_ROUTES['analyzer_lite']) for articles fetched within
                              LIVE_ANALYZE_FRESHNESS_HOURS (6h) — live traffic only,
                              gated by LIVE_LLM_ENABLED. Same output fields as annotate
                              (category/sub/geo/intensity/EN summary) plus local
                              VADER/FinBERT/MarianMT/importance, unchanged. Terminal on
                              success (stage='annotated' — same value annotate uses, so
                              nothing downstream cares which analyzer produced it). A
                              failed/skipped analysis leaves stage='fetched'; anything
                              that ages past the freshness window falls through to
                              annotate below instead of being stranded.
  annotate  (30m, heavy)    — chunks of 8; 100% on-prem NLP (annotator.py):
                              prototype-embedding classification (+ confidence),
                              NER→gazetteer geo, rule intensity, importance
                              (rules base + weights/corroboration/floors), VADER,
                              FinBERT, extractive EN summary, MarianMT AR (non-lite)
                              — handles all historical/backfill volume (cheap, no rate
                              limits) plus any live article analyze didn't reach in time
                              — advances Article.stage → 'annotated' (confident)
                                or 'refine' (low-confidence, queued for the judge)
                              — a failed annotation leaves stage='fetched' so the
                                stage retries it (no separate repair stage)
  refine    (60m, heavy)    — second-opinion judge over stage='refine' articles —
                              i.e. historical/backfill articles annotate flagged
                              low-confidence; live articles never reach this stage
                              (refiner.py; provider = REFINE_PROVIDER: zeroshot
                              default / ollama JSON-schema / cloud LLM / off)
                              — re-judges category/sub/geo/intensity; stage →
                                'refined' + refined_on; failed verdicts retry
  aggregate (30m, heavy)    — singleton: cluster + upsert Events, routes inline;
                              trailing AGGREGATE_LIVE_WINDOW_HOURS (72h) per tick
  tag       (60m, heavy)    — EmbeddingTopicMatcher chunks of 10 (local — no LLM)
  route     (6h, heavy)     — repair only: events that missed inline routing
aggregate_full_task (daily 01:00) — full 168h aggregate sweep, so multi-day events
  that age past the live 72h window still re-aggregate
discover_topics_task (daily 05:00, LLM)
refresh_topics_task (daily 04:00, WikipediaCurrentEventsAdapter + LLM enrichment)
generate_newsletter_task (daily 06:00, LLM)
pipeline_health_task (every 30m) — freshness/staleness report, persisted to Redis and
  rendered on /admin/dashboard/'s Health section (see Admin below)
```

Stream tasks run independently: prices (5m), NOTAMs (15m), earthquakes (5m), forex (15m). Each saves to MongoDB and publishes to a Redis SSE channel (`sse:prices`, `sse:notams`, `sse:earthquakes`). A stream's `fetch()`/`save()` failure propagates out of `BaseStream.run()` so the task fails visibly (TaskRun status) instead of reporting a silent 0.

### LLM routing

`get_llm_service(role)` in `services/llm/__init__.py` reads `settings.LLM_ROUTES[role]` (a list of provider names) and tries each in order on failure. Providers: `groq`, `cerebras`, `mistral`, `openrouter`, `ollama_small/medium/large`. Strip code fences before `json.loads()` — always use `services.llm.strip_code_fences()`.

Historical/backfill volume runs on local CPU models rather than the LLM — cheaper, faster, and no rate limits; live traffic gets a full cloud-LLM pass instead (see the `analyze` stage above), since its low volume actually fits within free-tier rate limits and buys meaningfully better quality. When touching these areas, prefer extending the local model over adding LLM calls to the (much higher-volume) historical/backfill path:

| Task | Historical/backfill (local) | Live (`analyze` stage, cloud LLM) | Module |
| ---- | ---------------------------- | ---------------------------------- | ------ |
| Article `category`/`sub_category`/geo/`intensity` | Taxonomy-prototype embeddings (same MiniLM as clustering) + confidence-gated zero-shot NLI judge (`mDeBERTa-v3-mnli-xnli`, refine stage), pretrained NER (`wikineural`) → gazetteer geocode, rule-based intensity | `analyzer.py` via `LLM_ROUTES['analyzer_lite']` — one batched prompt per chunk | `services/processing/annotator.py` + `refiner.py` (taxonomy + prototypes + priors in `taxonomy.py`; gazetteer/aliases in `geocode.py`) / `analyzer.py` |
| Article importance scoring | Intensity→1–10 rules base + weight/corroboration/floors (`importance_source='rules'`) | Same rules base + post-processing, fed by the LLM's intensity instead | `services/scoring/__init__.py::ImportanceScorer` |
| Article sentiment | VADER (rule-based) — same for both paths | | `services/processing/vader.py` |
| Arabic translation | `Helsinki-NLP/opus-mt-en-ar` (MarianMT) — same for both paths | | `services/translation/` |
| Event → topic tagging | sentence-transformer cosine similarity (`paraphrase-multilingual-MiniLM-L12-v2`, same model as clustering) | | `services/topics/matcher.py::EmbeddingTopicMatcher` |
| Event → market-symbol routing | Deterministic weighted rules (category/sub-category/country/sentiment) | | `services/forecasting/routing.py` |

`LIVE_ANALYZE_FRESHNESS_HOURS` (`services/stages.py`, default 6h) is what makes an article "live" for routing purposes: fetched within that window AND not backfill-tagged → `analyze`; everything else (backfill, or aged past the window) → `annotate`. `REFINE_PROVIDER` selects the refine stage's judge over annotate's low-confidence *historical* output only (live articles never reach refine): `'zeroshot'` (default, on-prem), `'ollama'` (local LLM, JSON-schema constrained via `services/llm get_provider('ollama_medium')`), `'cloud'` (`analyzer_lite` route, same client `analyze` uses — also refreshes the abstractive EN summary), or `'off'`. Per-model opt-outs: `NER_ENABLED`/`ZEROSHOT_ENABLED` env vars. Evaluate against live source data with `manage.py eval_analyzer [--refine]` + the `analyzer-eval` Claude skill.

Still LLM-driven (free-form generation, low volume): topic description/keyword enrichment + discovery (`services/workflow/topics.py`) and the daily newsletter (`services/newsletter/generator.py`).

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

- Django admin at `/admin/`; custom operations dashboard at `/admin/dashboard/` (pipeline status, manual triggers, per-queue queued/running/failed counts + broker depth from `TaskRun`, a Health section rendering the last `pipeline_health_task` report — per-stage staleness, stream freshness, current-topics count). Individual tasks — args, result, status, retries, error/traceback — are browsable at `/admin/core/taskrun/`. Flower (live Celery worker/task ground truth — TaskRun rows are best-effort history and can go stale if a worker is killed) runs as its own compose service with no published port, proxied at `/flower/` behind staff auth (`app/views.py` via django-proxy).
