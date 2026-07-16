"""NOTAM stream — keyless FAA NOTAM Search (notams.aim.faa.gov/notamSearch).

aviationweather.gov dropped NOTAMs (Jul 2026), and the FAA's official NOTAM API
(external-api.faa.gov) requires registered client credentials. The backend the
FAA's own public web app calls needs no key — only a browser-like User-Agent —
and serves both US (K/P) and international ICAO locations with ``formatType=ICAO``.

It is per-location (no global bbox), so we poll a curated set of major world
airports (``AIRPORTS``) concurrently and union the results — representative global
coverage for the map layer without any registration. Coordinates come from each
NOTAM's ICAO Q-line (``…/lower/upper/DDMM[NS]DDDMM[EW]RRR``); the few NOTAMs with
no parseable Q-line fall back to the polled airport's coordinates.

Upserts NotamZone (live state) and appends NotamRecord (history) for new NOTAMs.
"""
import logging
import math
import random
import re
import time
from datetime import datetime, timezone

import requests
from django.utils import timezone as dj_timezone

from .base import BaseStream, redis_publish

logger = logging.getLogger(__name__)

SEARCH_URL = 'https://notams.aim.faa.gov/notamSearch/search'
# The endpoint sits behind Akamai, which bot-blocks (HTTP 403) a client that
# bursts. We poll sequentially with jittered pacing and bail out early once it
# starts refusing every request, so a flagged run stops hammering (which only
# deepens the block) and surfaces as a FAILED TaskRun instead.
_PACE_SECONDS = (0.25, 0.6)          # jittered sleep between location queries
_MAX_CONSECUTIVE_FAILURES = 4        # stop early — the source is blocking us
_PER_QUERY_RETRIES = 1               # one retry (short backoff) per location

# The endpoint hangs/blocks for non-browser User-Agents, so send the same headers
# the FAA's public web app does. No credentials are involved.
_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'Referer': 'https://notams.aim.faa.gov/notamSearch/nsapp.html',
    'X-Requested-With': 'XMLHttpRequest',
}

# Curated global airport panel — ICAO -> (ISO-2 country, lat, lon). The lat/lon is
# the fallback location for a NOTAM whose Q-line has no parseable coordinate.
AIRPORTS: dict[str, tuple[str, float, float]] = {
    'KJFK': ('US', 40.64, -73.78), 'KLAX': ('US', 33.94, -118.41),
    'KORD': ('US', 41.98, -87.90), 'KATL': ('US', 33.64, -84.43),
    'KSFO': ('US', 37.62, -122.38), 'KDFW': ('US', 32.90, -97.04),
    'KMIA': ('US', 25.79, -80.29), 'PHNL': ('US', 21.32, -157.92),
    'CYYZ': ('CA', 43.68, -79.63), 'MMMX': ('MX', 19.44, -99.07),
    'SBGR': ('BR', -23.43, -46.47), 'SAEZ': ('AR', -34.82, -58.54),
    'EGLL': ('GB', 51.47, -0.45), 'EGKK': ('GB', 51.15, -0.19),
    'LFPG': ('FR', 49.01, 2.55), 'EDDF': ('DE', 50.03, 8.56),
    'EHAM': ('NL', 52.31, 4.76), 'LEMD': ('ES', 40.47, -3.57),
    'LIRF': ('IT', 41.80, 12.25), 'LTFM': ('TR', 41.26, 28.74),
    'UUEE': ('RU', 55.97, 37.41), 'EFHK': ('FI', 60.32, 24.96),
    'OMDB': ('AE', 25.25, 55.36), 'OTHH': ('QA', 25.27, 51.61),
    'OERK': ('SA', 24.96, 46.70), 'LLBG': ('IL', 32.01, 34.89),
    'HECA': ('EG', 30.11, 31.41), 'FAOR': ('ZA', -26.13, 28.24),
    'HKJK': ('KE', -1.32, 36.93), 'DNMM': ('NG', 6.58, 3.32),
    'VIDP': ('IN', 28.57, 77.10), 'VABB': ('IN', 19.09, 72.87),
    'VTBS': ('TH', 13.69, 100.75), 'WSSS': ('SG', 1.36, 103.99),
    'WMKK': ('MY', 2.75, 101.71), 'VHHH': ('HK', 22.31, 113.91),
    'ZBAA': ('CN', 40.08, 116.58), 'ZSPD': ('CN', 31.14, 121.81),
    'RJTT': ('JP', 35.55, 139.78), 'RJAA': ('JP', 35.76, 140.39),
    'RKSI': ('KR', 37.46, 126.44), 'YSSY': ('AU', -33.95, 151.18),
    'YMML': ('AU', -37.67, 144.84), 'NZAA': ('NZ', -37.01, 174.79),
}

_Q_LINE = re.compile(r'Q\)\s*([^\r\n]+)')
# Trailing token of a Q-line: DDMM[NS]DDDMM[EW]RRR (lat°min, lon°min, radius NM).
_Q_COORD = re.compile(r'(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])(\d{3})\s*$')


def _circle_to_polygon(lat: float, lon: float, radius_nm: float, points: int = 32) -> dict:
    """Approximate a lat/lon circle (nautical-mile radius) as a GeoJSON Polygon."""
    radius_deg = max(radius_nm, 1) / 60.0  # 1 NM ≈ 1 arc-minute
    coords = []
    for i in range(points + 1):
        angle = math.radians(i * 360 / points)
        coords.append([
            round(lon + radius_deg * math.cos(angle), 6),
            round(lat + radius_deg * math.sin(angle), 6),
        ])
    return {'type': 'Polygon', 'coordinates': [coords]}


