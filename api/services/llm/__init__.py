import hashlib
import itertools
import logging
import random
import re
import threading
import time as _time
from typing import Callable
import requests
from openai import OpenAI
from django.conf import settings

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL)
_NO_KEY = 'none'
_LLM_TIMEOUT = float(getattr(settings, 'LLM_TIMEOUT_SECONDS', 300))
_OLLAMA_TIMEOUT = float(getattr(settings, 'OLLAMA_TIMEOUT_SECONDS', 60))


# ── Cross-worker rotating list ────────────────────────────────────────────────

class _Cycle:
    """
    Rotating list backed by a Redis atomic counter so rotation is coordinated
    across all workers. Falls back to an in-process counter when Redis is
    unavailable (local dev without a running Redis).
    """

    def __init__(self, items: list, *, redis_key: str | None = None) -> None:
        self._items = list(items)
        self._redis_key = redis_key
        self._local_idx = 0
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def _incr(self) -> int:
        """
        Atomically increment the shared counter and return the value before
        the increment. Uses Redis INCR (O(1), creates the key at 0 on first
        call); falls back to an in-process counter on any Redis error.
        """
        if self._redis_key and len(self._items) > 1:
            try:
                from services.cache import get_redis_client
                rc = get_redis_client(write=True)
                return int(rc.incr(self._redis_key)) - 1
            except Exception:
                pass
        with self._lock:
            val = self._local_idx
            self._local_idx += 1
            return val

    def next(self):
        """Next item in global round-robin order."""
        return self._items[self._incr() % len(self._items)]

    def rotation(self) -> list:
        """All items starting at the current global position."""
        start = self._incr() % len(self._items)
        return self._items[start:] + self._items[:start]



# ── 429 debounce ─────────────────────────────────────────────────────────────
# Policy (credential hashing, per-provider TTLs, logging) lives here; the actual
# Redis-backed-with-local-fallback flag is the shared services.cache.Blocklist
# mechanism (also used by services.data.historical's source timeout blocklist).

class _Debounce:
    """Per-credential 429 cooldown tracker, keyed by (provider, credential)."""

    TTLS: dict[str, int] = {
        'openrouter': 86400,   # daily free-tier quota per key
        'groq':           60,  # per-minute rate limit
        'cerebras':       60,  # 5 req/min quota
        'mistral':        60,  # 2 req/min per key
    }
    DEFAULT_TTL = 60

    def __init__(self) -> None:
        from services.cache import Blocklist
        self._store = Blocklist()

    @staticmethod
    def _rkey(provider: str, credential: str) -> str:
        from services.cache import key_llm_debounce
        h = hashlib.md5(credential.encode(), usedforsecurity=False).hexdigest()[:12]
        return key_llm_debounce(provider, h)

    def is_active(self, provider: str, credential: str) -> bool:
        return self._store.is_blocked(self._rkey(provider, credential))

    def mark(self, provider: str, credential: str, ttl: int | None = None) -> None:
        ttl = ttl or self.TTLS.get(provider, self.DEFAULT_TTL)
        tail = credential[-8:] if len(credential) > 8 else '***'
        logger.info('LLM 429 debounce: provider=%r ...%s cooling for %ds', provider, tail, ttl)
        self._store.block(self._rkey(provider, credential), ttl)


_debounce = _Debounce()


# ── Per-provider call stats (Redis counters) ──────────────────────────────────

# All req-stat keys expire this long after the provider's last call, so the ok/err
# counters (and the last_ok/last_err timestamps) reflect only *recent* activity — a
# provider that erupted in errors days ago but has been quiet since shows nothing,
# and any non-zero err count on the dashboard is therefore known to be recent.
_STAT_TTL = 24 * 3600  # 24h


