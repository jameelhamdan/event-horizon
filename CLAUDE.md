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
| Ingestion | Telethon (Telegram) + feedparser (RSS) + requests |
| NLP | spaCy (NER) + sentence-transformers + VADER + geopy |
| LLM | Anthropic Claude (via `services/llm.py`; provider configurable via `LLM_PROVIDER`) |
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
│   │   ├── models.py       # Source, Article, Event, Topic, PriceTick, NotamZone,
│   │   │                   # NotamRecord, EarthquakeRecord, StaticPoint, Forecast
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
│   │       ├── telegram_session.py     # Generates Telegram session string for a source
│   │       ├── setup_schedule.py       # Registers all periodic jobs with rq-scheduler
│   │       └── e2e_pipeline.py         # End-to-end pipeline test → JSON report
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
│   │       ├── forecasts.py    # ForecastListView, ForecastLatestView
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
│   │   ├── llm.py          # LLM client wrapper (provider-agnostic)
│   │   ├── processing/     # NLP processing pipeline
│   │   │   ├── analyzer.py     # Article analysis (NER via spaCy, VADER sentiment, geocoding)
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
│   │   │   └── sources/
│   │   │       ├── rss.py          # RSSService (feedparser)
│   │   │       └── telegram.py     # TelegramService (Telethon)
│   │   ├── forecasting/    # LLM market forecasting
│   │   │   ├── service.py      # run_forecasts(), score_forecasts()
│   │   │   ├── features.py     # build_feature_vector() — price + news features
│   │   │   └── routing.py      # route_event_to_symbols() — maps events to symbols
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
│   │   │   ├── index.tsx           # Main map page — activeTopic state, all overlays
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
│   │   │   │               # fetchStaticPoints(), fetchForecasts()
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
│   │   │   │   ├── ForecastPanel.tsx   # LLM market forecast display
│   │   │   │   ├── MapView.tsx         # L.divIcon category markers + all map layers
│   │   │   │   └── PriceTicker.tsx     # Real-time SSE price table
│   │   │   ├── topics/
│   │   │   │   └── TopicsPanel.tsx     # Active topics pill list, category colors
│   │   │   └── layers/
│   │   │       ├── NotamOverlay.tsx    # GeoJSON NOTAM zones with hover tooltips
│   │   │       └── EarthquakeLayer.tsx # USGS earthquake markers (magnitude circles)
│   │   └── types.ts        # All shared TypeScript types
│   ├── vite.config.ts      # Dev proxy /api → localhost:8000
│   └── Dockerfile
├── nginx/
│   └── templates/
│       └── default.conf.template  # nginx reverse proxy template (envsubst)
├── version.txt             # Application version string
├── docker-compose.yml      # All services: nginx, api, worker-heavy, worker-light,
│                           # scheduler, frontend, mongo, redis, cloudflared
└── CLAUDE.md               # ← you are here
```

---

## Features Overview

This is a real-time global event intelligence platform. Key feature areas:

| Feature | Description |
|---------|-------------|
| **Multi-source ingestion** | RSS feeds (feedparser) + Telegram channels (Telethon) → Article objects |
| **NLP pipeline** | spaCy NER + VADER sentiment + geopy geocoding + LLM category/sub-category + i18n translations |
| **Event aggregation** | Articles bucketed by (location, category, day) + semantic sub-clustering (multilingual sentence-transformers) |
| **Global topic tracking** | Wikipedia Portal:Current_events scraped daily → LLM-enriched topics → LLM semantic matching to events |
| **Stream data** | Real-time prices (Yahoo Finance + CoinGecko), NOTAMs (aviationweather.gov), earthquakes (USGS), forex (ECB) |
| **LLM market forecasting** | Directional predictions (up/down/neutral) with feature vectors → scored against actuals |
| **Daily newsletter** | LLM-generated per-category summaries → Markdown → HTML → AWS SES to subscribers |
| **Subscriber management** | Double opt-in email confirmation, token-based unsubscribe |
| **Interactive Leaflet map** | Event markers + NOTAM overlay + earthquake layer + static reference points |
| **Real-time SSE** | Redis pub/sub → Server-Sent Events → PriceTicker + NOTAM/earthquake notifications |
| **Dual-language UI** | English + Arabic translations (LLM-generated at process time; toggled via LanguageContext) |
| **Two-queue workers** | `default` queue (light I/O: fetch, prices, notam, earthquakes, forex) + `heavy` queue (NLP/LLM: process, aggregate, tag, forecast) |
| **Admin pipeline panel** | Custom Django admin actions for manual pipeline triggers (fetch/process/aggregate/run-all) |

---

## Conventions

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
- `Event.started_at` is a DateTimeField — always timezone-aware (`django.utils.timezone.now()`)
- `Event.topic_slugs` — list of matched topic slugs (tagged by `tag_topics_task`)
- `Event.topics` — dict of `{slug: confidence}` (float 0–1.0)
- `NotamZone` — current live NOTAM state (upserted by `notam_id`); fields: `notam_id`, `notam_type`, `geometry` (GeoJSON), `effective_from`, `effective_to`, `is_active`, `altitude_min_ft`, `altitude_max_ft`, `country_code`
- `NotamRecord` — append-only NOTAM history (every fetch); same fields + `fetched_at`
- `EarthquakeRecord` — USGS events; fields: `usgs_id` (unique), `magnitude`, `depth_km`, `location_name`, `latitude`, `longitude`, `occurred_at`, `tsunami_alert`, `alert_level` (green/yellow/orange/red)
- `PriceTick` — price samples; fields: `symbol`, `stream_key` (crypto/stock/commodity/forex/bond), `value`, `change_pct`, `volume`, `occurred_at`; 1-year TTL in production
- `misc` app contains only `EmailLog` model — admin panel for monitoring sent emails
- `Subscriber` in `newsletter/models.py` — fields: `email` (unique), `token` (UUID), `subscribed_at`, `confirmed_at` (nullable), `is_active`, `unsubscribed_at`; lifecycle: pending → confirmed → unsubscribed

### Tasks / Background Jobs

All task functions live in `services/tasks.py` (pipeline + streams + topics + forecasting) and `newsletter/tasks.py`. They are **plain Python functions** — no decorator.

- Enqueue: `from services.queue import enqueue; enqueue(my_task, arg1, kwarg=val)`
- Task names follow the `*_task` suffix convention
- Management commands call task functions **directly** for inline/foreground execution; use `--background` to enqueue instead
- `enqueue()` calls the function synchronously when `TASK_QUEUE_ENABLED=False` (dev default)
- **Queue routing**: pass `queue='heavy'` to `enqueue()` for NLP/LLM tasks; default queue is `'default'` (light I/O)

To add a new background task:
1. Write the plain function in `services/tasks.py`
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

**Heavy queue — NLP/LLM (5× base interval by default):**

| Task | Default interval | Env var |
|---|---|---|
| `process_articles_task` | 60m | `PROCESS_INTERVAL_MINUTES` |
| `aggregate_events_task` | 60m | `AGGREGATE_INTERVAL_MINUTES` |
| `tag_topics_task` | 75m | `TAG_TOPICS_INTERVAL_MINUTES` |
| `discover_topics_task` | 150m | `DISCOVER_TOPICS_INTERVAL_MINUTES` |
| `run_forecast_task` | 300m | `FORECAST_INTERVAL_MINUTES` |
| `score_forecasts_task` | 300m | `FORECAST_SCORE_INTERVAL_MINUTES` |

**Cron jobs (heavy queue):**

| Task | Schedule | Env var |
|---|---|---|
| `refresh_topics_task` | daily at 04:00 UTC | `TOPICS_REFRESH_HOUR` |
| `generate_newsletter_task` | daily at 06:00 UTC | `NEWSLETTER_GENERATE_HOUR` |

To change an interval: update the env var and restart the `scheduler` service (it re-runs `setup_schedule` on startup, clearing and re-registering all jobs).

### Worker (Two Queues)

Two separate RQ workers are run in Docker:

```bash
python manage.py rqworker default    # worker-light service: I/O tasks
python manage.py rqworker heavy      # worker-heavy service: NLP/LLM tasks
```

`RQ_QUEUES` in settings defines both `default` and `heavy` queues (both pointing to Redis). When adding new tasks, decide which queue based on CPU/LLM cost: fast I/O → `default`, NLP or LLM → `heavy`.

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

### Forecasting

`api/services/forecasting/`:
- `service.py` — `run_forecasts()`: builds feature vector per symbol, calls LLM with structured prompt, stores `Forecast` object; `score_forecasts()`: fills `actual_value` once horizon elapses
- `features.py` — `build_feature_vector(symbol, at_time)`: price momentum (1h/24h), news sentiment mean/std, event intensity, category counts, routed event IDs
- `routing.py` — `route_event_to_symbols(category, location, topic_slugs)`: maps event attributes to affected market symbols (e.g. conflict + Ukraine → wheat, energy)
- Default symbols: GC=F (gold), CL=F (oil), NG=F (natural gas), ZW=F (wheat), BTC-USD, ETH-USD, SPY, DX-Y.NYB, ^TNX
- `Forecast` model fields: `symbol`, `stream_key`, `direction` (up/down/neutral), `confidence`, `predicted_value`, `actual_value`, `reasoning`, `event_ids`, `feature_vector`
- API: `GET /api/forecasts/` (filter by symbol/stream_key/horizon), `GET /api/forecasts/latest/` (latest per symbol)

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

- `services/processing/analyzer.py` — main article processing: spaCy NER (entity extraction), VADER sentiment, geopy geocoding, LLM category + sub-category, LLM i18n translations (en + ar)
- `services/processing/cleaner.py` — HTML tag removal, whitespace normalization, non-ASCII handling
- `services/processing/clustering.py` — semantic event grouping (see above)
- `ArticleDocument` and `ArticleFeatures` dataclasses live in `core/models.py`

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
| GET | `/api/prices/<symbol>/` | Price history; params: `from`, `to`, `limit` (max 5000) |
| GET | `/api/notams/` | Active NOTAM zones; params: `active`, `country_code`, `notam_type` |
| GET | `/api/notams/history/` | NOTAM record history; params: `from`, `to`, `country_code`, `status`, `limit` |
| GET | `/api/earthquakes/` | Global earthquakes; params: `min_magnitude` (default 3.0), `hours` (default 24), `limit` |
| GET | `/api/static-points/` | Reference points (exchanges, ports, banks); params: `type`, `country_code` |
| GET | `/api/topics/` | Topics list; params: `active`, `current`, `top_level`, `category`, `date`, `parent`, `source`, `month`, `year` |
| GET | `/api/topics/<slug>/` | Topic detail |
| GET | `/api/topics/<slug>/events/` | Events tagged with topic; params: `start`, `end`, `limit` |
| GET | `/api/forecasts/` | LLM forecasts; params: `symbol`, `stream_key`, `horizon` (hours), `limit` |
| GET | `/api/forecasts/latest/` | Latest forecast per symbol; param: `stream_key` |
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
| LLM wrapper | `api/services/llm.py` |
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
| RSS ingestion | `api/services/data/sources/rss.py` |
| Telegram ingestion | `api/services/data/sources/telegram.py` |
| Forecasting service | `api/services/forecasting/service.py` |
| Event→symbol routing | `api/services/forecasting/routing.py` |
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
  └─ RSSService (feedparser) / TelegramService (Telethon) → Article objects in MongoDB

process_articles_task (every 60m, heavy queue, timeout 30m)
  └─ spaCy NER + VADER sentiment + geopy geocoding → Article metadata
     LLM: category + sub-category assignment
     LLM: English + Arabic translations → Article.translations

aggregate_events_task (every 60m, heavy queue, timeout 30m)
  └─ Bucket by (city, country, category, date)
     → semantic sub-cluster via SemanticClusterer (cosine similarity ≥ 0.55)
     → upsert Event objects in MongoDB keyed on (location_name, category, day)

tag_topics_task (every 75m, heavy queue, timeout 30m)
  └─ LLMTopicMatcher (batch, 10 events/call) → sets Event.topic_slugs
     Falls back to TopicMatcher per-event on LLM error

discover_topics_task (every 150m, heavy queue, timeout 30m)
  └─ LLM discovers new topics from recent events → creates Topic objects

refresh_topics_task (daily 04:00 UTC, heavy queue, timeout 30m)
  └─ WikipediaCurrentEventsAdapter (Portal:Current_events, last 30 days)
     → deduplicate_topics → semantic_merge_topics (threshold=0.85)
     → _enrich_topics (LLM: descriptions + expanded keywords, batch 30)
     → upsert Topic objects; mark stale topics is_current=False

generate_newsletter_task (daily 06:00 UTC, heavy queue, timeout 30m)
  └─ LLM-based newsletter draft → DailyNewsletter.body (Markdown)

Stream tasks (default queue, independent of pipeline):
  fetch_prices_task (5m)       → PriceTick + Redis sse:prices
  fetch_notams_task (15m)      → NotamZone (upsert) + NotamRecord (append) + Redis sse:notams
  fetch_earthquakes_task (5m)  → EarthquakeRecord + Redis sse:earthquakes
  fetch_forex_task (15m)       → PriceTick (stream_key='forex')

  run_forecast_task (300m)     → Forecast (LLM directional predictions)
  score_forecasts_task (300m)  → fills actual_value on elapsed forecasts
```

