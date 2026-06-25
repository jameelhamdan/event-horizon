import itertools
import logging
import re
import threading
import requests
from openai import OpenAI
from django.conf import settings

logger = logging.getLogger(__name__)

# Strips <think>...</think> blocks emitted by reasoning models (e.g. qwen3)
_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL)

_NO_KEY = 'none'


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(',') if v.strip()]


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

    Accepts one or more base_urls and one or more api_keys, rotating over each
    round-robin per request. When proxy_urls are provided (each pre-authenticated
    with an OpenRouter key), pass api_keys=_NO_KEY and rotate over the URLs instead.
    """

    def __init__(self, base_urls: list[str], api_keys: str, model: str) -> None:
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

    def _next_url(self) -> str:
        with self._url_lock:
            return next(self._url_cycle)

    def _next_key(self) -> str:
        with self._key_lock:
            return next(self._key_cycle)

    def chat(self, messages: list[dict], **kwargs) -> str:
        kwargs.pop('think', None)
        client = OpenAI(base_url=self._next_url(), api_key=self._next_key())
        try:
            completion = client.chat.completions.create(
                model=self._model,
                messages=messages,
                **kwargs,
            )
            content = completion.choices[0].message.content
            if not content:
                raise LLMError('No content returned in completion response.')
            return content

        except requests.HTTPError as e:
            logger.error(
                'LLM HTTP error (status %s) for model %s: %s',
                e.response.status_code, self._model, e.response.text,
            )
            raise LLMError(f'LLM request failed with status {e.response.status_code}') from e

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
                timeout=60,
            )
            response.raise_for_status()
            content = response.json()['message']['content']
            return _THINK_RE.sub('', content).strip()
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
    Provider definitions. Available providers: openrouter, ollama.

    OpenRouter supports two modes:
      - Proxy rotation: set OPENROUTER_PROXY_URLS to a comma-separated list of
        proxy base URLs (each pre-authenticated with one OpenRouter key). The client
        rotates over these URLs round-robin; no api_key is needed.
      - Direct: set OPENROUTER_API_KEYS (comma-separated); rotates keys against
        the standard openrouter.ai endpoint.
    """
    proxy_urls = _parse_csv(getattr(settings, 'OPENROUTER_PROXY_URLS', ''))
    model = (_parse_csv(settings.OPENROUTER_MODELS) or ['openrouter/free'])[0]
    if proxy_urls:
        openrouter_spec = {'base_urls': proxy_urls, 'api_keys': _NO_KEY, 'model': model}
    else:
        openrouter_spec = {
            'base_urls': ['https://openrouter.ai/api/v1'],
            'api_keys': settings.OPENROUTER_API_KEYS,
            'model': model,
        }

    return {
        'openrouter': openrouter_spec,
        'ollama': {
            'base_url': settings.OLLAMA_BASE_URL,
            'model': settings.OLLAMA_MODEL,
        },
    }


_backend_cache: dict[str, BaseLLMService] = {}
_backend_lock = threading.Lock()


def _build_backend(name: str, spec: dict) -> BaseLLMService:
    if 'base_url' in spec:
        return OllamaLLMService(spec['base_url'], spec['model'])
    return OpenAICompatLLMService(spec['base_urls'], spec['api_keys'], spec['model'])


def _get_backend(name: str, specs: dict[str, dict]) -> BaseLLMService | None:
    spec = specs.get(name)
    if spec is None:
        logger.warning('Unknown LLM provider %r — skipping', name)
        return None
    configured = spec.get('base_urls') or spec.get('base_url')
    if not configured:
        logger.warning('LLM provider %r is not configured (no base_url) — skipping', name)
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
