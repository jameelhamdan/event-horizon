"""Event → market-symbol routing — deterministic, auditable weight product.

See ``services.forecasting.routing`` for the actual rules (category/sub-category/
country/sentiment weighting).
"""
import logging

from django.utils import timezone

from services.forecasting.routing import route_event_to_weighted_symbols

logger = logging.getLogger(__name__)


def _route_event(event) -> list[dict]:
    return route_event_to_weighted_symbols(
        category=event.category or 'general',
        location=event.location_name or '',
        topic_slugs=list(event.topic_slugs or []),
        sub_categories=list(event.sub_categories or []),
        sentiment=getattr(event, 'avg_finbert_sentiment', None) or getattr(event, 'avg_sentiment', None),
    )


def route_events(events: list) -> int:
    """Route a list of Event objects and persist ``affected_indicators``. Returns the number updated."""
    if not events:
        return 0

    from services.utils import mark_stage

    now = timezone.now()
    for event in events:
        indicators = _route_event(event)
        event.affected_indicators = indicators
        event.router_source = 'rules'
        event.is_routed = bool(indicators)
        event.updated_on = now  # bulk_update bypasses auto_now
        mark_stage(event, 'route', ok=bool(indicators),
                   error=None if indicators else 'no indicators emitted')

    type(events[0]).objects.bulk_update(
        events, ['affected_indicators', 'router_source', 'is_routed', 'stage_status', 'updated_on'],
        batch_size=500,
    )
    return len(events)


__all__ = ['route_events']
