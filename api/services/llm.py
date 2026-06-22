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

# Sentinel for keyless OpenAI-compatible providers (e.g. g4f).
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
        """Single-turn convenience wrapper around chat()."""
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        return self.chat(messages, **kwargs)


class OpenAICompatLLMService(BaseLLMService):
    """
    Generic OpenAI-compatible chat client — serves OpenRouter, g4f,
    and any other OpenAI-compatible endpoint.

    api_keys: comma-separated keys rotated round-robin per request. Pass 'none'
    (the _NO_KEY sentinel) for keyless providers — a dummy key is sent.
    """

    def __init__(self, base_url: str, api_keys: str, model: str) -> None:
        if not base_url:
            raise LLMError('OpenAICompatLLMService requires a base_url')
        self._base_url = base_url
        self._model = model

        keys = _parse_csv(api_keys)
        if keys == [_NO_KEY] or not keys:
            keys = [_NO_KEY]
        self._keys = keys
        self._key_cycle = itertools.cycle(keys)
        self._key_lock = threading.Lock()

    def _next_key(self) -> str:
        with self._key_lock:
            return next(self._key_cycle)

    def chat(self, messages: list[dict], **kwargs) -> str:
        """
        Send a chat completion request.
        messages: list of {'role': 'system'|'user'|'assistant', 'content': str}
        Raises LLMError on failure.
        """
        # 'think' is an Ollama-only kwarg — drop it so it isn't forwarded upstream.
        kwargs.pop('think', None)
        client = OpenAI(base_url=self._base_url, api_key=self._next_key())
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
                'LLM HTTP error (status %s) for model %s @ %s: %s',
                e.response.status_code, self._model, self._base_url, e.response.text,
            )
            raise LLMError(f'LLM request failed with status {e.response.status_code}') from e

        except (KeyError, IndexError, AttributeError, TypeError) as e:
            logger.error(
                'Malformed response from %s for model %s: %s',
                self._base_url, self._model, str(e),
            )
            raise LLMError('Malformed response from LLM provider') from e

        except Exception as e:
            logger.error(
                'OpenAICompatLLMService error for model %s @ %s: %s',
                self._model, self._base_url, str(e),
            )
            raise LLMError(f'OpenAICompatLLMService error: {e}') from e


class OllamaLLMService(BaseLLMService):
    """
    Ollama-backed LLM client for a self-hosted Ollama server.

    Strips <think>...</think> reasoning blocks before returning content,
    so the caller always receives clean text / JSON.
    """

    def __init__(self, base_url: str, model: str) -> None:
        if not base_url:
            raise LLMError('OllamaLLMService requires a base_url (OLLAMA_BASE_URL)')
        self._base_url = base_url.rstrip('/')
        self._model = model

    def chat(self, messages: list[dict], **kwargs) -> str:
        """
        Send a chat request to Ollama.
        kwargs may include temperature (float) and think (bool).

        think defaults to False: for hybrid reasoning models (e.g. qwen3) this
        disables the <think>...</think> reasoning pass, which is pure wasted
        latency for our structured analysis tasks (classification, translation,
        topic matching). Pass think=True to opt back in.
        Raises LLMError on failure.
        """
        options = {}
        if 'temperature' in kwargs:
            options['temperature'] = kwargs['temperature']
        # OpenAI-style max_tokens maps to Ollama's num_predict output cap.
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
    """
    Wraps an ordered list of backends. chat()/complete() try each in order,
    catching LLMError, until one succeeds. Raises LLMError if all fail.
    """

    def __init__(self, backends: list[BaseLLMService], names: list[str]) -> None:
        if not backends:
            raise LLMError('FallbackLLMService requires at least one backend')
        self._backends = backends
        self._names = names

    @property
    def _model(self) -> str:
        # For diagnostics (test_llm prints svc._model).
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
    Provider definitions. Base URLs live here (code); secrets/models come from settings.
    Available providers: openrouter, ollama, g4f.
    """
    return {
        'openrouter': {
            'type': 'openai',
            'base_url': 'https://openrouter.ai/api/v1',
            'api_keys': settings.OPENROUTER_API_KEYS,
            'model': (_parse_csv(settings.OPENROUTER_MODELS) or ['openrouter/free'])[0],
        },
        'g4f': {
            'type': 'openai',
            'base_url': settings.G4F_BASE_URL,
            'api_keys': _NO_KEY,
            'model': settings.G4F_MODEL,
        },
        'ollama': {
            'type': 'ollama',
            'base_url': settings.OLLAMA_BASE_URL,
            'model': settings.OLLAMA_MODEL,
        },
    }


# Cache of instantiated backends, keyed by provider name.
_backend_cache: dict[str, BaseLLMService] = {}
_backend_lock = threading.Lock()


def _build_backend(name: str, spec: dict) -> BaseLLMService:
    if spec['type'] == 'ollama':
        return OllamaLLMService(spec['base_url'], spec['model'])
    return OpenAICompatLLMService(spec['base_url'], spec['api_keys'], spec['model'])


def _get_backend(name: str, specs: dict[str, dict]) -> BaseLLMService | None:
    """Return a cached backend for the named provider, or None if unconfigured."""
    spec = specs.get(name)
    if spec is None:
        logger.warning('Unknown LLM provider %r — skipping', name)
        return None
    if not spec.get('base_url'):
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
