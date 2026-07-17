"""Live connectivity + per-key smoke test for the 3rd-party LLM providers.

Unlike the other ``tests_*`` modules (which are offline and deterministic), this one
makes *real* network calls using the keys in ``.env.app`` at the repo root. It tests
**each key individually** so you can confirm both keys of a 2-key pool actually work
(and aren't, say, two views of the same rate-limited account).

For every (provider, key) pair it sends one tiny chat completion and reports
latency, the model's reply, and token usage — or the error.

Run standalone (no Django/Mongo needed):

    python api/tests/tests_llm_providers.py
    python api/tests/tests_llm_providers.py --provider groq   # one provider
    python api/tests/tests_llm_providers.py --ollama          # also hit the Ollama box

Exit code is non-zero if any tested key fails.
"""

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / '.env.app'

# Models mirror api/settings/base.py. OpenRouter has no env model in .env.app, so a
# real free model is used by default; override any of these with the matching env var.
PROVIDERS = {
    'openrouter': {
        'base_url': 'https://openrouter.ai/api/v1',
        'keys_var': 'OPENROUTER_API_KEYS',
        'model_var': 'OPENROUTER_MODELS',
        'default_model': 'meta-llama/llama-3.3-70b-instruct:free',
    },
    'groq': {
        'base_url': 'https://api.groq.com/openai/v1',
        'keys_var': 'GROQ_API_KEYS',
        'model_var': 'GROQ_MODEL',
        'default_model': 'llama-3.1-8b-instant',
    },
    'cerebras': {
        'base_url': 'https://api.cerebras.ai/v1',
        'keys_var': 'CEREBRAS_API_KEYS',
        'model_var': 'CEREBRAS_MODEL',
        'default_model': 'gpt-oss-120b',
    },
}

_PROMPT = [{'role': 'user', 'content': 'Reply with exactly the word: OK'}]


def _load_env(path: Path) -> dict:
    """Minimal KEY=VALUE parser — avoids a hard dependency on python-decouple here."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or '').split(',') if v.strip()]


def _mask(key: str) -> str:
    return f'{key[:8]}..{key[-4:]}' if len(key) > 14 else key


def _test_openai_compat(base_url: str, key: str, model: str, insecure: bool = False) -> dict:
    from openai import OpenAI

    http_client = None
    if insecure:
        import httpx
        http_client = httpx.Client(verify=False)
    # max_retries=0 so a 429/error returns immediately instead of backing off.
    client = OpenAI(base_url=base_url, api_key=key, http_client=http_client, max_retries=0)
    started = time.monotonic()
    completion = client.chat.completions.create(
        model=model, messages=_PROMPT, temperature=0, max_tokens=16, timeout=30,
    )
    elapsed = time.monotonic() - started
    usage = getattr(completion, 'usage', None)
    return {
        'ok': True,
        'elapsed': elapsed,
        'reply': (completion.choices[0].message.content or '').strip()[:60],
        'tokens': getattr(usage, 'total_tokens', None) if usage else None,
    }


def _test_ollama(base_url: str, model: str, insecure: bool = False) -> dict:
    import requests

    started = time.monotonic()
    resp = requests.post(
        f'{base_url.rstrip("/")}/api/chat',
        json={'model': model, 'messages': _PROMPT, 'stream': False, 'think': False},
        timeout=120,
        verify=not insecure,
    )
    resp.raise_for_status()
    elapsed = time.monotonic() - started
    content = resp.json().get('message', {}).get('content', '')
    return {'ok': True, 'elapsed': elapsed, 'reply': content.strip()[:60], 'tokens': None}


def run(only_provider: str | None = None, include_ollama: bool = False,
        insecure: bool = False, model_override: str | None = None,
        keys_limit: int | None = None) -> int:
    # Prefer .env.app on disk (local runs); fall back to real env vars so this
    # also works inside the api/worker container where keys arrive via env_file.
    file_env = _load_env(ENV_FILE)
    env = {**os.environ, **file_env}
    if file_env:
        print(f'(keys from {ENV_FILE})')
    else:
        print('(.env.app not found — using process environment)')

    failures = 0
    tested = 0

    for name, spec in PROVIDERS.items():
        if only_provider and name != only_provider:
            continue
        keys = _csv(env.get(spec['keys_var'], ''))
        if keys_limit:
            keys = keys[:keys_limit]
        model = model_override or env.get(spec['model_var']) or spec['default_model']
        print(f'\n=== {name}  (model: {model}) ===')
        if not keys:
            print(f'   - no keys in {spec["keys_var"]} — skipped')
            continue
        for i, key in enumerate(keys, 1):
            tested += 1
            label = f'key {i}/{len(keys)} [{_mask(key)}]'
            try:
                r = _test_openai_compat(spec['base_url'], key, model, insecure=insecure)
                toks = f', {r["tokens"]} tok' if r['tokens'] else ''
                print(f'   [PASS] {label}: {r["elapsed"]:.2f}s{toks} -> {r["reply"]!r}')
            except Exception as exc:  # noqa: BLE001 — surface every failure verbatim
                failures += 1
                print(f'   [FAIL] {label}: {type(exc).__name__}: {str(exc)[:200]}')

    if include_ollama and not only_provider:
        base = env.get('OLLAMA_BASE_URL', 'http://localhost:11434')
        # qwen3 tiers from settings/base.py
        for tier_model in ('qwen3:4b', 'qwen3:8b', 'qwen3:14b'):
            tested += 1
            print(f'\n=== ollama {tier_model}  ({base}) ===')
            try:
                r = _test_ollama(base, tier_model, insecure=insecure)
                print(f'   [PASS] {r["elapsed"]:.2f}s -> {r["reply"]!r}')
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f'   [FAIL] {type(exc).__name__}: {str(exc)[:200]}')

    print(f'\n{"-" * 40}\n{tested - failures}/{tested} OK, {failures} failed')
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=list(PROVIDERS), help='test only this provider')
    parser.add_argument('--ollama', action='store_true', help='also test the Ollama box')
    parser.add_argument('--insecure', action='store_true',
                        help='disable TLS verification (use only to bypass a local MITM proxy)')
    parser.add_argument('--model', help='override the model id for the tested provider')
    parser.add_argument('--keys', type=int, help='test only the first N keys (saves quota)')
    args = parser.parse_args()
    return run(only_provider=args.provider, include_ollama=args.ollama, insecure=args.insecure,
               model_override=args.model, keys_limit=args.keys)


if __name__ == '__main__':
    sys.exit(main())
