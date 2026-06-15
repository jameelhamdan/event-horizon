"""Forecast API views."""

from datetime import datetime, timedelta, timezone as dt_timezone

from rest_framework.response import Response
from rest_framework.views import APIView

from core import models as core_models
from api.serializers import ForecastSerializer


def _parse_int(value, default: int, max_value: int | None = None) -> int:
    try:
        result = int(value) if value is not None else default
    except (ValueError, TypeError):
        result = default
    return min(result, max_value) if max_value is not None else result


class ForecastListView(APIView):
    """
    GET /api/forecasts/
    Query params: symbol, stream_key, horizon (hours, default 4), limit (max 200, default 20)
    """
    def get(self, request):
        qs = core_models.Forecast.objects.all()

        if symbol := request.query_params.get('symbol'):
            qs = qs.filter(symbol=symbol)

        if stream_key := request.query_params.get('stream_key'):
            qs = qs.filter(stream_key=stream_key)

        if horizon := request.query_params.get('horizon'):
            try:
                qs = qs.filter(horizon_hours=int(horizon))
            except (ValueError, TypeError):
                pass

        limit = _parse_int(request.query_params.get('limit'), 20, 200)
        data = {'results': ForecastSerializer(qs[:limit], many=True).data}
        data['count'] = len(data['results'])
        return Response(data)


class ForecastLatestView(APIView):
    """
    GET /api/forecasts/latest/
    Returns the most recent forecast per (symbol, horizon).
    Multi-horizon (1h/1d/1w): each symbol yields one row per applicable horizon,
    so deduping on symbol alone would silently drop horizons.
    Query params: stream_key, symbol
    """

    def get(self, request):
        qs = core_models.Forecast.objects.all()

        if stream_key := request.query_params.get('stream_key'):
            qs = qs.filter(stream_key=stream_key)
        if symbol := request.query_params.get('symbol'):
            qs = qs.filter(symbol=symbol)

        # qs is ordered by -generated_at (model Meta), so the first time we see a
        # (symbol, horizon) pair it is the most recent forecast for that pair.
        seen: set[tuple[str, int]] = set()
        latest: list = []
        for fc in qs:
            key = (fc.symbol, fc.horizon_hours)
            if key not in seen:
                seen.add(key)
                latest.append(fc)
            if len(latest) >= 150:
                break

        data = {'results': ForecastSerializer(latest, many=True).data}
        data['count'] = len(data['results'])
        return Response(data)
