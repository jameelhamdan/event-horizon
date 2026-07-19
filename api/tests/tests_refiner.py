"""Dependency-light self-tests for the refine stage's judge service
(services/processing/refiner.py): provider selection, verdict parsing, the
conflict-evidence gate, and the zeroshot/cloud/ollama paths via disabled
pipelines or patched clients — no model downloads, no network.

Run standalone:
    python -m tests.tests_refiner
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from tests._runner import bootstrap_django, run

bootstrap_django()


@contextmanager
def _disabled(env_var: str, cache_fn):
    os.environ[env_var] = 'false'
    cache_fn.cache_clear()
    try:
        yield
    finally:
        os.environ.pop(env_var, None)
        cache_fn.cache_clear()


# ── provider selection ────────────────────────────────────────────────────────

def test_default_provider_is_zeroshot():
    from services.processing.refiner import LLMRefiner
    assert LLMRefiner().provider == 'zeroshot'


def test_unknown_or_off_provider_yields_no_verdicts():
    from services.processing.refiner import LLMRefiner
    assert LLMRefiner(provider='off').judge([('t', 'c')]) == [None]
    assert LLMRefiner(provider='nonsense').judge([('t', 'c')]) == [None]


def test_zeroshot_unavailable_yields_none_per_item():
    from services.processing import refiner
    from services.processing.refiner import LLMRefiner

    with _disabled('ZEROSHOT_ENABLED', refiner._zeroshot_pipeline):
        got = LLMRefiner(provider='zeroshot').judge([('a', ''), ('b', '')])
    assert got == [None, None]


# ── conflict-evidence gate ────────────────────────────────────────────────────

def test_conflict_evidence_matches_real_conflict_vocabulary():
    from services.processing.refiner import _CONFLICT_EVIDENCE
    for text in (
        'Ukrainian drone attacks kill seven warehouse workers',
        'Israeli airstrike on Gaza funeral kills 7',
        'Rebels ambush army convoy, casualties reported',
    ):
        assert _CONFLICT_EVIDENCE.search(text), text


def test_conflict_evidence_rejects_violent_metaphors():
    from services.processing.refiner import _CONFLICT_EVIDENCE
    for text in (
        'All the EVs that were discontinued or killed off this year',
        'ChatGPT is silencing an entire generation, author says',
        'The iPad mini gets its biggest update in 5 years',
    ):
        assert not _CONFLICT_EVIDENCE.search(text), text


# ── verdict parsing ───────────────────────────────────────────────────────────

def test_parse_verdict_valid():
    from services.processing.refiner import LLMRefiner
    v = LLMRefiner._parse_verdict(
        {'category': 'disaster', 'sub_category': 'flood', 'country': 'Pakistan', 'city': None, 'intensity': 1.7},
        provider='ollama',
    )
    assert v['category'] == 'disaster' and v['sub_category'] == 'flood'
    assert v['country'] == 'Pakistan'
    assert v['intensity'] == 1.0  # clamped
    assert v['provider'] == 'ollama'


def test_parse_verdict_invalid_category_is_rejected():
    from services.processing.refiner import LLMRefiner
    assert LLMRefiner._parse_verdict({'category': 'sports'}, provider='ollama') is None


def test_parse_verdict_invalid_sub_dropped():
    from services.processing.refiner import LLMRefiner
    v = LLMRefiner._parse_verdict({'category': 'health', 'sub_category': 'flood'}, provider='ollama')
    assert v['category'] == 'health' and v['sub_category'] is None


# ── verdict application ────────────────────────────────────────────────────

def _fake_article(**overrides):
    """A minimal stand-in for an Article — LLMRefiner.apply only reads/writes
    plain attributes, so a real DB model isn't needed to test it."""
    defaults = dict(
        title='Floods hit the region', content='Heavy rain caused flooding.',
        category='general', sub_category='other',
        location=None, latitude=None, longitude=None,
        event_intensity=0.2, translations={}, extra_data={},
        refined_by=None,
    )
    return SimpleNamespace(**{**defaults, **overrides})


