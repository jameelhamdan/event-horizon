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

# WSGI_APPLICATION = 'app.wsgi_application'
ASGI_APPLICATION = 'app.asgi.application'
# os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

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
# Each gates both scheduling (setup_schedule) and the task function itself.
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
# Per-provider settings live in .env (below). Routing — which provider serves each
# use-case — lives in LLM_ROUTES (a Python dict, edited in code, further below).
# Available providers: openrouter, ollama, g4f.

# OpenRouter (https://openrouter.ai) — comma-separated keys rotate round-robin
OPENROUTER_API_KEYS = config('OPENROUTER_API_KEYS', default='')
OPENROUTER_MODELS = config('OPENROUTER_MODELS', default='openrouter/free')

# Ollama (self-hosted, no key)
OLLAMA_BASE_URL = config('OLLAMA_BASE_URL', default='http://localhost:11434')
OLLAMA_MODEL = config('OLLAMA_MODEL', default='qwen3:4b')

# g4f / gpt4free (local proxy, registration-free). NOTE: the hlohaus789/g4f Docker image
# serves the OpenAI-compatible API on :8080 (not :1337) and git-pulls main on boot, so models
# drift — keep G4F_MODEL on a working provider (gpt-4o; gpt-4o-mini is broken upstream).
G4F_BASE_URL = config('G4F_BASE_URL', default='http://localhost:8080/v1')
G4F_MODEL = config('G4F_MODEL', default='gpt-4o')

# Routing: role -> provider name OR ordered fallback list (tried in order on failure).
# Unconfigured providers are skipped; unknown roles fall back to 'default'.
LLM_ROUTES = {
    # Default chain for every role/use-case. g4f leads — it's the headless, always-on
    # Docker service (registration-free); then OpenRouter as fallback.
    'default': ['g4f', 'openrouter'],

    # Override per role if you want. Roles: analyzer, topics, newsletter,
    # historical, routing. e.g. lead the per-article analyzer with a local Ollama model for
    # stronger Arabic/JSON quality:
    #   'analyzer': ['ollama', 'g4f'],

    # Event→symbol routing (LLMEventRouter). Falls back to deterministic routing.py on error.
    'routing': ['g4f', 'openrouter'],
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
    # Heavy queue — NLP / LLM tasks (processing, clustering, topic matching)
    'heavy': {
        'URL': _REDIS_URL,
        'DEFAULT_TIMEOUT': _JOB_TIMEOUT,
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

CRON_CLASSES = []

AUTH_USER_MODEL = 'accounts.User'
SESSION_ENGINE = 'qsessions.backends.cached_db'
DEFAULT_AUTO_FIELD = 'django_mongodb_backend.fields.ObjectIdAutoField'
SILENCED_SYSTEM_CHECKS = ['mongodb.E001']
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

# USE_I18N and USE_L10N are deprecated in Django 6.0 (always enabled)
# They are kept here for backward compatibility but have no effect
USE_I18N = False

USE_L10N = False

USE_TZ = True

DATE_FORMAT = 'Y-m-d'
DATETIME_FORMAT = 'Y-m-d H:i'
SHORT_DATE_FORMAT = 'Y-m-d'
SHORT_DATETIME_FORMAT = 'Y-m-d H:i'
TIME_FORMAT = 'H:i'

STATIC_URL = '/django_static/'
STATIC_ROOT = BACKEND_DIR / '.static'

FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
STORAGES = {
    'default': {
        'BACKEND': FILE_STORAGE,
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}
