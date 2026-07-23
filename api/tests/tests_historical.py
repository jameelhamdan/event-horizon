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


# ── _extract_title_and_text boilerplate removal ─────────────────────────────
# Fixtures shaped after real BBC (semantic <nav>/<article>, a linked
# "related articles" block) and Brookings (sidebar widget with an unrelated
# country name) page structures — the confirmed source of the category/geo
# contamination this fix targets: the old regex pulled every <p> on the page,
# nav chrome included, ahead of or alongside the real article text.

_BBC_SHAPED_HTML = """
<html><head><title>Israel-Gaza war: Hostage families demand action - BBC News</title></head>
<body>
<nav class="orb-nav-links"><ul>
<li><a href="/news">Home</a></li><li><a href="/news/business">Business</a></li>
<li><a href="/news/technology">Technology</a></li><li><a href="/news/health">Health</a></li>
<li><a href="/culture">Culture</a></li><li><a href="/arts">Arts</a></li>
</ul></nav>
<article>
<p>Families of hostages held by Hamas in Gaza gathered outside the Israeli defence ministry on Tuesday to demand the government do more to secure their release.</p>
<p>The protest came as Israeli forces continued strikes on targets in Gaza City, with the military saying it had hit dozens of militant positions overnight.</p>
</article>
<div class="related-articles"><p><a href="/a">Read more: Gaza crisis explained</a> <a href="/b">Analysis: What next</a></p></div>
<footer class="site-footer"><p>BBC. Copyright 2023.</p></footer>
</body></html>
"""

_BROOKINGS_SHAPED_HTML = """
<html><head><title>Springfield council approves zoning plan - Local News</title></head>
<body>
<aside class="sidebar"><div class="widget"><p>Related topics: Iran Foreign Policy Middle East Nuclear Deal</p></div></aside>
<main>
<p>The city council in Springfield voted Tuesday to approve a new zoning ordinance that will reshape the downtown business district over the next decade.</p>
<p>Supporters of the plan say it will bring much-needed housing and retail investment to the area.</p>
</main>
</body></html>
"""

# No semantic <article>/<main> wrapper at all — nav is a bare <div><p> of
# links, not a <nav> tag, so only the link-density/short-paragraph shape
# heuristic (not the structural-tag strip) can catch it.
_NO_SEMANTIC_TAGS_HTML = """
<html><head><title>Refinery blast kills workers - Wire Service</title></head>
<body>
<div class="header"><p><a href="/1">Business</a> <a href="/2">Technology</a> <a href="/3">Health</a> <a href="/4">Sport</a></p></div>
<div class="content">
<p>An explosion at an oil refinery in the country's south killed at least four workers and injured a dozen more on Wednesday, officials said.</p>
<p>The blast, which occurred during a routine maintenance shutdown, sent a plume of smoke visible for miles.</p>
</div>
</body></html>
"""


def test_extract_title_and_text_strips_bbc_style_nav_and_related_links():
    from services.data.bodies import _extract_title_and_text
    title, text = _extract_title_and_text(_BBC_SHAPED_HTML)
    assert title == 'Israel-Gaza war: Hostage families demand action - BBC News'
    assert 'hostages held by Hamas' in text
    assert 'Israeli defence ministry' in text
    for chrome in ('Business', 'Technology', 'Culture', 'Arts', 'Read more', 'Copyright'):
        assert chrome not in text


def test_extract_title_and_text_strips_sidebar_contamination():
    """The bug that geocoded an unrelated Springfield zoning story to Iran:
    a sidebar widget's stray country name must not reach Article.content."""
    from services.data.bodies import _extract_title_and_text
    _title, text = _extract_title_and_text(_BROOKINGS_SHAPED_HTML)
    assert 'Springfield' in text
    assert 'zoning ordinance' in text
    assert 'Iran' not in text


def test_extract_title_and_text_link_density_catches_unwrapped_nav():
    from services.data.bodies import _extract_title_and_text
    _title, text = _extract_title_and_text(_NO_SEMANTIC_TAGS_HTML)
    assert 'explosion at an oil refinery' in text
    for chrome in ('Business', 'Technology', 'Health', 'Sport'):
        assert chrome not in text


def test_is_boilerplate_paragraph_short_label_no_punctuation():
    from services.data.bodies import _is_boilerplate_paragraph
    assert _is_boilerplate_paragraph('<p>Technology</p>', 'Technology') is True


def test_is_boilerplate_paragraph_real_sentence_kept():
    from services.data.bodies import _is_boilerplate_paragraph
    p_html = '<p>Officials said the death toll was expected to rise as search efforts continued.</p>'
    plain = 'Officials said the death toll was expected to rise as search efforts continued.'
    assert _is_boilerplate_paragraph(p_html, plain) is False


def test_is_boilerplate_paragraph_link_dense_short_block_dropped():
    from services.data.bodies import _is_boilerplate_paragraph
    p_html = '<p><a href="/1">Business</a> <a href="/2">Technology</a> <a href="/3">Health</a></p>'
    plain = 'Business Technology Health'
    assert _is_boilerplate_paragraph(p_html, plain) is True


