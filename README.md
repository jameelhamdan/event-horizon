# Event Horizon

Live geopolitical event map with a daily AI-written email briefing. Ingests news from RSS feeds and web sources, runs NLP analysis, clusters articles into geolocated events, and displays them on an interactive Leaflet map.

See [project.md](project.md) for full requirements and architecture. See [CLAUDE.md](CLAUDE.md) for developer conventions and recipes.

---

## Stack

| Layer | Tech |
| ----- | ---- |
| Backend | Django 6 + DRF + django-mongodb-backend |
| Task queue | Redis + RQ + django-rq |
| Storage | MongoDB 8 |
| Ingestion | feedparser (RSS) + requests |
| NLP | LLM entity/sentiment/category + sentence-transformers clustering + FinBERT + geonamescache geocoding |
| Email | AWS SES (prod) / SMTP (dev) |
| Newsletter | LLM-generated daily briefing → subscriber list |
| Frontend | React 19 + Vite + react-leaflet |
| Serving | uvicorn + nginx (reverse proxy) |
| Tunnel | Cloudflare Tunnel (TLS termination) |
| Containers | Docker Compose |

---

## Quick Start

### Production

```bash
cp api/.env.example .env.app   # fill in SECRET_KEY, CLOUDFLARE_TUNNEL_TOKEN, etc.
docker compose up -d
docker compose exec api python manage.py migrate
docker compose exec api python manage.py createsuperuser
```

Deployment is **configurationless**: migrations seed reference data (`MarketSymbol`, `StaticPoint`). Bootstrap backfill (prices + articles) and the first forecast train/run are triggered from the admin dashboard. See [docs/operations.md](docs/operations.md).

Access:

- Map: `https://yourdomain.com`
- Admin: `https://yourdomain.com/admin/`
- API: `https://yourdomain.com/api/`

### Local Development (no Docker)

Prerequisites: Python 3.13+, MongoDB, Redis, Node 22+

```bash
# Backend
cd api
cp .env.example .env.app       # set SECRET_KEY, TASK_QUEUE_ENABLED=true
python manage.py migrate
python manage.py runserver     # Django on :8000

# Workers (separate terminals, from api/)
python manage.py worker heavy --num-workers 2
python manage.py rqworker-pool default --num-workers 4

# Frontend (separate terminal)
cd ui
npm install
npm run dev                    # Vite on :5173, proxies /api → :8000
```

---

## Environment Variables

See `api/.env.example` for the full annotated list. Key variables:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `SECRET_KEY` | — | Django secret key (required) |
| `DATABASE_URL` | `mongodb://root:1234@localhost:27017/radar-live?authSource=admin` | MongoDB URI |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis URI |
| `DOMAIN` | `localhost` | Public domain name |
| `ENV_NAME` | `development` | Shown in `X-App-Version` header |
| `TASK_QUEUE_ENABLED` | `false` | Enable RQ task enqueueing (set `true` in prod) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server (default LLM backend) |
| `GROQ_API_KEYS` | — | Groq API key(s), comma-separated (free cloud fallback) |
| `CEREBRAS_API_KEYS` | — | Cerebras API key(s), comma-separated (free cloud fallback) |
| `OPENROUTER_API_KEYS` | — | OpenRouter key(s) (last-resort fallback) |
| `NEWSLETTER_ENABLED` | `true` | Enable newsletter generation + sending |
| `FORECAST_ENABLED` | `true` | Enable forecasting pipeline |
| `EMAIL_PROVIDER` | `smtp` | `smtp` or `ses` |
| `DEFAULT_FROM_EMAIL` | `newsletter@localhost` | Verified sender address |
| `AWS_SES_ACCESS_KEY_ID` | — | AWS key with `ses:SendRawEmail` (when `EMAIL_PROVIDER=ses`) |
| `AWS_SES_SECRET_KEY` | — | AWS secret key |
| `AWS_SES_REGION` | `us-east-1` | SES region |
| `NEWSLETTER_BASE_URL` | `http://localhost` | Base URL for unsubscribe/confirm links |
| `CLOUDFLARE_TUNNEL_TOKEN` | — | Cloudflare Tunnel token (prod) |

---

## Pipeline

```text
fetch            (every 10m)
  └─ feedparser (RSS) / requests → Article objects in MongoDB

process          (every 4h, fan-out to heavy workers)
  └─ LLM analyzer — entities, sentiment, category/sub-category, city/country
  └─ FinBERT — financial sentiment [-1, 1]
  └─ geonamescache — city/country → lat/lng

aggregate        (every 4h, 30m after process)
  └─ Groups articles by (location, category, day) + semantic clustering → Event objects

tag + route      (every 6h)
  └─ LLM topic matcher → Event.topic_slugs
  └─ Deterministic routing → Event.affected_indicators (market symbols)

forecast         (daily)
  └─ LightGBM trained on event features + price history → directional predictions

newsletter       (daily)
  └─ Top events → LLM prose → DailyNewsletter → subscriber emails
```

---

## Project Structure

```text
api/
  app/           ASGI entry, root URLs, middleware, auth backend
  core/          Article, Event, Source, Topic models + admin dashboard
  accounts/      Custom User model (email-based auth)
  api/           DRF serializers + APIView endpoints
  newsletter/    Subscriber + DailyNewsletter models, subscribe/confirm/unsubscribe views
  misc/          Static pages, sitemap
  services/
    data/        RSS + HTTP ingestion
    processing/  ArticleCleaner — LLM analyzer, FinBERT, clustering
    forecasting/ LightGBM model, features, backtest, event router
    topics/      Topic scraper, LLM matcher, dedup
    routing/     LLM event → symbol router
    streams/     Price, NOTAM, earthquake, forex live feeds
    newsletter/  Newsletter generator
    email/       SES / SMTP email service
    llm/         LLM provider abstraction (Ollama, Groq, Cerebras, OpenRouter)
    workflow/    Pipeline orchestration (articles, events, topics)
    tasks.py     RQ task definitions
    queue.py     Enqueue helpers + task run tracking
  scripts/       One-off / startup scripts (init_models.py — preloads ML weights)
  tests/         E2E and diagnostic commands (e2e_full, e2e_pipeline, test_llm, …)
  settings/      Django configuration
  templates/     Email + admin HTML templates
  crontab        Periodic schedule (run by supercronic inside the api container)
  release.sh     Container entrypoint: collectstatic → migrate → supercronic → uvicorn
ui/
  src/
    pages/       Page components
    components/  Map, event list, newsletter views
    api/         Fetch helpers
nginx/
  templates/     nginx reverse proxy config
docker-compose.yml
```