---

## Docker Services

| Service | Command | Port |
|---------|---------|------|
| `api` | `uvicorn app.asgi:application` | 8000 (internal) |
| `worker-light` | `python manage.py rqworker default` | — |
| `worker-heavy` | `python manage.py rqworker heavy` | — |
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
| `LLM_PROVIDER` | `anthropic` | LLM service provider (anthropic / openai) |
| `ANTHROPIC_API_KEY` | — | API key for Anthropic Claude |
| `FETCH_INTERVAL_MINUTES` | `10` | fetch_articles_task period |
| `PROCESS_INTERVAL_MINUTES` | `10` | process_articles_task base period (×6 for heavy queue = 60m) |
| `AGGREGATE_INTERVAL_MINUTES` | `10` | aggregate_events_task base period (×6 = 60m) |
| `TAG_TOPICS_INTERVAL_MINUTES` | `15` | tag_topics_task base period (×5 = 75m) |
| `DISCOVER_TOPICS_INTERVAL_MINUTES` | `30` | discover_topics_task base period (×5 = 150m) |
| `TOPICS_REFRESH_HOUR` | `4` | Hour (UTC) for daily refresh_topics_task |
| `TOPIC_SOURCES_DAYS` | `30` | Wikipedia Current Events lookback window (days) |
| `PRICE_FETCH_INTERVAL_MINUTES` | `5` | fetch_prices_task period |
| `NOTAM_FETCH_INTERVAL_MINUTES` | `15` | fetch_notams_task period |
| `EARTHQUAKE_FETCH_INTERVAL_MINUTES` | `5` | fetch_earthquakes_task period |
| `EARTHQUAKE_MIN_MAGNITUDE` | `3.0` | USGS minimum magnitude filter |
| `FOREX_FETCH_INTERVAL_MINUTES` | `15` | fetch_forex_task period |
| `FORECAST_INTERVAL_MINUTES` | `60` | run_forecast_task base period (×5 = 300m) |
| `FORECAST_SCORE_INTERVAL_MINUTES` | `60` | score_forecasts_task base period (×5 = 300m) |
| `NEWSLETTER_GENERATE_HOUR` | `6` | Hour (UTC) for daily newsletter generation |
| `JOB_TIMEOUT_SECONDS` | `1800` | RQ job timeout (30m) — passed to `enqueue()` and `setup_schedule` |
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
- **Two queues**: `default` for fast I/O, `heavy` for NLP/LLM. New NLP/LLM tasks must pass `queue='heavy'` to `enqueue()` and `setup_schedule`
- **Schedule is stored in Redis**: `setup_schedule` clears and re-registers all jobs on every `scheduler` container start — this is intentional and idempotent
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
- **LLM responses: always strip code fences**: use `re.sub(r'^```(?:json)?\s*', '', r)` + `re.sub(r'\s*```$', '', r)` before `json.loads()`. All LLM-calling code in the project does this — do not omit it in new code.
- **Telegram session setup**: run `python manage.py telegram_session <source_code>` once interactively to generate the session string; it is stored in `Source.headers.TELEGRAM_SESSION`
- **Static points bootstrap**: run `python manage.py bootstrap_static_points` once to seed `StaticPoint` (exchanges, ports, central banks)

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

