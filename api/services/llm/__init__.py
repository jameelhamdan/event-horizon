import itertools
import logging
import re
import threading
import httpx
import requests
from openai import OpenAI
from django.conf import settings

logger = logging.getLogger(__name__)

# Strips <think>...</think> blocks emitted by reasoning models (e.g. qwen3)
_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL)

_NO_KEY = 'none'

# Per-request LLM timeout (seconds). Generous default (5m) so slow local models
# (Ollama) and busy free-tier providers don't get cut off mid-generation.
_LLM_TIMEOUT = float(getattr(settings, 'LLM_TIMEOUT_SECONDS', 300))


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(',') if v.strip()]


def strip_code_fences(text: str) -> str:
    """Strip markdown code fences that LLMs sometimes wrap around JSON responses."""
    text = re.sub(r'^```(?:json)?\s*', '', (text or '').strip())
    return re.sub(r'\s*```$', '', text)


def _parse_proxy_entries(
    proxy_csv: str, fallback_keys: list[str]
) -> list[tuple[str, str]]:
    """
    Parse 'url::key,url,url::key2' into resolved (proxy_url, api_key) pairs.

    Entries without an explicit '::key' suffix draw from fallback_keys in
    round-robin — "loosely tied" because the key pool and proxy list can have
    different lengths and are cycled independently before being zipped here.
    """
    raw: list[tuple[str, str | None]] = []
    for entry in _parse_csv(proxy_csv):
        if '::' in entry:
            url, key = entry.split('::', 1)
            raw.append((url.strip(), key.strip() or None))
        else:
            raw.append((entry.strip(), None))

    if not raw:
        return []

    fb_cycle = itertools.cycle(fallback_keys) if fallback_keys else None
    pairs: list[tuple[str, str]] = []
    for url, key in raw:
        if key:
            pairs.append((url, key))
        elif fb_cycle:
            pairs.append((url, next(fb_cycle)))
        else:
            pairs.append((url, _NO_KEY))
    return pairs


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
    Generic OpenAI-compatible chat client.

    Proxy resolution order (first configured wins):
      1. http_proxies — static (proxy_url, api_key) pairs from OPENROUTER_HTTP_PROXIES;
         keys are "loosely tied" (paired at init from the key pool, not 1:1 locked).
      2. Direct — base_url + key, both rotated round-robin.
    """

    def __init__(
        self,
        base_urls: list[str],
        api_keys: str | list[str],
        model: str,
        http_proxies: list[tuple[str, str]] | None = None,
    ) -> None:
        if not base_urls:
            raise LLMError('OpenAICompatLLMService requires at least one base_url')
        self._model = model

        self._url_cycle = itertools.cycle(base_urls)
        self._url_lock = threading.Lock()

        keys = _parse_csv(api_keys) if isinstance(api_keys, str) else list(api_keys)
        if not keys or keys == [_NO_KEY]:
            keys = [_NO_KEY]
        self._key_cycle = itertools.cycle(keys)
        self._key_lock = threading.Lock()

        # Static (proxy_url, api_key) pairs — rotated together.
        self._proxy_cycle: itertools.cycle | None = (
            itertools.cycle(http_proxies) if http_proxies else None
        )
        self._proxy_lock = threading.Lock()

    def _next_url(self) -> str:
        with self._url_lock:
            return next(self._url_cycle)

    def _next_key(self) -> str:
        with self._key_lock:
            return next(self._key_cycle)

    def _build_client(self, base_url: str) -> OpenAI:
        """Pick the next proxy + key and return a configured OpenAI client."""
        # 1. Static (proxy_url, api_key) pairs from OPENROUTER_HTTP_PROXIES.
        if self._proxy_cycle is not None:
            with self._proxy_lock:
                proxy_url, api_key = next(self._proxy_cycle)
            return OpenAI(
                base_url=base_url,
                api_key=api_key,
                http_client=httpx.Client(proxy=proxy_url),
            )

        # 2. Direct — no proxy.
        return OpenAI(base_url=base_url, api_key=self._next_key())

    def chat(self, messages: list[dict], **kwargs) -> str:
        kwargs.pop('think', None)
        client = self._build_client(self._next_url())
        try:
            completion = client.chat.completions.create(
                model=self._model,
                messages=messages,
                timeout=_LLM_TIMEOUT,
                **kwargs,
            )
            content = completion.choices[0].message.content
            if not content:
                raise LLMError('No content returned in completion response.')
            return content

        except (KeyError, IndexError, AttributeError, TypeError) as e:
            logger.error('Malformed response from LLM model %s: %s', self._model, str(e))
            raise LLMError('Malformed response from LLM provider') from e

        except Exception as e:
            logger.error('OpenAICompatLLMService error for model %s: %s', self._model, str(e))
            raise LLMError(f'OpenAICompatLLMService error: {e}') from e


class OllamaLLMService(BaseLLMService):
    """Ollama-backed LLM client. Strips <think>...</think> reasoning blocks."""

    def __init__(self, base_url: str, model: str) -> None:
        if not base_url:
            raise LLMError('OllamaLLMService requires a base_url (OLLAMA_BASE_URL)')
        self._base_url = base_url.rstrip('/')
        self._model = model

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
                timeout=_LLM_TIMEOUT,
            )
            response.raise_for_status()
            content = response.json()['message']['content']
            result = _THINK_RE.sub('', content).strip()
            # An empty body (e.g. a reasoning model that emits only <think>…</think>)
            # must be treated as a failure so the fallback chain continues to the
            # next provider instead of handing back '' that downstream JSON parsing
            # then chokes on.
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


def _provider_specs() -> dict[str, dict]:
    """
    Provider definitions. Available providers: openrouter, ollama, groq, cerebras.

    OpenRouter endpoint modes (configure one):
      - OPENROUTER_PROXY_URLS: comma-separated pre-authenticated base URLs, rotated round-robin.
      - OPENROUTER_API_KEYS: comma-separated keys against the direct openrouter.ai endpoint.

    Optional network-level HTTP proxies (OPENROUTER_HTTP_PROXIES):
      Static 'http://host:port::api_key,...' pairs; the '::api_key' suffix is optional —
      proxies without a key draw from OPENROUTER_API_KEYS in round-robin.
    """
    proxy_urls = _parse_csv(getattr(settings, 'OPENROUTER_PROXY_URLS', ''))
    raw_keys = _parse_csv(getattr(settings, 'OPENROUTER_API_KEYS', ''))
    model = (_parse_csv(settings.OPENROUTER_MODELS) or ['openrouter/free'])[0]

    http_proxy_csv = getattr(settings, 'OPENROUTER_HTTP_PROXIES', '')
    http_proxies = _parse_proxy_entries(http_proxy_csv, raw_keys) if http_proxy_csv else None

    if proxy_urls:
        openrouter_spec = {
            'base_urls': proxy_urls,
            'api_keys': _NO_KEY,
            'model': model,
            'http_proxies': http_proxies,
        }
    else:
        openrouter_spec = {
            'base_urls': ['https://openrouter.ai/api/v1'],
            'api_keys': settings.OPENROUTER_API_KEYS,
            'model': model,
            'http_proxies': http_proxies,
        }

    groq_keys = _parse_csv(getattr(settings, 'GROQ_API_KEYS', ''))
    cerebras_keys = _parse_csv(getattr(settings, 'CEREBRAS_API_KEYS', ''))

    specs: dict[str, dict] = {
        'openrouter': openrouter_spec,
        'ollama':        {'base_url': settings.OLLAMA_BASE_URL, 'model': settings.OLLAMA_MODEL_LARGE},
        'ollama_small':  {'base_url': settings.OLLAMA_BASE_URL, 'model': settings.OLLAMA_MODEL_SMALL},
        'ollama_medium': {'base_url': settings.OLLAMA_BASE_URL, 'model': settings.OLLAMA_MODEL_MEDIUM},
        'ollama_large':  {'base_url': settings.OLLAMA_BASE_URL, 'model': settings.OLLAMA_MODEL_LARGE},
    }
    # Always register these known providers so an absent API key is reported as
    # "not configured" (debug) rather than "Unknown LLM provider" (warning).
    specs['groq'] = {
        'base_urls': ['https://api.groq.com/openai/v1'],
        'api_keys': groq_keys,
        'model': settings.GROQ_MODEL,
    }
    specs['cerebras'] = {
        'base_urls': ['https://api.cerebras.ai/v1'],
        'api_keys': cerebras_keys,
        'model': settings.CEREBRAS_MODEL,
    }
    return specs


_backend_cache: dict[str, BaseLLMService] = {}
_backend_lock = threading.Lock()


def _build_backend(name: str, spec: dict) -> BaseLLMService:
    if 'base_url' in spec:
        return OllamaLLMService(spec['base_url'], spec['model'])
    return OpenAICompatLLMService(
        spec['base_urls'],
        spec['api_keys'],
        spec['model'],
        http_proxies=spec.get('http_proxies'),
    )


def _get_backend(name: str, specs: dict[str, dict]) -> BaseLLMService | None:
    spec = specs.get(name)
    if spec is None:
        logger.warning('Unknown LLM provider %r — skipping', name)
        return None
    configured = spec.get('base_urls') or spec.get('base_url')
    if not configured:
        logger.debug('LLM provider %r is not configured (no base_url) — skipping', name)
        return None
    # OpenAI-compat providers (groq/cerebras) need a key; an empty key list means
    # the provider just isn't enabled in this deployment — skip it quietly.
    if 'base_urls' in spec and not spec.get('api_keys'):
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
