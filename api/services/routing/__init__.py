"""Event → market-symbol routing services.

Two interchangeable sources, both producing ``Event.affected_indicators``:
  * ``services.forecasting.routing`` — deterministic, auditable weight product (baseline + fallback)
  * ``LLMEventRouter`` (here) — LLM-based semantic routing (primary), with the deterministic
    router as a per-event fallback on any LLM error.
"""
from .llm_router import LLMEventRouter, route_events

__all__ = ['LLMEventRouter', 'route_events']
