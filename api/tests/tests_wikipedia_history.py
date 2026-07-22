"""Dependency-light self-tests for services/data/wikipedia.py — the Wikipedia
Current Events historical-backfill strategy (monthly-page day parsing,
event→datum mapping) plus the historical.py pieces it plugs into (strategy
routing, slug-title upgrade flag, title/body extraction).

No database or network required — month HTML is a fixture, sources are mocks.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_wikipedia_history
"""

import datetime
from unittest.mock import MagicMock, patch

from tests._runner import bootstrap_django, run

_DJANGO_READY = bootstrap_django()


def _dt(y, m, d):
    return datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc)


# Two day sections (Sept 15 + 16), mirroring the live monthly-page markup:
# day divs are current-events-main with UNPADDED-day ids; each day wraps a
# current-events-content with <p><b> section headings and nested topic lists.
_MONTH_HTML = """
<div class="mw-parser-output">
  <div role="region" aria-label="September 15" id="2021_September_15" class="current-events-main vevent">
    <div class="current-events-content description">
      <p><b>Armed conflicts and attacks</b></p>
      <ul>
        <li><a href="/wiki/Somali_Civil_War">Somali Civil War</a>
          <ul>
            <li>Seven soldiers are killed when militants attack an army position.
                (<a class="external text" href="https://www.reuters.com/somalia-attack">Reuters</a>)</li>
          </ul>
        </li>
        <li>A standalone gunfight in <a href="/wiki/Pakistan">Pakistan</a> kills three people.
            (<a class="external text" href="https://www.dawn.com/gunfight">Dawn</a>)
            (<a class="external text" href="https://apnews.com/gunfight">AP</a>)</li>
        <li>An uncited claim about fighting somewhere far away with no source link at all.</li>
        <li>Short. (<a class="external text" href="https://example.com/x">E</a>)</li>
      </ul>
      <p><b>Business and economy</b></p>
      <ul>
        <li>Property giant Evergrande admits it is under tremendous pressure and may default.
            (<a class="external text" href="https://www.dw.com/evergrande">DW</a>)</li>
      </ul>
    </div>
  </div>
  <div role="region" aria-label="September 16" id="2021_September_16" class="current-events-main vevent">
    <div class="current-events-content description">
      <p><b>Disasters and accidents</b></p>
      <ul>
        <li>A magnitude 5.4 earthquake strikes Luzhou, killing three people.
            (<a class="external text" href="https://www.bbc.com/luzhou-quake">BBC News</a>)</li>
      </ul>
    </div>
  </div>
</div>
"""


def test_month_page_title_and_day_section_id_formats():
    if not _DJANGO_READY:
        print('  - tests SKIPPED (django not configured)')
        return
    from services.data.wikipedia import day_section_id, month_page_title
    assert month_page_title(_dt(2021, 9, 15)) == 'Portal:Current_events/September_2021'
    # Day is NOT zero-padded in the section id.
    assert day_section_id(_dt(2021, 9, 9)) == '2021_September_9'
    assert day_section_id(_dt(2021, 9, 15)) == '2021_September_15'


def test_parse_day_events_extracts_only_that_days_cited_leaves():
    if not _DJANGO_READY:
        return
    from services.data.wikipedia import parse_day_events

    events = parse_day_events(_MONTH_HTML, _dt(2021, 9, 15))
    # 3 kept: nested Somali leaf, standalone Pakistan leaf, Evergrande leaf.
    # Dropped: uncited claim (no external link), 'Short.' (< min length),
    # and everything from Sept 16.
    assert len(events) == 3, [e['text'] for e in events]

    somali, pakistan, evergrande = events
    assert somali['topics'] == ['Somali Civil War']
    assert somali['category'] == 'conflict'
    assert somali['cites'] == ['https://www.reuters.com/somalia-attack']
    assert 'Seven soldiers are killed' in somali['text']
    assert 'Reuters' not in somali['text']  # trailing cite anchors stripped

    assert pakistan['topics'] == []
    assert pakistan['cites'] == ['https://www.dawn.com/gunfight', 'https://apnews.com/gunfight']
    assert not pakistan['text'].endswith(')')

    assert evergrande['category'] == 'economic'


def test_parse_day_events_missing_day_returns_empty():
    if not _DJANGO_READY:
        return
    from services.data.wikipedia import parse_day_events
    assert parse_day_events(_MONTH_HTML, _dt(2021, 9, 17)) == []
    assert parse_day_events('', _dt(2021, 9, 15)) == []


