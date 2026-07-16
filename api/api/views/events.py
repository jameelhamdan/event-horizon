"""API views — events, sources, prices, NOTAMs, earthquakes, static points, SSE."""
import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

import redis.asyncio as aioredis
from django.core.cache import caches
from django.http import StreamingHttpResponse
from django.views import View
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core import models as core_models
from api.serializers import (
    ArticleSerializer, EventSerializer, SourceSerializer,
    PriceTickSerializer, PriceBarSerializer, NotamZoneSerializer, NotamRecordSerializer,
    EarthquakeRecordSerializer, StaticPointSerializer,
    TopicSerializer, MarketSymbolSerializer,
)

_CACHE_TTL = 30  # seconds


def _redis_cache():
    return caches['redis-cache']


def _build_source_map() -> dict[str, str]:
    """{source_code: name} — cached briefly in Redis. Sources change rarely, so a
    short stale window is fine, and this spares every uncached caller (TopicEvents,
    PriceLatest) a full Source scan per request."""
    cache = _redis_cache()
    if (cached := cache.get('api:sources:map')) is not None:
        return cached
    source_map = {s.code: s.name for s in core_models.Source.objects.only('code', 'name')}
    cache.set('api:sources:map', source_map, 60)
    return source_map


def _parse_bool_param(value: str | None, default: bool = True) -> bool | None:
    """Parse a query param string to True/False/None (None = 'all', no filter)."""
    if value is None:
        return default
    low = value.lower()
    if low == 'true':
        return True
    if low == 'false':
        return False
    return None  # 'all' or unrecognised → no filter


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc)


def _parse_int(value: str | None, default: int, max_value: int | None = None) -> int:
    try:
        result = int(value) if value is not None else default
    except (ValueError, TypeError):
        result = default
    return min(result, max_value) if max_value is not None else result


def _apply_date_range(qs, field: str, request, start_param: str = 'start', end_param: str = 'end'):
    """Filter ``qs`` by ``<field>__gte``/``__lte`` from the given query params.

    Returns (filtered_qs, None) on success, or (None, error_response) if either
    param fails to parse — caller should ``return`` the error response as-is.
    """
    if start := request.query_params.get(start_param):
        try:
            qs = qs.filter(**{f'{field}__gte': _parse_dt(start)})
        except ValueError:
            return None, Response({'error': f'Invalid {start_param} date'}, status=status.HTTP_400_BAD_REQUEST)

    if end := request.query_params.get(end_param):
        try:
            qs = qs.filter(**{f'{field}__lte': _parse_dt(end)})
        except ValueError:
            return None, Response({'error': f'Invalid {end_param} date'}, status=status.HTTP_400_BAD_REQUEST)

    return qs, None


def _envelope(results, **extra) -> dict:
    """Standard {'results': ..., 'count': ...} response body, plus any extra keys."""
    return {'results': results, 'count': len(results), **extra}


