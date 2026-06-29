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
except Exception as e:
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

APP_NAME = config('APP_NAME', default='conflictradar.live')

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
    'django_rq',
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
        'rq': {
            'handlers': ['api_handler'],
            'level': 'INFO',
            'propagate': False,
        },
        'rq.worker': {
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

# ── Feature flags (A4) — let a deployment run a lean core without code changes. ──
# Each gates both scheduling (api/crontab) and the task function itself.
NEWSLETTER_ENABLED = config('NEWSLETTER_ENABLED', default=True, cast=bool)
STREAM_PRICES_ENABLED = config('STREAM_PRICES_ENABLED', default=True, cast=bool)
STREAM_NOTAM_ENABLED = config('STREAM_NOTAM_ENABLED', default=True, cast=bool)
STREAM_EARTHQUAKE_ENABLED = config('STREAM_EARTHQUAKE_ENABLED', default=True, cast=bool)
STREAM_FOREX_ENABLED = config('STREAM_FOREX_ENABLED', default=True, cast=bool)

# ── DRF throttling (A3) — the public read API is exposed; rate-limit anon traffic.
# Backed by the default cache (Redis, below). Tune the rate via env.
REST_FRAMEWORK = {
    'DEFAULT_THROTTLE_CLASSES': ['rest_framework.throttling.AnonRateThrottle'],
    'DEFAULT_THROTTLE_RATES': {
        'anon': config('API_THROTTLE_ANON', default='120/min'),
    },
}

# ── LLM providers + routing ─────────────────────────────────────────────────────
# Available providers: openrouter, ollama.

# OpenRouter — two modes (configure one):
#   Proxy rotation: set OPENROUTER_PROXY_URLS to a comma-separated list of proxy
#   base URLs, each pre-authenticated with one OpenRouter key. The client rotates
#   over these URLs round-robin (no api_key needed).
#   Direct: set OPENROUTER_API_KEYS (comma-separated keys, rotated round-robin).
OPENROUTER_PROXY_URLS = config('OPENROUTER_PROXY_URLS', default='')
OPENROUTER_API_KEYS = config('OPENROUTER_API_KEYS', default='')
OPENROUTER_MODELS = config('OPENROUTER_MODELS', default='openrouter/free')
# Network-level HTTP proxies for LLM calls. Format:
#   http://host:port::api_key,http://host2:port,http://host3:port::api_key3
# The '::api_key' suffix is optional; proxies without an explicit key draw from
# OPENROUTER_API_KEYS in round-robin (loosely tied — pool and proxy list may differ
# in length and are cycled independently before being paired at startup).
OPENROUTER_HTTP_PROXIES = config('OPENROUTER_HTTP_PROXIES', default='')

# Open-source proxy pool: auto-fetches free HTTP proxies from GitHub-hosted lists
# and the ProxyScrape API, validates each candidate against openrouter.ai, then
# rotates working proxies round-robin. Pool refreshes in the background every
# OPENROUTER_PROXY_REFRESH_HOURS hours. Takes precedence over OPENROUTER_HTTP_PROXIES.
OPENROUTER_PROXY_POOL_ENABLED = config('OPENROUTER_PROXY_POOL_ENABLED', default=False, cast=bool)
# Override default sources (TheSpeedX, ShiftyTR, clarketm, ProxyScrape) with a
# comma-separated list of raw-text URLs, each returning one "host:port" per line.
OPENROUTER_PROXY_SOURCES = config('OPENROUTER_PROXY_SOURCES', default='')
OPENROUTER_PROXY_REFRESH_HOURS = config('OPENROUTER_PROXY_REFRESH_HOURS', default=6, cast=float)
OPENROUTER_PROXY_VALIDATE_TIMEOUT = config('OPENROUTER_PROXY_VALIDATE_TIMEOUT', default=5, cast=int)
OPENROUTER_PROXY_MAX_POOL = config('OPENROUTER_PROXY_MAX_POOL', default=100, cast=int)

# ── Article importance scoring ────────────────────────────────────────────────
# LLM-based 1.0–10.0 significance rating applied hourly (see api/crontab).
# Low-scoring articles are excluded from the NLP pipeline
# (ARTICLE_MIN_IMPORTANCE_TO_PROCESS) and deleted after a grace period
# (ARTICLE_MIN_IMPORTANCE + ARTICLE_CLEANUP_GRACE_HOURS).
ARTICLE_IMPORTANCE_SCORING_ENABLED = config('ARTICLE_IMPORTANCE_SCORING_ENABLED', default=True, cast=bool)
# Articles below this score are skipped by process_articles_task.
ARTICLE_MIN_IMPORTANCE_TO_PROCESS = config('ARTICLE_MIN_IMPORTANCE_TO_PROCESS', default=4.0, cast=float)
# Articles below this score are deleted by cleanup_low_importance_articles_task.
ARTICLE_MIN_IMPORTANCE = config('ARTICLE_MIN_IMPORTANCE', default=4.0, cast=float)
# Grace window before low-importance articles are eligible for deletion.
ARTICLE_CLEANUP_GRACE_HOURS = config('ARTICLE_CLEANUP_GRACE_HOURS', default=48, cast=int)
# Processed+unlocated articles older than this are pruned by prune_stale_articles_task.
ARTICLE_STALE_PROCESSED_DAYS = config('ARTICLE_STALE_PROCESSED_DAYS', default=7, cast=int)
# Fetch-time filters applied to every RSS article (zero LLM cost).
ARTICLE_MIN_WORD_COUNT = config('ARTICLE_MIN_WORD_COUNT', default=30, cast=int)
# Near-duplicate title dedup using Jaccard token overlap (Redis cache, TTL=ARTICLE_DEDUP_HOURS).
ARTICLE_DEDUP_TITLE_ENABLED = config('ARTICLE_DEDUP_TITLE_ENABLED', default=True, cast=bool)
ARTICLE_DEDUP_JACCARD_THRESHOLD = config('ARTICLE_DEDUP_JACCARD_THRESHOLD', default=0.75, cast=float)
ARTICLE_DEDUP_HOURS = config('ARTICLE_DEDUP_HOURS', default=24, cast=int)

# Ollama (self-hosted, no key) — three model tiers for different task complexities.
OLLAMA_BASE_URL = config('OLLAMA_BASE_URL', default='http://localhost:11434')
OLLAMA_MODEL_SMALL  = config('OLLAMA_MODEL_SMALL',  default='qwen3:4b')
OLLAMA_MODEL_MEDIUM = config('OLLAMA_MODEL_MEDIUM', default='qwen3:8b')
OLLAMA_MODEL_LARGE  = config('OLLAMA_MODEL_LARGE',  default='qwen3:14b')
OLLAMA_MODEL = config('OLLAMA_MODEL', default=OLLAMA_MODEL_LARGE)  # backward-compat alias

# Groq — free tier, OpenAI-compatible (https://console.groq.com).
GROQ_API_KEYS = config('GROQ_API_KEYS', default='')
GROQ_MODEL = config('GROQ_MODEL', default='llama-3.1-8b-instant')

# Cerebras — free tier, OpenAI-compatible (https://cloud.cerebras.ai).
CEREBRAS_API_KEYS = config('CEREBRAS_API_KEYS', default='')
CEREBRAS_MODEL = config('CEREBRAS_MODEL', default='llama3.1-8b')

# Per-request LLM timeout (seconds) — applies to both OpenRouter and Ollama clients.
# Generous so slow local models / busy free-tier providers aren't cut off mid-generation.
LLM_TIMEOUT_SECONDS = config('LLM_TIMEOUT_SECONDS', default=300, cast=int)

# Routing: role -> provider name OR ordered fallback list (tried in order on failure).
# Unconfigured providers are silently skipped; unknown roles fall back to 'default'.
#
# Ollama tiers map task complexity to model size (4b/8b/14b).
# Groq and Cerebras provide free-tier cloud fallback when Ollama is unavailable.
LLM_ROUTES = {
    'default':       ['ollama_medium', 'groq', 'openrouter'],
    'analyzer':      ['ollama_large',  'openrouter'],           # EN+AR translations
    'analyzer_lite': ['ollama_medium', 'groq', 'openrouter'],   # EN-only backfill
    'newsletter':    ['ollama_large',  'openrouter'],            # long-form prose
    'scoring':       ['ollama_small',  'groq', 'cerebras', 'openrouter'],
    'historical':    ['ollama_small',  'groq', 'cerebras', 'openrouter'],
    'topics':        ['ollama_medium', 'groq', 'openrouter'],
    'routing':       ['ollama_small',  'groq', 'cerebras', 'openrouter'],
}

# ── Forecasting (event-fused symbol prediction) ───────────────────────────────
FORECAST_ENABLED = config('FORECAST_ENABLED', default=True, cast=bool)
FORECAST_MODEL_DIR = config('FORECAST_MODEL_DIR', default=str(BASE_DIR / 'forecast_models'))
# Horizons (trading days) trained + served. Comma-separated.
FORECAST_HORIZONS_DAYS = [
    int(h) for h in config('FORECAST_HORIZONS_DAYS', default='1,5').split(',') if h.strip()
]
FORECAST_TRAIN_WINDOW_DAYS = config('FORECAST_TRAIN_WINDOW_DAYS', default=540, cast=int)
# Live router source: 'llm' (LLMEventRouter, rules fallback) or 'rules' (deterministic only).
FORECAST_ROUTER = config('FORECAST_ROUTER', default='llm')

# ── RQ / django-rq ────────────────────────────────────────────────────────────
_REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/0')
_JOB_TIMEOUT = int(config('JOB_TIMEOUT_SECONDS', default=1800))

RQ_QUEUES = {
    # Light queue — fast I/O tasks (fetchers, stream collectors)
    'default': {
        'URL': _REDIS_URL,
        'DEFAULT_TIMEOUT': _JOB_TIMEOUT,
    },
    # Heavy queue — steady NLP / LLM tasks (processing, clustering, topic matching)
    'heavy': {
        'URL': _REDIS_URL,
        'DEFAULT_TIMEOUT': _JOB_TIMEOUT,
    },
    # Bulk queue — long one-shot jobs (multi-year backfills, model training). Kept
    # off the heavy queue so a hours-long job never blocks the live NLP pipeline.
    'bulk': {
        'URL': _REDIS_URL,
        'DEFAULT_TIMEOUT': -1,
    },
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