def _record_llm_call(provider: str, success: bool, latency_ms: int) -> None:
    """Increment lightweight Redis counters for the admin dashboard.

    Every touched key gets its TTL refreshed to _STAT_TTL, so the whole per-provider
    stats block is a rolling 24h window rather than an all-time tally.
    """
    try:
        from services.cache import get_redis_client, key_llm_req_stat
        rc = get_redis_client(write=True)
        pipe = rc.pipeline(transaction=False)
        key = 'ok' if success else 'err'
        count_key = key_llm_req_stat(provider, key)
        ms_key = key_llm_req_stat(provider, 'ms')
        # incr/incrbyfloat create the key without a TTL, so re-arm expiry each call.
        pipe.incr(count_key)
        pipe.expire(count_key, _STAT_TTL)
        pipe.incrbyfloat(ms_key, latency_ms)
        pipe.expire(ms_key, _STAT_TTL)
        pipe.set(key_llm_req_stat(provider, f'last_{key}'), int(_time.time()), ex=_STAT_TTL)
        pipe.execute()
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(',') if v.strip()]


def strip_code_fences(text: str) -> str:
    """Strip markdown code fences that LLMs sometimes wrap around JSON responses."""
    text = re.sub(r'^```(?:json)?\s*', '', (text or '').strip())
    return re.sub(r'\s*```$', '', text)


def _retry_after_seconds(exc: Exception) -> int | None:
    """Extract Retry-After seconds from a 429 response if the provider sent one."""
    try:
        headers = exc.response.headers  # type: ignore[attr-defined]
        for header in ('retry-after', 'x-ratelimit-reset-requests', 'x-ratelimit-reset'):
            val = headers.get(header)
            if val:
                return max(1, int(float(val)))
    except Exception:
        pass
    return None


# ── LLM backends ─────────────────────────────────────────────────────────────

class LLMError(Exception):
    pass


class BaseLLMService:
    """Shared interface — every backend exposes chat() and complete()."""

    def chat(self, messages: list[dict], **kwargs) -> str:
        content, _ = self.chat_with_usage(messages, **kwargs)
        return content

    def chat_with_usage(self, messages: list[dict], **kwargs) -> tuple[str, dict]:
        """Like chat() but also returns a usage dict: {provider, model, prompt_tokens,
        completion_tokens, total_tokens}. Empty dict when unavailable."""
        raise NotImplementedError

    def complete(self, prompt: str, system: str | None = None, **kwargs) -> str:
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        return self.chat(messages, **kwargs)


