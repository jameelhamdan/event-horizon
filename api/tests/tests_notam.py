"""Dependency-light self-tests for the keyless FAA NOTAM stream parsing/normalize
core (services/streams/notam.py). Sample payloads mirror real notamSearch records.

No network or database — only the pure parse/normalize helpers are exercised
(fetch() hits notams.aim.faa.gov; save() needs Mongo).

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_notam
"""

from datetime import datetime, timezone

from tests._runner import bootstrap_django, run

bootstrap_django()

from services.streams import notam as N  # noqa: E402

# Real-shape samples (US domestic + international ICAO).
_US = {
    'facilityDesignator': 'JFK', 'notamNumber': '11/346', 'featureName': 'Aerodrome',
    'startDate': '11/19/2025 2100', 'endDate': 'PERM',
    'icaoMessage': '11/346 NOTAMN \r\nQ) KZNY/QMXLC/IV/M/A/000/999/4038N07346W005 \r\n'
                   'A) KJFK \r\nB) 2511192100 \r\nC) PERM \r\nE) TWY KG BTN TWY A AND TWY B CLSD',
    'traditionalMessage': '!JFK 11/346 JFK TWY KG BTN TWY A AND TWY B CLSD',
}
_INTL = {
    'facilityDesignator': 'EGLL', 'notamNumber': 'A1171/26', 'featureName': 'International',
    'startDate': '03/24/2026 1213', 'endDate': '09/05/2026 2300',
    'icaoMessage': 'A1171/26 NOTAMR A1169/26\nQ) EGTT/QOBCE/IV/M  /AE/000/004/5131N00026W001\n'
                   'A) EGLL B) 2603241213 C) 2609052300\nE) ADD A NEW CRANE',
    'traditionalMessage': '',
}
_NO_QLINE = {
    'facilityDesignator': 'HKJK', 'notamNumber': 'A0042/26', 'featureName': 'Warning',
    'startDate': '07/01/2026 0000', 'endDate': 'PERM',
    'icaoMessage': 'A0042/26 NOTAMN\nE) FREE TEXT ONLY, NO Q-LINE',
    'traditionalMessage': '',
}


# ── _parse_qline ───────────────────────────────────────────────────────────────

def test_parse_qline_us_coords_and_fl():
    q = N._parse_qline(_US['icaoMessage'])
    assert q is not None
    assert round(q['lat'], 2) == 40.63 and round(q['lon'], 2) == -73.77  # 40°38'N 073°46'W
    assert q['radius_nm'] == 5
    assert q['lower_ft'] == 0 and q['upper_ft'] == 99900  # FL000 / FL999


def test_parse_qline_international_with_spaced_fields():
    q = N._parse_qline(_INTL['icaoMessage'])
    assert q is not None
    assert round(q['lat'], 2) == 51.52 and round(q['lon'], 2) == -0.43  # 51°31'N 000°26'W
    assert q['radius_nm'] == 1


def test_parse_qline_missing_returns_none():
    assert N._parse_qline(_NO_QLINE['icaoMessage']) is None
    assert N._parse_qline('') is None
    assert N._parse_qline(None) is None


# ── _parse_faa_dt ──────────────────────────────────────────────────────────────

def test_parse_faa_dt_normal():
    assert N._parse_faa_dt('11/19/2025 2100') == datetime(2025, 11, 19, 21, 0, tzinfo=timezone.utc)


def test_parse_faa_dt_perm_and_empty_are_none():
    assert N._parse_faa_dt('PERM') is None
    assert N._parse_faa_dt('PERMANENT') is None
    assert N._parse_faa_dt('') is None
    assert N._parse_faa_dt(None) is None


def test_parse_faa_dt_strips_estimated_marker():
    assert N._parse_faa_dt('01/01/2026 1200 EST') == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ── _normalize ─────────────────────────────────────────────────────────────────

def test_normalize_perm_notam_is_active_with_no_expiry():
    r = N._normalize(_US, 'US', 40.64, -73.78)
    assert r['notam_id'] == 'JFK/11/346'          # facility + number
    assert r['source_region'] == 'FAA'
    assert r['country_code'] == 'US'
    assert r['status'] == 'active'
    assert r['effective_to'] is None              # PERM
    assert r['geometry']['geometry']['type'] == 'Polygon'
    assert r['notam_type'] == 'Aerodrome'


def test_normalize_uses_fallback_coords_when_no_qline():
    r = N._normalize(_NO_QLINE, 'KE', -1.32, 36.93)
    ring = r['geometry']['geometry']['coordinates'][0]
    # Polygon centered on the fallback airport coords (lon, lat order in GeoJSON).
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    assert min(lons) < 36.93 < max(lons)
    assert min(lats) < -1.32 < max(lats)


def test_normalize_expired_when_end_in_past():
    past = dict(_INTL, endDate='01/01/2020 0000')
    r = N._normalize(past, 'GB', 51.47, -0.45)
    assert r['status'] == 'expired'


def test_normalize_requires_notam_number():
    assert N._normalize({'facilityDesignator': 'X'}, 'US', 0.0, 0.0) is None


# ── _circle_to_polygon ─────────────────────────────────────────────────────────

def test_circle_polygon_is_closed_ring():
    poly = N._circle_to_polygon(10.0, 20.0, 5)
    ring = poly['coordinates'][0]
    assert ring[0] == ring[-1]           # closed
    assert len(ring) == 33               # 32 segments + closing point


TESTS = [
    test_parse_qline_us_coords_and_fl,
    test_parse_qline_international_with_spaced_fields,
    test_parse_qline_missing_returns_none,
    test_parse_faa_dt_normal,
    test_parse_faa_dt_perm_and_empty_are_none,
    test_parse_faa_dt_strips_estimated_marker,
    test_normalize_perm_notam_is_active_with_no_expiry,
    test_normalize_uses_fallback_coords_when_no_qline,
    test_normalize_expired_when_end_in_past,
    test_normalize_requires_notam_number,
    test_circle_polygon_is_closed_ring,
]


if __name__ == '__main__':
    run(TESTS)
