# CLAUDE.md — Happinga-Meter Dev Guide

This file gives Claude everything needed to write correct, consistent code for this project without re-reading the codebase from scratch each session.

---

## Stack

| Layer | Tech |
|-------|------|
| Backend | Django 6 + django-mongodb-backend |
| Task queue | django-rq + Redis (two queues: `default`/light and `heavy`) |
| Scheduling | rq-scheduler (`setup_schedule` management command) |
| Storage | MongoDB 8 |
| Ingestion | feedparser (RSS) + requests |
| NLP | LLM entities/sentiment/category/geo + sentence-transformers + FinBERT + geonamescache |
| LLM | Multi-provider via `services/llm.py` — `openrouter` (default, proxy-URL rotation or direct keys), `ollama`; per-use-case routing + fallback chains in `settings.LLM_ROUTES` |
| Frontend | React 19 + Vite + react-router-dom + react-leaflet (TypeScript) |
| Real-time | Server-Sent Events (SSE) over Redis pub/sub |
| Email | AWS SES (newsletter + confirmation emails) |
| Serving | uvicorn (backend) + nginx reverse proxy |
| Containers | Docker Compose |

---

## Directory Map

> Ignore `__pycache__/` and `*.pyc` files everywhere — they are Python bytecode caches, not source.

```
./
├── api/                    # All Django/Python source (Docker build context: ./api, PYTHONPATH=/app)
│   ├── app/                # WSGI/ASGI entry, URLs, middleware, auth backend
│   │   ├── __init__.py     # Version string + build tag
│   │   ├── asgi.py         # ASGI application entry point
│   │   ├── urls.py         # Root URLconf — admin/ + api/
│   │   ├── backends.py     # ModelAuthBackend (respects user.can_login)
│   │   └── middleware.py   # X-App-Version header
│   ├── apps.py             # MongoAdminConfig, MongoAuthConfig, MongoContentTypesConfig
│   ├── core/               # Django app — data models + management commands
│   │   ├── apps.py         # name='core', label='core'
│   │   ├── models.py       # Source, Article, Event, Topic, PriceTick, PriceBar, Forecast,
│   │   │                   # NotamZone, NotamRecord, EarthquakeRecord, StaticPoint
│   │   ├── admin.py        # Admin for all core models (pipeline action buttons, import/export)
│   │   └── management/commands/
│   │       ├── fetch_data.py           # Enqueues fetch_articles_task
│   │       ├── process_articles.py     # Enqueues process_articles_task
│   │       ├── aggregate_events.py     # Enqueues aggregate_events_task
│   │       ├── refresh_topics.py       # Enqueues refresh_topics_task
│   │       ├── tag_topics.py           # Enqueues tag_topics_task
│   │       ├── retroactive_tag_topic.py # Enqueues retroactive_tag_topic_task
│   │       ├── fetch_stream.py         # One-off stream fetch (prices/notam/earthquakes/forex)
│   │       ├── bootstrap_static_points.py # Seeds exchanges, ports, central banks
│   │       ├── setup_schedule.py       # Registers all periodic jobs with rq-scheduler
│   │       ├── e2e_pipeline.py         # End-to-end pipeline test → JSON report
│   │       └── e2e_full.py             # Full-system invariant check (exits non-zero on failure); 13 stages
│   ├── accounts/           # Custom User model + Session + Group proxies
│   │   ├── apps.py         # name='accounts', label='accounts'
│   │   ├── models.py       # User (email-based), UserManager
│   │   └── admin.py
│   ├── api/                # DRF REST API
│   │   ├── apps.py         # name='api', label='api'
│   │   ├── serializers.py
│   │   ├── urls.py
│   │   └── views/
│   │       ├── events.py       # EventListView, EventDetailView, SourceListView,
│   │       │                   # PriceLatestView, PriceHistoryView, NotamZoneListView,
│   │       │                   # NotamHistoryView, EarthquakeListView, StaticPointListView,
│   │       │                   # TopicListView, TopicDetailView, TopicEventsView,
│   │       │                   # SSEStreamView
│   │       ├── forecasts.py     # ForecastListView, ForecastLatestView, ForecastAccuracyView (model-backed)
│   │       └── newsletter.py   # SubscribeView, ConfirmView, UnsubscribeView,
│   │                           # NewsletterListView, NewsletterLatestView, NewsletterDetailView
│   ├── newsletter/         # Django app — newsletter models + admin + tasks
│   │   ├── models.py       # DailyNewsletter, Subscriber
│   │   ├── admin.py
│   │   ├── tasks.py        # generate_newsletter_task, send_newsletter_task
│   │   └── management/commands/
│   │       ├── generate_newsletter.py
│   │       └── send_newsletter.py
│   ├── misc/               # Django app — EmailLog model (admin monitoring)
│   │   ├── models.py       # EmailLog
│   │   └── admin.py
│   ├── services/           # Stateless Python services (no Django models)
│   │   ├── tasks.py        # All pipeline task functions (plain Python — no decorator)
│   │   ├── queue.py        # enqueue() helper — wraps django-rq; sync fallback in dev
│   │   ├── workflow.py     # Workflow class — orchestrates pipeline steps
│   │   ├── llm.py          # LLM client wrapper (provider-agnostic) + strip_code_fences()
│   │   ├── scoring.py      # ArticleImportanceScorer (LLM batch 1–10 rating) + score_unscored_articles()
│   │   ├── text_utils.py   # Shared text primitives: tokenize(), jaccard(), STOP_WORDS
│   │   ├── tests_scoring.py # Dependency-light unit tests (text_utils, strip_code_fences, scorer, dedup)
│   │   ├── processing/     # NLP processing pipeline
│   │   │   ├── analyzer.py     # Article analysis (LLM category/sub-category, geonamescache geocoding, i18n)
│   │   │   ├── cleaner.py      # Text normalization
│   │   │   └── clustering.py   # SemanticClusterer — sentence-transformers
│   │   ├── topics/         # Topic management
│   │   │   ├── matcher.py      # TopicMatcher (keyword) + LLMTopicMatcher (batch LLM)
│   │   │   ├── scraper.py      # Orchestrates source adapters; TOPIC_SOURCES_DAYS env var
│   │   │   ├── dedup.py        # deduplicate_topics() + semantic_merge_topics()
│   │   │   ├── types.py        # TopicDict TypedDict
│   │   │   ├── _dates.py       # Date helpers — parses "March 2025" and "2022"
│   │   │   └── sources/
│   │   │       └── current_events.py   # WikipediaCurrentEventsAdapter (Portal:Current_events)
│   │   ├── streams/        # Real-time data streams
│   │   │   ├── base.py         # BaseStream abstract class
│   │   │   ├── prices.py       # Yahoo Finance + CoinGecko → PriceTick
│   │   │   ├── notam.py        # aviationweather.gov → NotamZone + NotamRecord
│   │   │   ├── earthquakes.py  # USGS FDSN → EarthquakeRecord
│   │   │   └── forex.py        # ECB → PriceTick (stream_key='forex')
│   │   ├── data/           # Ingestion — DataService, ArticleDatum
│   │   │   ├── __init__.py     # exports DataService
│   │   │   ├── base.py         # ArticleDatum TypedDict
│   │   │   ├── historical.py   # HistoricalBackfillService, RSSHistoricalService,
│   │   │   │                   # RankedArticle, WeekResult
│   │   │   └── rss.py          # RSSService (feedparser)
│   │   ├── forecasting/    # Event-fused symbol prediction (v2)
│   │   │   ├── routing.py      # route_event_to_weighted_symbols() — deterministic event→symbol (baseline+fallback)
│   │   │   ├── history.py      # OHLC backfill (yfinance + CoinGecko) → PriceBar
│   │   │   ├── features.py     # leak-free as-of feature matrix (price + event + topic features)
│   │   │   ├── model.py        # LightGBM classifier + regressor per horizon
│   │   │   ├── backtest.py     # walk-forward backtest, 4 ablation arms
│   │   │   └── tests_forecast.py  # dependency-light self-tests
│   │   ├── routing/        # LLMEventRouter (LLM event→symbol, rules fallback)
│   │   │   └── llm_router.py
│   │   ├── newsletter/     # Newsletter generation + sending
│   │   │   ├── generator.py    # generate_newsletter() — LLM-based section writer
│   │   │   └── sender.py       # send_newsletter() — Markdown→HTML, SES
│   │   └── email/          # Email delivery helpers (SES wrapper + confirmation emails)
│   ├── migrations/         # All app migrations (centralized, mapped via MIGRATION_MODULES)
│   │   ├── accounts/
│   │   ├── admin/
│   │   ├── auth/
│   │   ├── contenttypes/
│   │   ├── core/
│   │   ├── misc/
│   │   └── newsletter/
│   ├── settings/
│   │   └── base.py         # All config — DB, cache, RQ_QUEUES, auth, logging
│   ├── templates/
│   │   └── admin/core/
│   ├── manage.py           # Django CLI
│   ├── requirements.txt
│   ├── release.sh          # collectstatic + migrate (run by Docker on api startup)
│   └── Dockerfile
├── ui/                     # React 19 + Vite SPA (TypeScript, react-router-dom)
│   ├── src/
│   │   ├── main.tsx        # App entry — BrowserRouter + all Routes + LanguageProvider
│   │   ├── pages/
│   │   │   ├── index.tsx           # Main map page — activeTopic state, all overlays; sidebar = events list only
│   │   │   ├── markets.tsx         # Markets & Forecasts page — PriceTicker + ForecastPanel + EventsHeatmap
│   │   │   ├── about.tsx           # About page
│   │   │   ├── privacy.tsx         # Privacy policy
│   │   │   ├── terms.tsx           # Terms of service
│   │   │   └── newsletter/
│   │   │       ├── index.tsx       # Newsletter list + reader
│   │   │       ├── detail.tsx      # /newsletter/:year/:month/:day
│   │   │       ├── confirm.tsx     # /newsletter/confirm/:token
│   │   │       └── unsubscribe.tsx # /newsletter/unsubscribe/:token
│   │   ├── contexts/
│   │   │   └── LanguageContext.tsx # Global lang state (en/ar) + t translations object
│   │   ├── hooks/
│   │   │   ├── useSSE.ts           # EventSource wrapper with auto-reconnect
│   │   │   ├── useDocumentTitle.ts # Sets <title> + meta tags
│   │   │   └── useSubscribe.ts     # Newsletter subscription form state
│   │   ├── i18n/
│   │   │   ├── strings.ts          # UIStrings typed translations (en + ar)
│   │   │   └── categories.ts       # Category label translations + categoryLabel()
│   │   ├── api/            # Typed API client modules
│   │   │   ├── events.ts   # fetchEvents(), fetchEventDetail()
│   │   │   ├── newsletter.ts  # fetchNewsletters(), subscribeNewsletter()
│   │   │   ├── streams.ts  # fetchPrices(), fetchNotams(), fetchEarthquakes(),
│   │   │   │               # fetchStaticPoints(), fetchForecasts() (placeholder)
│   │   │   └── topics.ts   # fetchTopics(), fetchTopicDetail()
│   │   ├── components/
│   │   │   ├── layout.tsx          # SiteHeader — nav, language toggle
│   │   │   ├── CookieConsent.tsx   # Consent banner (localStorage)
│   │   │   ├── SubscribePopup.tsx  # Newsletter subscribe form
│   │   │   ├── StatusDisplay.tsx   # Reusable loading/error/success states
│   │   │   ├── CategoryBadge.tsx   # Colored category badge
│   │   │   ├── markdown.tsx        # Custom react-markdown renderer
│   │   │   ├── ui/                 # Button, Card, Input — reusable primitives
│   │   │   ├── events/
│   │   │   │   ├── EventCard.tsx       # Topic badges; onTopicClick prop
│   │   │   │   ├── EventList.tsx       # Passes topic props down
│   │   │   │   ├── EventUI.tsx         # CategoryBadge, EventMeta, useLocalizedField
│   │   │   │   ├── ForecastPanel.tsx   # Forecasts: 1d/5d toggle, direction/P(up)/Δ%, accuracy badge
│   │   │   │   ├── ForecastChart.tsx   # recharts daily close + dashed forward projection + band
│   │   │   │   ├── PriceChart.tsx      # recharts intraday PriceTick history (ticker)
│   │   │   │   ├── MapView.tsx         # L.divIcon category markers + all map layers
│   │   │   │   └── PriceTicker.tsx     # Real-time SSE price table
│   │   │   ├── topics/
│   │   │   │   └── TopicsPanel.tsx     # Active topics pill list, category colors
│   │   │   ├── markets/
│   │   │   │   └── EventsHeatmap.tsx   # weighted event→symbol heatmap + most-impacted bars
│   │   │   └── layers/
│   │   │       ├── NotamOverlay.tsx    # GeoJSON NOTAM zones with hover tooltips
│   │   │       └── EarthquakeLayer.tsx # USGS earthquake markers (magnitude circles)
│   │   └── types.ts        # All shared TypeScript types
│   ├── vite.config.ts      # Dev proxy /api → localhost:8000
│   └── Dockerfile
├── nginx/
│   └── templates/
│       └── default.conf.template  # nginx reverse proxy template (envsubst)
│                           # (backend version lives in api/version.txt; frontend in ui/package.json)
├── docker-compose.yml      # All services: nginx, api, worker-heavy, worker-light,
│                           # worker-bulk, scheduler, frontend, mongo, redis, cloudflared
└── CLAUDE.md               # ← you are here
```

