import json as _json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from django.utils import timezone

from services.llm import strip_code_fences

logger = logging.getLogger(__name__)

_THRESHOLD = 3.0
_DISCOVERY_MIN_UNTAGGED = 3
_DISCOVERY_MAX_CLUSTERS = 5


def _needs_tagging(topics) -> bool:
    """Return True if an event's topics field needs (re)processing.
    Handles both the current dict format and the legacy list-of-strings format.
    """
    if not topics:
        return True
    if isinstance(topics, list):
        return True
    return False


def refresh_topics() -> int:
    """Scrape all configured sources and upsert Topic objects (is_current=True).

    Returns the number of active current topics after the refresh.
    """
    from core.models import Topic, EventCategory
    from services.topics.scraper import TopicScraper
    from services.topics._dates import parse_approximate_date

    scraped = TopicScraper().scrape_all()
    if not scraped:
        logger.warning('[topics] Scrape returned no topics — skipping upsert')
        return 0

    from services.topics.dedup import semantic_merge_topics
    scraped = semantic_merge_topics(scraped)
    scraped = _enrich_topics(scraped)

    valid_categories = {c.value for c in EventCategory}
    seen_slugs: set[str] = set()
    new_slugs: list[str] = []
    now = timezone.now()

    for topic in scraped:
        slug = (topic.get('slug') or '').strip()
        if not slug:
            continue
        seen_slugs.add(slug)

        category = (topic.get('category') or '').lower().strip()
        if category not in valid_categories:
            category = ''

        source_ids = topic.get('source_ids') or [topic.get('source_id', '')]
        source_ids = [s for s in source_ids if s]

        is_current = topic.get('is_current', True)

        defaults = {
            'name':        (topic.get('name') or slug)[:255],
            'keywords':    topic.get('keywords') or [],
            'description': (topic.get('description') or '')[:1000],
            'category':    category,
            'source_url':  topic.get('source_url', ''),
            'parent_slug': topic.get('parent') or None,
            'is_current':  is_current,
            'is_active':   True,
            'ended_at':    None,
        }

        existing = Topic.objects.filter(slug=slug).first()

        if existing:
            for key, val in defaults.items():
                setattr(existing, key, val)
            merged_sources = list({*(existing.source_ids or []), *source_ids})
            existing.source_ids = merged_sources
            existing.save(update_fields=[
                'name', 'keywords', 'description', 'category', 'source_url',
                'parent_slug', 'is_current', 'is_active', 'ended_at', 'source_ids',
            ])
            logger.info('[topics] Updated topic: %s', slug)
        else:
            approx = topic.get('approximate_start')
            started_at = parse_approximate_date(approx) if approx else now
            Topic.objects.create(
                slug=slug,
                started_at=started_at,
                source_ids=source_ids,
                **defaults,
            )
            new_slugs.append(slug)
            logger.info('[topics] Created topic: %s', slug)

    # Topics no longer in the scrape: mark is_current=False.
    stale_qs = Topic.objects.filter(is_current=True, is_active=True).exclude(
        slug__in=list(seen_slugs)
    )
    stale_topics = [
        t for t in list(stale_qs)
        if not getattr(t, 'is_pinned', False)
        and (
            'event-discovery' not in (t.source_ids or [])
            or (t.started_at and (now - t.started_at).total_seconds() > 86400 * 2)
        )
    ]
    for t in stale_topics:
        t.is_current = False
        if t.ended_at is None:
            t.ended_at = now
        t.save(update_fields=['is_current', 'ended_at'])
    if stale_topics:
        logger.info('[topics] Marked %d topic(s) as no longer current', len(stale_topics))

    if new_slugs:
        from services.queue import enqueue
        from services.tasks import retroactive_tag_topic_task
        for slug in new_slugs:
            enqueue(retroactive_tag_topic_task, slug=slug)
            logger.info('[topics] Enqueued retroactive tagging for: %s', slug)

    prune_stale_topics()

    active_count = Topic.objects.filter(is_current=True, is_active=True).count()
    logger.info('[topics] refresh_topics done — %d current active topic(s)', active_count)
    return active_count


def prune_stale_topics(stale_days: int | None = None) -> int:
    """Hide top-bar topics with no tagged events in stale_days (default 90).

    Sets is_top_level=False for non-pinned top-level topics with no recent events.
    Runs automatically in refresh_topics (daily).
    """
    from core.models import Topic, Event

    stale_days = stale_days or 90
    now = timezone.now()
    cutoff = now - timedelta(days=stale_days)

    fresh: set[str] = set()
    for ev in Event.objects.filter(started_at__gte=cutoff).only('topic_slugs'):
        fresh.update(ev.topic_slugs or [])

    demoted = 0
    for topic in Topic.objects.filter(is_active=True):
        if not topic.is_top_level or topic.is_pinned or topic.slug in fresh:
            continue
        if topic.started_at and topic.started_at >= cutoff:
            continue
        topic.is_top_level = False
        topic.save(update_fields=['is_top_level'])
        demoted += 1
    if demoted:
        logger.info('[topics] pruned %d stale topic(s) from header — no events in %dd', demoted, stale_days)
    return demoted