def test_is_boilerplate_paragraph_long_paragraph_with_one_inline_link_kept():
    """A real paragraph that happens to contain one inline link must survive —
    only short, mostly-linked blocks (the menu/nav shape) should be dropped."""
    from services.data.bodies import _is_boilerplate_paragraph
    p_html = (
        '<p>According to a <a href="/report">newly published report</a>, the '
        'agency found that emergency response times in the region had worsened '
        'significantly over the past two years, prompting renewed calls for reform.</p>'
    )
    plain = (
        'According to a newly published report, the agency found that emergency '
        'response times in the region had worsened significantly over the past '
        'two years, prompting renewed calls for reform.'
    )
    assert _is_boilerplate_paragraph(p_html, plain) is False


# ── Large-HTML extraction (trafilatura must see body past the regex cap) ────────

def test_extract_title_and_text_body_survives_past_regex_cap():
    """Regression: the pre-trafilatura truncation used to cut the article body
    off large pages (The Guardian front-loads ~200 KB of inline scripts before
    the body), stranding trafilatura and forcing the nav-only regex fallback.
    A body past 200 KB of leading <script> must still be extracted."""
    from services.data.bodies import _extract_title_and_text
    body = (
        '<article><p>Wildfires continued to threaten swaths of forest and fields '
        'in Israel on Thursday, though firefighters successfully reopened the main '
        'road linking the two principal cities. Officials declared a national '
        'emergency and ordered evacuations as strong winds spread the flames.</p>'
        '<p>The prime minister said additional aircraft had been requested from '
        'neighbouring countries to help bring the fires under control by nightfall.</p>'
        '</article>'
    )
    html = (
        '<html><head><title>Israel declares national emergency | The Guardian</title></head>'
        '<body><script>' + ('var x=1;' * 30000) + '</script>' + body + '</body></html>'
    )
    assert len(html) > 200_000
    _title, text = _extract_title_and_text(html)
    assert text and 'national emergency' in text
    assert 'firefighters successfully reopened' in text


# ── Paywall body detection ──────────────────────────────────────────────────────

def test_is_paywall_body_drops_leading_wall_text():
    from services.data.bodies import _is_paywall_body
    ft = ('UK inflation falls more than expected to 2.6% in June Subscribe to '
          'unlock this article Try unlimited access Only 1 euro for 4 weeks')
    ps = ('Available exclusively to PS subscribers, PS Deep Dives delivers a '
          'weekly expert commentary examining a major global challenge')
    assert _is_paywall_body(ft) is True
    assert _is_paywall_body(ps) is True


def test_is_paywall_body_keeps_real_article_with_trailing_cta():
    """A full article that merely closes with a subscribe CTA (marker deep in
    the text) must be kept — only walls that interrupt early are dropped."""
    from services.data.bodies import _is_paywall_body
    real_lead = ('China targets panda bond reform, mandates global credit '
                 'mapping to lure foreign capital. ') * 20
    assert _is_paywall_body(real_lead + 'Subscribe to read more like this.') is False
    assert _is_paywall_body(real_lead) is False


# ── Non-article URL / junk detection ────────────────────────────────────────────

def test_is_non_article_url_image_asset_and_sections():
    from services.data.bodies import is_non_article_url
    assert is_non_article_url('https://www.technologyreview.com/205x205_property-1the-checkup-mail-icon/') is True
    assert is_non_article_url('https://www.forbes.com/advisor/ca/personal-loans/personal-loan-requirements/') is True
    assert is_non_article_url('https://arstechnica.com/tag/clip-art/') is True
    assert is_non_article_url('https://example.com/') is True


def test_is_non_article_url_keeps_real_articles():
    from services.data.bodies import is_non_article_url
    assert is_non_article_url('https://www.theguardian.com/world/2025/may/01/israel-fires-wildfires-jerusalem') is False
    assert is_non_article_url('https://www.propublica.org/article/nike-cambodia-factory-heat') is False


def test_is_junk_article_flags_hex_asset_titles():
    from services.data.bodies import is_junk_article
    # UUID/GUID asset filenames rendered title-cased by the slug-from-URL fallback
    assert is_junk_article('12B8E10B B55D 4824 817F A3C9Cfe9F779', 'https://ex.com/a') is True
    assert is_junk_article('B3Ae2589 0497 4534 8B8C 8Bf6Fabf53B0', 'https://ex.com/b') is True
    assert is_junk_article('9D348C6E 833A 4A38 Ab67 Acb', 'https://ex.com/c') is True  # truncated tail
    # real headlines with hex-looking words must NOT be flagged
    assert is_junk_article('China Parade', 'https://ex.com/story/china-parade') is False
    assert is_junk_article('Ace Beef Cafe Dead Feed', 'https://ex.com/story/x') is False  # all-hex words but wrong shape