def _parse_qline(icao_msg: str | None) -> dict | None:
    """Pull (lat, lon, radius_nm, lower_ft, upper_ft) out of a NOTAM ICAO Q-line.

    Returns None if the line is missing or has no parseable coordinate token.
    """
    m = _Q_LINE.search(icao_msg or '')
    if not m:
        return None
    fields = [f.strip() for f in m.group(1).split('/')]
    coord = _Q_COORD.search(fields[-1]) if fields else None
    if not coord:
        return None
    la_d, la_m, ns, lo_d, lo_m, ew, rad = coord.groups()
    lat = int(la_d) + int(la_m) / 60.0
    lon = int(lo_d) + int(lo_m) / 60.0
    if ns == 'S':
        lat = -lat
    if ew == 'W':
        lon = -lon
    # Two 3-digit flight-level fields precede the coord: lower/upper (FL → ×100 ft).
    lower_ft = upper_ft = None
    if len(fields) >= 3 and fields[-3].isdigit() and fields[-2].isdigit():
        lower_ft = int(fields[-3]) * 100
        upper_ft = int(fields[-2]) * 100
    return {
        'lat': round(lat, 6), 'lon': round(lon, 6), 'radius_nm': int(rad),
        'lower_ft': lower_ft, 'upper_ft': upper_ft,
    }


def _parse_faa_dt(value: str | None) -> datetime | None:
    """Parse an FAA NOTAM date ('MM/DD/YYYY HHMM', optional trailing ' EST').
    'PERM'/'PERMANENT'/empty → None (no expiry)."""
    if not value:
        return None
    v = value.strip()
    if v.upper().startswith('PERM'):
        return None
    v = re.sub(r'\s+(EST|E)$', '', v)  # 'estimated' marker — drop, keep the datetime
    try:
        return datetime.strptime(v, '%m/%d/%Y %H%M').replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _normalize(notam: dict, country: str, fb_lat: float, fb_lon: float) -> dict | None:
    """Normalize one FAA notamSearch record into our schema."""
    number = notam.get('notamNumber')
    facility = notam.get('facilityDesignator') or ''
    if not number:
        return None

    q = _parse_qline(notam.get('icaoMessage'))
    if q:
        lat, lon, radius = q['lat'], q['lon'], q['radius_nm']
        alt_min, alt_max = q['lower_ft'], q['upper_ft']
    else:
        lat, lon, radius, alt_min, alt_max = fb_lat, fb_lon, 5, None, None

    effective_from = _parse_faa_dt(notam.get('startDate'))
    effective_to = _parse_faa_dt(notam.get('endDate'))
    now = dj_timezone.now()
    status = 'expired' if effective_to and effective_to < now else 'active'

    return {
        # facility + number is globally unique (and stable across polls); the same
        # NOTAM surfacing under two nearby airports dedupes to one row.
        'notam_id': f'{facility}/{number}',
        'source_region': 'FAA',
        'notam_type': notam.get('featureName') or 'general',
        'status': status,
        'effective_from': effective_from or now,
        'effective_to': effective_to,
        'geometry': {
            'type': 'Feature',
            'geometry': _circle_to_polygon(lat, lon, radius),
            'properties': {},
        },
        'altitude_min_ft': alt_min,
        'altitude_max_ft': alt_max,
        'location_name': facility,
        'country_code': country,
        'raw_text': notam.get('traditionalMessage') or notam.get('icaoMessage') or '',
    }


def _fetch_airport(icao: str, session: requests.Session) -> list[dict]:
    """POST one location query and return its raw notamList (may be a bare list).
    Retries once on any request error / non-200 (incl. Akamai 403)."""
    for attempt in range(_PER_QUERY_RETRIES + 1):
        try:
            resp = session.post(
                SEARCH_URL, headers=_HEADERS, timeout=30,
                data={
                    'searchType': '0',
                    'designatorsForLocation': icao,
                    'notamsOnly': 'true',
                    'offset': '0',
                    'formatType': 'ICAO',
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data['notamList'] if isinstance(data, dict) else (data if isinstance(data, list) else [])
        except requests.RequestException:
            if attempt >= _PER_QUERY_RETRIES:
                raise
            time.sleep(1.0 * (attempt + 1))
    return []


class NotamStream(BaseStream):
    stream_type = 'notam'

    def fetch(self) -> list[dict]:
        icaos = list(AIRPORTS)
        session = requests.Session()
        records: dict[str, dict] = {}
        ok = consecutive_failures = 0

        for icao in icaos:
            try:
                raw = _fetch_airport(icao, session)
                ok += 1
                consecutive_failures = 0
            except requests.RequestException as exc:
                consecutive_failures += 1
                logger.warning('[notam] %s query failed: %s', icao, exc)
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning('[notam] %d consecutive failures — source is blocking; '
                                   'stopping early after %d ok', consecutive_failures, ok)
                    break
                continue

            country, fb_lat, fb_lon = AIRPORTS[icao]
            for notam in raw or []:
                norm = _normalize(notam, country, fb_lat, fb_lon)
                if norm:
                    records.setdefault(norm['notam_id'], norm)  # first-seen wins (dedupe)
            time.sleep(random.uniform(*_PACE_SECONDS))  # gentle pacing — avoid the bot block

        # Nothing succeeded ⇒ the source is down/blocked. Raise so BaseStream.run
        # surfaces a FAILED TaskRun instead of a misleading success-with-0.
        if ok == 0:
            raise RuntimeError('[notam] every location query failed — source blocked or down')

        logger.info('[notam] %d NOTAM(s) across %d/%d airport(s)', len(records), ok, len(icaos))
        return list(records.values())

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
