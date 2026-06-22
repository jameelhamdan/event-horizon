"""
NOTAM stream — aviationweather.gov (global ICAO coverage, no key required).

Upserts NotamZone (live state) and appends NotamRecord (history) for new NOTAMs.
"""
import logging
import math
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.utils import timezone as dj_timezone

from .base import BaseStream, redis_publish

logger = logging.getLogger(__name__)

NOTAM_URL = 'https://aviationweather.gov/api/data/notam'
NOTAM_PARAMS = {
    'format': 'json',
    'bbox': '-180,-90,180,90',  # global
    'featureType': 'notam',
}
HEADERS = {'User-Agent': f'Mozilla/5.0 (compatible; {settings.APP_NAME}/1.0)'}


def _circle_to_polygon(lat: float, lon: float, radius_nm: float, points: int = 32) -> dict:
    """Approximate a lat/lon circle (nautical miles radius) as a GeoJSON Polygon."""
    radius_deg = radius_nm / 60.0  # 1 NM ≈ 1 arc-minute
    coords = []
    for i in range(points + 1):
        angle = math.radians(i * 360 / points)
        coords.append([
            round(lon + radius_deg * math.cos(angle), 6),
            round(lat + radius_deg * math.sin(angle), 6),
        ])
    return {'type': 'Polygon', 'coordinates': [coords]}


def _parse_geometry(feature: dict) -> dict:
    """Extract or construct a GeoJSON geometry from a NOTAM feature."""
    geom = feature.get('geometry')
    if geom:
        return geom
    props = feature.get('properties', {})
    lat = props.get('latitude') or props.get('lat')
    lon = props.get('longitude') or props.get('lon')
    radius = props.get('radius')
    if lat and lon and radius:
        return _circle_to_polygon(float(lat), float(lon), float(radius))
    if lat and lon:
        return {'type': 'Point', 'coordinates': [float(lon), float(lat)]}
    return {}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S', '%Y%m%d%H%M%S'):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _normalize(feature: dict) -> dict | None:
    """Normalize one GeoJSON feature from aviationweather.gov into our schema."""
    props = feature.get('properties', {})
    notam_id = props.get('notamID') or props.get('id') or feature.get('id')
    if not notam_id:
        return None

    effective_from = _parse_dt(props.get('startDate') or props.get('effectiveStart'))
    effective_to = _parse_dt(props.get('endDate') or props.get('effectiveEnd'))

    now = dj_timezone.now()
    if effective_to and effective_to < now:
        status = 'expired'
    else:
        status = 'active'

    return {
        'notam_id': str(notam_id),
        'source_region': 'ICAO',
        'notam_type': props.get('classification') or props.get('type') or 'general',
        'status': status,
        'effective_from': effective_from or now,
        'effective_to': effective_to,
        'geometry': {'type': 'Feature', 'geometry': _parse_geometry(feature), 'properties': {}},
        'altitude_min_ft': props.get('lowerLimit'),
        'altitude_max_ft': props.get('upperLimit'),
        'location_name': props.get('location') or props.get('icaoLocation') or '',
        'country_code': (props.get('icaoLocation') or '')[:2],
        'raw_text': props.get('notamText') or props.get('text') or '',
    }


class NotamStream(BaseStream):
    stream_type = 'notam'

    def fetch(self) -> list[dict]:
        try:
            resp = requests.get(NOTAM_URL, params=NOTAM_PARAMS, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(f'[notam] fetch failed: {exc}')
            return []

        # A2 schema validation: surface drift on this undocumented API loudly.
        if not isinstance(data, list) and 'features' not in data:
            logger.warning('[notam] response missing "features" — possible schema drift '
                           '(keys=%s)', sorted(data)[:8] if isinstance(data, dict) else type(data).__name__)
        features = data if isinstance(data, list) else data.get('features', [])
        records = []
        for feature in features:
            normalized = _normalize(feature)
            if normalized:
                records.append(normalized)
        if features and not records:
            logger.warning('[notam] %d features but none normalized — possible schema drift '
                           '(property names changed)', len(features))
        return records

    def save(self, records: list[dict]) -> int:
        from core.models import NotamRecord, NotamZone

        all_ids = [r['notam_id'] for r in records]

        existing_ids = set(
            NotamRecord.objects.filter(notam_id__in=all_ids).values_list('notam_id', flat=True)
        )

        new_records = [r for r in records if r['notam_id'] not in existing_ids]

        # Append to history (new NOTAMs only)
        NotamRecord.objects.bulk_create([NotamRecord(**r) for r in new_records])

        # Upsert live zone state for all fetched NOTAMs
        active_ids = []
        for r in records:
            is_active = r['status'] == 'active'
            NotamZone.objects.update_or_create(
                notam_id=r['notam_id'],
                defaults={
                    'notam_type': r['notam_type'],
                    'geometry': r['geometry'],
                    'effective_from': r['effective_from'],
                    'effective_to': r['effective_to'],
                    'is_active': is_active,
                    'location_name': r['location_name'],
                    'country_code': r['country_code'],
                    'altitude_min_ft': r['altitude_min_ft'],
                    'altitude_max_ft': r['altitude_max_ft'],
                },
            )
            if is_active:
                active_ids.append(r['notam_id'])

        # Deactivate zones not seen in this fetch
        NotamZone.objects.exclude(notam_id__in=all_ids).update(is_active=False)

        redis_publish('sse:notams', {
            'type': 'notam_update',
            'active_count': len(active_ids),
            'new_count': len(new_records),
        })

        return len(new_records)
