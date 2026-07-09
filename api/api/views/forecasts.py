"""Forecast API views — model-backed (event-fused symbol prediction).

Reads the latest ``Forecast`` rows per (symbol, horizon), plus a rolling accuracy/calibration
summary from scored forecasts. Forecasts are produced by ``run_forecast_task``; if none exist
yet (models not trained / no backfill), the list endpoints return an empty result set.
"""
import hashlib
import json
from datetime import timedelta

from django.core.cache import caches
from rest_framework.response import Response
from rest_framework.views import APIView

from core import models as core_models
from api.serializers import ForecastSerializer, ForecastAccuracySerializer

_ACCURACY_CACHE_TTL = 300  # seconds — scored forecasts change at most daily


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
    Rolling directional accuracy + Brier over scored forecasts, per horizon.
    Params: symbol, history=1 (weekly accuracy series per horizon),
    recent=N (last N scored forecasts, max 50).
    """

    def get(self, request):
        params = dict(sorted(request.query_params.items()))
        cache_key = 'api:forecasts:accuracy:' + hashlib.md5(json.dumps(params).encode()).hexdigest()
        cache = caches['redis-cache']
        if (cached := cache.get(cache_key)) is not None:
            return Response(cached)

        qs = core_models.Forecast.objects.filter(is_correct__isnull=False)
        if symbol := request.query_params.get('symbol'):
            qs = qs.filter(symbol=symbol)

        want_history = request.query_params.get('history') == '1'

        agg: dict[int, dict] = {}
        # horizon → ISO-week start date → {scored, correct}
        weekly: dict[int, dict[str, dict]] = {}
        for f in qs:
            a = agg.setdefault(f.horizon_days, {'scored': 0, 'correct': 0, 'sq_err': 0.0})
            a['scored'] += 1
            a['correct'] += 1 if f.is_correct else 0
            realized_up = 1.0 if (f.realized_change_pct or 0) > 0 else 0.0
            a['sq_err'] += (f.proba_up - realized_up) ** 2
            if want_history and f.as_of_date:
                d = f.as_of_date
                week = (d - timedelta(days=d.weekday())).date().isoformat()
                w = weekly.setdefault(f.horizon_days, {}).setdefault(week, {'scored': 0, 'correct': 0})
                w['scored'] += 1
                w['correct'] += 1 if f.is_correct else 0

        results = []
        for horizon, a in sorted(agg.items()):
            n = a['scored']
            row = {
                'horizon_days': horizon,
                'scored': n,
                'correct': a['correct'],
                'accuracy': round(a['correct'] / n, 4) if n else None,
                'brier': round(a['sq_err'] / n, 4) if n else None,
            }
            results.append(row)
        data = ForecastAccuracySerializer(results, many=True).data

        body = {'results': data, 'count': len(data)}

        if want_history:
            body['history'] = {
                str(horizon): [
                    {
                        'week': week,
                        'scored': w['scored'],
                        'accuracy': round(w['correct'] / w['scored'], 4) if w['scored'] else None,
                    }
                    for week, w in sorted(buckets.items())
                ]
                for horizon, buckets in weekly.items()
            }

        recent_n = request.query_params.get('recent')
        if recent_n:
            try:
                n = min(max(int(recent_n), 1), 50)
            except ValueError:
                n = 0
            if n:
                body['recent'] = ForecastSerializer(qs.order_by('-scored_at')[:n], many=True).data

        cache.set(cache_key, body, _ACCURACY_CACHE_TTL)
        return Response(body)
