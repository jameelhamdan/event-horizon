import os
import subprocess
import sys
from pathlib import Path
import app

from decouple import config

BACKEND_DIR = Path(__file__).resolve().parent.parent   # .../backend/
BASE_DIR = BACKEND_DIR.parent                           # project root

# App Versioning
try:
    commit_id = subprocess.check_output(["git", "describe", "--always"], cwd=BASE_DIR).decode('utf-8').strip()
except Exception:
    commit_id = ''

VERSION_NUMBER = app.__version__

if commit_id:
    app.__build__ = f'{VERSION_NUMBER}-{commit_id}'
else:
    app.__build__ = f'{VERSION_NUMBER}'

ENV_NAME = config('ENV_NAME', default='development')
VERSION = f'{ENV_NAME}-{app.__build__}'

SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False)

APP_NAME = config('APP_NAME', default='eventhorizonai.dev')

ADMINS = (
    ('admin', f'contact@{APP_NAME}'),
)

ALLOWED_HOSTS = ['*']
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='http://localhost').split(',')

INSTALLED_APPS = [
    'django_mongodb_backend',
    'apps.MongoAdminConfig',
    'apps.MongoAuthConfig',
    'apps.MongoContentTypesConfig',
    'qsessions',
    'django.contrib.messages',
    'django.contrib.sitemaps',
    'django.contrib.staticfiles',

    'rest_framework',
    'import_export',
    'corsheaders',

    # Apps
    'accounts',
    'core',
    'newsletter',
    'misc',
    'api',
    'services',
    'tests',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'app.middleware.VersionHeaderMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'qsessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

X_FRAME_OPTIONS = 'SAMEORIGIN'
CORS_ALLOWED_ORIGINS = []

if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
elif DOMAIN := config('DOMAIN'):
    CORS_ALLOWED_ORIGINS = [f'https://{DOMAIN}']


ROOT_URLCONF = 'app.urls'
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            os.path.join(BACKEND_DIR, 'templates'),
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

SENTRY_DSN = config('DSN', default='')

if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        # Add data like request headers and IP for users,
        # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
        send_default_pii=False,  # Explicitly disable PII
        max_request_body_size="never",  # Don't send request bodies
        include_source_context=False,  # Don't send source code context
        include_local_variables=False,  # Don't send local variable values
    )

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'root': {
        'level': 'INFO',
        'handlers': ['console'],
    },
    'formatters': {
        'verbose': {
            'format': '%(levelname)s %(asctime)s %(module)s %(filename)s %(funcName)s %(process)d %(thread)d %(threadName)s %(message)s',
        },
    },
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        },
    },
    'handlers': {
        'null': {
            'level': 'DEBUG',
            'class': 'logging.NullHandler',
        },
        'console': {
            'level': 'ERROR',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
            'stream': sys.stdout,
        },
        'api_handler': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
            'stream': sys.stdout,
        },
    },
    'loggers': {
        'api': {
            'handlers': ['api_handler'],
            'level': 'INFO',
            'propagate': True,
        },
        'services': {
            'handlers': ['api_handler'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'celery': {
            'handlers': ['api_handler'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': True,
        },
        'django.db.backends': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.security.DisallowedHost': {
            'handlers': ['null'],
            'propagate': False,
        },
    },
}

ASGI_APPLICATION = 'app.asgi.application'

FILE_UPLOAD_MAX_MEMORY_SIZE = 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10000

# Mongo are required
DATABASE_URL = config('DATABASE_URL', default='mongodb://root:1234@localhost:27017/radar-live?authSource=admin')

DATABASES = {
    'default': {
        'ENGINE': 'django_mongodb_backend',
        'HOST': DATABASE_URL,
    }
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    },
    'redis-cache': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': config('REDIS_URL', default='redis://localhost:6379/0'),
        'OPTIONS': {
            'MAX_ENTRIES': 10000,
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    },
}

TASK_QUEUE_ENABLED = config('TASK_QUEUE_ENABLED', default=False, cast=bool)

# Persist a TaskRun row (core.models.TaskRun) for every enqueue() call — task
# history/duration/error queryable in /admin/core/taskrun/. Off by default in
# dependency-light unit tests (tests/tests_queue.py) that don't have Mongo
# available — see services/queue.py.
TASK_RUN_TRACKING_ENABLED = config('TASK_RUN_TRACKING_ENABLED', default=True, cast=bool)