class OpenAICompatLLMService(BaseLLMService):
    """
    Generic OpenAI-compatible chat client with cross-worker round-robin key
    and model rotation (both via Redis) and per-(key, model) 429 debouncing.

    Debounce granularity is (provider, key, model): a 429 on one model does
    not block other models on the same key. A key is skipped only when every
    model on it is debounced. When all (key, model) pairs are exhausted,
    LLMError is raised so FallbackLLMService falls through to the next provider.
    TTLs: OpenRouter 24 h (daily quota), Groq/Cerebras 60 s (per-minute limit);
    Retry-After header overrides when present.
    """

    # Hard cap on models tried per call, regardless of how many are available
    # for the picked key (OpenRouter can surface up to OPENROUTER_MODELS_COUNT).
    # Without this, a single call's worst case is len(models) × LLM_TIMEOUT_SECONDS
    # — a provider that hangs (rather than fails fast) on every model can burn
    # minutes on one FallbackLLMService leg before ever reaching the next provider.
    MAX_MODELS_PER_CALL = 2

    def __init__(
        self,
        base_url: str,
        api_keys: str | list[str],
        model: str | list[str] | Callable[[], list[str]],
        provider_name: str = 'unknown',
    ) -> None:
        if not base_url:
            raise LLMError('OpenAICompatLLMService requires a base_url')

        from services.cache import key_llm_cycle

        self._base_url = base_url
        self._provider = provider_name

        # ``model`` may be a callable (OpenRouter passes ``discovery.get_models``)
        # so the daily-discovered free-model list is read live on each call rather
        # than snapshotted at construction — a backend cached in _backend_cache
        # would otherwise serve a frozen list until the worker process recycled,
        # nullifying the daily refresh. Static providers pass a str/list and the
        # callable path is a no-op.
        self._model_fn = model if callable(model) else None
        initial = list(model()) if self._model_fn else ([model] if isinstance(model, str) else list(model))
        self._models = _Cycle(initial, redis_key=key_llm_cycle(provider_name, 'models'))
        if not self._models and self._model_fn is None:
            raise LLMError('OpenAICompatLLMService requires at least one model')

        keys = _parse_csv(api_keys) if isinstance(api_keys, str) else list(api_keys)
        if not keys:
            keys = [_NO_KEY]
        self._keys = _Cycle(keys, redis_key=key_llm_cycle(provider_name, 'keys'))

    def _refresh_models(self) -> None:
        """Re-read a dynamically-sourced model list (OpenRouter's daily-discovered
        picks in Redis). No-op for static providers. The _Cycle's Redis rotation
        counter is keyed by provider, so swapping ``_items`` preserves cross-worker
        round-robin position (the modulo tolerates a length change)."""
        if self._model_fn is None:
            return
        try:
            latest = list(self._model_fn() or [])
        except Exception:
            return
        if latest and latest != self._models._items:
            self._models._items = latest

    @property
    def _model(self) -> str:
        return ', '.join(self._models._items)

    def _pick_key_and_models(self) -> tuple[str, list[str]] | None:
        """
        Return (api_key, available_models) where available_models is the
        rotation-ordered list of models not yet debounced for that key.
        Keys are tried in global round-robin order; the first key with at
        least one live model wins. Returns None when all (key, model) pairs
        are currently debounced.
        """
        models = self._models.rotation()  # global round-robin order, advance once
        n = len(self._keys._items)
        start = self._keys._incr() % n
        for i in range(n):
            key = self._keys._items[(start + i) % n]
            available = [
                m for m in models
                if not _debounce.is_active(self._provider, f'{key}:{m}')
            ]
            if available:
                return key, available
        return None

    def chat_with_usage(self, messages: list[dict], **kwargs) -> tuple[str, dict]:
        from openai import RateLimitError

        kwargs.pop('think', None)

        self._refresh_models()
        if not self._models:
            raise LLMError(f'No models available for provider {self._provider!r}')
        result = self._pick_key_and_models()
        if result is None:
            raise LLMError(
                f'All (key, model) combinations for {self._provider!r} are rate-limited (debounced)'
            )
        api_key, models = result
        models = models[: self.MAX_MODELS_PER_CALL]
        client = OpenAI(base_url=self._base_url, api_key=api_key)

        last_error: Exception | None = None
        for model in models:
            t0 = _time.monotonic()
            try:
                completion = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    timeout=_LLM_TIMEOUT,
                    **kwargs,
                )
                content = completion.choices[0].message.content
                if not content:
                    raise LLMError('No content returned in completion response.')
                latency = int((_time.monotonic() - t0) * 1000)
                _record_llm_call(self._provider, True, latency)
                usage: dict = {}
                if completion.usage:
                    usage = {
                        'provider': self._provider,
                        'model': completion.model or model,
                        'prompt_tokens': completion.usage.prompt_tokens or 0,
                        'completion_tokens': completion.usage.completion_tokens or 0,
                        'total_tokens': completion.usage.total_tokens or 0,
                    }
                return content, usage
            except RateLimitError as e:
                _debounce.mark(self._provider, f'{api_key}:{model}', _retry_after_seconds(e))
                _record_llm_call(self._provider, False, int((_time.monotonic() - t0) * 1000))
                last_error = LLMError(f'429 rate-limited ({self._provider}/{model})')
                # continue — only this (key, model) pair is exhausted
            except (KeyError, IndexError, AttributeError, TypeError) as e:
                _record_llm_call(self._provider, False, int((_time.monotonic() - t0) * 1000))
                logger.warning('Malformed response from LLM model %s: %s', model, str(e)[:160])
                last_error = LLMError('Malformed response from LLM provider')
            except Exception as e:
                _record_llm_call(self._provider, False, int((_time.monotonic() - t0) * 1000))
                logger.warning('LLM model %s failed: %s', model, str(e)[:160])
                last_error = e

        raise LLMError(f'OpenAICompatLLMService error: {last_error}') from last_error


# ── Ollama concurrency guard ──────────────────────────────────────────────────

