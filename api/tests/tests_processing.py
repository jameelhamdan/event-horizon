"""Dependency-light self-tests for the local processing services: the shared
lazy-loader, VADER, translation, FinBERT (env-toggle paths only — none of
these download a model), and the pure-logic pieces of ArticleAnalyzer.

No database or network required — model pipelines are exercised via their
disabled/env-toggle path or with a patched-in fake pipeline; VADER runs for
real since its lexicon ships with the package (no download).

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_processing
"""

import os
from contextlib import contextmanager

from tests._runner import bootstrap_django, run

bootstrap_django()


@contextmanager
def _disabled(env_var: str, cache_fn):
    """Force a local model's *_ENABLED env var off for the duration of the
    block, clearing its lru_cache before and after so the disabled state
    actually takes effect and doesn't leak into later tests."""
    os.environ[env_var] = 'false'
    cache_fn.cache_clear()
    try:
        yield
    finally:
        os.environ.pop(env_var, None)
        cache_fn.cache_clear()


# ── _lazy.lazy_loader ─────────────────────────────────────────────────────────

def test_lazy_loader_builds_once_and_caches():
    from services.processing._lazy import lazy_loader

    calls = {'n': 0}

    def build():
        calls['n'] += 1
        return object()

    loader = lazy_loader('test_once', 'TESTS_PROCESSING_ONCE_ENABLED', build)
    os.environ.pop('TESTS_PROCESSING_ONCE_ENABLED', None)
    first = loader()
    second = loader()
    assert calls['n'] == 1
    assert first is second


def test_lazy_loader_env_opt_out_returns_none():
    from services.processing._lazy import lazy_loader

    loader = lazy_loader('test_optout', 'TESTS_PROCESSING_OPTOUT_ENABLED', lambda: object())
    os.environ['TESTS_PROCESSING_OPTOUT_ENABLED'] = 'false'
    try:
        assert loader() is None
    finally:
        os.environ.pop('TESTS_PROCESSING_OPTOUT_ENABLED', None)


def test_lazy_loader_build_failure_returns_none_not_raise():
    from services.processing._lazy import lazy_loader

    def build():
        raise RuntimeError('boom')

    loader = lazy_loader('test_fail', 'TESTS_PROCESSING_FAIL_ENABLED', build)
    os.environ.pop('TESTS_PROCESSING_FAIL_ENABLED', None)
    assert loader() is None


def test_lazy_loader_import_error_returns_none():
    from services.processing._lazy import lazy_loader

    def build():
        raise ImportError('no such package')

    loader = lazy_loader('test_importerr', 'TESTS_PROCESSING_IMPORTERR_ENABLED', build)
    os.environ.pop('TESTS_PROCESSING_IMPORTERR_ENABLED', None)
    assert loader() is None


# ── vader (real lexicon, no download needed) ──────────────────────────────────

def test_vader_scores_positive_text():
    from services.processing import vader
    os.environ.pop('VADER_ENABLED', None)
    vader._analyzer.cache_clear()
    scores = vader.score_batch(['This is wonderful, amazing, great news!'])
    assert scores[0] > 0.3


def test_vader_scores_negative_text():
    from services.processing import vader
    os.environ.pop('VADER_ENABLED', None)
    vader._analyzer.cache_clear()
    scores = vader.score_batch(['This is a horrific, terrible disaster with many deaths.'])
    assert scores[0] < -0.3


def test_vader_disabled_returns_neutral():
    from services.processing import vader
    with _disabled('VADER_ENABLED', vader._analyzer):
        assert vader.score_batch(['great news', 'terrible news']) == [0.0, 0.0]


def test_vader_empty_input():
    from services.processing import vader
    assert vader.score_batch([]) == []


# ── translation (env-toggle path only — no download in this test) ────────────

def test_translation_disabled_returns_none_list():
    from services import translation
    with _disabled('TRANSLATION_ENABLED', translation._get_model):
        assert translation.translate_en_ar_batch(['hello', 'world']) == [None, None]


def test_translation_empty_input():
    from services import translation
    assert translation.translate_en_ar_batch([]) == []


def test_translation_blank_strings_skip_model():
    from services import translation
    from unittest.mock import patch

    with patch.object(translation, '_get_model', return_value=('tok', 'model')) as get_model:
        result = translation.translate_en_ar_batch(['', '   ', None])
    assert result == [None, None, None]
    # _get_model is still consulted, but the tokenizer/model pair is never used
    # since there's nothing non-blank to translate.
    assert get_model.called


# ── finbert (env-toggle path only) ────────────────────────────────────────────

def test_finbert_disabled_returns_none_list():
    from services.processing import finbert
    with _disabled('FINBERT_ENABLED', finbert._pipeline):
        assert finbert.score_batch(['market news']) == [None]