# ── Feature flags — gates both scheduling (api/crontab) and the task function itself.
NEWSLETTER_ENABLED = config('NEWSLETTER_ENABLED', default=True, cast=bool)
STREAM_PRICES_ENABLED = config('STREAM_PRICES_ENABLED', default=True, cast=bool)
# Default False: aviationweather.gov removed NOTAM data entirely (Jul 2026) — every
# /api/data/notam request 404s. Re-enable once services/streams/notam.py is rewritten
# against the FAA NOTAM API (external-api.faa.gov, requires client_id/client_secret,
# per-location radius queries only — see the notam.py module docstring).
STREAM_NOTAM_ENABLED = config('STREAM_NOTAM_ENABLED', default=False, cast=bool)
STREAM_EARTHQUAKE_ENABLED = config('STREAM_EARTHQUAKE_ENABLED', default=True, cast=bool)
STREAM_FOREX_ENABLED = config('STREAM_FOREX_ENABLED', default=True, cast=bool)

# ── DRF throttling — rate-limit anonymous traffic on the public read API.
REST_FRAMEWORK = {
    'DEFAULT_THROTTLE_CLASSES': ['rest_framework.throttling.AnonRateThrottle'],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '120/min',
    },
}

# ── LLM providers + routing ─────────────────────────────────────────────────────
# Available providers: openrouter, ollama.

OPENROUTER_API_KEYS = config('OPENROUTER_API_KEYS', default='')
OPENROUTER_MODELS = config('OPENROUTER_MODELS', default='openrouter/free')
# Dynamic discovery: a daily task probes OpenRouter's free models and caches the
# top working picks in Redis (see services/llm/discovery.py). OPENROUTER_MODELS is
# the fallback when discovery is disabled or the cache is empty.
OPENROUTER_DYNAMIC_MODELS = config('OPENROUTER_DYNAMIC_MODELS', default=True, cast=bool)
OPENROUTER_MODELS_COUNT = config('OPENROUTER_MODELS_COUNT', default=5, cast=int)

# ── Article importance scoring ────────────────────────────────────────────────
# LLM-based 1.0–10.0 significance rating. Low-scoring articles are skipped by the
# NLP pipeline and deleted after a grace period.
ARTICLE_IMPORTANCE_SCORING_ENABLED = config('ARTICLE_IMPORTANCE_SCORING_ENABLED', default=True, cast=bool)
ARTICLE_MIN_IMPORTANCE_TO_PROCESS = config('ARTICLE_MIN_IMPORTANCE_TO_PROCESS', default=2.0, cast=float)  # below this → skip process_articles_task
ARTICLE_MIN_IMPORTANCE = config('ARTICLE_MIN_IMPORTANCE', default=4.0, cast=float)                        # below this → eligible for deletion
ARTICLE_CLEANUP_GRACE_HOURS = 48
# Window the every-30-min 'aggregate' stage re-clusters. Kept below the 168h
# tag/route repair lookback (services.stages.EVENT_STAGE_WINDOW_HOURS) so each
# tick doesn't re-cluster a full week; aggregate_full_task sweeps the full 168h
# once daily to catch multi-day events that age past this window.
AGGREGATE_LIVE_WINDOW_HOURS = config('AGGREGATE_LIVE_WINDOW_HOURS', default=72, cast=int)
ARTICLE_STALE_PROCESSED_DAYS = 7
# Per-calendar-month retention cap, ranked by importance_score — applies retroactively
# to every month, not just the current one. Keeps DB growth bounded over years of
# ingest; articles referenced by any Event.article_ids are exempt from this prune.
ARTICLE_MONTHLY_IMPORTANCE_CAP = config('ARTICLE_MONTHLY_IMPORTANCE_CAP', default=5000, cast=int)
ARTICLE_MIN_WORD_COUNT = 30               # fetch-time filter, zero LLM cost
ARTICLE_DEDUP_TITLE_ENABLED = True        # Jaccard dedup on titles (Redis-backed)
ARTICLE_DEDUP_JACCARD_THRESHOLD = 0.75
ARTICLE_DEDUP_HOURS = 24