class EventListView(APIView):
    """
    GET /api/events/
    Query params: category, topic, symbol, start, end, limit (max 500),
    bbox (lat_min,lng_min,lat_max,lng_max)
    """

    def get(self, request):
        params = dict(sorted(request.query_params.items()))
        cache_key = 'api:events:list:' + hashlib.md5(json.dumps(params).encode()).hexdigest()
        cache = _redis_cache()
        if (cached := cache.get(cache_key)) is not None:
            return Response(cached)

        qs = core_models.Event.objects.all()

        if category := request.query_params.get('category'):
            qs = qs.filter(category=category)

        if topic_slug := request.query_params.get('topic'):
            qs = qs.filter(topic_slugs=topic_slug)

        # Events routed to a given market symbol (affected_indicators is a list of
        # {symbol, weight} dicts; the key transform compiles to the Mongo dot-path
        # 'affected_indicators.symbol', which matches inside arrays).
        symbol = request.query_params.get('symbol')
        if symbol:
            qs = qs.filter(affected_indicators__symbol=symbol)

        qs, err = _apply_date_range(qs, 'started_at', request)
        if err:
            return err

        if bbox := request.query_params.get('bbox'):
            try:
                lat_min, lng_min, lat_max, lng_max = (float(v) for v in bbox.split(','))
                qs = qs.filter(
                    latitude__gte=lat_min, latitude__lte=lat_max,
                    longitude__gte=lng_min, longitude__lte=lng_max,
                )
            except (ValueError, TypeError):
                return Response(
                    {'error': 'Invalid bbox. Use: lat_min,lng_min,lat_max,lng_max'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        limit = _parse_int(request.query_params.get('limit'), 100, 500)
        source_map = _build_source_map()
        data = _envelope(EventSerializer(qs[:limit], many=True, context={'source_map': source_map}).data)
        cache.set(cache_key, data, _CACHE_TTL)
        return Response(data)


class EventDetailView(APIView):
    """GET /api/events/<id>/"""

    def get(self, request, event_id):
        cache_key = f'api:events:detail:{event_id}'
        cache = _redis_cache()
        if (cached := cache.get(cache_key)) is not None:
            return Response(cached)

        try:
            event = core_models.Event.objects.get(pk=event_id)
        except core_models.Event.DoesNotExist:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        article_uuids = []
        for raw_id in event.article_ids:
            try:
                article_uuids.append(uuid.UUID(str(raw_id)))
            except (ValueError, AttributeError):
                pass

        articles = core_models.Article.objects.filter(id__in=article_uuids)
        source_map = _build_source_map()
        data = EventSerializer(event, context={'source_map': source_map}).data
        data['articles'] = ArticleSerializer(articles, many=True).data
        cache.set(cache_key, data, _CACHE_TTL)
        return Response(data)


class SourceListView(APIView):
    """GET /api/sources/"""

    def get(self, request):
        cache = _redis_cache()
        if (cached := cache.get('api:sources:list')) is not None:
            return Response(cached)
        data = {'results': SourceSerializer(core_models.Source.objects.all(), many=True).data}
        cache.set('api:sources:list', data, _CACHE_TTL)
        return Response(data)


class PriceLatestView(APIView):
    """GET /api/prices/latest/ — most recent tick per symbol; query param: stream_key"""

    def get(self, request):
        stream_key = request.query_params.get('stream_key')
        cache_key = f'api:prices:latest:{stream_key or "all"}'
        cache = _redis_cache()
        if (cached := cache.get(cache_key)) is not None:
            return Response(cached)

        # Drive the lookup off the curated symbol set instead of walking the whole
        # PriceTick collection: one indexed ('symbol', 'occurred_at') lookup per
        # symbol via the model's -occurred_at default ordering.
        symbols_qs = core_models.MarketSymbol.objects.filter(is_active=True)
        if stream_key:
            symbols_qs = symbols_qs.filter(stream_key=stream_key)

        latest = []
        for symbol in symbols_qs.values_list('symbol', flat=True):
            tick = core_models.PriceTick.objects.filter(symbol=symbol).first()
            if tick is not None:
                latest.append(tick)

        data = {'results': PriceTickSerializer(latest, many=True).data}
        cache.set(cache_key, data, _CACHE_TTL)
        return Response(data)


class PriceHistoryView(APIView):
    """GET /api/prices/<symbol>/ — query params: from, to, limit (max 5000)"""

    def get(self, request, symbol):
        qs = core_models.PriceTick.objects.filter(symbol=symbol)
        now = datetime.now(tz=dt_timezone.utc)

        try:
            start = _parse_dt(raw) if (raw := request.query_params.get('from')) else now - timedelta(hours=24)
        except ValueError:
            return Response({'error': 'Invalid from date'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            end = _parse_dt(raw) if (raw := request.query_params.get('to')) else now
        except ValueError:
            return Response({'error': 'Invalid to date'}, status=status.HTTP_400_BAD_REQUEST)

        qs = qs.filter(occurred_at__gte=start, occurred_at__lte=end)
        limit = _parse_int(request.query_params.get('limit'), 500, 5000)
        serializer = PriceTickSerializer(qs[:limit], many=True)
        return Response(_envelope(serializer.data, symbol=symbol))


class PriceBarsView(APIView):
    """GET /api/prices/<symbol>/bars/ — daily OHLC history. Params: interval (1d), limit (max 5000)."""

    def get(self, request, symbol):
        interval = request.query_params.get('interval', '1d')
        qs = core_models.PriceBar.objects.filter(symbol=symbol, interval=interval).order_by('date')
        limit = _parse_int(request.query_params.get('limit'), 1000, 5000)
        # Take the most recent `limit` bars but return them oldest→newest for charting.
        total = qs.count()
        start = max(total - limit, 0)
        bars = list(qs[start:total])
        serializer = PriceBarSerializer(bars, many=True)
        return Response(_envelope(serializer.data, symbol=symbol, interval=interval))


class NotamZoneListView(APIView):
    """GET /api/notams/ — query params: active (true/false/all), country_code, notam_type"""

    def get(self, request):
        qs = core_models.NotamZone.objects.all()
        if (active := _parse_bool_param(request.query_params.get('active'))) is not None:
            qs = qs.filter(is_active=active)

        if country_code := request.query_params.get('country_code'):
            qs = qs.filter(country_code__iexact=country_code)
        if notam_type := request.query_params.get('notam_type'):
            qs = qs.filter(notam_type__iexact=notam_type)

        serializer = NotamZoneSerializer(qs[:1000], many=True)
        return Response(_envelope(serializer.data))


class NotamHistoryView(APIView):
    """GET /api/notams/history/ — query params: from, to, country_code, status, limit"""

    def get(self, request):
        qs = core_models.NotamRecord.objects.all()

        if country_code := request.query_params.get('country_code'):
            qs = qs.filter(country_code__iexact=country_code)
        if notam_status := request.query_params.get('status'):
            qs = qs.filter(status=notam_status)

        qs, err = _apply_date_range(qs, 'effective_from', request, start_param='from', end_param='to')
        if err:
            return err

        limit = _parse_int(request.query_params.get('limit'), 200, 2000)
        serializer = NotamRecordSerializer(qs[:limit], many=True)
        return Response(_envelope(serializer.data))


class EarthquakeListView(APIView):
    """GET /api/earthquakes/ — query params: min_magnitude, hours, limit"""

    def get(self, request):
        try:
            min_mag = float(request.query_params.get('min_magnitude', '3.0'))
        except ValueError:
            min_mag = 3.0

        hours = _parse_int(request.query_params.get('hours'), 24)
        limit = _parse_int(request.query_params.get('limit'), 200, 2000)
        cutoff = datetime.now(tz=dt_timezone.utc) - timedelta(hours=hours)

        qs = core_models.EarthquakeRecord.objects.filter(
            magnitude__gte=min_mag,
            occurred_at__gte=cutoff,
        )
        serializer = EarthquakeRecordSerializer(qs[:limit], many=True)
        return Response(_envelope(serializer.data))


class StaticPointListView(APIView):
    """GET /api/static-points/ — query params: type, country_code"""

    def get(self, request):
        qs = core_models.StaticPoint.objects.filter(is_active=True)

        if point_type := request.query_params.get('type'):
            qs = qs.filter(point_type=point_type)
        if country_code := request.query_params.get('country_code'):
            qs = qs.filter(country_code__iexact=country_code)

        serializer = StaticPointSerializer(qs, many=True)
        return Response(_envelope(serializer.data))


class SymbolListView(APIView):
    """GET /api/symbols/ — the curated MarketSymbol panel.

    Query params: group, stream_key, forecast (true/false), popular (true/false),
    active (true default / false / all).
    """

    def get(self, request):
        qs = core_models.MarketSymbol.objects.all()
        if (active := _parse_bool_param(request.query_params.get('active'))) is not None:
            qs = qs.filter(is_active=active)
        if group := request.query_params.get('group'):
            qs = qs.filter(group=group)
        if stream_key := request.query_params.get('stream_key'):
            qs = qs.filter(stream_key=stream_key)
        if (forecast := _parse_bool_param(request.query_params.get('forecast'), default=None)) is not None:
            qs = qs.filter(is_forecast=forecast)
        if (popular := _parse_bool_param(request.query_params.get('popular'), default=None)) is not None:
            qs = qs.filter(is_popular=popular)
        serializer = MarketSymbolSerializer(qs, many=True)
        return Response(_envelope(serializer.data))


class TopicListView(APIView):
    """
    GET /api/topics/

    Query params:
      active    true (default) | false | all
      current   true | false | all — filter by is_current flag
      category  EventCategory slug
      date      YYYY-MM-DD — topics active on that date
      parent    parent topic slug — returns sub-topics of that topic
      source    source_id string — topics confirmed by a specific adapter
      month     1-12 — historical topics for that calendar month
      year      integer — historical topics for that year
    """

    def get(self, request):
        from django.db.models import Q
        qs = core_models.Topic.objects.all()

        if (active := _parse_bool_param(request.query_params.get('active'))) is not None:
            qs = qs.filter(is_active=active)
        if (current := _parse_bool_param(request.query_params.get('current'), default=None)) is not None:
            qs = qs.filter(is_current=current)
        if (top_level := _parse_bool_param(request.query_params.get('top_level'), default=None)) is not None:
            qs = qs.filter(is_top_level=top_level)

        if category := request.query_params.get('category'):
            qs = qs.filter(category=category)

        if parent := request.query_params.get('parent'):
            qs = qs.filter(parent_slug=parent)

        # Temporal date filter
        if date_str := request.query_params.get('date'):
            try:
                from datetime import timezone as dt_tz
                dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=dt_tz.utc)
                qs = qs.filter(
                    Q(started_at__lte=dt) | Q(started_at__isnull=True)
                ).filter(
                    Q(ended_at__gte=dt) | Q(ended_at__isnull=True)
                )
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use YYYY-MM-DD.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Historical calendar filters
        if month_str := request.query_params.get('month'):
            try:
                qs = qs.filter(historical_month=int(month_str))
            except (ValueError, TypeError):
                return Response({'error': 'Invalid month. Use 1-12.'}, status=status.HTTP_400_BAD_REQUEST)

        if year_str := request.query_params.get('year'):
            try:
                qs = qs.filter(historical_year=int(year_str))
            except (ValueError, TypeError):
                return Response({'error': 'Invalid year.'}, status=status.HTTP_400_BAD_REQUEST)

        limit = _parse_int(request.query_params.get('limit'), 200, 1000)
        topics = list(qs[:limit])

        # source filter — applied in Python (JSONField list contains check)
        if source_id := request.query_params.get('source'):
            topics = [t for t in topics if source_id in (t.source_ids or [])]

        return Response(_envelope(TopicSerializer(topics, many=True).data))


class TopicDetailView(APIView):
    """GET /api/topics/<slug>/"""

    def get(self, request, slug):
        try:
            topic = core_models.Topic.objects.get(slug=slug)
        except core_models.Topic.DoesNotExist:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(TopicSerializer(topic).data)


class TopicEventsView(APIView):
    """
    GET /api/topics/<slug>/events/

    Query params: start, end, limit (max 200, default 50)
    """

    def get(self, request, slug):
        if not core_models.Topic.objects.filter(slug=slug).exists():
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        qs = core_models.Event.objects.filter(topic_slugs=slug)

        qs, err = _apply_date_range(qs, 'started_at', request)
        if err:
            return err

        limit = _parse_int(request.query_params.get('limit'), 50, 200)
        source_map = _build_source_map()
        data = _envelope(
            EventSerializer(qs[:limit], many=True, context={'source_map': source_map}).data,
            topic=slug,
        )
        return Response(data)


class SSEStreamView(View):
    """GET /api/sse/ — async Server-Sent Events from Redis pub/sub"""

    SSE_CHANNELS = ('sse:stream', 'sse:prices', 'sse:notams', 'sse:earthquakes')

    async def get(self, request):
        redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

        async def event_stream():
            r = aioredis.from_url(redis_url)
            pubsub = r.pubsub()
            await pubsub.subscribe(*self.SSE_CHANNELS)
            yield 'data: {"type":"connected"}\n\n'
            try:
                async for message in pubsub.listen():
                    if message['type'] == 'message':
                        raw = message['data']
                        payload = raw.decode() if isinstance(raw, bytes) else raw
                        yield f'data: {payload}\n\n'
            except (asyncio.CancelledError, GeneratorExit):
                pass
            finally:
                await pubsub.unsubscribe(*self.SSE_CHANNELS)
                await r.aclose()

        response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        response['Access-Control-Allow-Origin'] = '*'
        return response
