"""Shared cache access — key registry + backend selection.

All cross-process/cross-worker state MUST go through get_redis_client() or the
cache_* helpers below (backed by CACHES['redis-cache'], real shared Redis).
Never use django.core.cache's bare `cache` (CACHES['default'] is LocMemCache —
process-local, not shared) for anything that needs to survive across workers
or task runs.
"""


def get_redis_client(write: bool = True):
    """Raw redis-py client for INCR/pipeline/KEYS-scan use (LLM counters, admin dashboard)."""
    from django.core.cache import caches
    return caches['redis-cache'].client.get_client(write=write)


def cache_get(key: str):
    from django.core.cache import caches
    return caches['redis-cache'].get(key)


def cache_set(key: str, value, timeout: int | None = None) -> None:
    from django.core.cache import caches
    caches['redis-cache'].set(key, value, timeout=timeout)


# ── Key registry — one place, namespaced, so nothing collides ──────────────
# pipeline:*  — fetch/process/backfill state
# llm:*       — provider round-robin, debounce, call stats

KEY_ARTICLE_TITLE_DEDUP = 'pipeline:dedup:article_title'
KEY_BOOTSTRAP_INITIAL_DATA_DONE = 'pipeline:bootstrap:initial_data:done'


def key_backfill_checkpoint(start: str, end: str) -> str:
    return f'pipeline:backfill:{start}:{end}:done'


def key_llm_cycle(provider: str, kind: str) -> str:
    """kind: 'models' | 'keys'."""
    return f'llm:cycle:{provider}:{kind}'


def key_llm_debounce(provider: str, credential_hash: str) -> str:
    return f'llm:debounce:{provider}:{credential_hash}'


def key_llm_debounce_scan_pattern(provider: str) -> str:
    return f'llm:debounce:{provider}:*'


def key_llm_req_stat(provider: str, field: str) -> str:
    """field: 'ok' | 'err' | 'ms' | 'last_ok' | 'last_err'."""
    return f'llm:req:{provider}:{field}'


def key_llm_req_prefix(provider: str) -> str:
    return f'llm:req:{provider}'