# Optional egress proxy for Wayback Machine requests during historical backfill
# (front-page mining + dead-URL body fallback — see services/data/wayback.py).
# Wayback rate-limits per IP; leave empty to go direct.
WAYBACK_PROXY_URL = config('WAYBACK_PROXY_URL', default='')

# LLM master switches. These are only the *seed defaults* — the live values are
# stored in core.RuntimeConfig and edited from the admin dashboard's Actions
# section (see services/runtime_config.py), so an operator can flip either
# without a redeploy/restart and it takes effect on the next tick / backfill
# chunk. They're read at execution time, so a change also applies to an
# already-dispatched, in-flight backfill. These env vars only decide the initial
# value the first time RuntimeConfig is created (fail-open fallback if Mongo read
# ever fails).
#
# LIVE_LLM_ENABLED gates the live score/process/geocode stages. When off, the
# live pipeline keeps fetching but stops LLM annotation — articles accumulate as
# pending and resume when re-enabled.
# BACKFILL_LLM_ENABLED gates historical backfill annotation. When off,
# backfill_day_chunk_task fetches + saves and defers annotation
# (annotation_deferred=True) for a later annotate_deferred_articles_task pass.
LIVE_LLM_ENABLED = config('LIVE_LLM_ENABLED', default=True, cast=bool)
BACKFILL_LLM_ENABLED = config('BACKFILL_LLM_ENABLED', default=True, cast=bool)

# Ollama (self-hosted, no key) — three model tiers for different task complexities.
OLLAMA_BASE_URL = config('OLLAMA_BASE_URL', default='http://localhost:11434')
OLLAMA_MODEL_SMALL  = 'qwen3:4b'
OLLAMA_MODEL_MEDIUM = 'qwen3:8b'
OLLAMA_MODEL_LARGE  = 'qwen3:14b'

# Groq — free tier, OpenAI-compatible (https://console.groq.com).
GROQ_API_KEYS = config('GROQ_API_KEYS', default='')
GROQ_MODEL = 'llama-3.1-8b-instant'

# Cerebras — free tier, OpenAI-compatible (https://cloud.cerebras.ai).
# NB: very low request quota (5 req/min, 150/hour, 2,400/day; 30k tokens/min) —
# use as a fast *secondary*, or primary only for low-volume roles (newsletter).
CEREBRAS_API_KEYS = config('CEREBRAS_API_KEYS', default='')
# 'gemma-4-31b' was not a model Cerebras serves (Gemma is Google's; Cerebras has
# no gemma-4/31b), so the cerebras leg silently failed over on every call and
# never contributed. llama-3.3-70b is a generally-available Cerebras model; keep
# it env-overridable so the operator can swap it without a redeploy when the
# served roster changes. Confirm with: manage.py test_llm --role scoring.
CEREBRAS_MODEL = config('CEREBRAS_MODEL', default='llama-3.3-70b')

# Mistral — free "Experiment" tier, OpenAI-compatible (https://api.mistral.ai/v1).
# Requires opting into data-training on La Plateforme per account (we only send
# public scraped RSS news content — low sensitivity). 2 req/min per key;
# comma-separated keys round-robin the same as GROQ_API_KEYS/CEREBRAS_API_KEYS
# (see services/llm/__init__.py::_Cycle).
MISTRAL_API_KEYS = config('MISTRAL_API_KEYS', default='')
MISTRAL_MODEL = config('MISTRAL_MODEL', default='mistral-small-latest')

