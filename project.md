# Project: conflictradar.live

Live geopolitical event detection and mapping system. Ingests news from RSS feeds and web sources, runs NLP analysis, clusters articles into geolocated events, and displays them on an interactive world map. Sends a daily AI-written email briefing to subscribers.

---

## Requirements

### Functional

1. **Data Ingestion** — fetch articles from RSS feeds and web sources on a schedule
2. **NLP Processing** — extract locations, sentiment, intensity, and category from each article
3. **Event Aggregation** — cluster articles by location + time + category into Event objects
4. **REST API** — serve events, sources, and newsletters to the frontend via DRF
5. **Live Map** — display events as markers on a Leaflet map, colored by category and sized by intensity
6. **Filtering** — filter events by category, date range, and bounding box
7. **Event Detail** — drill into an event to see contributing articles with source links
8. **Auto-refresh** — frontend polls for new events every 60 seconds without full page reload
9. **Newsletter** — daily AI-written briefing generated from top events, sent via email to subscribers
10. **Subscription flow** — subscribe, confirm via email link, unsubscribe via token link

### Non-Functional

- All pipeline stages run every **10 minutes** with a **30-minute hard timeout**
- API response time < 500ms for typical event list queries
- Frontend works on modern browsers (Chrome, Firefox, Safari latest)
- Docker Compose starts the full stack with a single `docker compose up`
- HTTPS via Let's Encrypt with automatic cert renewal
- Stateless backend workers — all shared state in MongoDB or Redis
- Emails rendered as HTML with plain-text fallback; sent via AWS SES in production

---

## Architecture

```text
Browser
  └── nginx (:80 / :443)            reverse proxy + TLS termination
        ├── /api/           → backend:8000   Django REST API (DRF)
        ├── /admin/         → backend:8000   Django admin
        ├── /django_static/ → backend:8000   Whitenoise static files
        └── /               → frontend:80    React SPA

backend (uvicorn :8000)
  ├── api/            DRF serializers + APIView endpoints
  ├── newsletter/     Subscriber + DailyNewsletter models + subscribe/confirm/unsubscribe
  ├── core/           Article, Event, Source models + pipeline tasks
  └── accounts/       Custom User model (email-based auth)

worker (python worker.py)
  ├── 4× RQ workers   process high / default / low queues
  ├── Scheduler       enqueues pipeline + newsletter jobs on schedule
  └── Health check    HTTP :8001

certbot               auto-renews Let's Encrypt certs every 12 hours

MongoDB 8 (:27017)    all data storage
Redis (:6379)         RQ job queues + Django cache (geocode + sessions)
```

---

## Pipeline

### Stage 1 — fetch_data (every 10m, timeout 30m)

- Reads all configured `Source` objects
- Fetches new articles via feedparser (RSS) or HTTP requests
- Deduplicates by content hash
- Writes raw `Article` objects to MongoDB

### Stage 2 — process_articles (every 10m, timeout 30m)

- Queries unprocessed Articles (`processed_on=null`)
- Runs spaCy NER to extract named entities and location candidate
- Runs VADER to compute sentiment score [-1, 1]
- Geocodes location string to lat/lng via geopy (Nominatim), 30-day cache
- Scores event intensity from keyword heuristics
- Classifies category via rule-based classifier
- Writes NLP fields back to Article, sets `processed_on`

### Stage 3 — aggregate_events (every 10m, timeout 30m)

- Queries Articles processed since last aggregation
- Groups by (location_name, category, time window)
- Creates or updates `Event` objects with article counts, averages

### Stage 4 — generate_newsletter (daily, timeout 30m)

- Queries today's Events ordered by article_count
- Sends event list to LLM with a structured JSON prompt
- Stores LLM-generated subject + paragraphs as a `DailyNewsletter` (status: draft)
- Idempotent — skips if a newsletter already exists for that date

### Stage 5 — send_newsletter (daily, timeout 30m)

- Loads the draft `DailyNewsletter` for today
- For each active `Subscriber`: replaces unsubscribe URL placeholder, sends HTML + text email via AWS SES
- Updates newsletter status to `sent` (or `error` on partial failure)

---

## API Endpoints