# Generate Telegram session string for a source (interactive, run once per source)
python manage.py telegram_session <source_code>

# Generate newsletter for a date
python manage.py generate_newsletter --date 2025-03-08

# Send newsletter for a date
python manage.py send_newsletter --date 2025-03-08

# Run the full pipeline end-to-end and write a JSON report for manual inspection
python manage.py e2e_pipeline                              # default: 6h fetch, 24h window, 5 samples
python manage.py e2e_pipeline --source <code> --fetch-hours 12 --hours 48
python manage.py e2e_pipeline --skip-fetch --skip-process  # aggregate + tag only
python manage.py e2e_pipeline --samples 10 --output /tmp/report.json
# Report written to ./e2e_report_<timestamp>.json — contains per-step counts,
# ok/error flags, and sample article/event/topic snapshots at each stage.

# Run RQ workers locally (run each in a separate terminal)
python manage.py rqworker default    # light I/O queue
python manage.py rqworker heavy      # NLP/LLM queue

# Register periodic schedule with rq-scheduler (run once, or on every scheduler start)
python manage.py setup_schedule

# Run rq-scheduler locally (after setup_schedule)
rqscheduler --url redis://localhost:6379/0

# Inspect RQ queue stats
python manage.py rqstats

# RQ queue inspector (built into django-rq)
# http://localhost:8000/admin/django-rq/

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
- [ ] API endpoints return expected shape (test with curl or browser)
- [ ] `python manage.py e2e_pipeline` completes without errors; inspect the JSON report to verify article → event → topic flow

Before shipping any frontend change:
- [ ] `npm run build` succeeds in `ui/`
- [ ] Map renders markers correctly
- [ ] Event list and cards expand/collapse without errors
- [ ] Filters (category, topic) apply correctly to map and list
- [ ] Topic pills in `TopicsPanel` toggle `activeTopic` correctly
- [ ] Language toggle switches between EN and AR without errors
- [ ] All new strings are present in both `en` and `ar` in `strings.ts`
