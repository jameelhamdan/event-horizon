"""Market forecasting subsystem — LLM v1 (decision-support) + v2 quantitative model.

The whole subsystem is gated by the ``FORECASTING_ENABLED`` Django setting so it
can be switched off wholesale in deployments that don't need predictions. Both the
periodic scheduler (``core.management.commands.setup_schedule``) and the task
entrypoints (``services.tasks``) consult :func:`is_enabled` — keep this the single
source of truth for that switch rather than reading the setting directly.
"""

from __future__ import annotations


def is_enabled() -> bool:
    """Whether the forecasting subsystem (run / score / train) is active.

    Controlled by the ``FORECASTING_ENABLED`` setting (default ``True``). When
    ``False`` the forecast jobs are neither scheduled nor executed — they no-op.
    """
    from django.conf import settings

    return getattr(settings, "FORECASTING_ENABLED", True)
