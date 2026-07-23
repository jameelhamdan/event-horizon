"""Staff-only internal API — read already-ingested Article rows straight from
production Mongo, filtered by year/month/day. Lets eval/analysis tooling
(see .claude/skills/pipeline-eval-live) sample real, already-fetched-and-
annotated articles instead of live-fetching from RSS/Wayback/Wikipedia —
faster, no rate limits, and immune to upstream sites changing their markup.

Not part of the public map API surface: gated by IsAdminUser (Django admin
session auth — log into /admin/ first) rather than left open like the rest of
api/views/, since it exposes pipeline-internal fields (content, stage,
refined_by) the public ArticleSerializer deliberately omits.
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from core import models as core_models
from api.serializers import HistoricalArticleSerializer
from api.views.events import _envelope, _parse_int


class HistoricalArticleListView(APIView):
    """
    GET /api/internal/articles/historical/
    Query params:
      year   (required) int
      month  (required) int, 1-12
      day    (optional) int, 1-31 — narrows to one calendar day instead of the whole month
      source (optional) source_code, repeatable (?source=bbc-world&source=brookings)
      stage  (optional) fetched|annotated|refine|refined
      limit  (default 200, max 2000)

    Staff-only (IsAdminUser) — see module docstring.
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        try:
            year = int(request.query_params['year'])
            month = int(request.query_params['month'])
        except (KeyError, ValueError):
            return Response({'error': 'year and month are required integers'}, status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= month <= 12):
            return Response({'error': 'month must be 1-12'}, status=status.HTTP_400_BAD_REQUEST)

        day = request.query_params.get('day')
        if day is not None:
            try:
                day = int(day)
                start = datetime(year, month, day, tzinfo=dt_timezone.utc)
            except ValueError:
                return Response({'error': 'invalid day for that year/month'}, status=status.HTTP_400_BAD_REQUEST)
            end = start + timedelta(days=1)
        else:
            start = datetime(year, month, 1, tzinfo=dt_timezone.utc)
            end = datetime(year + 1, 1, 1, tzinfo=dt_timezone.utc) if month == 12 else datetime(year, month + 1, 1, tzinfo=dt_timezone.utc)

        # Explicit datetime range, not __date — see CLAUDE.md's Django/MongoDB notes.
        qs = core_models.Article.objects.filter(published_on__gte=start, published_on__lt=end)

        source_codes = request.query_params.getlist('source')
        if source_codes:
            qs = qs.filter(source_code__in=source_codes)

        if stage := request.query_params.get('stage'):
            qs = qs.filter(stage=stage)

        limit = _parse_int(request.query_params.get('limit'), 200, 2000)
        qs = qs.order_by('published_on')[:limit]
        data = _envelope(HistoricalArticleSerializer(qs, many=True).data, year=year, month=month, day=day)
        return Response(data)