def tag_events_with_topics(hours: int = 24, force_retag: bool = False) -> int:
    """Match recent Events to active Topics using the LLM (batch mode).

    Returns the number of events processed.
    """
    from core.models import Topic, Event

    lookback = timezone.now() - timedelta(hours=hours)
    all_active_topics = list(Topic.objects.filter(is_active=True))

    if not all_active_topics:
        logger.info('[topics] No active topics — skipping tag_events_with_topics')
        return 0

    qs = Event.objects.filter(started_at__gte=lookback)
    if not force_retag:
        events_all = list(qs)
        events = [
            e for e in events_all
            if _needs_tagging(e.topics) or e.topics_source == 'keyword'
        ]
    else:
        events = list(qs)

    if not events:
        logger.info('[topics] No events to tag in the last %d hour(s)', hours)
        return 0

    tagged = _tag_and_recount(events, all_active_topics)
    logger.info('[topics] tag_events_with_topics done — %d event(s) processed', tagged)
    return tagged


def _tag_and_recount(events: list, all_active_topics: list) -> int:
    """Run LLM tagging over events then refresh topic event counts. Returns tagged count."""
    tagged = _apply_topic_tags(events, all_active_topics)
    _update_topic_event_counts(all_active_topics)
    return tagged


def tag_events_by_ids(event_ids: list) -> int:
    """Tag a specific set of events by id — the per-record fan-out worker."""
    from core.models import Topic, Event

    all_active_topics = list(Topic.objects.filter(is_active=True))
    events = list(Event.objects.filter(pk__in=list(event_ids)))
    if not all_active_topics or not events:
        return 0
    return _tag_and_recount(events, all_active_topics)


def _apply_topic_tags(events: list, all_active_topics: list) -> int:
    """Run the LLM matcher over events and persist topics + re-routed indicators.
    Returns the number of events processed.
    """
    from services.topics.matcher import EmbeddingTopicMatcher
    from services.forecasting.routing import route_event_to_weighted_symbols
    from services.utils import mark_stage

    matcher = EmbeddingTopicMatcher()
    batch_results, batch_sources = matcher.match_batch(events, all_active_topics)

    tagged = 0
    for event in events:
        result = batch_results.get(str(event.pk), {})
        event.topics = result
        event.topic_slugs = list(result.keys())
        source = batch_sources.get(str(event.pk), 'keyword')
        event.topics_source = source
        route_sentiment = (
            event.avg_finbert_sentiment
            if event.avg_finbert_sentiment is not None
            else event.avg_sentiment
        )
        event.affected_indicators = route_event_to_weighted_symbols(
            event.category, event.location_name, event.topic_slugs,
            event.sub_categories or [], route_sentiment,
        )
        mark_stage(event, 'tag', ok=(source == 'embed'),
                   error=None if source == 'embed' else 'keyword fallback (embedding model unavailable)')
        event.save(update_fields=[
            'topics', 'topic_slugs', 'topics_source', 'affected_indicators', 'stage_status',
        ])
        tagged += 1
    return tagged