---

## Features Overview

This is a real-time global event intelligence platform. Key feature areas:

| Feature | Description |
|---------|-------------|
| **Multi-source ingestion** | RSS feeds (feedparser) + web sources (requests) → Article objects |
| **NLP pipeline** | LLM entities + category/sub-category + sentiment + FinBERT financial sentiment + geonamescache geocoding + i18n translations |
| **Event aggregation** | Articles bucketed by (location, category, day) + semantic sub-clustering (multilingual sentence-transformers) |
| **Global topic tracking** | Wikipedia Portal:Current_events scraped daily → LLM-enriched topics → LLM semantic matching to events |
| **Stream data** | Real-time prices (Yahoo Finance + CoinGecko), NOTAMs (aviationweather.gov), earthquakes (USGS), forex (ECB) |
| **Daily newsletter** | LLM-generated per-category summaries → Markdown → HTML → AWS SES to subscribers |
| **Subscriber management** | Double opt-in email confirmation, token-based unsubscribe |
| **Interactive Leaflet map** | Event markers + NOTAM overlay + earthquake layer + static reference points |
| **Real-time SSE** | Redis pub/sub → Server-Sent Events → PriceTicker + NOTAM/earthquake notifications |
| **Dual-language UI** | English + Arabic translations (LLM-generated at process time; toggled via LanguageContext) |
| **Two-queue workers** | `default` queue (light I/O: fetch, prices, notam, earthquakes, forex) + `heavy` queue (NLP/LLM: process, aggregate, tag) |
| **Admin pipeline panel** | Article admin: a single **"Run full pipeline → Events"** button runs `run_pipeline_task` (fetch→process→aggregate→tag) as one ordered job, plus individual step buttons |

---

## Conventions

### Versioning

The app version lives in **two** files that must be bumped **together** on every release:

- `api/version.txt` — backend version (read at startup by `app/__init__.py`, exposed via the `X-App-Version` header)
- `ui/package.json` — frontend version (`"version"` field)

Keep both at the same value (e.g. `2.11.0`). When you bump the version, update both — never one without the other.

### Django Apps

- Django apps (`core`, `accounts`, `api`, `newsletter`, `misc`) live directly under `api/` with simple names:
  ```python
  name = 'core'
  label = 'core'
  ```
- `services/` contains stateless Python modules only — no Django models, no AppConfig
- `AUTH_USER_MODEL = 'accounts.User'` (label-based, not import path)
- Never import `accounts.User` directly — always use `get_user_model()`
- Always import models explicitly: `from core import models as core_models`
- `apps.py` at `api/apps.py` defines `MongoAdminConfig`, `MongoAuthConfig`, `MongoContentTypesConfig` — these set `default_auto_field = ObjectIdAutoField` for Django's built-in apps

### Migrations

- All migrations are centralized under `api/migrations/` and mapped via `MIGRATION_MODULES` in settings
- Django built-in apps (`auth`, `admin`, `contenttypes`) use custom MongoDB-compatible migrations — all use `ObjectIdAutoField` PKs
- Never run `makemigrations` for `auth`, `admin`, or `contenttypes` — manage those manually

### Models

- All core data models use `MongoManager` from `django-mongodb-backend`
- Never use `__date` ORM lookup on MongoDB — use explicit datetime range:
  ```python
  # Wrong
  Article.objects.filter(published_on__date=today)
  # Right
  Article.objects.filter(published_on__gte=start_of_day, published_on__lt=end_of_day)
  ```
- `Article.article_ids` stores UUID strings — convert before ORM filter:
  ```python
  uuids = [uuid.UUID(a) for a in event.article_ids]
  articles = Article.objects.filter(id__in=uuids)
  ```
- `Article.banner_image_url` — nullable URLField; populated by RSS `media:content`/`media:thumbnail`/enclosure extraction at fetch time, or OG image scrape during `process_articles` (best-effort, HTTPS only)
- `Article.translations` — JSON dict keyed by language code (e.g. `{"ar": {"title": "...", "summary": "..."}}`)
- `Article.importance_score` — float 1.0–10.0 (nullable); assigned by `score_articles_task` via LLM + source weight multiplier + corroboration bonus. Set by `ArticleImportanceScorer` in `services/scoring.py`
- `Article.importance_source` — char `'llm'` or `'default'`; `'llm'` if the score came from a real LLM call, `'default'` if the LLM call failed and the fallback score was used
- `Source.weight` — float multiplier (default 1.0) applied to the LLM importance score; `0` suppresses the source (score → 1.0 minimum); adjusted automatically by `adjust_source_weights_task`
- `Source.weight_locked` — bool; when True, `adjust_source_weights_task` leaves `weight` unchanged for that source
- `Event.started_at` is a DateTimeField — always timezone-aware (`django.utils.timezone.now()`)
- `Event.topic_slugs` — list of matched topic slugs (tagged by `tag_topics_task`)
- `Event.topics` — dict of `{slug: confidence}` (float 0–1.0)
- `NotamZone` — current live NOTAM state (upserted by `notam_id`); fields: `notam_id`, `notam_type`, `geometry` (GeoJSON), `effective_from`, `effective_to`, `is_active`, `altitude_min_ft`, `altitude_max_ft`, `country_code`
- `NotamRecord` — append-only NOTAM history (every fetch); same fields + `fetched_at`
- `EarthquakeRecord` — USGS events; fields: `usgs_id` (unique), `magnitude`, `depth_km`, `location_name`, `latitude`, `longitude`, `occurred_at`, `tsunami_alert`, `alert_level` (green/yellow/orange/red)
- `PriceTick` — price samples; fields: `symbol`, `stream_key` (crypto/stock/commodity/forex/bond), `value`, `change_pct`, `volume`, `occurred_at`; 1-year TTL in production
- `PriceBar` — daily OHLC (forecasting substrate, no TTL); fields: `symbol`, `stream_key`, `name`, `interval` (`1d`), `open/high/low/close`, `volume`, `date`; backfilled via `services/forecasting/history.py`
- `Forecast` — model-backed forecast (one per symbol+horizon); fields: `symbol`, `stream_key`, `generated_at`, `as_of_date`, `horizon_days` (1\|5), `direction`, `proba_up`, `predicted_change_pct`, `predicted_price`, `band_low/high`, `confidence`, `current_value`, `router_source` (llm/rules), `model_version`, `realized_direction/change_pct`, `is_correct`, `scored_at`
- `MarketSymbol` — **single source of truth** for fetched/forecast/UI symbols (replaces hardcoded symbol lists). Fields: `symbol` (unique), `name`, `stream_key`, `provider` (yahoo/coingecko/ecb), `provider_id`, `group`, `is_active` (fetched by streams), `is_forecast` (forecasting panel target), `is_popular`+`rank`, `display_order`, `metadata`. Read via `services/market_symbols.py` helpers (graceful fallback to hardcoded defaults if empty). Seeded by migration `0006`. See [docs/symbols.md](docs/symbols.md).
- `TaskRun` — legacy per-execution record (status, duration, items, error, job_id). No longer auto-written; job history is now provided by the django-rq admin panel at `/admin/django-rq/`. Migration `0007`.
- `Article.stage_status` / `Event.stage_status` — per-record `{stage: {ok, at, error}}` written by `services/stages.py::mark_stage` (Article: `process`/`geocode`; Event: `tag`/`route`). Feeds `Workflow.pipeline_coverage()`. Migration `0008`.
- `Article.word_count` — int; populated at fetch time from the raw body; articles below `ARTICLE_MIN_WORD_COUNT` (default 30) are filtered before saving
- `misc` app contains only `EmailLog` model — admin panel for monitoring sent emails
- `Subscriber` in `newsletter/models.py` — fields: `email` (unique), `token` (UUID), `subscribed_at`, `confirmed_at` (nullable), `is_active`, `unsubscribed_at`; lifecycle: pending → confirmed → unsubscribed

