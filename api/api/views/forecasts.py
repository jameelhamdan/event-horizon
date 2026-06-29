"""Forecast API views — model-backed (event-fused symbol prediction).

Reads the latest ``Forecast`` rows per (symbol, horizon), plus a rolling accuracy/calibration
summary from scored forecasts. Forecasts are produced by ``run_forecast_task``; if none exist
yet (models not trained / no backfill), the list endpoints return an empty result set.
"""
from rest_framework.response import Response
from rest_framework.views import APIView

from core import models as core_models
from api.serializers import ForecastSerializer, ForecastAccuracySerializer


def _latest_per_symbol_horizon(qs, limit=400):
    """qs ordered by -generated_at → keep the newest row per (symbol, horizon)."""
    seen, out = set(), []
    for f in qs:
        key = (f.symbol, f.horizon_days)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
        if len(out) >= limit:
            break
    return out


class ForecastLatestView(APIView):
    """
    GET /api/forecasts/latest/
    Latest forecast per (symbol, horizon). Params: symbol, stream_key, horizon (1|5).
    """

    def get(self, request):
        qs = core_models.Forecast.objects.all()
        if symbol := request.query_params.get('symbol'):
            qs = qs.filter(symbol=symbol)
        if stream_key := request.query_params.get('stream_key'):
            qs = qs.filter(stream_key=stream_key)
        if horizon := request.query_params.get('horizon'):
            try:
                qs = qs.filter(horizon_days=int(horizon))
            except ValueError:
                pass

        latest = _latest_per_symbol_horizon(qs)  # qs default order = -generated_at
        data = ForecastSerializer(latest, many=True).data
        return Response({'results': data, 'count': len(data)})


class ForecastAccuracyView(APIView):
    """
    GET /api/forecasts/accuracy/
    Rolling directional accuracy + Brier over scored forecasts, per horizon. Param: symbol.
    """

    def get(self, request):
        qs = core_models.Forecast.objects.filter(is_correct__isnull=False)
        if symbol := request.query_params.get('symbol'):
            qs = qs.filter(symbol=symbol)

        agg: dict[int, dict] = {}
        for f in qs:
            a = agg.setdefault(f.horizon_days, {'scored': 0, 'correct': 0, 'sq_err': 0.0})
            a['scored'] += 1
            a['correct'] += 1 if f.is_correct else 0
            realized_up = 1.0 if (f.realized_change_pct or 0) > 0 else 0.0
            a['sq_err'] += (f.proba_up - realized_up) ** 2

        results = []
        for horizon, a in sorted(agg.items()):
            n = a['scored']
            results.append({
                'horizon_days': horizon,
                'scored': n,
                'correct': a['correct'],
                'accuracy': round(a['correct'] / n, 4) if n else None,
                'brier': round(a['sq_err'] / n, 4) if n else None,
            })
        data = ForecastAccuracySerializer(results, many=True).data
        return Response({'results': data, 'count': len(data)})
