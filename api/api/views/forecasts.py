"""Forecast API views — PLACEHOLDER.

The market-forecasting prediction layer was removed and is being reworked from
scratch. These views return a neutral / zero-diff forecast per symbol so the UI
forecast surface keeps working in the meantime. Nothing is stored: each result is
synthesized from the most recent ``PriceTick`` for the symbol.
"""

from datetime import datetime, timezone as dt_timezone

from rest_framework.response import Response
from rest_framework.views import APIView

from core import models as core_models
from api.serializers import ForecastSerializer

PLACEHOLDER_HORIZON_HOURS = 24


class ForecastLatestView(APIView):
    """
    GET /api/forecasts/latest/
    Placeholder: one neutral (0% predicted change) forecast per symbol, built from
    the latest price tick. Query params: stream_key, symbol.
    """

    def get(self, request):
        qs = core_models.PriceTick.objects.all()
        if stream_key := request.query_params.get('stream_key'):
            qs = qs.filter(stream_key=stream_key)
        if symbol := request.query_params.get('symbol'):
            qs = qs.filter(symbol=symbol)

        now = datetime.now(dt_timezone.utc)
        seen: set[str] = set()
        results: list[dict] = []
        # qs uses the model's default ordering (-occurred_at), so the first tick seen
        # for a symbol is its most recent one.
        for tick in qs:
            if tick.symbol in seen:
                continue
            seen.add(tick.symbol)
            results.append({
                'symbol': tick.symbol,
                'stream_key': tick.stream_key,
                'generated_at': now,
                'horizon_hours': PLACEHOLDER_HORIZON_HOURS,
                'direction': 'neutral',
                'predicted_change_pct': 0.0,
                'current_value': tick.value,
                'placeholder': True,
            })
            if len(seen) >= 200:
                break

        data = ForecastSerializer(results, many=True).data
        return Response({'results': data, 'count': len(data)})


class ForecastListView(ForecastLatestView):
    """GET /api/forecasts/ — placeholder; same neutral payload as /latest/."""