### Tasks / Background Jobs

All task functions live in `services/tasks.py` (pipeline + streams + topics) and `newsletter/tasks.py`. They are **plain Python functions** — no decorator.

- Enqueue: `from services.queue import enqueue; enqueue(my_task, arg1, kwarg=val)`
- Task names follow the `*_task` suffix convention
- Task functions must **return a value** (usually an `int` count) — django-rq stores it as the job result, visible in the `/admin/django-rq/` panel
- Management commands call task functions **directly** for inline/foreground execution; use `--background` to enqueue instead
- `enqueue()` calls the function synchronously when `TASK_QUEUE_ENABLED=False` (dev default)
- **Queue routing**: pass `queue='heavy'` to `enqueue()` for NLP/LLM tasks; default queue is `'default'` (light I/O)

To add a new background task:
1. Write the plain function in `services/tasks.py`; return a meaningful value (int count of records affected)
2. Enqueue it: `from services.queue import enqueue; enqueue(my_task, queue='heavy', ...)`
3. Add it to `setup_schedule.py` if it should run periodically

### Scheduling (rq-scheduler)

All periodic jobs are registered by the `setup_schedule` management command (`api/core/management/commands/setup_schedule.py`). The `scheduler` Docker service runs this command on startup then launches `rqscheduler`.

**Light queue (`default`) — fast I/O:**

| Task | Default interval | Env var |
|---|---|---|
| `fetch_articles_task` | 10m | `FETCH_INTERVAL_MINUTES` |
| `fetch_prices_task` | 5m | `PRICE_FETCH_INTERVAL_MINUTES` |
| `fetch_notams_task` | 15m | `NOTAM_FETCH_INTERVAL_MINUTES` |
| `fetch_earthquakes_task` | 5m | `EARTHQUAKE_FETCH_INTERVAL_MINUTES` |
| `fetch_forex_task` | 15m | `FOREX_FETCH_INTERVAL_MINUTES` |

**Heavy queue — NLP/LLM:**

| Task | Default interval | Env var |
|---|---|---|
| `process_articles_task` | 240m | `PROCESS_INTERVAL_MINUTES` |
| `aggregate_events_task` | 240m | `AGGREGATE_INTERVAL_MINUTES` |
| `score_articles_task` | 60m | `SCORE_INTERVAL_MINUTES` |

**Note:** `tag_topics_task` is not scheduled independently — it is enqueued automatically by `aggregate_events_task` on completion, using the same `hours` window. There is no `TAG_TOPICS_INTERVAL_MINUTES` env var.

**Light queue (`default`) — maintenance:**

| Task | Default interval | Env var |
|---|---|---|
| `cleanup_low_importance_articles_task` | daily at 03:00 UTC | — |
| `prune_stale_articles_task` | daily at 02:00 UTC | — |
| `adjust_source_weights_task` | weekly on Monday | — |

**Cron jobs (heavy queue):**

| Task | Schedule | Env var |
|---|---|---|
| `refresh_topics_task` | daily at 04:00 UTC | `TOPICS_REFRESH_HOUR` |
| `discover_topics_task` | daily at 05:00 UTC | `DISCOVER_TOPICS_HOUR` |
| `generate_newsletter_task` | daily at 06:00 UTC | `NEWSLETTER_GENERATE_HOUR` |

**Forecasting (bulk queue):**

| Task | Default interval | Env var |
|---|---|---|
| `dispatch_route_events_task` | 360m | `ROUTE_EVENTS_INTERVAL_MINUTES` |
| `backfill_prices_task` | weekly (first run: 1 week after deploy) | — |

Task functions are scheduled directly — `scheduler.schedule(when, func, ...)` — with no wrapper. Return values flow into RQ's job result store and appear in `/admin/django-rq/`.

To change an interval: update the env var and restart the `scheduler` service (it re-runs `setup_schedule` on startup, clearing and re-registering all jobs).

### Workers (Three Queues)

Three RQ worker pools run in Docker, sized by workload (concurrency = process count
via `rqworker-pool --num-workers`, **not** threads — an RQ worker runs one job at a time):

```bash
python manage.py rqworker-pool default --num-workers 4   # worker-light: fast I/O
python manage.py rqworker-pool heavy   --num-workers 2   # worker-heavy: steady NLP/LLM
python manage.py rqworker-pool bulk    --num-workers 1   # worker-bulk: long one-shots
```

`RQ_QUEUES` defines `default`, `heavy`, and `bulk` (all on Redis). Pick the queue by
workload, not just cost:
- **`default`** (4 workers) — fast I/O: fetchers, stream collectors, dispatchers.
- **`heavy`** (2 workers) — steady NLP/LLM: process/tag/route/score per-record workers. Sized to the LLM key/proxy rotation depth (rate-limiting belongs in the LLM client, not in worker count).
- **`bulk`** (1 worker, `DEFAULT_TIMEOUT=-1`) — long one-shot jobs: multi-year `backfill_history`/`backfill_all_sources`/`backfill_prices` and `train_forecast_model_task`. Isolated so an hours-long job never blocks the live pipeline.

### Scheduler

The `scheduler` Docker service runs `setup_schedule` then `rqscheduler`:

```
command: sh -c "python manage.py setup_schedule && rqscheduler --url $${REDIS_URL:-redis://redis:6379/0}"
```

`setup_schedule` clears all existing scheduled jobs and re-registers them — idempotent, safe to re-run.

### Semantic Clustering

`api/services/processing/clustering.py`:
- `SemanticClusterer.cluster(articles, threshold=0.55)` — groups articles by title similarity
- Model: `paraphrase-multilingual-MiniLM-L12-v2` (multilingual, ~90 MB, CPU-only)
- Uses `sentence_transformers.util.community_detection()` with `min_community_size=1`
- Model loaded lazily via `@cached_property`; singleton via `get_clusterer()`
- Called during `aggregate_events` AFTER geographic + category bucketing

### Streams (Real-Time Data)

`api/services/streams/`:
- All streams extend `BaseStream` (`base.py`) — implements `run()` → fetch → save → Redis publish
- `redis_publish(channel, payload)` broadcasts JSON to SSE subscribers
- Redis channels: `sse:prices`, `sse:notams`, `sse:earthquakes`
- **prices.py**: Yahoo Finance (stocks, ETFs, bonds, commodities) + CoinGecko (crypto); saves `PriceTick`
- **notam.py**: aviationweather.gov global NOTAM API; upserts `NotamZone` (live), appends `NotamRecord` (history); geometry stored as GeoJSON Polygon
- **earthquakes.py**: USGS FDSN event API; min magnitude configurable via `EARTHQUAKE_MIN_MAGNITUDE` (default 3.0); saves `EarthquakeRecord`; includes tsunami alert, alert level
- **forex.py**: ECB Statistical Data Warehouse (no API key); EUR pairs (USD, JPY, GBP, CNY, CHF); saves `PriceTick` with `stream_key='forex'`

### SSE (Server-Sent Events)

`GET /api/sse/` — async ASGI view that subscribes to Redis channels and streams events to connected clients.

- Event types emitted: `connected`, `price_tick`, `notam_update`, `earthquake_update`
- Each stream task publishes to Redis after saving; `SSEStreamView` relays to browser
- Frontend hook: `useSSE` (`ui/src/hooks/useSSE.ts`) — wraps `EventSource`, auto-reconnects on drop (5s backoff), calls handler per event type
- `PriceTicker` component uses `useSSE` for live price updates

### Forecasting (event-fused symbol prediction — v2)

The prediction layer was **rebuilt** as event-fused symbol forecasting. Full design + diagrams:
[`docs/forecasting.md`](docs/forecasting.md). The core idea: the **event→symbol router output is a
FEATURE/hypothesis, not the label**; the label is the realized return between two real price nodes
(`close@t → close@t+horizon`). The model learns whether events actually predict the panel.

Pipeline: `route_events` (LLM, rule fallback) → `Event.affected_indicators` → `features.py`
(leak-free, as-of `Event.latest_article_at`, fuses price + event + tagged-topic features) →
LightGBM **classifier (calibrated P(up)) + regressor (magnitude)** per horizon (**1d, 5d**) →
`Forecast` rows → `score_forecasts_task` fills realized outcome.

- **Panel symbols** (`services/forecasting/routing.py` `PANEL_SYMBOLS`): GC=F, CL=F, NG=F, ZW=F, DX-Y.NYB, ^TNX, ^VIX, SPY, BTC-USD, ETH-USD.
- **Two routers** (both write `Event.affected_indicators` + `Event.router_source`):
  - `services/forecasting/routing.py` — deterministic weight product (baseline + fallback).
  - `services/routing/llm_router.py` `LLMEventRouter` — batched LLM (role `'routing'`), falls back per-event to the deterministic router on any error.