class _OllamaSlots:
    """Cross-worker cap on concurrent Ollama requests.

    Ollama here is CPU-only (OLLAMA_NUM_PARALLEL=1) and serves one generation at
    a time, but worker-heavy's prefork processes can each fall back to it at
    once. This bounds concurrency to ``size`` slots shared across all workers via
    Redis (one key per slot, ``SET NX EX`` — self-healing: a crashed holder's
    slot frees when its TTL lapses). ``acquire()`` waits up to ``acquire_timeout``
    for a free slot, then returns None so the caller can *fast-reject* rather than
    queue behind Ollama's own internal queue and burn the full read timeout.

    Falls back to a per-process ``threading.Semaphore`` when Redis is unavailable
    (local dev) — imperfect across processes, but the prod path always has Redis.
    """

    def __init__(self, size: int) -> None:
        self._size = max(1, size)
        self._sem = threading.Semaphore(self._size)

    @staticmethod
    def _slot_key(i: int) -> str:
        return f'llm:ollama:slot:{i}'

    def _redis(self):
        try:
            from services.cache import get_redis_client
            return get_redis_client(write=True)
        except Exception:
            return None

    def acquire(self, acquire_timeout: float, slot_ttl: int) -> object | None:
        """Return a release token (slot index or the local semaphore), or None if
        no slot became free within ``acquire_timeout``."""
        rc = self._redis()
        deadline = _time.monotonic() + max(0.0, acquire_timeout)
        if rc is not None:
            token = f'{_time.time()}'
            while True:
                for i in range(self._size):
                    try:
                        if rc.set(self._slot_key(i), token, nx=True, ex=slot_ttl):
                            return i
                    except Exception:
                        return self._acquire_local(acquire_timeout)
                if _time.monotonic() >= deadline:
                    return None
                _time.sleep(0.1)
        return self._acquire_local(acquire_timeout)

    def _acquire_local(self, acquire_timeout: float):
        return self._sem if self._sem.acquire(timeout=max(0.0, acquire_timeout)) else None

    def release(self, token: object) -> None:
        if token is None:
            return
        if token is self._sem:
            self._sem.release()
            return
        rc = self._redis()
        if rc is not None:
            try:
                rc.delete(self._slot_key(int(token)))
            except Exception:
                pass


_ollama_slots: _OllamaSlots | None = None


def _get_ollama_slots() -> _OllamaSlots:
    global _ollama_slots
    if _ollama_slots is None:
        with _backend_lock:
            if _ollama_slots is None:
                size = int(getattr(settings, 'OLLAMA_MAX_CONCURRENCY', 1))
                _ollama_slots = _OllamaSlots(size)
    return _ollama_slots


