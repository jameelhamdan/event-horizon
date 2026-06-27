"""
Earthquake stream — USGS FDSN event API (global, no key required).

Only stores records not already in the DB (idempotent via usgs_id).
"""
import logging
from datetime import datetime, timezone

import requests
from django.conf import settings

from .base import BaseStream, redis_publish

logger = logging.getLogger(__name__)

USGS_URL = 'https://earthquake.usgs.gov/fdsnws/event/1/query'
HEADERS = {'User-Agent': f'Mozilla/5.0 (compatible; {settings.APP_NAME}/1.0)'}


class EarthquakeStream(BaseStream):
    stream_type = 'earthquake'

    def fetch(self) -> list[dict]:
        min_magnitude = float(getattr(settings, 'EARTHQUAKE_MIN_MAGNITUDE', '3.0'))
        params = {
            'format': 'geojson',
            'minmagnitude': min_magnitude,
            'orderby': 'time',
            'limit': 200,
        }
        try:
            resp = requests.get(USGS_URL, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(f'[earthquake] fetch failed: {exc}')
            return []

        records = []
        for feature in data.get('features', []):
            usgs_id = feature.get('id')
            if not usgs_id:
                continue
            props = feature.get('properties', {})
            coords = feature.get('geometry', {}).get('coordinates', [])
            lon, lat, depth = (coords + [None, None, None])[:3]
            occurred_ms = props.get('time')
            if occurred_ms:
                occurred_at = datetime.fromtimestamp(occurred_ms / 1000, tz=timezone.utc)
            else:
                continue
            records.append({
                'usgs_id': usgs_id,
                'magnitude': props.get('mag'),
                'magnitude_type': props.get('magType') or '',
                'depth_km': depth,
                'location_name': props.get('place') or '',
                'latitude': lat,
                'longitude': lon,
                'occurred_at': occurred_at,
                'tsunami_alert': bool(props.get('tsunami')),
                'alert_level': props.get('alert') or '',
            })
        return records

    def save(self, records: list[dict]) -> int:
        from core.models import EarthquakeRecord

        if not records:
            return 0

        existing_ids = set(
            EarthquakeRecord.objects.filter(
                usgs_id__in=[r['usgs_id'] for r in records]
            ).values_list('usgs_id', flat=True)
        )

        new = [
            EarthquakeRecord(**r)
            for r in records
            if r['usgs_id'] not in existing_ids
            and r.get('latitude') is not None
            and r.get('longitude') is not None
            and r.get('magnitude') is not None
        ]
        if new:
            # ignore_conflicts guards against two concurrent fetch_earthquakes_task
            # workers racing on the same USGS page and trying to insert the same usgs_id.
            EarthquakeRecord.objects.bulk_create(new, ignore_conflicts=True)
            redis_publish('sse:earthquakes', {
                'type': 'earthquake_update',
                'new_count': len(new),
                'max_magnitude': max(e.magnitude for e in new),
            })

        return len(new)
