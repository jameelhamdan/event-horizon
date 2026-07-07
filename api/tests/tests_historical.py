"""Dependency-light self-tests for services/data/historical.py's day-window
backfill helpers and services/tasks.py::_weighted_top_n rescale.

No database or network required — all logic exercised here is pure Python.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_historical
"""

import datetime

from tests._runner import bootstrap_django, run

_DJANGO_READY = bootstrap_django()


def _dt(y, m, d):
    return datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc)


def test_iter_days_single_day():
    from services.data.historical import iter_days
    days = list(iter_days(_dt(2024, 1, 1), _dt(2024, 1, 2)))
    assert days == [(_dt(2024, 1, 1), _dt(2024, 1, 2))]


def test_iter_days_multi_day_range():
    from services.data.historical import iter_days
    days = list(iter_days(_dt(2024, 1, 1), _dt(2024, 1, 4)))
    assert days == [
        (_dt(2024, 1, 1), _dt(2024, 1, 2)),
        (_dt(2024, 1, 2), _dt(2024, 1, 3)),
        (_dt(2024, 1, 3), _dt(2024, 1, 4)),
    ]


def test_iter_days_empty_range():
    from services.data.historical import iter_days
    assert list(iter_days(_dt(2024, 1, 1), _dt(2024, 1, 1))) == []


def test_iter_days_truncates_partial_start_day():
    """A start_date with a time-of-day component still aligns to that calendar day."""
    from services.data.historical import iter_days
    start = datetime.datetime(2024, 1, 1, 15, 30, tzinfo=datetime.timezone.utc)
    days = list(iter_days(start, _dt(2024, 1, 2)))
    assert days == [(_dt(2024, 1, 1), _dt(2024, 1, 2))]


def test_weighted_top_n_zero_weight_skips():
    if not _DJANGO_READY:
        print('  - test_weighted_top_n_* SKIPPED (django not configured)')
        return
    from services.tasks import _weighted_top_n
    assert _weighted_top_n(0) == 0


def test_weighted_top_n_per_day_bounds():
    """Rescaled for day-granularity backfill: lo=2, hi=6 (was 10-25 per week)."""
    if not _DJANGO_READY:
        return
    from services.tasks import _weighted_top_n
    assert _weighted_top_n(0.1) == 2
    assert _weighted_top_n(2.0) == 6
    assert _weighted_top_n(None) == _weighted_top_n(1.0)


def test_weighted_top_n_monotonic_with_weight():
    if not _DJANGO_READY:
        return
    from services.tasks import _weighted_top_n
    assert _weighted_top_n(0.5) <= _weighted_top_n(1.0) <= _weighted_top_n(1.5)


def test_discover_entries_merges_across_sitemap_candidates():
    """C1: a source with entries split across two sitemap candidates (e.g. a
    full-history sitemap_index.xml AND a recency-only news-sitemap.xml) must get
    entries from both, deduped by URL — not just whichever candidate hits first."""
    if not _DJANGO_READY:
        print('  - test_discover_entries_* SKIPPED (django not configured)')
        return

    from unittest.mock import MagicMock
    from services.data.historical import RSSHistoricalService

    source = MagicMock(url='https://example.com/rss', name='Example', author_slug='example', code='example')
    svc = RSSHistoricalService(source)

    day_start, day_end = _dt(2024, 1, 1), _dt(2024, 1, 2)

    def fake_parse_sitemap(url, start, end):
        if url == 'https://example.com/sitemap_index.xml':
            return [{'url': 'https://example.com/a', 'title': 'A', 'date': day_start}]
        if url == 'https://example.com/news-sitemap.xml':
            return [
                {'url': 'https://example.com/a', 'title': 'A dup', 'date': day_start},  # duplicate URL
                {'url': 'https://example.com/b', 'title': 'B', 'date': day_start},
            ]
        return []

    svc._candidate_sitemap_urls = lambda: [
        'https://example.com/sitemap.xml',
        'https://example.com/sitemap_index.xml',
        'https://example.com/news-sitemap.xml',
    ]
    svc._parse_sitemap = fake_parse_sitemap

    entries = svc._discover_entries(day_start, day_end)
    urls = sorted(e['url'] for e in entries)
    assert urls == ['https://example.com/a', 'https://example.com/b']


