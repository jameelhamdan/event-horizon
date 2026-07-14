"""Shared lazy-load helper for local CPU models (FinBERT, VADER, translation).

Each model module wants the same three things: an env-var opt-out, a cache so the
model loads exactly once per process, and a load failure that degrades to "disabled"
rather than crashing the caller. ``lazy_loader`` factors that out so each module only
supplies its own build function.
"""

import functools
import logging
from typing import Callable, TypeVar

T = TypeVar('T')


def lazy_loader(label: str, env_var: str, build: Callable[[], T]) -> Callable[[], T | None]:
    """Return a zero-arg, ``lru_cache``d accessor that builds ``build()`` on first call.

    Returns ``None`` (logged, never raised) if ``env_var`` opts out, a required
    package is missing, or ``build()`` raises for any other reason — callers should
    always treat ``None`` as "this local model is unavailable" and degrade gracefully.
    """
    logger = logging.getLogger(f'services.processing.{label}')

    @functools.lru_cache(maxsize=1)
    def _load() -> T | None:
        if _os_env_disabled(env_var):
            logger.info('[%s] %s is off — %s disabled', label, env_var, label)
            return None
        try:
            return build()
        except ImportError:
            logger.warning('[%s] required package not installed — %s disabled', label, label)
            return None
        except Exception:
            logger.exception('[%s] failed to load model — %s disabled', label, label)
            return None

    return _load


def _os_env_disabled(env_var: str) -> bool:
    import os
    return os.getenv(env_var, 'true').strip().lower() in ('0', 'false', 'no')
