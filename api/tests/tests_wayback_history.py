"""Dependency-light self-tests for services/data/wayback.py — Wayback
front-page mining (provider registry, link extraction, snapshot selection,
polite-client retry semantics) and its historical.py strategy routing.

No database or network — HTTP is mocked, sources are mocks, HTML is a fixture.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_wayback_history
"""

import datetime
import re
from unittest.mock import MagicMock, patch

from tests._runner import bootstrap_django, run

_DJANGO_READY = bootstrap_django()


def _dt(y, m, d, h=0):
    return datetime.datetime(y, m, d, h, tzinfo=datetime.timezone.utc)


# BBC-ish archived front page (id_ capture: original relative links, plus one
# archive-prefixed absolute link, nav chrome, offsite + non-article links).
_FRONTPAGE_HTML = """
<html><body>
  <nav><a href="/news">Home</a><a href="/news/world">World</a></nav>
  <a href="/news/world-us-canada-58579833">California governor beats bid to oust him in recall vote</a>
  <a href="https://www.bbc.com/news/world-asia-58565341">North Korea fires two ballistic missiles into sea near Japan</a>
  <a href="/web/20210915120000id_/https://www.bbc.com/news/business-58575433">Inflation jumps to highest rate in nearly a decade this year</a>
  <a href="/news/world-us-canada-58579833">California governor beats bid to oust him in recall vote</a>
  <a href="/sport/football-58579000">Big match report that is long enough to pass the filter</a>
  <a href="https://other-site.com/news/world-foo-12345678">Offsite story that would otherwise match the pattern fine</a>
  <a href="/news/world-europe-58581000">Short one</a>
</body></html>
"""

_BBC_RE = re.compile(r'/news/[a-z][a-z0-9-]*-\d{8,}')


def test_registry_configs_are_wellformed():
    if not _DJANGO_READY:
        print('  - tests SKIPPED (django not configured)')
        return
    from services.data.wayback import FRONTPAGES, supports_wayback
    for code, cfg in FRONTPAGES.items():
        assert cfg['url'].startswith('https://'), code
        assert hasattr(cfg['article_re'], 'search'), code
        assert supports_wayback(code)
    assert not supports_wayback('ft-world')  # deep sitemap archive — stays on RSS strategy
    assert not supports_wayback('wikipedia-current-events')


def test_extract_frontpage_links_filters_and_ranks():
    if not _DJANGO_READY:
        return
    from services.data.wayback import extract_frontpage_links

    links = extract_frontpage_links('https://www.bbc.com/news', _FRONTPAGE_HTML, _BBC_RE)
    urls = [u for _, u in links]
    # Kept, in page order: recall vote (relative), missiles (absolute),
    # inflation (archive-prefixed → stripped). Dropped: nav (pattern),
    # duplicate recall URL, /sport/ (pattern), offsite (host), short anchor.
    assert urls == [
        'https://www.bbc.com/news/world-us-canada-58579833',
        'https://www.bbc.com/news/world-asia-58565341',
        'https://www.bbc.com/news/business-58575433',
    ], urls
    assert links[0][0].startswith('California governor')


def test_nearest_noon_snapshot_selection():
    if not _DJANGO_READY:
        return
    from services.data.wayback import _nearest_noon
    assert _nearest_noon(['20210915001000', '20210915113000', '20210915235900']) == '20210915113000'
    assert _nearest_noon(['20210915040000']) == '20210915040000'


def _resp(status=200, json_data=None, text=''):
    r = MagicMock()
    r.status_code = status
    r.text = text
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError('not json')
    return r


def test_wayback_get_retries_non_200_and_trusts_200():
    if not _DJANGO_READY:
        return
    from services.data import wayback

    # 503 then 200 → retried, succeeds.
    with patch.object(wayback.requests, 'get', side_effect=[_resp(503), _resp(200, [])]) as g, \
         patch.object(wayback.time, 'sleep'):
        assert wayback._wayback_get('http://x', retries=2).status_code == 200
        assert g.call_count == 2

    # Exhausted retries → None.
    with patch.object(wayback.requests, 'get', return_value=_resp(503)), \
         patch.object(wayback.time, 'sleep'):
        assert wayback._wayback_get('http://x', retries=1) is None

    # Past deadline → no request at all.
    with patch.object(wayback.requests, 'get') as g:
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        assert wayback._wayback_get('http://x', deadline=past) is None
        assert g.call_count == 0


def test_cdx_snapshots_empty_200_is_authoritative():
    if not _DJANGO_READY:
        return
    from services.data import wayback

    with patch.object(wayback, '_wayback_get', return_value=_resp(200, [])):
        assert wayback.cdx_snapshots('https://x', _dt(2021, 9, 15), _dt(2021, 9, 16)) == []
    rows = [['timestamp'], ['20210915110000'], ['20210915180000']]
    with patch.object(wayback, '_wayback_get', return_value=_resp(200, rows)):
        assert wayback.cdx_snapshots('https://x', _dt(2021, 9, 15), _dt(2021, 9, 16)) == [
            '20210915110000', '20210915180000']


