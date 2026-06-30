"""Dynamic OpenRouter free-model discovery.

OpenRouter's free-model roster and per-model availability change constantly (models
get deprecated, rate-limited, or start leaking reasoning tokens). Instead of pinning
a static model id, a daily task queries the models API, filters to working free text
models, *probes* the top candidates, and caches the survivors in Redis for the LLM
layer (``services.llm._provider_specs``) to consume.

Cache key holds a JSON list of model ids, freshest pick first. ``get_models()`` falls
back to ``settings.OPENROUTER_MODELS`` when the cache is empty/unreachable.
"""

import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

MODELS_URL = 'https://openrouter.ai/api/v1/models'
CHAT_URL = 'https://openrouter.ai/api/v1'
REDIS_KEY = 'llm:openrouter:models'
_CACHE_TTL = 60 * 60 * 36  # 36h — outlives a single missed daily run

# id substrings that aren't general-purpose chat models
_EXCLUDE = (
    'lyria', 'whisper', 'tts', 'embed', 'moderation', 'guard', 'safety',
    'image', 'video', 'sora', 'dall', 'vision', 'audio', 'coder', 'code-',
)

_PROBE = [{'role': 'user', 'content': 'Reply with exactly the word: OK'}]


def _redis():
    import redis as redis_lib
    return redis_lib.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))


def _keys() -> list[str]:
    from django.conf import settings
    return [k.strip() for k in (settings.OPENROUTER_API_KEYS or '').split(',') if k.strip()]


def _is_free_text(model: dict) -> bool:
    pricing = model.get('pricing') or {}
    free = float(pricing.get('prompt') or 0) == 0 and float(pricing.get('completion') or 0) == 0
    arch = model.get('architecture') or {}
    out = arch.get('output_modalities') or arch.get('modality') or 'text'
    out_str = out if isinstance(out, str) else ' '.join(out)
    return free and 'text' in out_str


def list_free_models(min_context: int = 16000, insecure: bool = False) -> list[str]:
    """All free text models ranked by context length (largest first)."""
    keys = _keys()
    if not keys:
        logger.warning('[openrouter] no API key configured — cannot list models')
        return []
    resp = requests.get(
        MODELS_URL, headers={'Authorization': f'Bearer {keys[0]}'},
        timeout=30, verify=not insecure,
    )
    resp.raise_for_status()
    ranked: list[tuple[str, int]] = []
    for m in resp.json().get('data', []):
        mid = m.get('id', '')
        if not mid or any(x in mid.lower() for x in _EXCLUDE):
            continue
        if not _is_free_text(m):
            continue
        if (m.get('context_length') or 0) < min_context:
            continue
        ranked.append((mid, m.get('context_length') or 0))
    ranked.sort(key=lambda t: -t[1])
    return [mid for mid, _ in ranked]


def _probe(model: str, key: str, insecure: bool = False) -> str:
    from openai import OpenAI
    http_client = None
    if insecure:
        import httpx
        http_client = httpx.Client(verify=False)
    client = OpenAI(base_url=CHAT_URL, api_key=key, http_client=http_client, max_retries=0)
    completion = client.chat.completions.create(
        model=model, messages=_PROBE, temperature=0, max_tokens=16, timeout=30,
    )
    return (completion.choices[0].message.content or '').strip()


def discover(limit: int = 5, candidates: int = 12, insecure: bool = False) -> list[str]:
    """Return up to ``limit`` free models that respond cleanly *right now*.

    Probes the top ``candidates`` by context with a tiny request and keeps only those
    that reply with a short, instruction-following answer — this drops models that are
    rate-limited, deprecated, or leak reasoning/empty output.
    """
    models = list_free_models(insecure=insecure)
    keys = _keys()
    if not models or not keys:
        return models[:limit]

    working: list[str] = []
    for mid in models[:candidates]:
        try:
            started = time.monotonic()
            reply = _probe(mid, keys[0], insecure=insecure)
            elapsed = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001 — any failure just disqualifies the model
            logger.info('[openrouter] probe %s failed: %s', mid, str(exc)[:120])
            continue
        if 'OK' in reply.upper() and len(reply) <= 40:
            working.append(mid)
            logger.info('[openrouter] probe %s OK (%.2fs)', mid, elapsed)
            if len(working) >= limit:
                break
        else:
            logger.info('[openrouter] probe %s rejected (reply=%r)', mid, reply[:60])
    return working


def cache_models(models: list[str]) -> None:
    try:
        _redis().set(REDIS_KEY, json.dumps(models), ex=_CACHE_TTL)
    except Exception as exc:  # noqa: BLE001
        logger.warning('[openrouter] failed to cache models: %s', exc)


def get_models() -> list[str]:
    """Cached discovered models, freshest first; falls back to settings."""
    from django.conf import settings
    try:
        raw = _redis().get(REDIS_KEY)
        if raw:
            models = json.loads(raw)
            if models:
                return models
    except Exception as exc:  # noqa: BLE001
        logger.debug('[openrouter] cache read failed: %s', exc)
    fallback = [m.strip() for m in (settings.OPENROUTER_MODELS or '').split(',') if m.strip()]
    return fallback or ['openrouter/free']


def refresh(limit: int | None = None, insecure: bool = False) -> list[str]:
    """Discover + cache the current top working free models. Returns the list."""
    from django.conf import settings
    limit = limit or getattr(settings, 'OPENROUTER_MODELS_COUNT', 5)
    models = discover(limit=limit, insecure=insecure)
    if models:
        cache_models(models)
        logger.info('[openrouter] cached %d models: %s', len(models), ', '.join(models))
    else:
        logger.warning('[openrouter] discovery found no working models — keeping existing cache')
    return models