- **Data:** `PriceBar` (daily OHLC, backfilled via **yfinance** in `services/forecasting/history.py`) is the training/charting substrate, distinct from the high-frequency `PriceTick`. **Crypto OHLC is fetched via yfinance too** (BTC-USD/ETH-USD resolve natively) because the CoinGecko free tier caps history at ~365 days — CoinGecko stays the live-tick source and a yfinance fallback only. Backfill is **incremental** (only the tail since the last stored bar; `--full` forces a full re-pull) and defaults to **10 years**.
- **Models:** `Forecast` (model-backed) + `PriceBar`. Artifacts persist per horizon under `FORECAST_MODEL_DIR` (`model_h{h}.joblib`), loaded lazily/cached.
- **Backtest** (the gradeable artifact): `services/forecasting/backtest.py` — walk-forward, 4 ablation arms (naive / price-only / price+rule-events / price+llm-events), reports accuracy/F1/AUC/Brier + reliability, with a leakage self-check; `evaluate_forecast` writes a JSON report.
- **Tasks:** `backfill_prices_task`, `route_events_task`, `train_forecast_model_task`, `run_forecast_task`, `score_forecasts_task` (all in `services/tasks.py`, scheduled in `setup_schedule.py`, gated by `FORECAST_ENABLED`).
- **Commands:** `backfill_prices`, `route_events`, `train_forecast`, `run_forecast`, `evaluate_forecast`, `forecast_e2e` (full-flow runner).
- **API:** model-backed `ForecastSerializer`; `GET /api/forecasts/` + `/latest/` (param `horizon`), `/api/forecasts/accuracy/`, `/api/prices/<symbol>/bars/`.
- **UI:** dedicated **`/markets` page** (`pages/markets.tsx`) with live `PriceTicker`, `ForecastPanel.tsx` (1d/5d toggle, direction/P(up)/Δ%, accuracy badge, expandable chart) + `ForecastChart.tsx` (recharts daily close + dashed forward projection + confidence band), and `markets/EventsHeatmap.tsx` (weighted event→symbol heatmap). The intraday `PriceChart.tsx` (PriceTick) is unchanged.
- **Tests:** `services/forecasting/tests_forecast.py` — dependency-light self-tests (leakage, router fallback, metrics, train/predict roundtrip): `DJANGO_SETTINGS_MODULE=settings.base python -m services.forecasting.tests_forecast`.
- **Settings:** `FORECAST_ENABLED`, `FORECAST_MODEL_DIR`, `FORECAST_HORIZONS_DAYS` (`1,5`), `FORECAST_TRAIN_WINDOW_DAYS`, `FORECAST_ROUTER` (`llm`/`rules`); deps `lightgbm` + `scikit-learn` + `joblib`; LLM route `'routing'`.

### Topics

`api/services/topics/`:
- `matcher.py` — two matchers:
  - `TopicMatcher` — keyword-overlap; used by `retroactive_tag_topic` (fast, no LLM)
  - `LLMTopicMatcher` — batch LLM semantic matching; used by `tag_events_with_topics`; sends 10 events per call; falls back to `TopicMatcher` per-event on any LLM error
- `scraper.py` — runs `WikipediaCurrentEventsAdapter`; lookback window via `TOPIC_SOURCES_DAYS` env var (default: `30`)
- `sources/current_events.py` — `WikipediaCurrentEventsAdapter`: fetches `Portal:Current_events` daily subpages going back `num_days`; extracts situation-level prefixes (text before `:` in bullets); category from section heading
- `dedup.py` — `deduplicate_topics()` (slug-level) + `semantic_merge_topics()` (cosine ≥ 0.85)
- `_dates.py` — `parse_approximate_date()`: handles `"October 2023"` and year-only `"2014"`
- `Topic` model fields: `slug`, `name`, `keywords`, `description`, `category`, `is_current`, `is_active`, `source_ids`, `started_at`, `ended_at`, `topic_score`, `is_top_level`, `is_pinned`, `historical_month/day/year`
- `is_current` — in today's news cycle; `is_active` — enabled for display; `is_top_level` — promoted by score ≥ `TOP_LEVEL_SCORE_THRESHOLD` or `is_pinned`
- **Auto-hide stale topics**: `Workflow.prune_stale_topics()` (run in `refresh_topics`, daily) sets `is_top_level=False` for any non-pinned top-level topic with no tagged events in `TOPIC_STALE_DAYS` (default 90), so dormant topics drop off the header. Pinned topics and topics first seen within the window are exempt; `is_current`/`is_active` are left to the scrape lifecycle.
- Frontend API: `GET /api/topics/?active=true&current=true`

### Newsletter

- `DailyNewsletter` in `api/newsletter/models.py` — fields: `date` (unique), `subject`, `body` (Markdown), `articles` (JSON snapshot), `cover_image_url`, `cover_image_credit`, `generated_at`, `sent_at`, `sent_count`, `status` (draft/sending/sent/error), `event_count`
- `Subscriber` in `api/newsletter/models.py` — double opt-in: `email`, `token` (UUID), `subscribed_at`, `confirmed_at`, `is_active`, `unsubscribed_at`
- Newsletter body is stored as **Markdown** and converted to HTML at send time in `sender.py` — `<h2>` tags get inline-styled for email client compatibility
- `generate_newsletter()` in `services/newsletter/generator.py` — groups events by category, sends per-category LLM prompt, stores article snapshot + cover image; idempotent (skips if date exists)
- `send_newsletter()` in `services/newsletter/sender.py` — converts Markdown → HTML, sends to active subscribers via AWS SES; skips already-sent newsletters; logs to `EmailLog`
- `send_confirmation_email(subscriber)` in `services/email/` — sends double opt-in link
- `ArticleDatum` in `services/data/base.py` uses a required base TypedDict + optional `banner_image_url` extension (`total=False` on the subclass only); all other fields are required
- Frontend newsletter routes: `/newsletter`, `/newsletter/:year/:month/:day`, `/newsletter/confirm/:token`, `/newsletter/unsubscribe/:token`
- `NewsletterView` accepts an optional `initialData` prop — pass it to skip the internal fetch when data is already loaded

### NLP / Processing

- `services/processing/analyzer.py` — single LLM call per article: category + sub-category, city/country, entities, sentiment, intensity (0–1 newsworthiness/severity), and i18n translations (en + ar). geonamescache geocoding. `cleaner.py` orchestrates the analyzer + FinBERT (`finbert.py`); no local NLP heuristics — `event_intensity` is the LLM `intensity`
- `services/processing/cleaner.py` — HTML tag removal, whitespace normalization, non-ASCII handling
- `services/processing/clustering.py` — semantic event grouping (see above)
- `ArticleDocument` and `ArticleFeatures` dataclasses live in `core/models.py`

### Article Importance Scoring

`services/scoring.py`:
- `ArticleImportanceScorer.score_articles(articles)` — LLM batch scores (1.0–10.0), applies `source.weight` multiplier, cross-source corroboration bonus (+0.5 per extra source, max +2.0), and per-category floor (conflict/disaster ≥ 6.0, political/economic ≥ 4.0)
- `score_unscored_articles(hours, article_ids=None)` — main entry point for `score_articles_task`; accepts optional `article_ids` list to score specific records without touching the unscored queue
- `ArticleImportanceScorer.BATCH_SIZE = 30` — headlines per LLM call
- `ArticleImportanceScorer.DEFAULT_SCORE = 5.0` — fallback when LLM call fails
- LLM role: `'scoring'`; uses `strip_code_fences()` before JSON parsing
- `source.weight = 0` is honoured (score clamps to 1.0 minimum, not coerced to neutral 1.0 multiplier)
- `score_batch_llm(titles, role='scoring')` — accepts a `role` parameter for routing (e.g. `'historical'` for backfill)

### Shared Text Utilities

`services/text_utils.py` — canonical text primitives shared across the codebase:
- `tokenize(text) → frozenset[str]` — lowercase word tokens, drops stop words and tokens ≤ 2 chars; returns `frozenset` so it's safe to use in set operations and as dict/cache keys
- `jaccard(a, b) → float` — Jaccard similarity between two token sets; 0.0 if either is empty
- `STOP_WORDS: frozenset[str]` — 39-word list tuned for news dedup (not the full NLP stop list)

Consumers:
- `services/data/__init__.py` — `_filter_title_dupes` uses `_tokenize_title` + `_jaccard` (aliased from `text_utils`)
- `services/scoring.py` — `_corroboration_bonuses` uses `_tokenize` + `_jaccard`
- `services/topics/matcher.py` — `TopicMatcher._tokenize`
- `services/topics/sources/current_events.py` — `_emit_topic` keywords

### LLM Utilities

`services/llm.py` also provides:
- `strip_code_fences(text) → str` — strips `` ```json `` / `` ``` `` markdown wrappers LLMs sometimes return around JSON responses; handles `None` input safely. Use this before every `json.loads()` call on LLM output — do not re-implement inline.

### Title Deduplication

`services/data.__init__._filter_title_dupes(datums, threshold=0.75, hours=24)`:
- Drops incoming articles whose title is a near-duplicate of a recently fetched one (Jaccard ≥ threshold)
- Maintains a rolling window of title token sets in Django's cache (Redis key `article_title_dedup`)
- **Intra-batch dedup**: checked against `new_sets` (grows as batch is accepted), so two near-identical articles in the *same* fetch batch are both caught
- Controlled by `ARTICLE_DEDUP_TITLE_ENABLED` / `ARTICLE_DEDUP_JACCARD_THRESHOLD` / `ARTICLE_DEDUP_HOURS`
- Articles with empty title are always kept (no tokens → no match)

### API (DRF)

- All views use `rest_framework.views.APIView` or `generics.*`
- All responses serialized via DRF serializers in `api/serializers.py`
- No raw `JsonResponse` — use `Response` from `rest_framework.response`
- URL pattern: `/api/<resource>/` list, `/api/<resource>/<id>/` detail