def test_fetch_day_cdx_restricted_falls_back_to_direct_redirect():
    """Guardian-style domains refuse public CDX queries; the direct
    /web/{ts}id_/ redirect form must still discover the day's front page —
    but only when the redirect lands inside the requested day."""
    if not _DJANGO_READY:
        return
    from services.data import wayback
    from services.data.wayback import WaybackHistoricalService

    source = MagicMock(code='guardian-world', name='Guardian', author_slug='guardian')
    svc = WaybackHistoricalService(source, max_candidates=3)
    guardian_html = ('<a href="/world/2023/jan/17/some-story-slug">'
                     'A headline that is definitely long enough to pass</a>')

    with patch.object(wayback, 'cdx_snapshots', return_value=[]), \
         patch.object(wayback, 'fetch_nearest_capture',
                      return_value=('20230117124401', guardian_html)):
        datums = svc.fetch_day(_dt(2023, 1, 17), _dt(2023, 1, 18))
    assert len(datums) == 1
    assert datums[0]['source_url'] == 'https://www.theguardian.com/world/2023/jan/17/some-story-slug'
    assert datums[0]['extra_data']['wayback_snapshot'] == '20230117124401'

    # Redirect landed on a different day → no usable capture.
    with patch.object(wayback, 'cdx_snapshots', return_value=[]), \
         patch.object(wayback, 'fetch_nearest_capture',
                      return_value=('20230301120000', guardian_html)):
        assert svc.fetch_day(_dt(2023, 1, 17), _dt(2023, 1, 18)) == []


def test_fetch_day_end_to_end_with_mocked_http():
    if not _DJANGO_READY:
        return
    from services.data import wayback
    from services.data.wayback import WaybackHistoricalService

    source = MagicMock(code='bbc-world', name='BBC World', author_slug='bbc')
    svc = WaybackHistoricalService(source, max_candidates=2)
    # Isolate from the shared Redis source-blocklist: this test mocks all I/O,
    # so a real timeout from an earlier test (or a prior live run) leaving
    # 'bbc-world' temporarily blocked must not make it flake.
    with patch.object(wayback, 'cdx_snapshots', return_value=['20210915113000']), \
         patch.object(wayback, '_wayback_get', return_value=_resp(200, text=_FRONTPAGE_HTML)), \
         patch.object(wayback, '_is_source_blocked', return_value=False):
        datums = svc.fetch_day(_dt(2021, 9, 15), _dt(2021, 9, 16))

    assert len(datums) == 2  # capped, keeping top-of-page rank order
    d = datums[0]
    assert d['source_url'] == 'https://www.bbc.com/news/world-us-canada-58579833'
    assert d['title'].startswith('California governor')
    assert d['published_on'] == _dt(2021, 9, 15, 11) + datetime.timedelta(minutes=30)
    assert d['extra_data']['frontpage_rank'] == 0
    assert d['extra_data']['wayback_snapshot'] == '20210915113000'
    assert d['extra_data']['title_from_slug'] is False
    assert datums[1]['extra_data']['frontpage_rank'] == 1


def test_unsupported_source_raises():
    if not _DJANGO_READY:
        return
    from services.data.historical import HistoricalServiceError
    from services.data.wayback import WaybackHistoricalService
    try:
        WaybackHistoricalService(MagicMock(code='ft-world'))
    except HistoricalServiceError:
        pass
    else:
        raise AssertionError('expected HistoricalServiceError')


def test_build_strategy_routes_wayback_sources():
    if not _DJANGO_READY:
        return
    import core.models as m
    from services.data.historical import HistoricalBackfillService, RSSHistoricalService
    from services.data.wayback import WaybackHistoricalService

    bbc = MagicMock(code='bbc-world', name='BBC', author_slug='bbc',
                    url='https://feeds.bbci.co.uk/news/world/rss.xml',
                    sitemap_url='', weight=1.4, type=m.SourceType.RSS)
    ft = MagicMock(code='ft-world', name='FT', author_slug='ft',
                   url='https://www.ft.com/rss/home/uk',
                   sitemap_url='', weight=1.3, type=m.SourceType.RSS)
    svc = HistoricalBackfillService(sources=[bbc, ft], top_n=3)
    assert isinstance(svc._strategies['bbc-world'], WaybackHistoricalService)
    assert isinstance(svc._strategies['ft-world'], RSSHistoricalService)


def test_junk_page_titles_detected():
    """Paywall/interstitial <title>s must never be adopted as article titles
    (the 2021 e2e smoke produced Events literally titled 'Subscribe to read')."""
    if not _DJANGO_READY:
        return
    from services.data.historical import is_junk_page_title
    junk = ['Subscribe to read', 'Sign in - FT', 'Just a moment...',
            'Access Denied', '404 Not Found', 'Attention Required! | Cloudflare',
            'Please enable JavaScript', 'Are you a robot?']
    real = ['Kenya declares war on millions of birds', 'US Senate votes to continue',
            "Russia expelled from Council of Europe", None, '']
    for t in junk:
        assert is_junk_page_title(t), t
    for t in real:
        assert not is_junk_page_title(t), t


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_registry_configs_are_wellformed,
    test_extract_frontpage_links_filters_and_ranks,
    test_nearest_noon_snapshot_selection,
    test_wayback_get_retries_non_200_and_trusts_200,
    test_cdx_snapshots_empty_200_is_authoritative,
    test_fetch_day_cdx_restricted_falls_back_to_direct_redirect,
    test_fetch_day_end_to_end_with_mocked_http,
    test_unsupported_source_raises,
    test_build_strategy_routes_wayback_sources,
    test_junk_page_titles_detected,
]


if __name__ == '__main__':
    run(_TESTS)
