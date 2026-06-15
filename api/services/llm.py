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


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(',') if v.strip()]


class LLMError(Exception):
    pass


class OpenRouterLLMService:
    """
    OpenRouter-backed LLM client with round-robin API key rotation.

    Config (.env):
      OPENROUTER_API_KEYS  comma-separated API keys — rotated round-robin per request
      OPENROUTER_MODELS    model to use (first value used; default: openrouter/free)
    """

    _token_cycle: itertools.cycle | None = None
    _token_lock = threading.Lock()

    @classmethod
    def _init_tokens(cls, tokens: list[str]) -> None:
        with cls._token_lock:
            if cls._token_cycle is None:
                cls._token_cycle = itertools.cycle(tokens)

    @classmethod
    def _next_token(cls) -> str:
        with cls._token_lock:
            return next(cls._token_cycle)

    def __init__(self) -> None:
        tokens = _parse_csv(settings.OPENROUTER_API_KEYS)
        if not tokens:
            raise LLMError('OPENROUTER_API_KEYS is not set')
        self._init_tokens(tokens)
        models = _parse_csv(settings.OPENROUTER_MODELS)
        self._model = models[0] if models else 'openrouter/free'

        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self._next_token(),
        )

    def chat(self, messages: list[dict], **kwargs) -> str:
        """
        Send a chat completion request.
        messages: list of {'role': 'system'|'user'|'assistant', 'content': str}
        Raises LLMError on failure.
        """

        try:
            completion = self.client.chat.completions.create(
                model=self._model,
                messages=messages,
                **kwargs
            )
            content = completion.choices[0].message.content
            if not content:
                raise LLMError("No content returned in completion response.")
            return content

        except requests.HTTPError as e:
            logger.error(
                "OpenRouter HTTP error (status %s) for model %s with messages %s: %s",
                e.response.status_code, self._model, messages, e.response.text
            )
            raise LLMError(f"OpenRouter request failed with status {e.response.status_code}") from e

        except (KeyError, IndexError, AttributeError, TypeError) as e:
            logger.error(
                "Unexpected response structure from OpenRouter for model %s with messages %s: %s",
                self._model, messages, str(e)
            )
            raise LLMError("Malformed response from OpenRouter") from e

        except Exception as e:
            logger.error(
                "OpenRouterLLMService unexpected error for model %s with messages %s: %s",
                self._model, messages, str(e)
            )
            raise LLMError(f"OpenRouterLLMService error: {e}") from e


class OllamaLLMService:
    """
    Ollama-backed LLM client for a self-hosted Ollama server.

    Config (.env):
      OLLAMA_BASE_URL  base URL of the Ollama server, e.g. http://my-server:11434
      OLLAMA_MODEL     model name (default: qwen3)

    Strips <think>...</think> reasoning blocks before returning content,
    so the caller always receives clean text / JSON.
    """

    def __init__(self) -> None:
        base_url = settings.OLLAMA_BASE_URL
        if not base_url:
            raise LLMError('OLLAMA_BASE_URL is not set')
        self._base_url = base_url.rstrip('/')
        self._model = settings.OLLAMA_MODEL

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

    def complete(self, prompt: str, system: str | None = None, **kwargs) -> str:
        """Single-turn convenience wrapper around chat()."""
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        return self.chat(messages, **kwargs)


def get_llm_service() -> OpenRouterLLMService | OllamaLLMService:
    """Return the configured LLM backend (openrouter or ollama)."""
    if settings.LLM_BACKEND == 'ollama':
        return OllamaLLMService()
    return OpenRouterLLMService()