**Full endpoint reference:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/events/` | Events list; params: `category`, `topic`, `start`, `end`, `limit` (max 500), `bbox` |
| GET | `/api/events/<id>/` | Event detail + related articles |
| GET | `/api/sources/` | All configured data sources |
| GET | `/api/prices/latest/` | Most recent price tick per symbol; param: `stream_key` |
| GET | `/api/prices/<symbol>/` | Price history (PriceTick); params: `from`, `to`, `limit` (max 5000) |
| GET | `/api/prices/<symbol>/bars/` | Daily OHLC history (PriceBar); params: `interval`, `limit` (max 5000) |
| GET | `/api/notams/` | Active NOTAM zones; params: `active`, `country_code`, `notam_type` |
| GET | `/api/notams/history/` | NOTAM record history; params: `from`, `to`, `country_code`, `status`, `limit` |
| GET | `/api/earthquakes/` | Global earthquakes; params: `min_magnitude` (default 3.0), `hours` (default 24), `limit` |
| GET | `/api/static-points/` | Reference points (exchanges, ports, banks); params: `type`, `country_code` |
| GET | `/api/topics/` | Topics list; params: `active`, `current`, `top_level`, `category`, `date`, `parent`, `source`, `month`, `year` |
| GET | `/api/topics/<slug>/` | Topic detail |
| GET | `/api/topics/<slug>/events/` | Events tagged with topic; params: `start`, `end`, `limit` |
| GET | `/api/forecasts/` | Latest forecast per (symbol, horizon); params: `symbol`, `stream_key`, `horizon` (1\|5) |
| GET | `/api/forecasts/latest/` | Same as above (newest per symbol+horizon) |
| GET | `/api/forecasts/accuracy/` | Rolling directional accuracy + Brier over scored forecasts; param: `symbol` |
| GET | `/api/sse/` | Server-Sent Events stream (prices, NOTAMs, earthquakes) |
| POST | `/api/newsletter/subscribe/` | Subscribe; body: `{"email": "..."}` — rate limited 5/hour |
| GET | `/api/newsletter/confirm/<token>/` | Confirm subscription via token |
| GET | `/api/newsletter/unsubscribe/<token>/` | Unsubscribe via token |
| GET | `/api/newsletter/` | Newsletter list (paginated, ordered by date DESC) |
| GET | `/api/newsletter/latest/` | Most recent sent newsletter |
| GET | `/api/newsletter/<YYYY-MM-DD>/` | Newsletter by date |

### Frontend

- All API calls go through typed modules in `src/api/` (`events.ts`, `newsletter.ts`, `streams.ts`, `topics.ts`)
- React state lives in `src/pages/index.tsx`; pass down as props
- Map markers use custom `L.divIcon` via category shape SVG; never plain `Marker` with default icon
- Frontend uses **react-router-dom** `BrowserRouter` with routes defined in `src/main.tsx`
- **Top-level pages / nav tabs** (in `SiteHeader`): **Map** (`/`, event map; sidebar is the events list only) and **Markets** (`/markets`, live prices + forecasts + the event→market `EventsHeatmap`). Clicking an event's affected-indicator chip cross-links to `/markets?symbol=<symbol>` (PriceTicker focuses it). Markets/Forecasts are no longer sidebar sub-tabs on the map page.
- Route params available via `useParams()` from react-router-dom
- All source files are TypeScript (`.tsx`/`.ts`) — not `.jsx`/`.js`
- Dark theme color palette (inline styles):
  - Background: `#0f0f13`
  - Card: `#1a1a22`
  - Border: `#2a2a35`
  - Text primary: `#e8e8f0`
  - Text secondary: `#888899`
- Category colors (defined in `MapView.tsx` and `EventCard.tsx` — keep in sync):
  ```ts
  const CATEGORY_COLOR: Record<string, string> = {
    conflict:  '#e05252',
    protest:   '#e09652',
    disaster:  '#e0c852',
    political: '#7c9ef8',
    economic:  '#52c8a0',
    crime:     '#c852c8',
    general:   '#888',
  }
  ```
- Topic filtering: `activeTopic: string | null` state in `index.tsx`; passed to `fetchEvents()` as `?topic=<slug>` and down to `EventList` / `EventCard` for badge highlighting
- `TopicsPanel` fetches `active=true&current=true` topics; clicking a topic pill toggles `activeTopic`
- `EventCard` renders up to 3 topic slug badges; active badge highlighted in blue; overflow shown as `+N more`
- Document titles set via `useDocumentTitle()` hook — every page component should call it
- Real-time data via `useSSE()` hook — connects to `/api/sse/`, auto-reconnects on drop

### Frontend i18n

- **All user-visible strings** must go through the i18n system — never hardcode English text in components
- Access translations: `const { t, lang } = useLanguage()` (from `LanguageContext`)
- Strings defined in `ui/src/i18n/strings.ts` (`UIStrings` interface) for both `en` and `ar`
- `LanguageContext` is provided in `main.tsx` wrapping the whole app
- When adding a new string: add the key to `UIStrings` interface and both `en` and `ar` objects in `strings.ts`
- `categoryLabel(slug)` from `ui/src/i18n/categories.ts` for translating event category names
- Format helpers: `t.minutesAgo(n)`, `t.hoursAgo(n)`, `t.daysAgo(n)`, `t.articleCount(n)`, `t.eventCount(n)`

---

## Recipes — Common Tasks

### Add a new API endpoint

1. Add serializer to `api/api/serializers.py`
2. Add view to `api/api/views/` — subclass `APIView` or `generics.ListAPIView`
3. Register URL in `api/api/urls.py`
4. Add fetch function in `ui/src/api/`

### Add a new model field

1. Add field to model in `api/core/models.py`
2. Run `python manage.py makemigrations core`
3. Update relevant serializer in `api/api/serializers.py`
4. Update admin in `api/core/admin.py` if needed

### Add a new management command

1. Create `api/core/management/commands/<name>.py`
2. Subclass `BaseCommand`, implement `handle(self, *args, **options)`
3. Import models as `from core import models as core_models`
4. Call `from services.queue import enqueue; enqueue(my_task, ...)` for background execution

### Add a new scheduled job

1. Write a plain function in `services/tasks.py`
2. Add a `scheduler.schedule(...)` or `scheduler.cron(...)` call in `api/core/management/commands/setup_schedule.py` — pass `queue='heavy'` for NLP/LLM jobs
3. Restart the `scheduler` Docker service to apply

### Backfill historical data for a source

```bash
# Dry run first to check coverage
python manage.py backfill_history <source_code> \
    --start-date 2022-01-01 --end-date 2025-01-01 --dry-run

# Run the backfill — per-week cap defaults to the source's weight (10–25 by priority)
python manage.py backfill_history <source_code> \
    --start-date 2016-01-01 --end-date 2026-01-01           # ~10 years
python manage.py backfill_history <source_code> \
    --start-date 2022-01-01 --end-date 2025-01-01 --top-n 15  # override the cap

# Resume after interruption (checkpoint in Django cache)
python manage.py backfill_history <source_code> \
    --start-date 2022-01-01 --end-date 2025-01-01 --resume

# Then process the new articles through the NLP pipeline
python manage.py process_articles --limit 5000
```

RSS sources rank by LLM significance score (batch of 30 headlines per call).
Service code: `api/services/data/historical.py` — `HistoricalBackfillService`.

- **Per-source priority:** `--top-n` defaults to `None` → each source's per-week cap is derived from its `Source.weight` (0.1–2.0) via `services.tasks._weighted_top_n` (weight 0.1→10, 1.0→~17, 2.0→25). Pass `--top-n N` to force a fixed cap. Same for the all-sources run (omit the source code).
- **Body fetch:** the kept top-N per week are fanned out one-per-article as `backfill_save_article_task` jobs on the **light queue**, which fetch the full body (`historical.fetch_article_body`) and save — so they geocode and render on the map (title-only would never aggregate into Events). Concurrency comes from the worker pool, not in-process threads; bounded to top-N, not all candidates.
- **Lean NLP for backfill:** articles tagged with `extra_data['backfill_week']` are auto-processed in lean mode — English-only LLM analysis (no Arabic) and no banner scrape — by `Workflow.process_articles`. They still geocode + categorize, so they aggregate and appear on the map. No flag needed; the normal scheduler handles them.

### Add a new stream data type

1. Create `api/services/streams/<name>.py` extending `BaseStream`
2. Implement `fetch()` → list[dict] and `save(records)` → int
3. Call `self.redis_publish('sse:<name>', payload)` if real-time updates are needed
4. Add a task function in `services/tasks.py` calling `run()`
5. Register in `setup_schedule.py` on the `default` queue
6. Add a typed fetch function in `ui/src/api/streams.ts`

### Add a new React component

1. Create `ui/src/components/MyComponent.tsx`
2. Use inline styles matching the dark theme palette above
3. Access translations via `const { t } = useLanguage()` — never hardcode English strings
4. Import and use in a page or parent component

### Add a new filter to /api/events/

1. Add query param parsing in `EventListView.get()` in `api/api/views/events.py`
2. Chain `.filter(...)` on the queryset
3. Add param to `fetchEvents(filters)` in `ui/src/api/events.ts`
4. Add UI control; manage state in `ui/src/pages/index.tsx`

### Add a new UI string (i18n)

1. Add the key to `UIStrings` interface in `ui/src/i18n/strings.ts`
2. Add the English value under `en`
3. Add the Arabic value under `ar`
4. Use `t.<key>` in your component via `useLanguage()`

---

## Key Files — Quick Reference