def _enrich_topics(topics: list) -> list:
    """Batch LLM pass: generate descriptions and expand keywords before DB upsert.

    Sends topics in batches of 30. Falls back silently on any LLM error.
    Mutates and returns the same list.
    """
    from services.llm import get_llm_service

    if not topics:
        return topics

    try:
        llm = get_llm_service('topics')
    except Exception as exc:
        logger.warning('[topics] LLM enrichment skipped (no LLM service): %s', exc)
        return topics

    BATCH_SIZE = 30

    for batch_start in range(0, len(topics), BATCH_SIZE):
        batch = topics[batch_start: batch_start + BATCH_SIZE]

        lines = []
        for i, t in enumerate(batch):
            ctx = (t.get('description') or '').strip()
            if ctx.lower().startswith('ongoing armed conflict. location:'):
                loc = ctx[len('ongoing armed conflict. location:'):].strip().rstrip('.')
                ctx = f'Location: {loc}'
            line = (
                f'{i + 1}. {t.get("name") or t["slug"]}'
                f' ({t.get("category") or "general"}) [slug:{t["slug"]}]'
            )
            if ctx:
                line += f' — {ctx[:80]}'
            lines.append(line)

        prompt = (
            'News analyst. For each topic: 1-2 sentence description + 8-15 keywords'
            ' (people, places, orgs, terms).\n\n'
            'TOPICS:\n' + '\n'.join(lines) + '\n\n'
            'JSON array: [{"slug":"...","description":"...","keywords":["kw1",...]}, ...]\nJSON only.'
        )

        try:
            response = strip_code_fences(llm.chat([{'role': 'user', 'content': prompt}]))
            enriched = _json.loads(response)
            if not isinstance(enriched, list):
                raise ValueError('LLM returned non-list')

            by_slug = {
                e['slug']: e
                for e in enriched
                if isinstance(e, dict) and 'slug' in e
            }

            for t in batch:
                e = by_slug.get(t.get('slug', ''))
                if not e:
                    continue
                if desc := (e.get('description') or '').strip():
                    t['description'] = desc[:1000]
                kws = e.get('keywords')
                if isinstance(kws, list) and kws:
                    existing = t.get('keywords') or []
                    seen = {k.lower() for k in existing}
                    merged = list(existing)
                    for kw in kws:
                        if isinstance(kw, str) and kw.strip() and kw.lower() not in seen:
                            seen.add(kw.lower())
                            merged.append(kw.strip())
                    t['keywords'] = merged[:25]

            logger.info(
                '[topics] LLM enriched %d/%d topic(s) (batch %d/%d)',
                len(by_slug), len(batch),
                batch_start // BATCH_SIZE + 1,
                (len(topics) + BATCH_SIZE - 1) // BATCH_SIZE,
            )

        except Exception as exc:
            logger.warning(
                '[topics] LLM enrichment batch %d failed: %s',
                batch_start // BATCH_SIZE + 1, exc,
            )

    return topics


def _update_topic_event_counts(topics: list) -> None:
    """Recount event_count, compute topic_score, and auto-set is_top_level."""
    from core.models import Event

    now = timezone.now()
    window_7d = now - timedelta(hours=168)
    window_24h = now - timedelta(hours=24)

    slug_count: Counter = Counter()
    slug_articles_24h: Counter = Counter()
    slug_intensity: dict[str, list[float]] = defaultdict(list)
    slug_latest: dict[str, datetime] = {}

    for event in Event.objects.filter(started_at__gte=window_7d).only(
        'topic_slugs', 'article_count', 'avg_intensity', 'started_at'
    ):
        for slug in (event.topic_slugs or []):
            slug_count[slug] += 1
            if event.started_at and event.started_at >= window_24h:
                slug_articles_24h[slug] += event.article_count or 1
            if event.avg_intensity is not None:
                slug_intensity[slug].append(event.avg_intensity)
            if event.started_at:
                prev = slug_latest.get(slug)
                if prev is None or event.started_at > prev:
                    slug_latest[slug] = event.started_at

    for topic in topics:
        slug = topic.slug
        new_count = slug_count.get(slug, 0)

        latest = slug_latest.get(slug)
        if latest:
            age_hours = (now - latest).total_seconds() / 3600
        else:
            age_hours = 9999

        if age_hours < 24:
            recency = 1.0
        elif age_hours < 72:
            recency = 0.6
        elif age_hours < 168:
            recency = 0.2
        else:
            recency = 0.05

        articles_24h = slug_articles_24h.get(slug, 0)
        intensities = slug_intensity.get(slug, [])
        avg_intensity = round(sum(intensities) / len(intensities), 4) if intensities else 0.0
        score = round(articles_24h * (1 + avg_intensity) * recency, 4)

        new_top_level = topic.is_pinned or (score >= _THRESHOLD)

        changed = (
            topic.event_count != new_count
            or topic.topic_score != score
            or topic.is_top_level != new_top_level
        )
        if changed:
            topic.event_count = new_count
            topic.topic_score = score
            topic.is_top_level = new_top_level
            topic.save(update_fields=['event_count', 'topic_score', 'is_top_level'])