# Per-request LLM timeout. Cloud providers (Groq/Cerebras) are fast-inference
# chips — a stuck/degraded request should fail and fall back quickly rather
# than eat a large chunk of the task's own time limit. Was 300s; a hung
# provider combined with multi-model fallback (see OpenAICompatLLMService)
# could burn 25+ minutes on a single call and get SIGKILLed by the job
# timeout mid-request instead of failing cleanly.
LLM_TIMEOUT_SECONDS = 45
# Ollama is last-resort and requests aren't batched. Timeouts scale with model
# size since bigger models generate slower on CPU; OLLAMA_TIMEOUT_SECONDS is the
# fallback when a tier isn't listed in OLLAMA_TIMEOUTS.
OLLAMA_TIMEOUT_SECONDS = 20
OLLAMA_TIMEOUTS = {'small': 15, 'medium': 25, 'large': 45}
# The self-hosted Ollama box is CPU-only with OLLAMA_NUM_PARALLEL=1 /
# OLLAMA_MAX_LOADED_MODELS=1 — it genuinely serves one generation at a time.
# worker-heavy runs several prefork processes that can each fall back to Ollama
# at once; without a cross-worker cap, N-1 of them block on Ollama's internal
# queue and burn their full read timeout before getting a slot (the server logs
# `aborting completion request due to client closing the connection`). This
# guard (services/llm/__init__.py) bounds concurrent Ollama requests across all
# workers to OLLAMA_MAX_CONCURRENCY and *fast-rejects* once no slot frees within
# OLLAMA_ACQUIRE_SECONDS, so a blocked heavy worker fails out quickly instead of
# sitting idle. Keep OLLAMA_MAX_CONCURRENCY == the Ollama server's
# OLLAMA_NUM_PARALLEL; raise both together only if the box has the RAM/CPU.
OLLAMA_MAX_CONCURRENCY = config('OLLAMA_MAX_CONCURRENCY', default=1, cast=int)
OLLAMA_ACQUIRE_SECONDS = config('OLLAMA_ACQUIRE_SECONDS', default=2.0, cast=float)

# Routing: role -> provider name OR ordered fallback list (tried in order on failure).
# Unconfigured providers are silently skipped; unknown roles fall back to 'default'.
#
# Ollama tiers map task complexity to model size (4b/8b/14b).
# Groq and Cerebras provide free-tier cloud fallback when Ollama is unavailable.
# Lead with fast hosted providers; Ollama is the unlimited (but slow) last-resort.
#   - Groq leads the high-volume small/medium tasks (it has the headroom).
#   - Cerebras sits second — its tiny 5 req/min quota means it absorbs a few
#     fast calls per minute then 429s and cascades; it only *leads* the
#     low-volume daily newsletter, where 5 rpm + its 31B model are ideal.
#   - OpenRouter is the mid fallback; Ollama is always last.
# NB: article analysis is category/sub-category/geo/intensity only — sentiment
# (VADER) and Arabic translation (MarianMT) run locally, never through the LLM.
# See CLAUDE.md "LLM routing" for the full local-model map.
LLM_ROUTES = {
    'default':       ['groq', 'cerebras', 'openrouter', 'ollama_medium'],
    'analyzer_lite': ['groq', 'cerebras', 'mistral', 'openrouter', 'ollama_medium'],  # article analysis (EN-only LLM output)
    'newsletter':    ['cerebras', 'openrouter', 'ollama_large'],           # long-form, low volume
    'scoring':       ['groq', 'cerebras', 'openrouter', 'ollama_small'],
    'historical':    ['groq', 'cerebras', 'openrouter', 'ollama_small'],
    'topics':        ['groq', 'cerebras', 'openrouter', 'ollama_medium'],  # enrichment/discovery only — tagging is local (embeddings)
}

# ── Forecasting (event-fused symbol prediction) ───────────────────────────────
FORECAST_ENABLED = config('FORECAST_ENABLED', default=True, cast=bool)
FORECAST_MODEL_DIR = str(BASE_DIR / 'forecast_models')
FORECAST_HORIZONS_DAYS = [1, 5]    # trading-day horizons trained + served
FORECAST_TRAIN_WINDOW_DAYS = 540
FORECAST_ROUTER = 'rules'          # deterministic event→symbol routing (no LLM calls); tagged onto Event/Forecast.router_source

# ── Celery ────────────────────────────────────────────────────────────────────
_REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/0')
# Was 1800s (30 min). A stuck job (e.g. a hung LLM provider) doesn't fail fast —
# it sits until this ceiling kills it, and the whole queue backs up behind it in
# that increment. With the tightened LLM timeouts above, a single chunk's worst
# case is well under 5 min, so 600s still leaves a comfortable margin while
# cutting the cost of any future stuck job by 3x.
_JOB_TIMEOUT = 600