| Purpose | File |
|---------|------|
| Data models | `api/core/models.py` |
| Newsletter + Subscriber models | `api/newsletter/models.py` |
| All task functions | `api/services/tasks.py` |
| Enqueue helper | `api/services/queue.py` → `enqueue()` |
| Periodic schedule | `api/core/management/commands/setup_schedule.py` |
| Pipeline orchestration | `api/services/workflow.py` |
| LLM wrapper + strip_code_fences | `api/services/llm.py` |
| Importance scoring | `api/services/scoring.py` → `ArticleImportanceScorer`, `score_unscored_articles()` |
| Shared text utilities | `api/services/text_utils.py` → `tokenize()`, `jaccard()`, `STOP_WORDS` |
| Title deduplication | `api/services/data/__init__.py` → `_filter_title_dupes()` |
| Self-tests (scoring/text) | `api/services/tests_scoring.py` |
| Semantic clustering | `api/services/processing/clustering.py` |
| Article NLP analysis | `api/services/processing/analyzer.py` |
| Topic matching (keyword) | `api/services/topics/matcher.py` → `TopicMatcher` |
| Topic matching (LLM batch) | `api/services/topics/matcher.py` → `LLMTopicMatcher` |
| Topic source | `api/services/topics/sources/current_events.py` |
| Stream base class | `api/services/streams/base.py` |
| Price stream | `api/services/streams/prices.py` |
| NOTAM stream | `api/services/streams/notam.py` |
| Earthquake stream | `api/services/streams/earthquakes.py` |
| Forex stream | `api/services/streams/forex.py` |
| RSS ingestion | `api/services/data/rss.py` |
| Historical backfill | `api/services/data/historical.py` → `HistoricalBackfillService` |
| Event→symbol routing (rules) | `api/services/forecasting/routing.py` |
| Event→symbol routing (LLM) | `api/services/routing/llm_router.py` |
| Forecast features / model / backtest | `api/services/forecasting/{features,model,backtest}.py` |
| OHLC backfill | `api/services/forecasting/history.py` |
| Forecast docs (Mermaid) | `docs/forecasting.md` |
| API views | `api/api/views/` |
| API serializers | `api/api/serializers.py` |
| API URLs | `api/api/urls.py` |
| Django settings | `api/settings/base.py` |
| Root URLs | `api/app/urls.py` |
| Mongo app configs | `api/apps.py` |
| RQ admin panel | `/admin/django_rq/` (built-in django-rq panel) |
| React root / routes | `ui/src/main.tsx` |
| Main page state | `ui/src/pages/index.tsx` |
| Language context | `ui/src/contexts/LanguageContext.tsx` |
| i18n strings | `ui/src/i18n/strings.ts` |
| SSE hook | `ui/src/hooks/useSSE.ts` |
| API client (events) | `ui/src/api/events.ts` |
| API client (streams) | `ui/src/api/streams.ts` |
| API client (topics) | `ui/src/api/topics.ts` |
| API client (newsletter) | `ui/src/api/newsletter.ts` |
| Topics panel | `ui/src/components/topics/TopicsPanel.tsx` |
| Map component | `ui/src/components/events/MapView.tsx` |
| NOTAM overlay | `ui/src/components/layers/NotamOverlay.tsx` |
| Earthquake layer | `ui/src/components/layers/EarthquakeLayer.tsx` |
| Price ticker | `ui/src/components/events/PriceTicker.tsx` |
| Newsletter generator | `api/services/newsletter/generator.py` |
| Newsletter sender | `api/services/newsletter/sender.py` |
| Docker services | `docker-compose.yml` |
| Python deps | `api/requirements.txt` |

---

## Pipeline

```
fetch_articles_task (every 10m, default queue, timeout 30m)
  └─ RSSService (feedparser) / requests → Article objects in MongoDB
     Title dedup: Jaccard ≥ 0.75 against 24h Redis window (ARTICLE_DEDUP_TITLE_ENABLED)
     Word count filter: articles below ARTICLE_MIN_WORD_COUNT (30) are rejected

score_articles_task (every 60m, heavy queue, timeout 30m)
  └─ ArticleImportanceScorer:
       LLM (role='scoring') batch scores as plain float array → importance_score + source.weight multiplier
       + cross-source corroboration bonus (+0.5 per corroborating source, max +2.0)
       + per-category floor (conflict/disaster ≥ 6.0, political/economic ≥ 4.0)
     Only unscored articles (importance_score__isnull=True) in the last SCORE_INTERVAL_MINUTES×2 window

process_articles_task (every 240m, heavy queue, timeout 30m)
  └─ LLM: entities + sentiment + intensity + category/sub-category + city/country
     FinBERT sentiment + geonamescache geocoding → Article metadata
     LLM: English + Arabic translations → Article.translations (backfill: English only)

aggregate_events_task (every 240m, heavy queue, timeout 30m)
  └─ Bucket by (city, country, category, date)
     → semantic sub-cluster via SemanticClusterer (cosine similarity ≥ 0.55)
     → upsert Event objects in MongoDB keyed on (location_name, category, day)
     → on completion: enqueues dispatch_tag_topics_task (default queue, same hours window)

tag_topics_task (triggered by aggregate_events_task, heavy queue)
  └─ LLMTopicMatcher (batch, 10 events/call) → sets Event.topic_slugs
     Falls back to TopicMatcher per-event on LLM error

refresh_topics_task (daily 04:00 UTC, heavy queue, timeout 30m)
  └─ WikipediaCurrentEventsAdapter (Portal:Current_events, last 30 days)
     → deduplicate_topics → semantic_merge_topics (threshold=0.85)
     → _enrich_topics (LLM: descriptions + expanded keywords, batch 30)
     → upsert Topic objects; mark stale topics is_current=False

discover_topics_task (daily 05:00 UTC, heavy queue, timeout 30m)
  └─ LLM discovers new topics from recent untagged events → creates Topic objects

generate_newsletter_task (daily 06:00 UTC, heavy queue, timeout 30m)
  └─ LLM-based newsletter draft → DailyNewsletter.body (Markdown)

Stream tasks (default queue, independent of pipeline):
  fetch_prices_task (5m)       → PriceTick + Redis sse:prices
  fetch_notams_task (15m)      → NotamZone (upsert) + NotamRecord (append) + Redis sse:notams
  fetch_earthquakes_task (5m)  → EarthquakeRecord + Redis sse:earthquakes
  fetch_forex_task (15m)       → PriceTick (stream_key='forex')
```

---

## Docker Services