def test_is_good_quality_body_gates_length_and_paywall():
    from services.data.bodies import is_good_quality_body, GOOD_BODY_MIN_CHARS
    assert is_good_quality_body('x' * (GOOD_BODY_MIN_CHARS + 10)) is True   # substantial prose
    assert is_good_quality_body('too short') is False                       # thin body
    assert is_good_quality_body(None) is False
    # long enough but a paywall interstitial → not good quality
    wall = 'Subscribe to unlock this article. ' + 'x' * GOOD_BODY_MIN_CHARS
    assert is_good_quality_body(wall) is False


def test_always_fail_hydration_sources_are_known_paywalls():
    from services.data.bodies import ALWAYS_FAIL_HYDRATION_SOURCES
    assert 'ft-world' in ALWAYS_FAIL_HYDRATION_SOURCES
    assert 'wsj-markets' in ALWAYS_FAIL_HYDRATION_SOURCES
    # a normal source must NOT be in the skip/soft-delete set
    assert 'bbc-world' not in ALWAYS_FAIL_HYDRATION_SOURCES


# ── Egress proxy rotation ───────────────────────────────────────────────────────

def test_proxy_pool_parses_and_defaults_empty():
    from unittest.mock import patch
    from services.data import proxy
    assert proxy._parse_pool('') == []
    assert proxy._parse_pool('http://a:1, http://b:2 ') == ['http://a:1', 'http://b:2']
    # legacy WAYBACK_PROXY_URL folds into the wayback pool when the pool is unset
    with patch.object(proxy.settings, 'WAYBACK_PROXY_POOL', ''), \
         patch.object(proxy.settings, 'WAYBACK_PROXY_URL', 'http://legacy:9'):
        assert proxy.WAYBACK_PROXIES.urls() == ['http://legacy:9']


def test_proxy_pool_attempt_order_direct_first_and_always_nonempty():
    from unittest.mock import patch
    from services.data import proxy
    with patch.object(proxy.settings, 'EGRESS_PROXY_POOL', ''):
        assert proxy.EGRESS_PROXIES.attempt_order() == [None]   # empty pool → one direct attempt
    with patch.object(proxy.settings, 'EGRESS_PROXY_POOL', 'http://p:1'):
        order = proxy.EGRESS_PROXIES.attempt_order()
        assert order[0] is None and 'http://p:1' in order


def test_proxy_pool_get_retries_proxy_on_block():
    from unittest.mock import MagicMock, patch
    from services.data import proxy
    seen = []

    def fake_get(url, headers=None, timeout=15, proxies=None, **kw):
        seen.append(proxies)
        r = MagicMock()
        r.status_code = 200 if proxies else 403  # direct blocked, proxy ok
        return r

    with patch.object(proxy.settings, 'EGRESS_PROXY_POOL', 'http://p:1'), \
         patch('services.data.proxy.requests.get', side_effect=fake_get):
        resp = proxy.EGRESS_PROXIES.get('http://x', headers={}, timeout=5)
    assert resp.status_code == 200
    assert seen[0] is None and seen[1] == {'http': 'http://p:1', 'https': 'http://p:1'}


def test_proxy_pool_get_preserves_exception_type():
    import requests
    from unittest.mock import patch
    from services.data import proxy

    with patch.object(proxy.settings, 'EGRESS_PROXY_POOL', 'http://p:1'), \
         patch('services.data.proxy.requests.get', side_effect=requests.Timeout('t')):
        try:
            proxy.EGRESS_PROXIES.get('http://x', headers={}, timeout=5)
            raise AssertionError('expected Timeout')
        except requests.Timeout:
            pass


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
    test_extract_title_and_text_strips_bbc_style_nav_and_related_links,
    test_extract_title_and_text_strips_sidebar_contamination,
    test_extract_title_and_text_link_density_catches_unwrapped_nav,
    test_is_boilerplate_paragraph_short_label_no_punctuation,
    test_is_boilerplate_paragraph_real_sentence_kept,
    test_is_boilerplate_paragraph_link_dense_short_block_dropped,
    test_is_boilerplate_paragraph_long_paragraph_with_one_inline_link_kept,
    test_extract_title_and_text_body_survives_past_regex_cap,
    test_is_paywall_body_drops_leading_wall_text,
    test_is_paywall_body_keeps_real_article_with_trailing_cta,
    test_is_non_article_url_image_asset_and_sections,
    test_is_non_article_url_keeps_real_articles,
    test_is_junk_article_flags_hex_asset_titles,
    test_is_good_quality_body_gates_length_and_paywall,
    test_always_fail_hydration_sources_are_known_paywalls,
    test_proxy_pool_parses_and_defaults_empty,
    test_proxy_pool_attempt_order_direct_first_and_always_nonempty,
    test_proxy_pool_get_retries_proxy_on_block,
    test_proxy_pool_get_preserves_exception_type,
]


if __name__ == '__main__':
    run(_TESTS)