class OllamaLLMService(BaseLLMService):
    """Ollama-backed LLM client. Strips <think>...</think> reasoning blocks."""

    def __init__(
        self, base_url: str, model: str, timeout: float | None = None,
        provider_name: str = 'ollama',
    ) -> None:
        if not base_url:
            raise LLMError('OllamaLLMService requires a base_url (OLLAMA_BASE_URL)')
        self._base_url = base_url.rstrip('/')
        self._model = model
        self._timeout = float(timeout) if timeout else _OLLAMA_TIMEOUT
        # Route/tier name (ollama_small|medium|large), NOT a bare 'ollama' — so
        # per-tier call stats + token usage land under the same provider key the
        # admin dashboard reads (it enumerates providers from LLM_ROUTES, which
        # only ever names the tiers). Recording under 'ollama' orphaned the stats.
        self._provider = provider_name

    def chat_with_usage(self, messages: list[dict], **kwargs) -> tuple[str, dict]:
        options = {}
        if 'temperature' in kwargs:
            options['temperature'] = kwargs['temperature']
        if kwargs.get('max_tokens') is not None:
            options['num_predict'] = kwargs['max_tokens']
        t0 = _time.monotonic()
        # Bound concurrent Ollama requests across all workers. Wait briefly for a
        # free slot; if none frees, fail fast (LLMError) instead of blocking on
        # Ollama's internal queue for the full read timeout — Ollama is the last
        # tier in every route, so a fast reject just skips it for this call.
        slots = _get_ollama_slots()
        token = slots.acquire(
            float(getattr(settings, 'OLLAMA_ACQUIRE_SECONDS', 2.0)),
            slot_ttl=int(self._timeout) + 10,
        )
        if token is None:
            _record_llm_call(self._provider, False, int((_time.monotonic() - t0) * 1000))
            raise LLMError(f'Ollama busy ({self._provider}): no free slot')
        try:
            response = requests.post(
                f'{self._base_url}/api/chat',
                json={
                    'model': self._model,
                    'messages': messages,
                    'stream': False,
                    'think': kwargs.get('think', False),
                    # Without this Ollama defaults to keeping the model resident
                    # for 5 minutes after the last call. LLM_ROUTES has three
                    # tiers (ollama_small/medium/large); if a cloud-provider
                    # outage pushes several roles onto Ollama at once, all three
                    # multi-GB models can end up loaded simultaneously within
                    # that window. A short keep_alive bounds how long an idle
                    # tier stays resident — it does NOT bound concurrent peak
                    # (that needs OLLAMA_MAX_LOADED_MODELS set on the Ollama
                    # server itself; this app has no server-side control over it).
                    'keep_alive': '30s',
                    **(({'options': options}) if options else {}),
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
            content = body['message']['content']
            result = _THINK_RE.sub('', content).strip()
            if not result:
                raise LLMError(f'Ollama model {self._model} returned empty content')
            latency = int((_time.monotonic() - t0) * 1000)
            _record_llm_call(self._provider, True, latency)
            prompt_tokens = body.get('prompt_eval_count') or 0
            completion_tokens = body.get('eval_count') or 0
            usage = {
                'provider': self._provider,
                'model': body.get('model') or self._model,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'total_tokens': prompt_tokens + completion_tokens,
            }
            return result, usage
        except LLMError:
            _record_llm_call(self._provider, False, int((_time.monotonic() - t0) * 1000))
            raise
        except requests.HTTPError as e:
            _record_llm_call(self._provider, False, int((_time.monotonic() - t0) * 1000))
            logger.error('Ollama %s: %s', e.response.status_code, e.response.text[:200])
            raise LLMError(f'Ollama request failed ({e.response.status_code})') from e
        except Exception as e:
            _record_llm_call(self._provider, False, int((_time.monotonic() - t0) * 1000))
            logger.error('OllamaLLMService error: %s', e)
            raise LLMError(str(e)) from e
        finally:
            slots.release(token)


class FallbackLLMService(BaseLLMService):
    """Tries each backend in order, raising LLMError only if all fail."""

    def __init__(self, backends: list[BaseLLMService], names: list[str]) -> None:
        if not backends:
            raise LLMError('FallbackLLMService requires at least one backend')
        self._backends = backends
        self._names = names

    @property
    def _model(self) -> str:
        return ' -> '.join(self._names)

    def chat_with_usage(self, messages: list[dict], **kwargs) -> tuple[str, dict]:
        last_error: Exception | None = None
        for i, (name, backend) in enumerate(zip(self._names, self._backends)):
            try:
                result = backend.chat_with_usage(messages, **kwargs)
            except LLMError as e:
                last_error = e
                logger.warning('LLM provider %r failed, trying next: %s', name, e)
                continue
            if i > 0:
                # Only log when a fallback actually kicked in — the common
                # case (first provider succeeds) doesn't need a log line.
                logger.info('LLM provider %r succeeded after %d fallback(s)', name, i)
            return result
        raise LLMError(f'All LLM providers failed ({", ".join(self._names)})') from last_error


class BalancedLLMService(BaseLLMService):
    """Spreads calls across a group of interchangeable providers so they run
    concurrently, instead of hammering one and only failing over on error.

    Each call starts at a rotating offset (round-robin) and, on failure, walks
    the rest of the group — so concurrent workers land on *different* providers
    while any single call still has in-group failover. Raises LLMError only if
    every provider in the group fails (the outer FallbackLLMService then moves
    on to the next leg, e.g. openrouter → ollama).

    Unlike FallbackLLMService (strict priority order), this exists to multiply
    throughput: with groq+mistral+cerebras balanced, N concurrent analyzer
    chunks fan out across all three rather than serializing on groq's rate limit.
    The per-process counter is seeded randomly so prefork workers don't all
    start on the same provider.
    """

    def __init__(self, backends: list[BaseLLMService], names: list[str]) -> None:
        if not backends:
            raise LLMError('BalancedLLMService requires at least one backend')
        self._backends = backends
        self._names = names
        self._counter = itertools.count(random.randrange(len(backends)))

    @property
    def _model(self) -> str:
        return ' | '.join(self._names)

    def chat_with_usage(self, messages: list[dict], **kwargs) -> tuple[str, dict]:
        n = len(self._backends)
        start = next(self._counter) % n  # next() is atomic under the GIL
        last_error: Exception | None = None
        for k in range(n):
            i = (start + k) % n
            try:
                result = self._backends[i].chat_with_usage(messages, **kwargs)
            except LLMError as e:
                last_error = e
                logger.warning('LLM provider %r failed, trying next: %s', self._names[i], e)
                continue
            if k > 0:
                logger.info('LLM provider %r succeeded after %d in-group fallback(s)', self._names[i], k)
            return result
        raise LLMError(f'All balanced providers failed ({", ".join(self._names)})') from last_error


# ── Provider registry ─────────────────────────────────────────────────────────

_specs_cache: dict[str, dict] | None = None


def _provider_specs() -> dict[str, dict]:
    """Provider definitions, memoized per process. Available providers: openrouter,
    ollama, groq, cerebras.

    Derived entirely from ``settings`` (static within a process — the same
    assumption ``_backend_cache`` already bakes in), so it is built once and
    reused: callers previously rebuilt this whole dict, re-parsing every API-key
    CSV, on every ``get_llm_service()`` call. The one dynamic value —
    OpenRouter's discovered model list — is passed as the ``discovery.get_models``
    callable (read live per LLM call), so caching the dict doesn't freeze it.
    """
    global _specs_cache
    if _specs_cache is not None:
        return _specs_cache
    with _backend_lock:
        if _specs_cache is None:
            _specs_cache = _build_provider_specs()
        return _specs_cache


def _build_provider_specs() -> dict[str, dict]:
    from services.llm import discovery

    # Per-tier Ollama timeouts. Tunable via settings.OLLAMA_TIMEOUTS.
    ot = getattr(settings, 'OLLAMA_TIMEOUTS', {}) or {}
    ollama = settings.OLLAMA_BASE_URL
    return {
        'openrouter': {
            'base_url': 'https://openrouter.ai/api/v1',
            'api_keys': _parse_csv(getattr(settings, 'OPENROUTER_API_KEYS', '')),
            # Pass the accessor (not its result) so the cached backend re-reads the
            # daily-discovered list live per call; falls back to OPENROUTER_MODELS.
            # This also drops a per-get_llm_service() Redis GET for every other role,
            # whose backend never consumed this value anyway.
            'model': discovery.get_models,
        },
        'ollama':        {'base_url': ollama, 'model': settings.OLLAMA_MODEL_LARGE,  'timeout': ot.get('large')},
        'ollama_small':  {'base_url': ollama, 'model': settings.OLLAMA_MODEL_SMALL,  'timeout': ot.get('small')},
        'ollama_medium': {'base_url': ollama, 'model': settings.OLLAMA_MODEL_MEDIUM, 'timeout': ot.get('medium')},
        'ollama_large':  {'base_url': ollama, 'model': settings.OLLAMA_MODEL_LARGE,  'timeout': ot.get('large')},
        'groq': {
            'base_url': 'https://api.groq.com/openai/v1',
            'api_keys': _parse_csv(getattr(settings, 'GROQ_API_KEYS', '')),
            'model': settings.GROQ_MODEL,
        },
        'cerebras': {
            'base_url': 'https://api.cerebras.ai/v1',
            'api_keys': _parse_csv(getattr(settings, 'CEREBRAS_API_KEYS', '')),
            'model': settings.CEREBRAS_MODEL,
        },
        'mistral': {
            'base_url': 'https://api.mistral.ai/v1',
            'api_keys': _parse_csv(getattr(settings, 'MISTRAL_API_KEYS', '')),
            'model': settings.MISTRAL_MODEL,
        },
    }


_backend_cache: dict[str, BaseLLMService] = {}
_backend_lock = threading.Lock()


def _build_backend(name: str, spec: dict) -> BaseLLMService:
    if name.startswith('ollama'):
        return OllamaLLMService(
            spec['base_url'], spec['model'], timeout=spec.get('timeout'), provider_name=name,
        )
    return OpenAICompatLLMService(
        spec['base_url'],
        spec['api_keys'],
        spec['model'],
        provider_name=name,
    )


def _get_backend(name: str, specs: dict[str, dict]) -> BaseLLMService | None:
    spec = specs.get(name)
    if spec is None:
        logger.warning('Unknown LLM provider %r — skipping', name)
        return None
    if not spec.get('base_url'):
        logger.debug('LLM provider %r is not configured (no base_url) — skipping', name)
        return None
    if not name.startswith('ollama') and not spec.get('api_keys'):
        logger.debug('LLM provider %r is not configured (no API key) — skipping', name)
        return None
    with _backend_lock:
        if name not in _backend_cache:
            _backend_cache[name] = _build_backend(name, spec)
        return _backend_cache[name]


def get_llm_service(role: str = 'default') -> BaseLLMService:
    """
    Return the LLM backend for the given role, per settings.LLM_ROUTES.

    A route is a provider name or an ordered fallback list. Providers that are
    not configured are skipped; if the chain is empty, raises LLMError.
    """
    routes = settings.LLM_ROUTES
    route = routes.get(role) or routes.get('default')
    if route is None:
        raise LLMError(f'No LLM route for role {role!r} and no default route configured')
    # A route is a provider name, or a list whose elements are either a provider
    # name (strict-priority leg) or a set of names (a balanced group — spread
    # across concurrently, in-group failover). e.g.
    #   [{'groq', 'cerebras', 'mistral'}, 'openrouter', 'ollama_medium']
    elements = [route] if isinstance(route, str) else list(route)

    specs = _provider_specs()
    legs: list[BaseLLMService] = []      # one per route element (may itself be balanced)
    leg_names: list[str] = []            # aligned with legs, for FallbackLLMService
    tried: list[str] = []
    for element in elements:
        if isinstance(element, (set, frozenset)):
            # Balanced group: sort for stable identity; skip unconfigured members.
            group_backends, group_names = [], []
            for name in sorted(element):
                tried.append(name)
                backend = _get_backend(name, specs)
                if backend is not None:
                    group_backends.append(backend)
                    group_names.append(name)
            if not group_backends:
                continue
            if len(group_backends) == 1:
                legs.append(group_backends[0])
                leg_names.append(group_names[0])
            else:
                legs.append(BalancedLLMService(group_backends, group_names))
                leg_names.append('{' + '|'.join(group_names) + '}')
        else:
            tried.append(element)
            backend = _get_backend(element, specs)
            if backend is not None:
                legs.append(backend)
                leg_names.append(element)

    if not legs:
        raise LLMError(
            f'No configured LLM provider for role {role!r} (tried: {", ".join(tried)})'
        )
    if len(legs) == 1:
        return legs[0]
    return FallbackLLMService(legs, leg_names)


def resolved_provider_names(service: BaseLLMService) -> list[str]:
    """Ordered provider names a resolved service will actually try, in order.

    Callers that need to know which provider is *actually* primary for a route
    (e.g. to size batch requests) should use names[0] — LLM_ROUTES lists a
    static fallback order, but get_llm_service() skips unconfigured providers,
    so the effective primary can differ from route[0] (e.g. a deployment with
    only OLLAMA_BASE_URL set resolves straight to Ollama).
    """
    if isinstance(service, BalancedLLMService):
        return list(service._names)
    if isinstance(service, FallbackLLMService):
        # Flatten each leg — a leg may itself be a balanced group — so callers
        # sizing off names[0] see a real provider, not the '{a|b}' group label.
        flat: list[str] = []
        for backend in service._backends:
            flat.extend(resolved_provider_names(backend))
        return flat
    return [getattr(service, '_provider', 'unknown')]
