# conflictradar.live

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
| Serving | uvicorn + nginx (reverse proxy + TLS) |
| TLS | Let's Encrypt via certbot |
| Containers | Docker Compose |

---

## Quick Start

### Production (with HTTPS)

Point your domain's DNS A record at the server, then:

```bash
export DOMAIN=yourdomain.com
export CERTBOT_EMAIL=admin@yourdomain.com

bash init-letsencrypt.sh           # one-time: gets the TLS cert
DOMAIN=$DOMAIN docker compose up -d
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py createsuperuser
```

Deployment is **configurationless**: migrations seed config/reference data
(`MarketSymbol`, `StaticPoint`). Bootstrap backfill (prices + articles) plus the first
forecast train/run is triggered from the admin dashboard — no manual
`bootstrap_static_points` or backfill commands. See [docs/operations.md](docs/operations.md).
Operate it from the admin dashboard at `/admin/dashboard/`.

Access:

- Map: <https://yourdomain.com>
- API: <https://yourdomain.com/api/>
- Admin: <https://yourdomain.com/admin/>
- Worker health: <http://yourdomain.com:8001/>

### Local / HTTP-only

```bash
DOMAIN=localhost docker compose up --build
docker compose exec backend python manage.py migrate
```

Access at <http://localhost>.

### Local Development (no Docker)

Prerequisites: Python 3.13+, MongoDB, Redis, Node 22+

```bash
# Backend (from project root — decouple reads .env from CWD)
cd backend
python manage.py migrate
python manage.py runserver        # Django on :8000

# Worker (separate terminal)
python worker.py                  # RQ workers + scheduler + health on :8001

# Frontend (separate terminal)
cd frontend
npm install
npm run dev                       # Vite dev server on :5173, proxies /api to :8000
```

---

## Environment Variables

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `SECRET_KEY` | — | Django secret key (required) |
| `DATABASE_URL` | `mongodb://root:1234@localhost:27017/radar-live?authSource=admin` | MongoDB URI |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis URI |
| `DOMAIN` | `localhost` | Public domain name (nginx + Let's Encrypt) |
| `ENV_NAME` | `development` | Shown in `X-App-Version` header |
| `TASK_QUEUE_ENABLED` | `false` | Enable RQ task enqueueing |
| `WORKER_COUNT` | `4` | Number of RQ worker processes |
| `FETCH_INTERVAL_MINUTES` | `10` | Fetch schedule interval |
| `PROCESS_INTERVAL_MINUTES` | `10` | NLP pipeline schedule interval |
| `AGGREGATE_INTERVAL_MINUTES` | `10` | Event aggregation schedule interval |
| `DEFAULT_FROM_EMAIL` | — | Verified sender address |
| `EMAIL_PROVIDER` | `ses` | `ses` or `smtp` |
| `AWS_SES_ACCESS_KEY_ID` | — | AWS key with `ses:SendRawEmail` |
| `AWS_SES_SECRET_KEY` | — | AWS secret key |
| `AWS_SES_REGION` | `eu-north-1` | SES region |
| `NEWSLETTER_BASE_URL` | `http://localhost` | Base URL for unsubscribe/confirm links |

Example `.env` for local dev:

```bash
SECRET_KEY=your-secret-key-here-make-it-long
DATABASE_URL=mongodb://root:1234@localhost:27017/radar-live?authSource=admin
REDIS_URL=redis://localhost:6379/0
ENV_NAME=development
TASK_QUEUE_ENABLED=true
EMAIL_PROVIDER=smtp
```

---

## Pipeline

```text
fetch_data        (every 10m, timeout 30m)
  └─ feedparser (RSS) / requests → Article objects in MongoDB

process_articles  (every 10m, timeout 30m)
  └─ LLM analyzer — entities, sentiment, category/sub-category, city/country
  └─ FinBERT — financial sentiment [-1, 1]
  └─ geonamescache — city/country → coordinates
  └─ → Article NLP fields written back to MongoDB

aggregate_events  (every 10m, timeout 30m)
  └─ Groups articles by (location, category, time window) → Event objects

generate_newsletter  (daily, timeout 30m)
  └─ Top events for the day → LLM → prose briefing stored as DailyNewsletter

send_newsletter   (daily, timeout 30m)
  └─ DailyNewsletter → per-subscriber HTML email via AWS SES
```

---

## Project Structure

```text
backend/
  app/           WSGI/ASGI entry, root URLs, middleware, auth backend
  core/          Article, Event, Source models + pipeline tasks + management commands
  accounts/      Custom User model (email-based auth)
  api/           DRF serializers + APIView endpoints (events, sources, newsletter)
  newsletter/    Subscriber + DailyNewsletter models, subscribe/confirm/unsubscribe views
  services/
    cleaning/    ArticleCleaner — LLM entities/sentiment/category + FinBERT
    location/    Geocoder — Nominatim via geopy, Django cache
    data/        Ingestion — RSS (feedparser) + HTTP
    email/       Email service — AWS SES (prod) or SMTP (dev)
    llm/         LLM service — newsletter generation
  settings/      Django configuration
  worker.py      RQ workers + scheduler + health check
  templates/
    newsletter/  email.html, email.txt, confirm_email.html, confirm_email.txt
frontend/
  src/
    pages/       Page components (_layout.tsx, index, newsletter, about, privacy, terms)
    components/  App.tsx, MapView.jsx, EventList.jsx, EventCard.jsx, NewsletterList.tsx, NewsletterView.tsx
    api/         events.js, newsletter.ts — fetch helpers
    types.ts     Shared TypeScript types
  vite.config.js Dev proxy /api → localhost:8000
nginx/
  templates/default.conf.template  Reverse proxy config (envsubst)
init-letsencrypt.sh  One-time Let's Encrypt bootstrap script
```