| Service | Command | Port |
|---------|---------|------|
| `api` | `uvicorn app.asgi:application` | 8000 (internal) |
| `worker-light` | `rqworker-pool default --num-workers 4` | — |
| `worker-heavy` | `rqworker-pool heavy --num-workers 2` | — |
| `worker-bulk` | `rqworker-pool bulk --num-workers 1` | — |
| `scheduler` | `setup_schedule && rqscheduler --url $REDIS_URL` | — |
| `frontend` | build → copy dist | — |
| `nginx` | reverse proxy | 80, 443 |
| `redis` | broker + cache + SSE pub/sub | — |
| `mongo` | database | 27017 |
| `cloudflared` | Cloudflare Tunnel (optional) | — |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | — | Django secret key (required) |
| `DATABASE_URL` | `mongodb://root:1234@localhost:27017/radar-live?authSource=admin` | MongoDB URI |
| `DATABASE_NAME` | `radar-live` | MongoDB database name |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis URI (RQ broker + cache + SSE pub/sub) |
| `DOMAIN` | `localhost` | Public hostname for nginx + Let's Encrypt |
| `ENV_NAME` | `development` | Shown in X-App-Version header |
| `TASK_QUEUE_ENABLED` | `false` | If false, `enqueue()` calls functions synchronously (no Redis needed) |
| `OPENROUTER_PROXY_URLS` | — | Comma-separated proxy base URLs (each pre-authenticated with one OpenRouter key). When set, the client rotates over these URLs round-robin; no `OPENROUTER_API_KEYS` needed |
| `OPENROUTER_API_KEYS` | — | OpenRouter keys, comma-separated (rotated round-robin). Used only when `OPENROUTER_PROXY_URLS` is not set |
| `OPENROUTER_MODELS` | `openrouter/free` | OpenRouter model (first value used) |
| `OPENROUTER_HTTP_PROXIES` | — | Network-level HTTP proxies for LLM calls. Format: `http://host:port::api_key,http://host2:port` — the `::api_key` suffix is optional; proxies without an explicit key draw from `OPENROUTER_API_KEYS` round-robin (loosely tied) |
| `OPENROUTER_PROXY_POOL_ENABLED` | `false` | When true, auto-fetches open-source proxy lists (GitHub + ProxyScrape), validates each candidate against openrouter.ai, and rotates working proxies round-robin. Takes precedence over `OPENROUTER_HTTP_PROXIES`. Proxies become available once the background validation pass completes (~30s after startup). |
| `OPENROUTER_PROXY_SOURCES` | — | Override default proxy list sources (TheSpeedX, ShiftyTR, clarketm, ProxyScrape). Comma-separated raw-text URLs, each returning one `host:port` per line |
| `OPENROUTER_PROXY_REFRESH_HOURS` | `6` | How often the open-source proxy pool re-fetches and re-validates |
| `OPENROUTER_PROXY_VALIDATE_TIMEOUT` | `5` | Per-proxy HEAD request timeout (seconds) during validation |
| `OPENROUTER_PROXY_MAX_POOL` | `100` | Maximum working proxies kept in rotation after validation |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `qwen3:14b` | Ollama model name |
| `FETCH_INTERVAL_MINUTES` | `10` | fetch_articles_task period |
| `PROCESS_INTERVAL_MINUTES` | `240` | process_articles_task period |
| `AGGREGATE_INTERVAL_MINUTES` | `240` | aggregate_events_task period |
| `TOPICS_REFRESH_HOUR` | `4` | Hour (UTC) for daily refresh_topics_task |
| `DISCOVER_TOPICS_HOUR` | `5` | Hour (UTC) for daily discover_topics_task |
| `TOPIC_SOURCES_DAYS` | `30` | Wikipedia Current Events lookback window (days) |
| `TOPIC_STALE_DAYS` | `90` | Topics with no tagged events in this window are auto-hidden from the header (`Workflow.prune_stale_topics`, runs in `refresh_topics`) |
| `PRICE_FETCH_INTERVAL_MINUTES` | `5` | fetch_prices_task period |
| `NOTAM_FETCH_INTERVAL_MINUTES` | `15` | fetch_notams_task period |
| `EARTHQUAKE_FETCH_INTERVAL_MINUTES` | `5` | fetch_earthquakes_task period |
| `EARTHQUAKE_MIN_MAGNITUDE` | `3.0` | USGS minimum magnitude filter |
| `FOREX_FETCH_INTERVAL_MINUTES` | `15` | fetch_forex_task period |
| `NEWSLETTER_GENERATE_HOUR` | `6` | Hour (UTC) for daily newsletter generation |
| `NEWSLETTER_ENABLED` | `true` | Feature flag — gates newsletter generation/send (schedule + task) |
| `FORECAST_ENABLED` | `true` | Feature flag — gates forecast train/run/score tasks + schedule |
| `FORECAST_MODEL_DIR` | `<BASE_DIR>/forecast_models` | Where LightGBM artifacts persist (`model_h{h}.joblib`) |
| `FORECAST_HORIZONS_DAYS` | `1,5` | Horizons (trading days) trained + served |
| `FORECAST_TRAIN_WINDOW_DAYS` | `540` | Training lookback window |
| `FORECAST_ROUTER` | `llm` | Live event router: `llm` (LLMEventRouter, rules fallback) or `rules` |
| `STREAM_PRICES_ENABLED` | `true` | Feature flag — gates the prices stream (schedule + task) |
| `STREAM_NOTAM_ENABLED` | `true` | Feature flag — gates the NOTAM stream |
| `STREAM_EARTHQUAKE_ENABLED` | `true` | Feature flag — gates the earthquakes stream |
| `STREAM_FOREX_ENABLED` | `true` | Feature flag — gates the forex stream |
| `FINBERT_ENABLED` | `true` | When false, FinBERT model is not loaded; sentiment falls back to `None` (saves ~500 MB RAM) |
| `API_THROTTLE_ANON` | `120/min` | DRF anonymous rate limit for the public read API |
| `HEALTH_CHECK_INTERVAL_MINUTES` | `30` | `pipeline_health_task` period (logs warnings on stale outputs) |
| `HEALTH_ARTICLE_STALE_MIN` / `HEALTH_PRICE_STALE_MIN` / `HEALTH_QUAKE_STALE_MIN` | `180` / `60` / `360` | Staleness thresholds for the health monitor |
| `JOB_TIMEOUT_SECONDS` | `1800` | RQ job timeout (30m) — passed to `enqueue()` and `setup_schedule` |
| `PROCESS_CHUNK_SIZE` | `1` | Articles per `process` fan-out worker job (>1 batches cheap records) |
| `PROCESS_DISPATCH_LIMIT` / `TAG_DISPATCH_LIMIT` / `ROUTE_DISPATCH_LIMIT` | `500` | Per-tick fan-out cap so a cold start doesn't flood the queue |
| `ROUTE_EVENTS_INTERVAL_MINUTES` | `360` | dispatch_route_events_task period |
| `STUCK_RECOVERY_INTERVAL_MINUTES` | `720` | Safety-net re-dispatch of processed-but-unlocated articles |
| `BOOTSTRAP_ARTICLE_YEARS` | `1` | First-load article-backfill window (`bootstrap_initial_data_task`) |
| `ARTICLE_IMPORTANCE_SCORING_ENABLED` | `true` | Feature flag — gates `score_articles_task` + schedule |
| `ARTICLE_MIN_IMPORTANCE` | `3.0` | Articles below this score are flagged for cleanup by `cleanup_low_importance_articles_task` |
| `ARTICLE_MIN_IMPORTANCE_TO_PROCESS` | `2.0` | Articles below this threshold are skipped during `process_articles` (NLP step) |
| `ARTICLE_CLEANUP_GRACE_HOURS` | `48` | Minimum age before a low-importance article can be deleted |
| `ARTICLE_MIN_WORD_COUNT` | `30` | Articles with fewer words in the body are rejected at fetch time |
| `ARTICLE_DEDUP_TITLE_ENABLED` | `true` | Enable Jaccard title deduplication in `DataService.refresh_until()` |
| `ARTICLE_DEDUP_JACCARD_THRESHOLD` | `0.75` | Jaccard overlap threshold for title dedup (0.0–1.0) |
| `ARTICLE_DEDUP_HOURS` | `24` | Rolling window (hours) for the title dedup cache |
| `ARTICLE_STALE_PROCESSED_DAYS` | `90` | Processed articles older than this with no event may be pruned by `prune_stale_articles_task` |
| `SCORE_INTERVAL_MINUTES` | `60` | `score_articles_task` period |
| `AWS_ACCESS_KEY_ID` | — | AWS SES credentials |
| `AWS_SECRET_ACCESS_KEY` | — | AWS SES credentials |
| `AWS_SES_REGION` | `us-east-1` | AWS SES region |
| `VITE_GA_ID` | — | Google Analytics ID (frontend build arg) |
| `VITE_DOMAIN` | — | Frontend domain (build arg) |
| `VITE_APP_NAME` | — | App display name (build arg) |
| `CLOUDFLARE_TUNNEL_TOKEN` | — | Cloudflare Tunnel auth token (optional) |

---

## Gotchas

- **MongoDB date filters**: never `__date=`, always explicit datetime range
- **UUID filtering**: `article_ids` stores strings; convert with `uuid.UUID()` first
- **`enqueue()` dev mode**: when `TASK_QUEUE_ENABLED=False`, `enqueue()` calls the function synchronously — no Redis or worker needed locally
- **Pipeline ordering**: with `TASK_QUEUE_ENABLED=True`, enqueuing fetch + process + aggregate as separate jobs **races them** — aggregate can run before process finishes, so no new Events. Chain dependent steps in a single task (see `run_pipeline_task`) instead of enqueuing them separately.
- **Aggregation needs a location**: `aggregate_events` only buckets articles with a non-empty `Article.location` and `published_on` within the window. An article whose LLM/geo step failed is saved with `processed_on` set but `location=None` → it **never aggregates**, and `process_articles` won't retry it (skips already-processed rows). Recover with `process_articles(only_failed=True)` (admin: **"Reprocess un-located"**); `aggregate_events` logs how many in-window articles it skipped for missing location.
- **Two queues**: `default` for fast I/O, `heavy` for NLP/LLM. New NLP/LLM tasks must pass `queue='heavy'` to `enqueue()` and `setup_schedule`
- **Schedule is stored in Redis**: `setup_schedule` clears and re-registers all jobs on every `scheduler` container start — this is intentional and idempotent
- **`backfill_prices_task` first run is deferred**: scheduled at `now + 1 week`, not `now` — so restarting the scheduler does not trigger an immediate multi-year price backfill. Trigger manually with `python manage.py backfill_prices` when needed.
- **`tag_topics_task` has no independent schedule**: it is enqueued by `aggregate_events_task` on completion. Do not add it back to `setup_schedule` — it would race aggregate and tag stale events unnecessarily.
- **`discover_topics_task` is a daily cron**: runs once at `DISCOVER_TOPICS_HOUR` (default 05:00 UTC). Do not restore the old interval schedule — daily is sufficient and avoids redundant LLM calls.
- **Restart scheduler to change intervals**: edit the env var and restart the `scheduler` service; it re-runs `setup_schedule` automatically
- **App names**: Django apps use simple names (`'core'`, `'accounts'`, `'api'`, `'newsletter'`, `'misc'`) — no path prefix
- **Model imports**: use `from core import models as core_models` — never bare `import core.models`
- **services/ imports**: plain Python modules — e.g. `from services.processing.clustering import get_clusterer`
- **RQ admin**: use the built-in django-rq panel at `/admin/django-rq/` — no custom queue monitor views needed
- **DRF**: all API responses must go through serializers — no hand-built dicts in views
- **Migrations**: all centralized in `api/migrations/`; mapped via `MIGRATION_MODULES` in settings
- **Built-in migrations**: `auth`, `admin`, `contenttypes` migrations are custom MongoDB-compatible files — do not regenerate with `makemigrations`
- **DATABASE_URL goes in HOST**: `django-mongodb-backend` reads the connection string from `DATABASES['default']['HOST']`, not `DATABASE_URL`
- **Frontend proxy**: in dev, Vite proxies `/api` → `localhost:8000`; in prod, nginx does it
- **nginx HTTPS**: run `./nginx/init-letsencrypt.sh` once before `docker compose up` in production
- **decouple .env**: `python-decouple` searches from CWD — place `.env` in project root or `cd api` before running manage.py locally
- **ArticleDatum `total=False`**: only `banner_image_url` is optional; required fields enforced by `_ArticleDatumRequired` base TypedDict — do not flatten to a single `total=False` dict
- **Newsletter body is Markdown**: stored as raw Markdown in `DailyNewsletter.body`; converted to HTML at send time — do not store HTML
- **Email `<h2>` styling**: done via regex replace in `sender.py` (inline styles) — email clients strip `<style>` blocks inconsistently
- **Newsletter date URL**: `/newsletter/YYYY/MM/DD` falls back to latest published newsletter on 404 — treat the date as a soft hint, not a hard key
- **Semantic clustering threshold**: default 0.55 cosine similarity. Lower = more aggressive merging; higher = more splits. Do not change without testing.
- **Frontend TypeScript only**: all UI files are `.tsx`/`.ts` — never create `.jsx`/`.js`
- **Frontend i18n mandatory**: all user-visible strings must use `useLanguage()` → `t.key`; never hardcode English text in components
- **Topic sources**: single source `WikipediaCurrentEventsAdapter` using `Portal:Current_events` date subpages. `TOPIC_SOURCES_DAYS` env var sets the lookback window (default: `30`). Old sources (`wikipedia-ongoing-conflicts`, `wikipedia-current-situations`, `gdelt-conflicts`) are removed — do not reference them.
- **`tag_events_with_topics` uses LLM**: `LLMTopicMatcher` sends batches of 10 events per LLM call; `retroactive_tag_topic` still uses the fast keyword-based `TopicMatcher`.
- **`refresh_topics` runs LLM enrichment**: `Workflow._enrich_topics()` calls the LLM after scraping to generate proper descriptions and expand keywords (batches of 30). Falls back silently — topics are upserted with raw scraped metadata if LLM is unavailable.
- **LLM responses: always strip code fences**: call `strip_code_fences(raw)` from `services.llm` before `json.loads()`. Do not re-implement the two `re.sub` lines inline — the shared helper exists for this purpose and handles `None` safely.
- **LLM routing**: call `get_llm_service(role)` with the use-case role (`analyzer`, `topics`, `newsletter`, `historical`, `routing`, `scoring`; unknown → `default`). Routes live in `settings.LLM_ROUTES` (dict in `settings/base.py`) — a provider name (`'openrouter'`) or an ordered fallback list (`FallbackLLMService` tries each on `LLMError`). Available providers: `openrouter`, `ollama`. Provider config (URLs/keys/model) comes from env vars. There is no `LLM_BACKEND` / `LLM_PROVIDER` / `G4F_*` var anymore.
- **OpenRouter proxy rotation**: set `OPENROUTER_PROXY_URLS` to 20 comma-separated proxy base URLs (each pre-keyed). The client cycles them round-robin — no api_key sent. If unset, falls back to direct openrouter.ai with `OPENROUTER_API_KEYS`.
- **Open-source proxy pool**: set `OPENROUTER_PROXY_POOL_ENABLED=true` to auto-source free proxies from GitHub lists (TheSpeedX, ShiftyTR, clarketm) and ProxyScrape. Validation (~30s background task at startup) tests each candidate with a HEAD request to openrouter.ai; only passing proxies enter rotation. Pool is a singleton (`services/proxy_pool.py::get_proxy_pool()`); takes precedence over `OPENROUTER_HTTP_PROXIES`. During the initial validation window, LLM calls fall back to direct (pool returns `None`).
- **LLM proxy resolution order**: `proxy_pool` (open-source pool) → `http_proxies` (static `OPENROUTER_HTTP_PROXIES` pairs) → direct. Keys from `_key_cycle` are always used when the pool supplies the URL; for static pairs the key is bundled in the pair.
- **Static points bootstrap**: run `python manage.py bootstrap_static_points` once to seed `StaticPoint` (exchanges, ports, central banks)
- **Bootstrap on fresh deploy**: run `python manage.py shell -c "from services.tasks import bootstrap_initial_data_task; bootstrap_initial_data_task()"` manually after first deploy. The scheduler no longer auto-triggers it on startup — trigger it yourself when ready.
- **Fan-out pipeline**: process/tag/route are light **dispatcher** tasks (`dispatch_*`, default queue) that enqueue idempotent per-record **workers** (`process_article_task`, `tag_events_chunk_task`, `route_events_chunk_task`, etc.) on the heavy queue. Scale via `worker-heavy` replicas. The admin "Run full pipeline" button still uses the sequential `run_pipeline_task`.
- **Symbols are DB-driven**: never hardcode symbol lists — add a `MarketSymbol` row and read via `services/market_symbols.py`. The forecasting panel is `MarketSymbol.is_forecast` (5 base symbols: `CL=F, GC=F, BTC-USD, SPY, EURUSD=X`); changing it requires a retrain (auto on next daily `train_forecast_model_task`).
- **Job monitoring**: use the built-in django-rq panel at `/admin/django-rq/` — shows queue depths, worker status, job details, return values, and failed-job tracebacks. Task functions return an `int` count which appears as the job result.
- **Shared tokenize/jaccard**: always import from `services.text_utils` — never redefine locally. `tokenize()` returns `frozenset[str]` (safe for set operations and cache); `jaccard()` returns 0.0 for empty inputs without raising.
- **`source.weight=0` vs `None`**: `weight=None` (unset) resolves to 1.0 (neutral); `weight=0` is the operator's signal to suppress a source (score clamps to 1.0 minimum, not boosted). Use `if weight is None: weight = 1.0` — never `weight or 1.0`.
- **score_articles_task accepts article_ids**: pass `article_ids=[str(a.id), ...]` to re-score specific articles without touching the unscored queue — used by the admin action to re-score selected records.
- **Title dedup threshold**: default 0.75 Jaccard is conservative to avoid cross-topic false positives. Lower only if you see many near-duplicates slipping through; raising above 0.9 defeats the dedup entirely.
- **Self-tests**: `python -m services.tests_scoring` (no DB) + `python -m services.forecasting.tests_forecast` (no DB) are the dependency-light test suites. Run both before any merge touching `services/` or `core/models.py`.

