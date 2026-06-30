import hashlib
import logging
import re
import threading
import time as _time
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
                from django.core.cache import caches
                rc = caches['redis-cache'].client.get_client(write=True)
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

class _Debounce:
    """
    Redis-backed per-credential cooldown tracker; falls back to an in-process
    dict when Redis is unavailable (e.g. local dev without a running Redis).
    """

    TTLS: dict[str, int] = {
        'openrouter': 86400,   # daily free-tier quota per key
        'groq':           60,  # per-minute rate limit
        'cerebras':       60,  # 5 req/min quota
    }
    DEFAULT_TTL = 60

    def __init__(self) -> None:
        self._local: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _rkey(provider: str, credential: str) -> str:
        h = hashlib.md5(credential.encode(), usedforsecurity=False).hexdigest()[:12]
        return f'llm:debounce:{provider}:{h}'

    def is_active(self, provider: str, credential: str) -> bool:
        k = self._rkey(provider, credential)
        try:
            from django.core.cache import caches
            return bool(caches['redis-cache'].get(k))
        except Exception:
            with self._lock:
                return self._local.get(k, 0.0) > _time.monotonic()

    def mark(self, provider: str, credential: str, ttl: int | None = None) -> None:
        ttl = ttl or self.TTLS.get(provider, self.DEFAULT_TTL)
        k = self._rkey(provider, credential)
        tail = credential[-8:] if len(credential) > 8 else '***'
        logger.info('LLM 429 debounce: provider=%r ...%s cooling for %ds', provider, tail, ttl)
        try:
            from django.core.cache import caches
            caches['redis-cache'].set(k, 1, timeout=ttl)
        except Exception:
            with self._lock:
                self._local[k] = _time.monotonic() + ttl


_debounce = _Debounce()


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

    def __init__(
        self,
        base_url: str,
        api_keys: str | list[str],
        model: str | list[str],
        provider_name: str = 'unknown',
    ) -> None:
        if not base_url:
            raise LLMError('OpenAICompatLLMService requires a base_url')

        self._base_url = base_url
        self._provider = provider_name

        self._models = _Cycle(
            [model] if isinstance(model, str) else list(model),
            redis_key=f'llm:cycle:{provider_name}:models',
        )
        if not self._models:
            raise LLMError('OpenAICompatLLMService requires at least one model')

        keys = _parse_csv(api_keys) if isinstance(api_keys, str) else list(api_keys)
        if not keys:
            keys = [_NO_KEY]
        self._keys = _Cycle(keys, redis_key=f'llm:cycle:{provider_name}:keys')

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

    def chat(self, messages: list[dict], **kwargs) -> str:
        from openai import RateLimitError

        kwargs.pop('think', None)

        result = self._pick_key_and_models()
        if result is None:
            raise LLMError(
                f'All (key, model) combinations for {self._provider!r} are rate-limited (debounced)'
            )
        api_key, models = result
        client = OpenAI(base_url=self._base_url, api_key=api_key)

        last_error: Exception | None = None
        for model in models:
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
                return content
            except RateLimitError as e:
                _debounce.mark(self._provider, f'{api_key}:{model}', _retry_after_seconds(e))
                last_error = LLMError(f'429 rate-limited ({self._provider}/{model})')
                # continue — only this (key, model) pair is exhausted
            except (KeyError, IndexError, AttributeError, TypeError) as e:
                logger.warning('Malformed response from LLM model %s: %s', model, str(e)[:160])
                last_error = LLMError('Malformed response from LLM provider')
            except Exception as e:
                logger.warning('LLM model %s failed: %s', model, str(e)[:160])
                last_error = e

        raise LLMError(f'OpenAICompatLLMService error: {last_error}') from last_error


class OllamaLLMService(BaseLLMService):
    """Ollama-backed LLM client. Strips <think>...</think> reasoning blocks."""

    def __init__(self, base_url: str, model: str, timeout: float | None = None) -> None:
        if not base_url:
            raise LLMError('OllamaLLMService requires a base_url (OLLAMA_BASE_URL)')
        self._base_url = base_url.rstrip('/')
        self._model = model
        self._timeout = float(timeout) if timeout else _OLLAMA_TIMEOUT

    def chat(self, messages: list[dict], **kwargs) -> str:
        options = {}
        if 'temperature' in kwargs:
            options['temperature'] = kwargs['temperature']
        if kwargs.get('max_tokens') is not None:
            options['num_predict'] = kwargs['max_tokens']
        try:
            response = requests.post(
                f'{self._base_url}/api/chat',
                json={
                    'model': self._model,
                    'messages': messages,
                    'stream': False,
                    'think': kwargs.get('think', False),
                    **(({'options': options}) if options else {}),
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            content = response.json()['message']['content']
            result = _THINK_RE.sub('', content).strip()
            if not result:
                raise LLMError(f'Ollama model {self._model} returned empty content')
            return result
        except LLMError:
            raise
        except requests.HTTPError as e:
            logger.error('Ollama %s: %s', e.response.status_code, e.response.text[:200])
            raise LLMError(f'Ollama request failed ({e.response.status_code})') from e
        except Exception as e:
            logger.error('OllamaLLMService error: %s', e)
            raise LLMError(str(e)) from e


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

    def chat(self, messages: list[dict], **kwargs) -> str:
        last_error: Exception | None = None
        for name, backend in zip(self._names, self._backends):
            try:
                return backend.chat(messages, **kwargs)
            except LLMError as e:
                last_error = e
                logger.warning('LLM provider %r failed, trying next: %s', name, e)
        raise LLMError(f'All LLM providers failed ({", ".join(self._names)})') from last_error


# ── Provider registry ─────────────────────────────────────────────────────────

def _provider_specs() -> dict[str, dict]:
    """Provider definitions. Available providers: openrouter, ollama, groq, cerebras."""
    from services.llm import discovery

    # Per-tier Ollama timeouts. Tunable via settings.OLLAMA_TIMEOUTS.
    ot = getattr(settings, 'OLLAMA_TIMEOUTS', {}) or {}
    ollama = settings.OLLAMA_BASE_URL
    return {
        'openrouter': {
            'base_url': 'https://openrouter.ai/api/v1',
            'api_keys': _parse_csv(getattr(settings, 'OPENROUTER_API_KEYS', '')),
            # Dynamic free-model list refreshed daily; falls back to OPENROUTER_MODELS.
            'model': discovery.get_models(),
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
    }


_backend_cache: dict[str, BaseLLMService] = {}
_backend_lock = threading.Lock()


def _build_backend(name: str, spec: dict) -> BaseLLMService:
    if name.startswith('ollama'):
        return OllamaLLMService(spec['base_url'], spec['model'], timeout=spec.get('timeout'))
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
    names = [route] if isinstance(route, str) else list(route)

    specs = _provider_specs()
    backends: list[BaseLLMService] = []
    resolved: list[str] = []
    for name in names:
        backend = _get_backend(name, specs)
        if backend is not None:
            backends.append(backend)
            resolved.append(name)

    if not backends:
        raise LLMError(
            f'No configured LLM provider for role {role!r} (tried: {", ".join(names)})'
        )
    if len(backends) == 1:
        return backends[0]
    return FallbackLLMService(backends, resolved)
