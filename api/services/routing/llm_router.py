"""LLM event→symbol router.

Reads an event (title, category, sub-categories, tagged topics, sentiment, location) and asks
the LLM which panel symbols it moves and in which direction (signed weight in [-1, 1]). The
output is a *feature/hypothesis* stored on ``Event.affected_indicators`` — never the label.

Mirrors ``services.topics.matcher.LLMTopicMatcher``: batched calls, code-fence stripping,
deterministic per-event fallback (``services.forecasting.routing``) on any error.
"""
from __future__ import annotations

import json
import logging

from services.llm import strip_code_fences
from services.forecasting.routing import (
    get_panel_symbols,
    route_event_to_weighted_symbols,
)

logger = logging.getLogger(__name__)

_PANEL_DESC = {
    'GC=F': 'Gold (safe-haven)', 'CL=F': 'Crude oil', 'NG=F': 'Natural gas',
    'ZW=F': 'Wheat', 'DX-Y.NYB': 'US Dollar index', '^TNX': 'US 10Y yield',
    '^VIX': 'Volatility/fear index', 'SPY': 'S&P 500 equities',
    'BTC-USD': 'Bitcoin', 'ETH-USD': 'Ethereum', 'EURUSD=X': 'EUR/USD',
}


def _event_sentiment(event) -> float | None:
    return getattr(event, 'avg_finbert_sentiment', None) or getattr(event, 'avg_sentiment', None)


def _fallback(event) -> list[dict]:
    """Deterministic per-event routing — never lets the pipeline go dark."""
    return route_event_to_weighted_symbols(
        category=event.category or 'general',
        location=event.location_name or '',
        topic_slugs=list(event.topic_slugs or []),
        sub_categories=list(event.sub_categories or []),
        sentiment=_event_sentiment(event),
    )


class LLMEventRouter:
    """Batch LLM router. ``route_batch`` returns {str(event.pk): [{symbol, weight}]}."""

    BATCH_SIZE = 10

    def route_batch(self, events: list) -> dict[str, list[dict]]:
        from services.llm import get_llm_service, LLMError

        results: dict[str, list[dict]] = {str(e.pk): [] for e in events}
        if not events:
            return results

        try:
            llm = get_llm_service('routing')
        except LLMError as exc:
            logger.warning('[router] no LLM available (%s) — using deterministic fallback', exc)
            return {str(e.pk): _fallback(e) for e in events}

        panel = get_panel_symbols()
        panel_set = set(panel)
        panel_lines = '\n'.join(f'- {s}: {_PANEL_DESC.get(s, s)}' for s in panel)
        total = (len(events) + self.BATCH_SIZE - 1) // self.BATCH_SIZE

        for start in range(0, len(events), self.BATCH_SIZE):
            batch = events[start: start + self.BATCH_SIZE]
            num = start // self.BATCH_SIZE + 1
            logger.info('[router] LLM batch %d/%d (%d events)', num, total, len(batch))

            # Use 1-based numeric keys instead of ObjectIds — shorter in both input and output.
            idx_to_event = {str(i + 1): e for i, e in enumerate(batch)}
            event_lines = '\n'.join(
                f'{i + 1}. {(e.title or "(no title)")[:160]} '
                f'[{e.category or "general"} | {e.location_name or "?"}]'
                + (f' [topics: {", ".join(e.topic_slugs)}]' if e.topic_slugs else '')
                for i, e in enumerate(batch)
            )
            prompt = (
                'Markets analyst. For each event, list affected panel indicators and direction.\n\n'
                f'PANEL:\n{panel_lines}\n\n'
                f'EVENTS:\n{event_lines}\n\n'
                'Return JSON: keys are event numbers ("1", "2", ...), values are '
                '{symbol: weight} dicts (weight -1..1, sign = direction up/down). '
                'Omit unaffected symbols; empty dict {} if none.\n'
                'Example: {"1": {"CL=F": 0.7, "^VIX": -0.3}, "2": {}}'
            )

            try:
                response = llm.chat(
                    [{'role': 'user', 'content': prompt}],
                    temperature=0,
                    max_tokens=min(800, 60 * len(batch) + 100),
                ).strip()
                parsed = json.loads(strip_code_fences(response))
                if not isinstance(parsed, dict):
                    raise ValueError('LLM returned non-dict')

                for idx, event in idx_to_event.items():
                    key = str(event.pk)
                    cleaned = self._clean(parsed.get(idx), panel_set)
                    results[key] = cleaned if cleaned else _fallback(event)
            except Exception as exc:  # noqa: BLE001 — broad: any failure falls back
                logger.warning('[router] batch %d/%d failed (%s) — deterministic fallback', num, total, exc)
                for event in batch:
                    results[str(event.pk)] = _fallback(event)

        return results

    @staticmethod
    def _clean(raw, panel_set: set[str]) -> list[dict]:
        if not isinstance(raw, dict):
            return []
        out: list[dict] = []
        for sym, w in raw.items():
            if sym not in panel_set:
                continue
            try:
                w = float(w)
            except (TypeError, ValueError):
                continue
            w = max(-1.0, min(1.0, w))
            if w == 0:
                continue
            out.append({'symbol': sym, 'weight': round(w, 4)})
        return out


def route_events(events: list, source: str = 'llm') -> int:
    """Route a list of Event objects and persist ``affected_indicators`` + ``router_source``.

    ``source='llm'`` uses the LLM router (rules fallback per event); ``source='rules'`` uses the
    deterministic router only. Returns the number of events updated.
    """
    if not events:
        return 0
    if source == 'llm':
        routed = LLMEventRouter().route_batch(events)
    else:
        routed = {str(e.pk): _fallback(e) for e in events}

    from services.stages import mark_stage

    updated = 0
    for event in events:
        indicators = routed.get(str(event.pk), [])
        event.affected_indicators = indicators
        event.router_source = source
        mark_stage(event, 'route', ok=bool(indicators),
                   error=None if indicators else 'no indicators emitted')
        event.save(update_fields=['affected_indicators', 'router_source', 'stage_status', 'updated_on'])
        updated += 1
    return updated
