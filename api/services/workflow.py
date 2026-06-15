"""
Pipeline workflow methods.

Tasks are plain callables — enqueue them via services.queue.enqueue().
The management commands (fetch_data, process_articles, aggregate_events) are
thin wrappers that parse CLI args and call or enqueue these functions.
"""
import logging
import os
import re
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from django.utils import timezone

logger = logging.getLogger(__name__)


def _needs_tagging(topics) -> bool:
    """
    Return True if an event's topics field needs (re)processing.
    Handles both the new dict format and the old list-of-strings format.
    """
    if not topics:
        return True
    # Old list-of-strings format from 0005 implementation
    if isinstance(topics, list):
        return True
    return False


def _fetch_og_image(url: str) -> str | None:
    """Best-effort: fetch og:image meta tag from a URL. Returns None on any failure."""
    try:
        r = requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'}, stream=True)
        # Read only the first 64 KB — enough to find the <head> og:image tag
        chunk = next(r.iter_content(65536), b'')
        r.close()
        text = chunk.decode('utf-8', errors='ignore')
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            text,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            text,
        )
        return m.group(1).strip() if m else None
    except Exception:
        return None


class Workflow:
    @classmethod
    def fetch_articles(
        cls,
        source_code: str | None,
        start_date: datetime,
        deadline: datetime | None = None,
    ) -> int:
        """
        Fetch messages from one or all sources starting at start_date and save as Articles.
        Returns the total number of newly created articles.

        deadline: if provided, stops between sources once the current time exceeds it,
                  so the task exits cleanly before the RQ hard-kill fires.
        """
        from services.data import DataService
        from core.models import Source

        sources = (
            list(Source.objects.filter(code=source_code))
            if source_code
            else list(Source.objects.filter(is_enabled=True))
        )
        total = 0
        for i, source in enumerate(sources):
            if deadline is not None and datetime.now(dt_timezone.utc) >= deadline:
                logger.warning(
                    '[fetch] deadline reached — stopping after %d/%d source(s)',
                    i, len(sources),
                )
                break
            try:
                count = DataService(source).refresh_until(start_date)
                total += count
            except Exception as e:
                count = 0
                logger.error(f'[fetch] Exception - {e}', exc_info=e)
            logger.info(f'[fetch] {source.code}: {count} new article(s)')
        logger.info(f'[fetch] done — {total} new article(s) across {len(sources)} source(s)')
        return total

    @classmethod
    def process_articles(
        cls,
        limit: int = 500,
        source_code: str | None = None,
        reprocess: bool = False,
    ) -> int:
        """
        Step 2 — Clean: run HuggingFace NER + VADER sentiment on Articles; use LLM for category + location.
        Returns the number of articles processed.
        """
        from core.models import Article, ArticleDocument
        from services.processing.cleaner import ArticleCleaner, CleaningError

        qs = Article.objects.all()
        if source_code:
            qs = qs.filter(source_code=source_code)
        if not reprocess:
            qs = qs.filter(processed_on__isnull=True)
        articles = list(qs[:limit])

        if not articles:
            return 0

        try:
            cleaner = ArticleCleaner()
        except CleaningError:
            logger.exception('NLP pipeline failed')
            raise

        processed = 0
        for article in articles:
            doc = ArticleDocument(
                id=str(article.id),
                title=article.title,
                content=article.content,
                source_code=article.source_code,
                published_on=article.published_on.isoformat(),
            )
            features = cleaner.clean(doc)
            article.entities = features.entities
            article.sentiment = features.sentiment
            article.finbert_sentiment = features.finbert_sentiment
            article.location = features.location
            article.latitude = features.latitude
            article.longitude = features.longitude
            article.event_intensity = features.event_intensity
            article.category = features.category
            article.sub_category = features.sub_category
            article.processed_on = timezone.now()
            article.extra_data = {**(article.extra_data or {}), 'llm': features.llm_data}
            article.translations = features.translations

            # Best-effort: fetch og:image if no banner set yet and URL is reachable
            update_fields = [
                'entities', 'sentiment', 'finbert_sentiment', 'location', 'latitude', 'longitude',
                'event_intensity', 'category', 'sub_category', 'processed_on',
                'extra_data', 'translations',
            ]
            if not article.banner_image_url and article.source_url and article.source_url.startswith('https://'):
                og = _fetch_og_image(article.source_url)
                if og:
                    article.banner_image_url = og
                    update_fields.append('banner_image_url')

            article.save(update_fields=update_fields)
            processed += 1
            location = features.location or '?'
            category = '/'.join(filter(None, [features.category, features.sub_category]))
            logger.info(f'[process] {article.title[:70]} → {category} @ {location}')

        return processed

    @classmethod
    def aggregate_events(cls, hours: int = 24, min_articles: int = 1) -> tuple[int, int]:
        """
        Group processed Articles by (location, calendar day) into Events.
        Uses lat/lng stored by the geocoder during process_articles.
        Returns (created_count, updated_count).
        """
        from core.models import Article, Event

        lookback = timezone.now() - timedelta(hours=hours)
        articles = list(
            Article.objects.filter(
                processed_on__isnull=False,
                location__isnull=False,
                published_on__gte=lookback,
            ).exclude(location='')
        )

        if not articles:
            return 0, 0

        # ── Primary bucketing ─────────────────────────────────────────────────
        # Group by (city, country, category, calendar day).  Category is included
        # so that a protest and a military clash in the same city on the same day
        # are never merged into a single event before semantic clustering begins.
        from services.processing.clustering import get_clusterer

        buckets: dict[tuple[str, str, str, str], list] = defaultdict(list)
        for article in articles:
            llm = (article.extra_data or {}).get('llm', {})
            city = llm.get('city') or ''
            country = llm.get('country') or ''
            category_key = article.category or 'general'
            date_key = article.published_on.date().isoformat()
            buckets[(city, country, category_key, date_key)].append(article)

        # ── Semantic sub-clustering ────────────────────────────────────────────
        # Within each geographic+category+day bucket, further split articles that
        # describe distinct events using title-level semantic similarity.
        clusterer = get_clusterer()
        sub_groups: list[list] = []
        for group in buckets.values():
            sub_groups.extend(clusterer.cluster(group))

        created_count = updated_count = 0

        for group in sub_groups:
            if len(group) < min_articles:
                continue

            llm = (group[0].extra_data or {}).get('llm', {})
            city = llm.get('city') or ''
            country = llm.get('country') or ''

            location = ', '.join(filter(None, [city, country])) or (group[0].location or '')
            if not location:
                continue

            representative = max(group, key=lambda a: a.event_intensity or 0)
            sentiments = [a.sentiment for a in group if a.sentiment is not None]
            finbert_sentiments = [a.finbert_sentiment for a in group if a.finbert_sentiment is not None]
            intensities = [a.event_intensity for a in group if a.event_intensity is not None]
            avg_sentiment = round(sum(sentiments) / len(sentiments), 4) if sentiments else None
            avg_finbert_sentiment = (
                round(sum(finbert_sentiments) / len(finbert_sentiments), 4) if finbert_sentiments else None
            )
            base_intensity = round(sum(intensities) / len(intensities), 4) if intensities else None
            # Corroboration boost: more articles covering the same event → higher importance.
            # Saturates at 10 articles (+0.3 max), capped at 1.0.
            corroboration_boost = min(len(group) / 10.0, 1.0) * 0.3
            avg_intensity = round(min((base_intensity or 0) + corroboration_boost, 1.0), 4) if base_intensity is not None else None

            started_at = min(a.published_on for a in group)
            latest_article_at = max(a.published_on for a in group)
            article_ids = [str(a.id) for a in group]
            source_codes = list({a.source_code for a in group})

            categories = [a.category for a in group if a.category]
            category = max(set(categories), key=categories.count) if categories else 'general'
            sub_categories = sorted({a.sub_category for a in group if a.sub_category})

            # Deterministic affected-indicator weights (plan §2). Prefer FinBERT
            # (news-domain) sentiment for the signed amplification, fall back to VADER.
            from services.forecasting.routing import route_event_to_weighted_symbols
            route_sentiment = avg_finbert_sentiment if avg_finbert_sentiment is not None else avg_sentiment
            affected_indicators = route_event_to_weighted_symbols(
                category, location, [], sub_categories, route_sentiment,
            )

            # Average lat/lon across all articles that have coordinates
            lats = [a.latitude for a in group if a.latitude is not None]
            lngs = [a.longitude for a in group if a.longitude is not None]
            lat = round(sum(lats) / len(lats), 6) if lats else representative.latitude
            lng = round(sum(lngs) / len(lngs), 6) if lngs else representative.longitude

            # Build event translations subdocument from representative article.
            # For each language in the representative's translations, copy the title
            # and build location_name from city + country in that language.
            rep_translations = getattr(representative, 'translations', {}) or {}
            event_translations: dict = {}
            for lang, fields in rep_translations.items():
                if not isinstance(fields, dict):
                    continue
                lang_city = fields.get('city') or ''
                lang_country = fields.get('country') or ''
                lang_location = ', '.join(p for p in [lang_city, lang_country] if p) or location
                event_translations[lang] = {
                    'title': fields.get('title') or representative.title,
                    'location_name': lang_location,
                }

            # Upsert: match on location_name + calendar day.
            # Use explicit datetime range — MongoDB backend does not support __date lookups.
            day_start = datetime(started_at.year, started_at.month, started_at.day, tzinfo=started_at.tzinfo)
            day_end = day_start + timedelta(days=1)

            event = Event.objects.filter(
                location_name=location,
                category=category,
                started_at__gte=day_start,
                started_at__lt=day_end,
            ).first()

            if event is None:
                Event.objects.create(
                    title=representative.title,
                    content=representative.content,
                    category=category,
                    location_name=location,
                    latitude=lat,
                    longitude=lng,
                    started_at=started_at,
                    latest_article_at=latest_article_at,
                    article_count=len(group),
                    avg_sentiment=avg_sentiment,
                    avg_finbert_sentiment=avg_finbert_sentiment,
                    avg_intensity=avg_intensity,
                    article_ids=article_ids,
                    source_codes=source_codes,
                    sub_categories=sub_categories,
                    affected_indicators=affected_indicators,
                    translations=event_translations,
                )
                created_count += 1
                logger.info(f'[aggregate] Created  {location} [{category}] — {len(group)} article(s)')
            else:
                event.title = representative.title
                event.category = category
                event.latitude = lat
                event.longitude = lng
                event.latest_article_at = latest_article_at
                event.article_count = len(group)
                event.avg_sentiment = avg_sentiment
                event.avg_finbert_sentiment = avg_finbert_sentiment
                event.avg_intensity = avg_intensity
                event.article_ids = article_ids
                event.source_codes = source_codes
                event.sub_categories = sub_categories
                event.affected_indicators = affected_indicators
                event.translations = event_translations
                event.save()
                updated_count += 1
                logger.info(f'[aggregate] Updated  {location} [{category}] — {len(group)} article(s)')

        return created_count, updated_count

    @classmethod
    def refresh_topics(cls) -> int:
        """
        Scrape all configured sources and upsert Topic objects (is_current=True).

        - New topics get started_at = now (or approximate_start from scraper).
        - Existing topics are updated; their started_at is preserved.
        - Topics absent from this scrape have is_current set to False + ended_at set.
        - Newly created topics trigger a retroactive_tag_topic job.

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
        scraped = cls._enrich_topics(scraped)

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

        # Topics no longer in the scrape: mark is_current=False, record ended_at.
        # Filtering is done in Python so pinned-check works for pre-migration documents
        # (MongoDB won't match a missing field via {is_pinned: false}).
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

        active_count = Topic.objects.filter(is_current=True, is_active=True).count()
        logger.info('[topics] refresh_topics done — %d current active topic(s)', active_count)
        return active_count

    @classmethod
    def tag_events_with_topics(cls, hours: int = 24, force_retag: bool = False) -> int:
        """
        Match recent Events to active Topics using the LLM (batch mode).

        Args:
            hours: Lookback window for events.
            force_retag: If True, re-evaluate all events in the window,
                         not just untagged ones.

        Returns the number of events processed.
        """
        from core.models import Topic, Event
        from services.topics.matcher import LLMTopicMatcher

        lookback = timezone.now() - timedelta(hours=hours)
        all_active_topics = list(Topic.objects.filter(is_active=True))

        if not all_active_topics:
            logger.info('[topics] No active topics — skipping tag_events_with_topics')
            return 0

        # Fetch events in the window
        qs = Event.objects.filter(started_at__gte=lookback)
        if not force_retag:
            # Process events that have no topics OR have the old list-of-strings format
            events_all = list(qs)
            events = [e for e in events_all if _needs_tagging(e.topics)]
        else:
            events = list(qs)

        if not events:
            logger.info('[topics] No events to tag in the last %d hour(s)', hours)
            return 0

        llm_matcher = LLMTopicMatcher()
        # Use all active topics — named conflicts span years so temporal filtering
        # is unnecessary and would reduce recall.
        batch_results = llm_matcher.match_batch(events, all_active_topics)

        from services.forecasting.routing import route_event_to_weighted_symbols
        tagged = 0
        for event in events:
            result = batch_results.get(str(event.pk), {})
            event.topics = result
            event.topic_slugs = list(result.keys())
            # Re-route now that topic slugs are known — topics carry the highest-signal
            # routing rules, so affected_indicators improves once an event is tagged.
            route_sentiment = (
                event.avg_finbert_sentiment
                if event.avg_finbert_sentiment is not None
                else event.avg_sentiment
            )
            event.affected_indicators = route_event_to_weighted_symbols(
                event.category, event.location_name, event.topic_slugs,
                event.sub_categories or [], route_sentiment,
            )
            event.save(update_fields=['topics', 'topic_slugs', 'affected_indicators'])
            tagged += 1

        logger.info('[topics] tag_events_with_topics done — %d event(s) processed', tagged)

        # Recount event_count for all active topics over a 7-day window
        cls._update_topic_event_counts(all_active_topics)

        return tagged

    @classmethod
    def _enrich_topics(cls, topics: list) -> list:
        """
        Batch LLM pass: generate a proper description and expand keywords for each
        scraped topic before it is upserted into the database.

        Sends topics in batches of 30. Falls back silently on any LLM error —
        topics are returned with their original (possibly sparse) metadata.
        Mutates and returns the same list.
        """
        import json as _json
        from services.llm import get_llm_service

        if not topics:
            return topics

        try:
            llm = get_llm_service()
        except Exception as exc:
            logger.warning('[topics] LLM enrichment skipped (no LLM service): %s', exc)
            return topics

        BATCH_SIZE = 30

        for batch_start in range(0, len(topics), BATCH_SIZE):
            batch = topics[batch_start: batch_start + BATCH_SIZE]

            lines = []
            for i, t in enumerate(batch):
                ctx = (t.get('description') or '').strip()
                # Template descriptions from ongoing.py encode the location — extract it
                if ctx.lower().startswith('ongoing armed conflict. location:'):
                    loc = ctx[len('ongoing armed conflict. location:'):].strip().rstrip('.')
                    ctx = f'Location: {loc}'
                line = (
                    f'{i + 1}. slug={t["slug"]}'
                    f' | name={t.get("name") or t["slug"]}'
                    f' | category={t.get("category") or "general"}'
                )
                if ctx:
                    line += f' | context={ctx[:120]}'
                lines.append(line)

            prompt = (
                'You are a news analyst. For each topic below, write a concise 1–2 sentence '
                'description and list 8–15 relevant keywords that would appear in news headlines '
                'about it (people, places, organisations, terms).\n\n'
                'TOPICS:\n' + '\n'.join(lines) + '\n\n'
                'Return a JSON array in the same order, one object per topic:\n'
                '[{"slug": "...", "description": "...", "keywords": ["kw1", "kw2", ...]}, ...]\n'
                'Respond with only the JSON array, no other text.'
            )

            try:
                response = llm.chat([{'role': 'user', 'content': prompt}]).strip()
                response = re.sub(r'^```(?:json)?\s*', '', response)
                response = re.sub(r'\s*```$', '', response)
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

    @classmethod
    def _update_topic_event_counts(cls, topics: list) -> None:
        """Recount event_count, compute topic_score, and auto-set is_top_level."""
        from collections import Counter
        from core.models import Event

        THRESHOLD = float(os.getenv('TOP_LEVEL_SCORE_THRESHOLD', '3.0'))

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

            new_top_level = topic.is_pinned or (score >= THRESHOLD)

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

    @classmethod
    def retroactive_tag_topic(cls, slug: str, lookback_hours: int = 72) -> int:
        """
        Retroactively tag historical events for a single newly-created topic.
        Only processes events that don't already have this slug in their topic_slugs.
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
        events = list(
            Event.objects.filter(started_at__gte=lookback)
        )
        # Skip events that already have this slug tagged
        events = [e for e in events if slug not in (e.topic_slugs or [])]

        if not events:
            logger.info('[topics] retroactive_tag_topic: no events to process for %s', slug)
            return 0

        matcher = TopicMatcher()
        tagged_count = 0

        for event in events:
            # Only match against the one new topic
            result = matcher.match(event, [topic])
            if not result:
                continue

            # Merge into existing topics (don't overwrite other tags)
            existing = event.topics
            if not isinstance(existing, dict):
                existing = {}
            existing.update(result)
            event.topics = existing
            event.topic_slugs = list(existing.keys())
            event.save(update_fields=['topics', 'topic_slugs'])
            tagged_count += 1
            logger.info('[topics] Retroactively tagged "%s" → %s', event.title[:60], slug)

        logger.info(
            '[topics] retroactive_tag_topic(%s) done — %d/%d event(s) tagged',
            slug, tagged_count, len(events),
        )
        return tagged_count

    @classmethod
    def discover_topics_from_events(cls, hours: int = 6) -> int:
        """
        Scan recent untagged events, group by (category, country), and use the LLM
        to discover new topics for clusters above a minimum size.

        Returns the number of new topics created.
        """
        from core.models import Event, Topic, EventCategory
        from services.llm import get_llm_service
        from services.queue import enqueue
        from services.tasks import retroactive_tag_topic_task

        DISCOVERY_MIN_UNTAGGED = int(os.getenv('DISCOVERY_MIN_UNTAGGED_EVENTS', '3'))
        DISCOVERY_MAX_CLUSTERS = int(os.getenv('DISCOVERY_MAX_CLUSTERS', '5'))

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

        # Group by (category, country) — extract country as second part of "City, Country"
        buckets: dict[tuple[str, str], list] = defaultdict(list)
        for event in untagged:
            parts = event.location_name.split(',')
            country = parts[-1].strip() if len(parts) > 1 else event.location_name.strip()
            bucket_key = (event.category or 'general', country)
            buckets[bucket_key].append(event)

        # Filter to clusters that meet the minimum size, take top N by cluster size
        candidates = sorted(
            [(key, evts) for key, evts in buckets.items() if len(evts) >= DISCOVERY_MIN_UNTAGGED],
            key=lambda x: len(x[1]),
            reverse=True,
        )[:DISCOVERY_MAX_CLUSTERS]

        if not candidates:
            logger.info('[discover] No clusters meet the minimum size of %d', DISCOVERY_MIN_UNTAGGED)
            return 0

        import json as _json
        from services.tasks import retroactive_tag_topic_task

        valid_categories = {c.value for c in EventCategory}
        llm = get_llm_service()
        created_count = 0

        for (category, country), events in candidates:
            titles_sample = '\n'.join(f'- {e.title}' for e in events[:10])
            prompt = (
                f'You are a news analyst. The following events all occurred in {country} '
                f'and are categorized as "{category}". They have not been matched to any '
                f'known topic yet.\n\nEvent titles:\n{titles_sample}\n\n'
                f'If these events share a coherent ongoing news topic, respond with a JSON '
                f'object with fields: slug (kebab-case, max 80 chars), name (concise, max 80 chars), '
                f'keywords (list of 5-15 relevant keywords), category (one of: '
                f'{", ".join(sorted(valid_categories))}), description (1-2 sentences).\n'
                f'If no coherent topic exists, respond with null.\n'
                f'Respond with only the JSON object or null, no other text.'
            )

            try:
                response_text = llm.chat([{'role': 'user', 'content': prompt}]).strip()
                response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
                response_text = re.sub(r'\s*```$', '', response_text)
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