def retroactive_tag_topic(slug: str, lookback_hours: int = 72) -> int:
    """Retroactively tag historical events for a single newly-created topic.
    Returns the number of events tagged with this topic.
    """
    from core.models import Topic, Event
    from services.topics.matcher import TopicMatcher

    try:
        topic = Topic.objects.get(slug=slug)
    except Topic.DoesNotExist:
        logger.warning('[topics] retroactive_tag_topic: unknown slug %s', slug)
        return 0

    lookback = timezone.now() - timedelta(hours=lookback_hours)
    events = list(Event.objects.filter(started_at__gte=lookback))
    events = [e for e in events if slug not in (e.topic_slugs or [])]

    if not events:
        logger.info('[topics] retroactive_tag_topic: no events to process for %s', slug)
        return 0

    matcher = TopicMatcher()
    tagged_count = 0

    for event in events:
        result = matcher.match(event, [topic])
        if not result:
            continue

        existing = event.topics
        if not isinstance(existing, dict):
            existing = {}
        existing.update(result)
        event.topics = existing
        event.topic_slugs = list(existing.keys())
        event.topics_source = 'keyword'

        try:
            from services.forecasting.routing import route_event_to_weighted_symbols
            route_sentiment = (
                event.avg_finbert_sentiment
                if event.avg_finbert_sentiment is not None
                else event.avg_sentiment
            )
            event.affected_indicators = route_event_to_weighted_symbols(
                event.category, event.location_name, event.topic_slugs,
                event.sub_categories or [], route_sentiment,
            )
        except Exception:
            pass  # routing is best-effort; topic tags are still saved

        event.save(update_fields=['topics', 'topic_slugs', 'topics_source', 'affected_indicators'])
        tagged_count += 1
        logger.info('[topics] Retroactively tagged "%s" → %s', event.title[:60], slug)

    logger.info(
        '[topics] retroactive_tag_topic(%s) done — %d/%d event(s) tagged',
        slug, tagged_count, len(events),
    )
    return tagged_count


def discover_topics_from_events(hours: int = 6) -> int:
    """Scan recent untagged events, group by (category, country), and use the LLM
    to discover new topics for clusters above a minimum size.

    Returns the number of new topics created.
    """
    from core.models import Event, Topic, EventCategory
    from services.llm import get_llm_service
    from services.queue import enqueue
    from services.tasks import retroactive_tag_topic_task

    lookback = timezone.now() - timedelta(hours=hours)
    untagged = list(
        Event.objects.filter(
            started_at__gte=lookback,
            topic_slugs=[],
        ).only('title', 'category', 'location_name', 'article_count')
    )

    if not untagged:
        logger.info('[discover] No untagged events in the last %dh', hours)
        return 0

    buckets: dict[tuple[str, str], list] = defaultdict(list)
    for event in untagged:
        parts = event.location_name.split(',')
        country = parts[-1].strip() if len(parts) > 1 else event.location_name.strip()
        bucket_key = (event.category or 'general', country)
        buckets[bucket_key].append(event)

    candidates = sorted(
        [(key, evts) for key, evts in buckets.items() if len(evts) >= _DISCOVERY_MIN_UNTAGGED],
        key=lambda x: len(x[1]),
        reverse=True,
    )[:_DISCOVERY_MAX_CLUSTERS]

    if not candidates:
        logger.info('[discover] No clusters meet the minimum size of %d', _DISCOVERY_MIN_UNTAGGED)
        return 0

    valid_categories = {c.value for c in EventCategory}
    llm = get_llm_service('topics')
    created_count = 0

    for (category, country), events in candidates:
        titles_sample = '\n'.join(f'- {e.title}' for e in events[:10])
        prompt = (
            f'Events in {country}, category "{category}":\n{titles_sample}\n\n'
            f'If they share a coherent ongoing topic: {{"slug":"kebab-case","name":"short name",'
            f'"keywords":["5-15 terms"],"category":"{category}","description":"1-2 sentences"}}\n'
            f'If no coherent topic: null\nJSON only.'
        )

        try:
            response_text = strip_code_fences(llm.chat([{'role': 'user', 'content': prompt}]))
            if response_text.strip().lower() == 'null':
                continue
            data = _json.loads(response_text)
            if not data or not isinstance(data, dict):
                continue

            slug = (data.get('slug') or '').strip()[:80]
            name = (data.get('name') or '').strip()[:255]
            if not slug or not name:
                continue

            if Topic.objects.filter(slug=slug).exists():
                continue

            cat = (data.get('category') or '').lower().strip()
            if cat not in valid_categories:
                cat = category if category in valid_categories else ''

            Topic.objects.create(
                slug=slug,
                name=name,
                keywords=data.get('keywords') or [],
                description=(data.get('description') or '')[:1000],
                category=cat,
                source_ids=['event-discovery'],
                is_current=True,
                is_active=True,
                is_top_level=False,
                started_at=timezone.now(),
            )
            created_count += 1
            logger.info('[discover] Created topic from events: %s (%s / %s)', slug, category, country)
            enqueue(retroactive_tag_topic_task, slug=slug)

        except Exception as exc:
            logger.warning('[discover] LLM call failed for (%s, %s): %s', category, country, exc)

    logger.info('[discover] discover_topics_from_events done — %d new topic(s) created', created_count)
    return created_count