---

## Dev Commands

```bash
# Start everything
docker compose up

# Run from api/ directory (decouple reads .env from CWD)
cd api

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Pipeline commands — all support inline (default) and --background (RQ queue) modes.
# Without --background: task runs directly in this process (no Redis required).
# With    --background: task is enqueued via django-rq; if TASK_QUEUE_ENABLED=False it
#                       still runs synchronously (enqueue() calls the function directly).

# Fetch articles for a source (last N hours)
python manage.py fetch_data <source_code> --hours 6
python manage.py fetch_data <source_code> --hours 6 --background

# Run NLP pipeline
python manage.py process_articles --limit 500
python manage.py process_articles --limit 500 --background

# Aggregate processed articles into events
python manage.py aggregate_events --hours 24
python manage.py aggregate_events --hours 24 --background

# Tag events with topics
python manage.py tag_topics --hours 24
python manage.py tag_topics --hours 24 --background

# Retroactively tag events for a single topic
python manage.py retroactive_tag_topic <slug>
python manage.py retroactive_tag_topic <slug> --background

# Refresh topics list
python manage.py refresh_topics
python manage.py refresh_topics --background

# One-off stream fetch
python manage.py fetch_stream prices
python manage.py fetch_stream notam
python manage.py fetch_stream earthquakes
python manage.py fetch_stream forex

# Seed static reference points (run once)
python manage.py bootstrap_static_points

# Forecasting (event-fused symbol prediction)
python manage.py backfill_prices --years 10           # seed daily OHLC PriceBar (incremental by default)
python manage.py backfill_prices --years 10 --full    # force full re-pull (repair gaps)
python manage.py route_events --router llm --hours 720 # (re)route events → affected_indicators
python manage.py train_forecast                        # fit LightGBM clf+reg per horizon
python manage.py run_forecast                          # write Forecast rows
python manage.py evaluate_forecast                     # walk-forward backtest → JSON report
python manage.py forecast_e2e --years 3 --backtest    # run the whole flow → JSON report
# Self-tests (no Mongo needed):
DJANGO_SETTINGS_MODULE=settings.base python -m services.forecasting.tests_forecast

# Backfill historical top-N articles per week from a source
python manage.py backfill_history <source_code> --start-date 2022-01-01 --end-date 2025-01-01
python manage.py backfill_history <source_code> --start-date 2022-01-01 --end-date 2025-01-01 --dry-run
python manage.py backfill_history <source_code> --start-date 2022-01-01 --end-date 2025-01-01 --top-n 10 --resume

# Generate newsletter for a date
python manage.py generate_newsletter --date 2025-03-08

# Send newsletter for a date
python manage.py send_newsletter --date 2025-03-08

# Full-system e2e TEST with real data — asserts invariants across every part
# (symbols, fan-out fetch/process/tag/route, TaskRun tracking, stage_status, coverage,
#  forecasting, REST API, dashboard, bootstrap guard). Exits non-zero on hard failure.
python manage.py e2e_full                                  # real RSS + NLP + prices + API
python manage.py e2e_full --fast                          # structural checks only (no network/LLM)
python manage.py e2e_full --source guardian-world --years 2 --skip-forecast
# Report → ./e2e_full_<timestamp>.json (per-check PASS/FAIL/WARN). Requires the live
# Mongo + Redis stack; forces synchronous fan-out so no RQ workers are needed.

# Run the full pipeline end-to-end and write a JSON report for manual inspection
python manage.py e2e_pipeline                              # default: 6h fetch, 24h window, 5 samples
python manage.py e2e_pipeline --source <code> --fetch-hours 12 --hours 48
python manage.py e2e_pipeline --skip-fetch --skip-process  # aggregate + tag only
python manage.py e2e_pipeline --samples 10 --output /tmp/report.json
# Report written to ./e2e_report_<timestamp>.json — contains per-step counts,
# ok/error flags, and sample article/event/topic snapshots at each stage.

# Run RQ workers locally (run each in a separate terminal). Single-worker rqworker is
# fine for dev; prod uses rqworker-pool --num-workers (default 4 / heavy 2 / bulk 1).
python manage.py rqworker default    # light I/O queue
python manage.py rqworker heavy      # steady NLP/LLM queue
python manage.py rqworker bulk       # long one-shot jobs (backfills, training)

# Register periodic schedule with rq-scheduler (run once, or on every scheduler start)
python manage.py setup_schedule

# Run rq-scheduler locally (after setup_schedule)
rqscheduler --url redis://localhost:6379/0

# Inspect RQ queue stats
python manage.py rqstats

# RQ queue inspector (built into django-rq)
# http://localhost:8000/admin/django-rq/

# Run dependency-light self-tests (no DB or network needed)
DJANGO_SETTINGS_MODULE=settings.base python -m services.tests_scoring
DJANGO_SETTINGS_MODULE=settings.base python -m services.forecasting.tests_forecast

# Frontend dev server (port 5173, proxies /api to localhost:8000)
cd ui && npm run dev

# Build frontend
cd ui && npm run build
```

---

## Testing Checklist

Before shipping any backend change:
- [ ] `python manage.py check` passes
- [ ] `python manage.py migrate --check` (no unapplied migrations)
- [ ] `python -m services.tests_scoring` passes (no DB needed)
- [ ] `python -m services.forecasting.tests_forecast` passes (no DB needed)
- [ ] API endpoints return expected shape (test with curl or browser)
- [ ] `python manage.py e2e_full --fast` passes (structural invariant checks)
- [ ] `python manage.py e2e_pipeline` completes without errors; inspect the JSON report to verify article → event → topic flow

Before shipping any frontend change:
- [ ] `npm run build` succeeds in `ui/`
- [ ] Map renders markers correctly
- [ ] Event list and cards expand/collapse without errors
- [ ] Filters (category, topic) apply correctly to map and list
- [ ] Topic pills in `TopicsPanel` toggle `activeTopic` correctly
- [ ] Language toggle switches between EN and AR without errors
- [ ] All new strings are present in both `en` and `ar` in `strings.ts`