def test_parse_sitemap_index_caps_and_prioritizes_by_proximity():
    """C1: a date-partitioned index (one <sitemap> per day, e.g. Al Jazeera's) must
    not recurse into every entry — it should cap the count and try the entries
    closest to the target window first."""
    if not _DJANGO_READY:
        print('  - test_parse_sitemap_index_* SKIPPED (django not configured)')
        return

    import xml.etree.ElementTree as ET
    from unittest.mock import MagicMock
    from services.data.historical import RSSHistoricalService, _SITEMAP_NS

    source = MagicMock(url='https://example.com/rss', name='Example', author_slug='example', code='example')
    svc = RSSHistoricalService(source)

    day_start, day_end = _dt(2024, 6, 15), _dt(2024, 6, 16)

    # 200 daily sub-sitemaps spanning a year, one per ~1.8 days, each lastmod == its own date.
    root = ET.Element(f'{{{_SITEMAP_NS}}}sitemapindex')
    base = _dt(2024, 1, 1)
    for i in range(200):
        d = base + datetime.timedelta(days=i * 2)
        sm = ET.SubElement(root, f'{{{_SITEMAP_NS}}}sitemap')
        ET.SubElement(sm, f'{{{_SITEMAP_NS}}}loc').text = f'https://example.com/day-{d.date()}.xml'
        ET.SubElement(sm, f'{{{_SITEMAP_NS}}}lastmod').text = d.isoformat()

    seen_urls = []

    def fake_parse_sitemap(url, start, end):
        seen_urls.append(url)
        return []

    svc._parse_sitemap = fake_parse_sitemap
    svc._parse_sitemap_index(root, day_start, day_end)

    assert len(seen_urls) <= svc._MAX_SUBSITEMAPS_PER_INDEX
    # The sub-sitemap dated closest to day_start must be tried first, not skipped
    # in favor of index order (which would starve it out under a naive cap).
    assert seen_urls[0] == 'https://example.com/day-2024-06-15.xml'


def test_strip_feed_subdomain_removes_feeds_prefix():
    from services.data.historical import _strip_feed_subdomain
    assert _strip_feed_subdomain('feeds.apnews.com') == 'apnews.com'
    assert _strip_feed_subdomain('feeds.bbci.co.uk') == 'bbci.co.uk'


def test_strip_feed_subdomain_leaves_other_subdomains_alone():
    from services.data.historical import _strip_feed_subdomain
    assert _strip_feed_subdomain('www.aljazeera.com') == 'www.aljazeera.com'
    assert _strip_feed_subdomain('feeds.com') == 'feeds.com'  # too short to be a real prefix strip
    assert _strip_feed_subdomain('example.com') == 'example.com'


def test_rss_historical_service_uses_stripped_base_url():
    if not _DJANGO_READY:
        print('  - test_rss_historical_service_uses_stripped_base_url SKIPPED (django not configured)')
        return
    from unittest.mock import MagicMock
    from services.data.historical import RSSHistoricalService

    source = MagicMock(url='https://feeds.apnews.com/rss/apf-topnews', code='ap-top')
    svc = RSSHistoricalService(source)
    assert svc._base_url == 'https://apnews.com'


def test_candidate_sitemap_urls_prefers_explicit_override():
    """Source.sitemap_url, when set, must be tried first (and robots.txt / standard
    paths still follow it, so a bad override doesn't strand discovery entirely)."""
    if not _DJANGO_READY:
        print('  - test_candidate_sitemap_urls_* SKIPPED (django not configured)')
        return

    from unittest.mock import MagicMock, patch
    from services.data.historical import RSSHistoricalService

    source = MagicMock(
        url='https://example.com/rss', code='example',
        sitemap_url='https://example.com/custom/sitemap.xml',
    )
    svc = RSSHistoricalService(source)

    with patch('services.data.historical.requests.get') as mock_get:
        mock_get.return_value = MagicMock(ok=False)  # robots.txt fetch "fails" gracefully
        candidates = svc._candidate_sitemap_urls()

    assert candidates[0] == 'https://example.com/custom/sitemap.xml'
    assert 'https://example.com/sitemap.xml' in candidates


