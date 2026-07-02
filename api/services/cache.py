"""Shared cache access — key registry + backend selection.

All cross-process/cross-worker state MUST go through get_redis_client() or the
cache_* helpers below (backed by CACHES['redis-cache'], real shared Redis).
Never use django.core.cache's bare `cache` (CACHES['default'] is LocMemCache —
process-local, not shared) for anything that needs to survive across workers
or task runs.
"""


import threading
import time


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


def redis_set_add(key: str, member: str, ttl: int | None = None) -> None:
    """Atomically add one member to a Redis SET — safe under concurrent callers
    (unlike a cache_get/mutate/cache_set read-modify-write on a serialized set).
    ttl=None leaves any existing expiry alone (matches SADD's own semantics)."""
    rc = get_redis_client(write=True)
    rc.sadd(key, member)
    if ttl is not None:
        rc.expire(key, ttl)


def redis_set_members(key: str) -> set[str]:
    rc = get_redis_client(write=False)
    return {m.decode() if isinstance(m, bytes) else m for m in rc.smembers(key)}


class Blocklist:
    """Redis-backed temporary block flag; falls back to an in-process dict when
    Redis is unavailable (e.g. local dev without a running Redis).

    Pure mechanism only — callers own key naming and TTL selection (mirrors
    cache_get/cache_set above). Used to skip a flaky/rate-limited/timing-out
    upstream (LLM provider, RSS source, ...) for a cooldown window instead of
    hammering it on every subsequent call.
    """

    def __init__(self) -> None:
        self._local: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        try:
            return bool(cache_get(key))
        except Exception:
            with self._lock:
                return self._local.get(key, 0.0) > time.monotonic()

    def block(self, key: str, ttl: int) -> None:
        try:
            cache_set(key, 1, timeout=ttl)
        except Exception:
            with self._lock:
                self._local[key] = time.monotonic() + ttl


# ── Key registry — one place, namespaced, so nothing collides ──────────────
# pipeline:*  — fetch/process/backfill state
# llm:*       — provider round-robin, debounce, call stats

KEY_ARTICLE_TITLE_DEDUP = 'pipeline:dedup:article_title'
KEY_BOOTSTRAP_INITIAL_DATA_DONE = 'pipeline:bootstrap:initial_data:done'


def key_backfill_checkpoint(start: str, end: str) -> str:
    """Redis SET of completed '{day_iso}:{chunk_idx}' members (SADD/SMEMBERS).
    v2: bumped from the pre-chunking scheme, which stored a single serialized
    Python set via cache_set — same cache key name, incompatible value type
    (SADD on a leftover string-typed key would raise WRONGTYPE). Old v1
    checkpoints never expired, so this avoids colliding with any still in Redis."""
    return f'pipeline:backfill:v2:{start}:{end}:done'


def key_backfill_source_block(source_code: str) -> str:
    return f'pipeline:backfill:blocked:{source_code}'


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