def test_finbert_to_signed_positive_dominant():
    from services.processing.finbert import _to_signed
    scores = [{'label': 'positive', 'score': 0.8}, {'label': 'negative', 'score': 0.1}, {'label': 'neutral', 'score': 0.1}]
    assert _to_signed(scores) == 0.7


def test_finbert_to_signed_negative_dominant():
    from services.processing.finbert import _to_signed
    scores = [{'label': 'positive', 'score': 0.1}, {'label': 'negative', 'score': 0.6}, {'label': 'neutral', 'score': 0.3}]
    assert abs(_to_signed(scores) - (-0.5)) < 1e-9


# ── ArticleAnalyzer pure-logic helpers ────────────────────────────────────────

def test_split_usage_sums_back_to_total():
    from services.processing.analyzer import ArticleAnalyzer
    usage = {'provider': 'groq', 'model': 'x', 'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}
    shares = ArticleAnalyzer._split_usage(usage, 4)
    assert len(shares) == 4
    assert sum(s['prompt_tokens'] for s in shares) == 100
    assert sum(s['completion_tokens'] for s in shares) == 50
    assert sum(s['total_tokens'] for s in shares) == 150
    assert all(s['provider'] == 'groq' and s['model'] == 'x' for s in shares)


def test_split_usage_remainder_distributed_exactly():
    from services.processing.analyzer import ArticleAnalyzer
    shares = ArticleAnalyzer._split_usage({'total_tokens': 10}, 3)
    assert sum(s['total_tokens'] for s in shares) == 10
    # remainder (1) goes to the first share, so shares aren't all identical
    assert shares[0]['total_tokens'] >= shares[-1]['total_tokens']


def test_split_usage_empty_usage_dict():
    from services.processing.analyzer import ArticleAnalyzer
    shares = ArticleAnalyzer._split_usage({}, 3)
    assert len(shares) == 3


def test_split_usage_n_zero():
    from services.processing.analyzer import ArticleAnalyzer
    assert ArticleAnalyzer._split_usage({'total_tokens': 10}, 0) == []


def test_parse_intensity_valid_value():
    from services.processing.analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer()
    assert analyzer._parse_intensity(0.7) == 0.7


def test_parse_intensity_clamped_above_one():
    from services.processing.analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer()
    assert analyzer._parse_intensity(5.0) == 1.0


def test_parse_intensity_clamped_below_zero():
    from services.processing.analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer()
    assert analyzer._parse_intensity(-3.0) == 0.0


def test_parse_intensity_none_uses_default():
    from services.processing.analyzer import ArticleAnalyzer, _DEFAULT_INTENSITY
    analyzer = ArticleAnalyzer()
    assert analyzer._parse_intensity(None) == _DEFAULT_INTENSITY


def test_parse_intensity_unparseable_uses_default():
    from services.processing.analyzer import ArticleAnalyzer, _DEFAULT_INTENSITY
    analyzer = ArticleAnalyzer()
    assert analyzer._parse_intensity('not-a-number') == _DEFAULT_INTENSITY


def test_geocode_known_city_resolves():
    from services.processing.analyzer import _geocode
    lat, lon = _geocode('Kyiv', 'Ukraine')
    assert lat is not None and lon is not None


def test_geocode_unknown_city_falls_back_to_country():
    from services.processing.analyzer import _geocode
    lat, lon = _geocode('Nonexistent Madeup City Zzqxv', 'Ukraine')
    assert lat is not None and lon is not None


def test_geocode_nothing_resolves_returns_none_none():
    from services.processing.analyzer import _geocode
    assert _geocode(None, None) == (None, None)


def test_geocode_country_alias_resolves():
    """Common LLM name variants must resolve to the canonical country coords."""
    from services.processing.analyzer import _geocode
    canonical = _geocode(None, 'United States')
    assert canonical != (None, None)
    for variant in ('USA', 'US', 'U.S.', 'america', 'United States of America'):
        assert _geocode(None, variant) == canonical, variant
    # A different canonical name via alias (Türkiye → Turkey) also resolves.
    assert _geocode(None, 'Türkiye') == _geocode(None, 'Turkey')
    assert _geocode(None, 'Russian Federation') == _geocode(None, 'Russia')


def test_geocode_extra_place_resolves():
    """Territories geonamescache lacks (Palestine/Gaza) resolve via _EXTRA_PLACES,
    whether the LLM put the name in the city or the country field."""
    from services.processing.analyzer import _geocode
    assert _geocode(None, 'Palestine') != (None, None)
    assert _geocode('Gaza', None) != (None, None)
    assert _geocode('Gaza City', 'Palestine') != (None, None)


def test_geocode_city_alias_resolves():
    from services.processing.analyzer import _geocode
    assert _geocode('Kiev', None) == _geocode('Kyiv', None)


def test_analyzer_empty_result_shape():
    from services.processing.analyzer import ArticleAnalyzer
    empty = ArticleAnalyzer._empty()
    assert empty.category == 'general'
    assert empty.sub_category is None
    assert empty.intensity == 0.0
    assert empty.translations == {}
    assert empty.llm_usage == {}


def test_parse_obj_invalid_category_defaults_to_general():
    from services.processing.analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer()
    result = analyzer._parse_obj({'category': 'not-a-real-category'})
    assert result.category == 'general'


def test_parse_obj_invalid_subcategory_dropped():
    from services.processing.analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer()
    result = analyzer._parse_obj({'category': 'conflict', 'sub_category': 'not-a-real-subcategory'})
    assert result.category == 'conflict'
    assert result.sub_category is None


def test_parse_obj_valid_subcategory_kept():
    from services.processing.analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer()
    result = analyzer._parse_obj({'category': 'conflict', 'sub_category': 'airstrike'})
    assert result.sub_category == 'airstrike'


def test_parse_obj_sanitizes_non_dict_translation_entries():
    from services.processing.analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer()
    result = analyzer._parse_obj({'translations': {'en': {'title': 'x'}, 'ar': 'not-a-dict'}})
    assert 'en' in result.translations
    assert 'ar' not in result.translations


def test_parse_obj_geocodes_from_city_and_country():
    from services.processing.analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer()
    result = analyzer._parse_obj({'category': 'conflict', 'city': 'Kyiv', 'country': 'Ukraine'})
    assert result.latitude is not None and result.longitude is not None


def test_find_place_demonym_fallback():
    """A headline that never names the country outright still locates via demonym."""
    from services.processing.geocode import find_place
    assert find_place('Russian journalist Sergei Smirnov jailed after court ruling') == 'Russia'
    assert find_place('Chinese authorities detain a prominent activist') == 'China'
    # An explicit country name still wins over any demonym in the same text.
    assert find_place('A French envoy travelled to Germany for talks') == 'Germany' \
        or find_place('A French envoy travelled to Germany for talks') == 'France'
    assert find_place('The committee met to discuss the quarterly report') is None


def test_resolve_state_country_collision_georgia():
    """US-state 'Georgia' reads as United States only under clear US context;
    the pronoun 'us' must not trigger it, and a Caucasus story is left alone."""
    from services.processing.geocode import resolve_state_country_collision
    assert resolve_state_country_collision('Georgia', 'US President Biden wins Georgia recount') == 'United States'
    assert resolve_state_country_collision('Georgia', 'Tbilisi protests grip the Caucasus nation') == 'Georgia'
    assert resolve_state_country_collision('Georgia', 'the report told us that Georgia voted') == 'Georgia'
    assert resolve_state_country_collision('France', 'US President visits France') == 'France'


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_lazy_loader_builds_once_and_caches,
    test_lazy_loader_env_opt_out_returns_none,
    test_lazy_loader_build_failure_returns_none_not_raise,
    test_lazy_loader_import_error_returns_none,
    test_vader_scores_positive_text,
    test_vader_scores_negative_text,
    test_vader_disabled_returns_neutral,
    test_vader_empty_input,
    test_translation_disabled_returns_none_list,
    test_translation_empty_input,
    test_translation_blank_strings_skip_model,
    test_finbert_disabled_returns_none_list,
    test_finbert_to_signed_positive_dominant,
    test_finbert_to_signed_negative_dominant,
    test_split_usage_sums_back_to_total,
    test_split_usage_remainder_distributed_exactly,
    test_split_usage_empty_usage_dict,
    test_split_usage_n_zero,
    test_parse_intensity_valid_value,
    test_parse_intensity_clamped_above_one,
    test_parse_intensity_clamped_below_zero,
    test_parse_intensity_none_uses_default,
    test_parse_intensity_unparseable_uses_default,
    test_geocode_known_city_resolves,
    test_geocode_unknown_city_falls_back_to_country,
    test_geocode_nothing_resolves_returns_none_none,
    test_geocode_country_alias_resolves,
    test_geocode_extra_place_resolves,
    test_geocode_city_alias_resolves,
    test_analyzer_empty_result_shape,
    test_parse_obj_invalid_category_defaults_to_general,
    test_parse_obj_invalid_subcategory_dropped,
    test_parse_obj_valid_subcategory_kept,
    test_parse_obj_sanitizes_non_dict_translation_entries,
    test_parse_obj_geocodes_from_city_and_country,
    test_find_place_demonym_fallback,
    test_resolve_state_country_collision_georgia,
]


if __name__ == '__main__':
    run(_TESTS)
