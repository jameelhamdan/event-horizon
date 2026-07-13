"""Live-editable runtime configuration, backed by the core.RuntimeConfig
singleton (a Mongo doc) and surfaced as toggles in the admin dashboard's Actions
section. Reads happen at execution time (stage dispatch, backfill chunk start),
so flipping a switch takes effect on the next tick / next chunk without a
redeploy or worker restart — including an already-dispatched, in-flight backfill.

Every read is wrapped so a Mongo hiccup can never take the pipeline down: on
error we fall back to the settings default (which itself defaults to enabled),
i.e. "fail open" — a missing config never silently pauses the LLM.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# flag key -> (RuntimeConfig field, settings fallback attr, human label)
_FLAGS: dict[str, tuple[str, str, str]] = {
    'live': ('live_llm_enabled', 'LIVE_LLM_ENABLED', 'Live-pipeline'),
    'backfill': ('backfill_llm_enabled', 'BACKFILL_LLM_ENABLED', 'Historical-backfill'),
}


def _default(flag: str) -> bool:
    _field, setting_attr, _label = _FLAGS[flag]
    return bool(getattr(settings, setting_attr, True))


def _get(flag: str) -> bool:
    field, _setting_attr, _label = _FLAGS[flag]
    try:
        from core.models import RuntimeConfig
        return bool(getattr(RuntimeConfig.load(), field))
    except Exception:  # noqa: BLE001 — fail open to the settings default
        logger.debug('[runtime_config] read of %r failed; using settings default', flag, exc_info=True)
        return _default(flag)


def is_live_llm_enabled() -> bool:
    return _get('live')


def is_backfill_llm_enabled() -> bool:
    return _get('backfill')


def set_llm_flag(flag: str, enabled: bool) -> str:
    """Persist a flag change; returns the human label for a confirmation message."""
    if flag not in _FLAGS:
        raise ValueError(f'Unknown LLM flag {flag!r} (expected one of {sorted(_FLAGS)})')
    field, _setting_attr, label = _FLAGS[flag]
    from core.models import RuntimeConfig
    cfg = RuntimeConfig.load()
    setattr(cfg, field, bool(enabled))
    cfg.save(update_fields=[field, 'updated_on'])
    logger.info('[runtime_config] %s LLM set to %s', label, 'enabled' if enabled else 'disabled')
    return label


def llm_flags() -> dict:
    """Current flag states for the dashboard, e.g. {'live': True, 'backfill': False}."""
    return {flag: _get(flag) for flag in _FLAGS}