def test_fetch_day_maps_events_to_datums():
    if not _DJANGO_READY:
        return
    from services.data.wikipedia import WikipediaHistoricalService

    source = MagicMock(code='wikipedia-current-events', name='Wikipedia Current Events',
                       author_slug='wikipedia')
    svc = WikipediaHistoricalService(source)
    with patch.object(svc, '_get_month_html', return_value=_MONTH_HTML):
        datums = svc.fetch_day(_dt(2021, 9, 15), _dt(2021, 9, 16))

    assert len(datums) == 3
    d = datums[0]
    assert d['source_url'] == 'https://www.reuters.com/somalia-attack'
    assert d['author'] == 'reuters.com'          # www. stripped
    assert d['published_on'] == _dt(2021, 9, 15) + datetime.timedelta(hours=12)
    assert d['title'] == d['content'][:200]
    assert d['extra_data']['wiki_event'] is True
    assert d['extra_data']['cite_domain'] == 'reuters.com'
    assert d['extra_data']['title_from_slug'] is True  # save path upgrades to page <title>
    assert d['extra_data']['wiki_topics'] == ['Somali Civil War']


def test_fetch_day_respects_max_candidates_and_first_match_only():
    if not _DJANGO_READY:
        return
    from services.data.wikipedia import WikipediaHistoricalService

    source = MagicMock(code='wikipedia-current-events', name='W', author_slug='w')
    svc = WikipediaHistoricalService(source, max_candidates=2)
    with patch.object(svc, '_get_month_html', return_value=_MONTH_HTML):
        assert len(svc.fetch_day(_dt(2021, 9, 15), _dt(2021, 9, 16))) == 2

    probe = WikipediaHistoricalService(source, max_candidates=1, first_match_only=True)
    with patch.object(probe, '_get_month_html', return_value=_MONTH_HTML):
        assert len(probe.fetch_day(_dt(2021, 9, 15), _dt(2021, 9, 16))) == 1


def test_build_strategy_routes_wiki_source():
    if not _DJANGO_READY:
        return
    from services.data.historical import HistoricalBackfillService
    from services.data.wikipedia import WIKI_DEFAULT_TOP_N, WikipediaHistoricalService

    wiki = MagicMock(code='wikipedia-current-events', name='W', author_slug='w', weight=1.5)
    svc = HistoricalBackfillService(sources=[wiki])
    assert isinstance(svc._strategies['wikipedia-current-events'], WikipediaHistoricalService)
    # Curated events: default per-day cap is WIKI_DEFAULT_TOP_N, not weight-derived 2-6.
    assert svc._resolve_top_n(wiki) == WIKI_DEFAULT_TOP_N
    # An explicit top_n still wins.
    assert HistoricalBackfillService(sources=[wiki], top_n=3)._resolve_top_n(wiki) == 3


def test_entry_to_datum_flags_slug_derived_titles():
    if not _DJANGO_READY:
        return
    from services.data.historical import RSSHistoricalService

    source = MagicMock(url='https://example.com/rss', name='Example',
                       author_slug='example', code='example', sitemap_url='')
    svc = RSSHistoricalService(source)
    with_title = svc._entry_to_datum(
        {'url': 'https://example.com/a', 'title': 'Real Title', 'date': _dt(2024, 1, 1)})
    slug_only = svc._entry_to_datum(
        {'url': 'https://example.com/some-story-slug', 'title': None, 'date': _dt(2024, 1, 1)})
    assert with_title['extra_data']['title_from_slug'] is False
    assert slug_only['extra_data']['title_from_slug'] is True


def test_extract_title_and_text():
    if not _DJANGO_READY:
        return
    from services.data.historical import _extract_title_and_text

    html = ('<html><head><title> Big  Story — Example News </title>'
            '<script>var x = "<p>not text</p>";</script></head>'
            '<body><nav>Menu</nav><p>First para.</p><p>Second <b>para</b>.</p></body></html>')
    title, body = _extract_title_and_text(html)
    assert title == 'Big Story — Example News'
    assert body.startswith('First para.') and 'Second' in body
    assert 'not text' not in body and 'Menu' not in body
    # A page with no extractable prose yields no body (trafilatura, the primary
    # extractor, does return bare text like "no paragraphs" for a structureless
    # body — so the empty case is what "no article content" means here).
    assert _extract_title_and_text('<html><body></body></html>') == (None, None)


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_month_page_title_and_day_section_id_formats,
    test_parse_day_events_extracts_only_that_days_cited_leaves,
    test_parse_day_events_missing_day_returns_empty,
    test_fetch_day_maps_events_to_datums,
    test_fetch_day_respects_max_candidates_and_first_match_only,
    test_build_strategy_routes_wiki_source,
    test_entry_to_datum_flags_slug_derived_titles,
    test_extract_title_and_text,
]


if __name__ == '__main__':
    run(_TESTS)
