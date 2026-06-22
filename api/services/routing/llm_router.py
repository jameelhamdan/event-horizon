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
import re

from services.forecasting.routing import (
    PANEL_SYMBOLS,
    route_event_to_weighted_symbols,
)

logger = logging.getLogger(__name__)

_PANEL_DESC = {
    'GC=F': 'Gold (safe-haven)', 'CL=F': 'Crude oil', 'NG=F': 'Natural gas',
    'ZW=F': 'Wheat', 'DX-Y.NYB': 'US Dollar index', '^TNX': 'US 10Y yield',
    '^VIX': 'Volatility/fear index', 'SPY': 'S&P 500 equities',
    'BTC-USD': 'Bitcoin', 'ETH-USD': 'Ethereum',
}
_PANEL_SET = set(PANEL_SYMBOLS)


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

        panel_lines = '\n'.join(f'- {s}: {_PANEL_DESC.get(s, s)}' for s in PANEL_SYMBOLS)
        total = (len(events) + self.BATCH_SIZE - 1) // self.BATCH_SIZE

        for start in range(0, len(events), self.BATCH_SIZE):
            batch = events[start: start + self.BATCH_SIZE]
            num = start // self.BATCH_SIZE + 1
            logger.info('[router] LLM batch %d/%d (%d events)', num, total, len(batch))

            event_lines = '\n'.join(
                f'{i + 1}. (id={e.pk}) {(e.title or "(no title)")[:160]} '
                f'[{e.category or "general"} | {e.location_name or "?"} | '
                f'topics: {", ".join(e.topic_slugs or []) or "none"}]'
                for i, e in enumerate(batch)
            )
            prompt = (
                'You are a markets analyst. For each news event, decide which market '
                'indicators it is likely to move and in which direction.\n\n'
                f'INDICATOR PANEL (only use these symbols):\n{panel_lines}\n\n'
                f'EVENTS:\n{event_lines}\n\n'
                'Return ONLY a JSON object. Each key is the event id shown as id=<value>. '
                'Each value is a list of {"symbol": <panel symbol>, "weight": <number -1..1>} '
                'where the sign is direction (positive = the event pushes the symbol up, '
                'negative = down) and the magnitude is confidence/strength. Omit symbols an '
                'event does not affect; use an empty list if none.\n'
                'Example: {"abc": [{"symbol": "CL=F", "weight": 0.7}, {"symbol": "^VIX", "weight": 0.5}], "def": []}'
            )

            try:
                response = llm.chat(
                    [{'role': 'user', 'content': prompt}],
                    temperature=0,
                    max_tokens=min(2000, 120 * len(batch) + 200),
                ).strip()
                response = re.sub(r'^```(?:json)?\s*', '', response)
                response = re.sub(r'\s*```$', '', response)
                parsed = json.loads(response)
                if not isinstance(parsed, dict):
                    raise ValueError('LLM returned non-dict')

                for event in batch:
                    key = str(event.pk)
                    raw = parsed.get(key)
                    cleaned = self._clean(raw)
                    # Empty/garbage → deterministic fallback so the event still gets routed.
                    results[key] = cleaned if cleaned else _fallback(event)
            except Exception as exc:  # noqa: BLE001 — broad: any failure falls back
                logger.warning('[router] batch %d/%d failed (%s) — deterministic fallback', num, total, exc)
                for event in batch:
                    results[str(event.pk)] = _fallback(event)

        return results

    @staticmethod
    def _clean(raw) -> list[dict]:
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            sym = item.get('symbol')
            if sym not in _PANEL_SET or sym in seen:
                continue
            try:
                w = float(item.get('weight'))
            except (TypeError, ValueError):
                continue
            w = max(-1.0, min(1.0, w))
            if w == 0:
                continue
            seen.add(sym)
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

    updated = 0
    for event in events:
        event.affected_indicators = routed.get(str(event.pk), [])
        event.router_source = source
        event.save(update_fields=['affected_indicators', 'router_source', 'updated_on'])
        updated += 1
    return updated
