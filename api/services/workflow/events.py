import logging
from collections import defaultdict
from datetime import datetime, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)


def aggregate_events(hours: int = 24, min_articles: int = 1) -> tuple[int, int]:
    """Group processed Articles by (location, category, day) into Events.
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

    skipped_no_location = Article.objects.filter(
        processed_on__isnull=False, location__isnull=True, published_on__gte=lookback,
    ).count()
    if skipped_no_location:
        logger.info(
            '[aggregate] %d processed article(s) in window have no location — excluded from '
            'events. Recover with process_articles(only_failed=True).', skipped_no_location,
        )

    if not articles:
        return 0, 0

    from services.processing.clustering import get_clusterer

    # Group by (city, country, category, calendar day) then semantic sub-cluster.
    buckets: dict[tuple[str, str, str, str], list] = defaultdict(list)
    for article in articles:
        llm = (article.extra_data or {}).get('llm', {})
        city = llm.get('city') or ''
        country = llm.get('country') or ''
        category_key = article.category or 'general'
        date_key = article.published_on.date().isoformat()
        buckets[(city, country, category_key, date_key)].append(article)

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
        corroboration_boost = min(len(group) / 10.0, 1.0) * 0.3
        avg_intensity = round(min((base_intensity or 0) + corroboration_boost, 1.0), 4) if base_intensity is not None else None

        started_at = min(a.published_on for a in group)
        latest_article_at = max(a.published_on for a in group)
        article_ids = [str(a.id) for a in group]
        source_codes = list({a.source_code for a in group})

        categories = [a.category for a in group if a.category]
        category = max(set(categories), key=categories.count) if categories else 'general'
        sub_categories = sorted({a.sub_category for a in group if a.sub_category})

        from services.forecasting.routing import route_event_to_weighted_symbols
        route_sentiment = avg_finbert_sentiment if avg_finbert_sentiment is not None else avg_sentiment
        affected_indicators = route_event_to_weighted_symbols(
            category, location, [], sub_categories, route_sentiment,
        )

        lats = [a.latitude for a in group if a.latitude is not None]
        lngs = [a.longitude for a in group if a.longitude is not None]
        lat = round(sum(lats) / len(lats), 6) if lats else representative.latitude
        lng = round(sum(lngs) / len(lngs), 6) if lngs else representative.longitude

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

        # Upsert on location_name + calendar day.
        # Use explicit datetime range — MongoDB backend does not support __date lookups.
        day_start = datetime(started_at.year, started_at.month, started_at.day, tzinfo=started_at.tzinfo)
        day_end = day_start + timedelta(days=1)

        event = Event.objects.filter(
            location_name=location,
            category=category,
            started_at__gte=day_start,
            started_at__lt=day_end,
        ).first()

        created = False
        if event is None:
            try:
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
                created = True
                created_count += 1
                logger.info(f'[aggregate] Created  {location} [{category}] — {len(group)} article(s)')
            except Exception:
                # Concurrent run already created this event — re-fetch and update below.
                event = Event.objects.filter(
                    location_name=location,
                    category=category,
                    started_at__gte=day_start,
                    started_at__lt=day_end,
                ).first()

        if not created and event is not None:
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
            event.translations = {**(event.translations or {}), **event_translations}
            event.save()
            updated_count += 1
            logger.info(f'[aggregate] Updated  {location} [{category}] — {len(group)} article(s)')

    return created_count, updated_count


def pipeline_coverage() -> list[dict]:
    """Per-stage count of records stuck at a step + a sample error.

    Returns one dict per stage: {stage, model, label, need, action, error_sample}
    where need is the number of records that reached the previous stage but not
    this one, and action is the dashboard Reprocess button's action key.
    """
    from core.models import Article, Event

    def _err_sample(model, stage):
        try:
            row = model.objects.filter(**{f'stage_status__{stage}__ok': False}).only('stage_status').first()
            if row:
                return ((row.stage_status or {}).get(stage) or {}).get('error')
        except Exception:  # noqa: BLE001 — nested JSON lookup may be unsupported
            pass
        return None

    out: list[dict] = []

    out.append({
        'stage': 'process', 'model': 'article', 'label': 'Unprocessed articles',
        'need': Article.objects.filter(processed_on__isnull=True).count(),
        'action': 'process', 'error_sample': None,
    })
    try:
        unlocated = (
            Article.objects.filter(processed_on__isnull=False, location__isnull=True).count()
            + Article.objects.filter(processed_on__isnull=False, location='').count()
        )
    except Exception:  # noqa: BLE001
        unlocated = Article.objects.filter(processed_on__isnull=False, location__isnull=True).count()
    out.append({
        'stage': 'geocode', 'model': 'article', 'label': 'Processed but un-located',
        'need': unlocated, 'action': 'reprocess_unlocated',
        'error_sample': _err_sample(Article, 'geocode'),
    })
    try:
        untagged = (
            Event.objects.filter(topics_source='').count()
            + Event.objects.filter(topics_source='keyword').count()
        )
    except Exception:  # noqa: BLE001
        untagged = Event.objects.filter(topics_source='').count()
    out.append({
        'stage': 'tag', 'model': 'event', 'label': 'Untagged / keyword-fallback events',
        'need': untagged, 'action': 'tag', 'error_sample': _err_sample(Event, 'tag'),
    })
    try:
        unrouted = Event.objects.filter(affected_indicators=[]).count()
    except Exception:  # noqa: BLE001
        unrouted = 0
    out.append({
        'stage': 'route', 'model': 'event', 'label': 'Unrouted events',
        'need': unrouted, 'action': 'route', 'error_sample': _err_sample(Event, 'route'),
    })
    return out