def test_candidate_sitemap_urls_no_override_falls_back_to_standard_paths():
    if not _DJANGO_READY:
        return

    from unittest.mock import MagicMock, patch
    from services.data.historical import RSSHistoricalService

    source = MagicMock(url='https://example.com/rss', code='example', sitemap_url='')
    svc = RSSHistoricalService(source)

    with patch('services.data.historical.requests.get') as mock_get:
        mock_get.return_value = MagicMock(ok=False)
        candidates = svc._candidate_sitemap_urls()

    assert candidates[0] == 'https://example.com/sitemap.xml'


def test_entry_to_datum_no_llm_score():
    """Discovery no longer LLM-scores; datum has no score/rank_signal field."""
    if not _DJANGO_READY:
        return

    from unittest.mock import MagicMock
    from services.data.historical import RSSHistoricalService

    source = MagicMock(url='https://example.com/rss', name='Example', author_slug='example', code='example')
    svc = RSSHistoricalService(source)
    entry = {'url': 'https://example.com/a', 'title': 'Some Title', 'date': _dt(2024, 1, 1)}
    datum = svc._entry_to_datum(entry)
    assert datum['title'] == 'Some Title'
    assert datum['source_url'] == 'https://example.com/a'
    assert 'score' not in datum
    assert 'rank_signal' not in datum


def test_iter_aggregate_windows_grid_aligned_and_covering():
    """Historical aggregation windows must start on the CLUSTER_DATE_WINDOW_DAYS
    ordinal grid (so no clustering bucket is split across calls) and cover the
    whole requested range without gaps."""
    if not _DJANGO_READY:
        return
    from services.workflow.events import CLUSTER_DATE_WINDOW_DAYS, iter_aggregate_windows

    start, end = _dt(2021, 7, 2), _dt(2021, 9, 20)
    windows = list(iter_aggregate_windows(start, end, window_days=30))

    assert windows[0][0] <= start                      # range fully covered
    assert windows[-1][1] == end
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert prev_end == next_start                  # no gaps, no overlap
    for w_start, _ in windows:
        assert w_start.toordinal() % CLUSTER_DATE_WINDOW_DAYS == 0   # grid-aligned
    for w_start, w_end in windows[:-1]:
        assert (w_end - w_start).days % CLUSTER_DATE_WINDOW_DAYS == 0


def test_iter_aggregate_windows_small_window_days_still_advances():
    if not _DJANGO_READY:
        return
    from services.workflow.events import CLUSTER_DATE_WINDOW_DAYS, iter_aggregate_windows
    windows = list(iter_aggregate_windows(_dt(2024, 1, 1), _dt(2024, 1, 10), window_days=1))
    assert all((e - s).days <= CLUSTER_DATE_WINDOW_DAYS for s, e in windows[:-1])
    assert windows[-1][1] == _dt(2024, 1, 10)


def test_aggregate_events_requires_both_range_bounds():
    if not _DJANGO_READY:
        return
    from services.workflow.events import aggregate_events
    try:
        aggregate_events(start=_dt(2024, 1, 1))
    except ValueError:
        pass
    else:
        raise AssertionError('expected ValueError for start without end')


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_iter_days_single_day,
    test_iter_days_multi_day_range,
    test_iter_days_empty_range,
    test_iter_days_truncates_partial_start_day,
    test_weighted_top_n_zero_weight_skips,
    test_weighted_top_n_per_day_bounds,
    test_weighted_top_n_monotonic_with_weight,
    test_discover_entries_merges_across_sitemap_candidates,
    test_parse_sitemap_index_caps_and_prioritizes_by_proximity,
    test_strip_feed_subdomain_removes_feeds_prefix,
    test_strip_feed_subdomain_leaves_other_subdomains_alone,
    test_rss_historical_service_uses_stripped_base_url,
    test_candidate_sitemap_urls_prefers_explicit_override,
    test_candidate_sitemap_urls_no_override_falls_back_to_standard_paths,
    test_entry_to_datum_no_llm_score,
    test_iter_aggregate_windows_grid_aligned_and_covering,
    test_iter_aggregate_windows_small_window_days_still_advances,
    test_aggregate_events_requires_both_range_bounds,
]


if __name__ == '__main__':
    run(_TESTS)