All responses are serialized by DRF. Dates are ISO 8601 UTC strings.

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/events/` | List events — `category`, `start`, `end`, `limit`, `bbox` |
| GET | `/api/events/<id>/` | Event detail + contributing articles |
| GET | `/api/sources/` | List configured sources |
| GET | `/api/newsletter/` | List sent newsletters |
| GET | `/api/newsletter/<date>/` | Newsletter detail (YYYY-MM-DD) |
| POST | `/api/newsletter/subscribe/` | Subscribe — sends confirmation email |
| GET | `/api/newsletter/confirm/<token>/` | Confirm subscription |
| GET | `/api/newsletter/unsubscribe/<token>/` | Unsubscribe |

---

## Data Models

### Source

Configuration for a data source (RSS feed, web source).

| Field | Type | Notes |
| ----- | ---- | ----- |
| code | CharField | Unique identifier |
| type | SourceType | RSS, WEBSITE, API, … |
| name | CharField | Display name |
| url | URLField | Optional |
| author_slug | CharField | Author/slug of the source |
| headers | JSONField | Optional per-source credentials/headers |

### Article

A single news item fetched from a source.

| Field | Type | Notes |
| ----- | ---- | ----- |
| id | UUIDField | PK, auto uuid4 |
| source_code | CharField | FK-like reference to Source.code |
| title | CharField | |
| content | TextField | |
| published_on | DateTimeField | |
| entities | JSONField | spaCy NER output |
| sentiment | FloatField | VADER compound score |
| location | CharField | Extracted place name |
| latitude / longitude | FloatField | Geocoded coordinates |
| event_intensity | FloatField | Severity score 0–1 |
| category | EventCategory | Rule-based classification |
| processed_on | DateTimeField | Set after NLP pipeline runs |

### Event

An aggregated event derived from one or more Articles at the same location.

| Field | Type | Notes |
| ----- | ---- | ----- |
| title | CharField | Summarised from articles |
| category | EventCategory | Dominant category |
| location_name | CharField | Geocoded place name |
| latitude / longitude | FloatField | |
| started_at | DateTimeField | Earliest article timestamp |
| article_count | IntegerField | |
| avg_sentiment | FloatField | |
| avg_intensity | FloatField | 0–1 |
| article_ids | JSONField | List of Article UUID strings |
| source_codes | JSONField | List of source codes |

### Subscriber

A newsletter subscriber.

| Field | Type | Notes |
| ----- | ---- | ----- |
| email | CharField | Unique |
| token | UUIDField | Used for confirm + unsubscribe links |
| subscribed_at | DateTimeField | |
| confirmed_at | DateTimeField | Null until confirmed |
| is_active | BooleanField | True only after email confirmation |
| unsubscribed_at | DateTimeField | Set on unsubscribe |

### DailyNewsletter

One newsletter edition per day.

| Field | Type | Notes |
| ----- | ---- | ----- |
| date | DateField | Unique |
| subject | CharField | LLM-generated headline |
| html_body | TextField | Rendered HTML (unsubscribe URL is a placeholder) |
| text_body | TextField | Plain-text fallback |
| status | CharField | draft / sending / sent / error |
| generated_at | DateTimeField | |
| sent_at | DateTimeField | |
| sent_count | IntegerField | Number of successful sends |
| event_count | IntegerField | Events used to generate the briefing |

---

## Event Categories

| Category | Description |
| -------- | ----------- |
| `conflict` | Armed conflict, airstrikes, military operations, casualties |
| `protest` | Demonstrations, civil unrest, strikes, riots |
| `disaster` | Natural or man-made disasters, evacuations, epidemics |
| `political` | Elections, coups, diplomatic events, government decisions |
| `economic` | Trade, markets, inflation, energy, fiscal policy |
| `crime` | Arrests, violence, corruption, trafficking, investigations |
| `general` | Anything that doesn't match above categories |

---

## Frontend

### Map

- CartoDB dark tiles, initial view: world zoom 2
- `CircleMarker` per event — color = category, radius = `6 + intensity × 14`
- Click marker → select event, fly map to it, highlight card in list

### Event List

- Fixed-width side panel
- Scrolls independently of map
- Selecting a card flies the map to that event's marker

### EventCard

- Shows: category badge, time-ago, title, location, article count, intensity bar
- Expand button lazy-loads contributing articles with source links via `/api/events/<id>/`

### Newsletter Pages

- `/newsletter` — list of sent newsletters with links to view each
- `/newsletter/<date>` — full newsletter view in browser

### Filters

- Category dropdown, start/end date inputs
- Sent as query params to `/api/events/`
- Clear button resets all filters

### Polling

- `setInterval` every 60 seconds re-fetches `/api/events/` with current filters

---

## Email Templates

All transactional emails are HTML with plain-text fallbacks, rendered via Django's template engine and sent via the configured email provider.

| Template | Purpose |
| -------- | ------- |
| `newsletter/email.html` | Daily briefing (HTML) |
| `newsletter/email.txt` | Daily briefing (plain text) |
| `newsletter/confirm_email.html` | Subscription confirmation (HTML) |
| `newsletter/confirm_email.txt` | Subscription confirmation (plain text) |

---

## Docker Services

| Service | Exposed | Role |
| ------- | ------- | ---- |
| nginx | :80, :443 | Reverse proxy + TLS termination |
| certbot | — | Let's Encrypt cert renewal (every 12h) |
| backend | internal :8000 | Django + uvicorn |
| worker | :8001 | RQ workers + scheduler + health check |
| frontend | internal :80 | React SPA (nginx:alpine) |
| mongo | :27017 | Database |
| redis | internal | Queue + cache |

---

## Deployment

### HTTP-only (local / no domain)

```bash
DOMAIN=localhost docker compose up --build
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py createsuperuser
```

Access at <http://localhost>.

### HTTPS with Let's Encrypt (production)

```bash
export DOMAIN=yourdomain.com
export CERTBOT_EMAIL=admin@yourdomain.com

bash init-letsencrypt.sh           # one-time cert setup
DOMAIN=$DOMAIN docker compose up -d
```

Certs auto-renew every 12 hours via the `certbot` service.

Access:

- Site: <https://yourdomain.com>
- API: <https://yourdomain.com/api/>
- Admin: <https://yourdomain.com/admin/>
- Worker health: <http://yourdomain.com:8001/>