def test_apply_sets_category_sub_and_refined_by():
    from services.processing.refiner import LLMRefiner

    article = _fake_article()
    verdict = {'category': 'disaster', 'sub_category': 'flood', 'provider': 'zeroshot'}
    LLMRefiner.apply(article, verdict)

    assert article.category == 'disaster'
    assert article.sub_category == 'flood'
    assert article.refined_by == 'zeroshot'
    assert article.extra_data['llm']['category'] == 'disaster'


def test_apply_re_refine_overwrites_previous_provider():
    """A second apply() call — simulating a manual re-refine with a newly
    configured REFINE_PROVIDER — must overwrite refined_by, not append to it."""
    from services.processing.refiner import LLMRefiner

    article = _fake_article(category='disaster', sub_category='flood', refined_by='zeroshot')
    verdict = {'category': 'disaster', 'sub_category': 'flood', 'provider': 'cloud', 'intensity': 0.6}
    LLMRefiner.apply(article, verdict)

    assert article.refined_by == 'cloud'
    assert article.event_intensity == 0.6


def test_apply_keeps_annotator_geo_when_verdict_place_unresolvable():
    from services.processing.refiner import LLMRefiner

    article = _fake_article(location='Paris, France', latitude=48.85, longitude=2.35)
    verdict = {
        'category': 'general', 'sub_category': 'other', 'provider': 'ollama',
        'city': 'Zzqxv Nowhere', 'country': None,
    }
    LLMRefiner.apply(article, verdict)

    assert article.location == 'Paris, France'
    assert article.latitude == 48.85


# ── cloud provider (patched analyzer) ────────────────────────────────────────

def test_cloud_provider_maps_analysis_to_verdict():
    from services.processing.analyzer import ArticleAnalysis, ArticleAnalyzer
    from services.processing.refiner import LLMRefiner

    ok = ArticleAnalysis(
        category='political', sub_category='election', country='France', city='Paris',
        latitude=48.85, longitude=2.35, intensity=0.4,
        llm_data={}, translations={'en': {'summary': 'A summary.'}}, llm_usage={'provider': 'groq'},
    )
    failed = ArticleAnalyzer._empty(error='boom')
    with patch.object(ArticleAnalyzer, 'analyze_batch', return_value=[ok, failed]):
        good, bad = LLMRefiner(provider='cloud').judge([('a', ''), ('b', '')])
    assert good['category'] == 'political' and good['summary'] == 'A summary.'
    assert good['provider'] == 'cloud'
    assert bad is None


# ── ollama provider (patched client) ─────────────────────────────────────────

def test_ollama_provider_parses_constrained_json():
    import services.llm as llm_mod
    from services.processing.refiner import LLMRefiner

    class _FakeService:
        def chat(self, messages, **kwargs):
            assert kwargs.get('format') is not None  # schema passed through
            return '{"category": "economic", "sub_category": "tariffs", "country": null, "city": null, "intensity": 0.4}'

    with patch.object(llm_mod, 'get_provider', return_value=_FakeService()):
        [v] = LLMRefiner(provider='ollama').judge([('Tariffs imposed', 'Body')])
    assert v == {
        'category': 'economic', 'sub_category': 'tariffs',
        'country': None, 'city': None, 'provider': 'ollama', 'intensity': 0.4,
    }


def test_ollama_provider_unconfigured_yields_none():
    import services.llm as llm_mod
    from services.processing.refiner import LLMRefiner

    with patch.object(llm_mod, 'get_provider', return_value=None):
        assert LLMRefiner(provider='ollama').judge([('t', '')]) == [None]


_TESTS = [
    test_default_provider_is_zeroshot,
    test_unknown_or_off_provider_yields_no_verdicts,
    test_zeroshot_unavailable_yields_none_per_item,
    test_conflict_evidence_matches_real_conflict_vocabulary,
    test_conflict_evidence_rejects_violent_metaphors,
    test_apply_sets_category_sub_and_refined_by,
    test_apply_re_refine_overwrites_previous_provider,
    test_apply_keeps_annotator_geo_when_verdict_place_unresolvable,
    test_parse_verdict_valid,
    test_parse_verdict_invalid_sub_dropped,
    test_parse_verdict_invalid_category_is_rejected,
    test_cloud_provider_maps_analysis_to_verdict,
    test_ollama_provider_parses_constrained_json,
    test_ollama_provider_unconfigured_yields_none,
]


if __name__ == '__main__':
    run(_TESTS)