# Task queue names (default/heavy/bulk) are selected per-call via enqueue(queue=...)
# and run_task.py's HEAVY_TASKS/BULK_TASKS maps — no static CELERY_TASK_ROUTES needed.
CELERY_BROKER_URL = _REDIS_URL
CELERY_RESULT_BACKEND = None  # core.models.TaskRun is the source of truth for task history/status
# pickle (not Celery's JSON default) — task args include datetime objects
# (start_date/end_date, etc.), which JSON can't serialize natively.
CELERY_TASK_SERIALIZER = 'pickle'
CELERY_RESULT_SERIALIZER = 'pickle'
CELERY_ACCEPT_CONTENT = ['pickle', 'json']
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TIMEZONE = 'UTC'
# Broadcast task/worker events on the broker so Flower shows live ground truth
# (what is *actually* running on each worker right now). TaskRun rows are
# best-effort history written from worker signals — a killed worker can leave
# them stale (see services/queue.py reap_stale_task_runs).
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True

# Flower's in-cluster address — the container publishes no port; it is only
# reachable through the staff-authenticated Django proxy at /flower/ (app/views.py).
FLOWER_INTERNAL_URL = config('FLOWER_INTERNAL_URL', default='http://flower:5555')

# Per-queue default time limit (seconds) applied by enqueue() when a call doesn't
# pass job_timeout explicitly. 'bulk' has no default cap (long one-shot backfills/
# training) so a job blocks there, not on the live 'heavy'/'default' pipeline queues.
CELERY_QUEUE_TIME_LIMITS = {
    'default': _JOB_TIMEOUT,
    'heavy': _JOB_TIMEOUT,
    'bulk': None,
}

# Worker concurrency per queue — display-only (admin dashboard's queue summary).
# MUST be kept in sync with each worker-* service's --concurrency flag in
# docker-compose.yml; not read from Celery at request time since app.control
# .inspect() is a live broadcast round-trip to every worker (slow, and returns
# nothing useful if a worker is mid-restart) — too fragile for a page that
# should render instantly even when a queue's workers are down.
CELERY_QUEUE_WORKERS = {
    'default': 4,
    'heavy': 4,
    'bulk': 1,
}

# Email — provider selection: 'ses' (AWS SES) or 'smtp' (Django SMTP / console)
EMAIL_PROVIDER = config('EMAIL_PROVIDER', default='smtp')

# SMTP settings (used when EMAIL_PROVIDER=smtp)
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='localhost')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')

# AWS SES settings (used when EMAIL_PROVIDER=ses)
AWS_SES_ACCESS_KEY_ID = config('AWS_SES_ACCESS_KEY_ID', default='')
AWS_SES_SECRET_KEY = config('AWS_SES_SECRET_KEY', default='')
AWS_SES_REGION = config('AWS_SES_REGION', default='us-east-1')

DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='newsletter@localhost')
NEWSLETTER_BASE_URL = config('NEWSLETTER_BASE_URL', default='http://localhost')

AUTH_USER_MODEL = 'accounts.User'
SESSION_ENGINE = 'qsessions.backends.cached_db'
DEFAULT_AUTO_FIELD = 'django_mongodb_backend.fields.ObjectIdAutoField'
SILENCED_SYSTEM_CHECKS = ['mongodb.E001', 'mongodb.fields.auto.E001']
MIGRATION_MODULES = {
    "admin": "migrations.admin",
    "auth": "migrations.auth",
    "contenttypes": "migrations.contenttypes",
    "accounts": "migrations.accounts",
    "core": "migrations.core",
    "newsletter": "migrations.newsletter",
    "misc": "migrations.misc",
}

AUTHENTICATION_BACKENDS = ['app.backends.ModelAuthBackend']

SESSION_COOKIE_AGE = 60 * 60 * 24 * 30  # One month session time

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {
            'min_length': 9,
        }
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-ca'

TIME_ZONE = 'UTC'

USE_TZ = True

DATE_FORMAT = 'Y-m-d'
DATETIME_FORMAT = 'Y-m-d H:i'
SHORT_DATE_FORMAT = 'Y-m-d'
SHORT_DATETIME_FORMAT = 'Y-m-d H:i'
TIME_FORMAT = 'H:i'

STATIC_URL = '/django_static/'
STATIC_ROOT = BACKEND_DIR / '.static'

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}
