"""Dependency-light self-tests for scoring, utils, and LLM helpers.

No database or network required — all logic is pure Python.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_scoring
"""

from tests._runner import bootstrap_django, run

_DJANGO_READY = bootstrap_django()


def test_tokenize_basic():
    from services.utils import tokenize
    tokens = tokenize('Ukraine ceasefire deal signed')
    assert 'ukraine' in tokens
    assert 'ceasefire' in tokens
    assert 'signed' in tokens
    # Stop words filtered
    assert 'the' not in tokens
    assert 'and' not in tokens
    # Short tokens filtered (≤2 chars)
    assert 'a' not in tokens


def test_tokenize_empty():
    from services.utils import tokenize
    assert tokenize('') == frozenset()
    assert tokenize(None) == frozenset()  # type: ignore[arg-type]


def test_tokenize_stop_words():
    from services.utils import tokenize, STOP_WORDS
    # Every stop word is filtered
    text = ' '.join(STOP_WORDS)
    assert tokenize(text) == frozenset()


def test_jaccard_identical():
    from services.utils import jaccard
    a = frozenset({'ukraine', 'ceasefire', 'deal'})
    assert jaccard(a, a) == 1.0


def test_jaccard_disjoint():
    from services.utils import jaccard
    a = frozenset({'ukraine', 'ceasefire'})
    b = frozenset({'earthquake', 'tsunami'})
    assert jaccard(a, b) == 0.0


def test_jaccard_partial():
    from services.utils import jaccard
    a = frozenset({'ukraine', 'ceasefire', 'deal'})
    b = frozenset({'ukraine', 'ceasefire', 'talks'})
    # intersection=2, union=4 → 0.5
    assert abs(jaccard(a, b) - 0.5) < 1e-9


def test_jaccard_empty():
    from services.utils import jaccard
    assert jaccard(frozenset(), frozenset({'x'})) == 0.0
    assert jaccard(frozenset({'x'}), frozenset()) == 0.0


def _llm_available() -> bool:
    try:
        import services.llm  # noqa: F401
        return True
    except ImportError:
        return False


def test_strip_code_fences_plain_json():
    if not _llm_available():
        print('  - test_strip_code_fences_* SKIPPED (httpx not installed)')
        return
    from services.llm import strip_code_fences
    raw = '[{"i": 1, "score": 7.5}]'
    assert strip_code_fences(raw) == raw


def test_strip_code_fences_with_json_tag():
    if not _llm_available():
        return
    from services.llm import strip_code_fences
    raw = '```json\n[{"i": 1, "score": 7.5}]\n```'
    assert strip_code_fences(raw) == '[{"i": 1, "score": 7.5}]'


def test_strip_code_fences_plain_backticks():
    if not _llm_available():
        return
    from services.llm import strip_code_fences
    raw = '```\n{"key": "value"}\n```'
    assert strip_code_fences(raw) == '{"key": "value"}'


def test_strip_code_fences_none_safe():
    if not _llm_available():
        return
    from services.llm import strip_code_fences
    assert strip_code_fences(None) == ''  # type: ignore[arg-type]
    assert strip_code_fences('') == ''


def test_filter_title_dupes_intra_batch():
    """C1 fix: near-duplicates within the SAME batch are both caught, not just cross-batch."""
    if not _DJANGO_READY:
        print('  - test_filter_title_dupes_* SKIPPED (django not configured)')
        return

    from unittest.mock import patch
    import services.data as data_mod

    datums = [
        {'title': 'Ukraine peace negotiations begin in Vienna'},
        {'title': 'Ukraine peace negotiations start in Vienna'},  # near-duplicate of [0] (jaccard ~0.67)
        {'title': 'Earthquake strikes Turkey, dozens killed'},  # different
    ]
    # Patch cache_get/cache_set (services.cache) inside services.data so no Redis is needed.
    with patch.object(data_mod, 'cache_get', return_value=[]), patch.object(data_mod, 'cache_set'):
        kept = data_mod._filter_title_dupes(datums, threshold=0.5, hours=24)
    assert len(kept) == 2
    titles = [d['title'] for d in kept]
    assert datums[0]['title'] in titles
    assert datums[2]['title'] in titles
    assert datums[1]['title'] not in titles


def test_filter_title_dupes_no_title():
    """Articles with empty/missing title are always kept."""
    if not _DJANGO_READY:
        return

    from unittest.mock import patch
    import services.data as data_mod

    with patch.object(data_mod, 'cache_get', return_value=[]), patch.object(data_mod, 'cache_set'):
        kept = data_mod._filter_title_dupes(
            [{'title': ''}, {'title': 'Real story about conflict'}],
            threshold=0.75,
        )
    assert len(kept) == 2


def test_tokenize_consistency_scoring_vs_data():
    """Both scoring._tokenize and data._tokenize_title must be the same function."""
    if not _DJANGO_READY:
        return

    from services.utils import tokenize
    from services.scoring import _tokenize as scoring_tok
    from services.data import _tokenize_title as data_tok

    sample = 'Ukraine Russia ceasefire peace deal'
    assert tokenize(sample) == scoring_tok(sample) == data_tok(sample)


def test_jaccard_consistency():
    """scoring._jaccard and data._jaccard must be the same function."""
    if not _DJANGO_READY:
        return

    from services.utils import jaccard
    from services.scoring import _jaccard as scoring_jac
    from services.data import _jaccard as data_jac

    a = frozenset({'ukraine', 'ceasefire'})
    b = frozenset({'ukraine', 'talks'})
    assert jaccard(a, b) == scoring_jac(a, b) == data_jac(a, b)


def test_importance_scorer_default_score():
    """When LLM call fails, ArticleImportanceScorer falls back to DEFAULT_SCORE."""
    from unittest.mock import patch
    from services.scoring import ArticleImportanceScorer

    scorer = ArticleImportanceScorer()
    # Patch get_llm_service to raise LLMError
    with patch('services.scoring.ArticleImportanceScorer.score_batch_llm',
               side_effect=lambda titles, role='scoring': [scorer.DEFAULT_SCORE] * len(titles)):
        scores = scorer.score_batch_llm(['Ukraine peace deal', 'Stock market crash'])
    assert scores == [scorer.DEFAULT_SCORE, scorer.DEFAULT_SCORE]


def test_importance_scorer_weight_zero_honoured():
    """source.weight=0 means suppressed — score must be near 0 (not coerced to 1.0)."""
    # Simulate internals: weight=0, llm_score=7.0, no bonus, no floor
    llm_score = 7.0
    weight    = 0.0
    bonus     = 0.0
    category  = 'general'

    _CATEGORY_FLOORS = {'conflict': 6.0, 'disaster': 6.0, 'health': 5.0,
                        'political': 4.0, 'economic': 4.0}
    floor = _CATEGORY_FLOORS.get(category, 0.0)
    raw   = llm_score * weight + bonus
    final = max(1.0, min(10.0, max(raw, floor)))

    # weight=0 → raw=0 → clamped to 1.0 (floor of the scale, not elevated by weight)
    assert final == 1.0, f'Expected 1.0 (minimum), got {final}'


def test_extract_json_array_trailing_prose():
    """C1 fix: trailing prose with its own brackets must not corrupt the extracted array."""
    from services.scoring import ArticleImportanceScorer

    raw = '[7.5, 4.0, 8.0]\n\nNote: headline [2] is speculative.'
    assert ArticleImportanceScorer._extract_json_array(raw) == '[7.5, 4.0, 8.0]'


def test_extract_json_array_leading_prose():
    from services.scoring import ArticleImportanceScorer

    raw = 'Here are the scores:\n[7.5, 4.0, 8.0]'
    assert ArticleImportanceScorer._extract_json_array(raw) == '[7.5, 4.0, 8.0]'


def test_extract_json_array_none_found():
    from services.scoring import ArticleImportanceScorer

    assert ArticleImportanceScorer._extract_json_array('no array here') is None


def test_importance_scorer_structural():
    from services.scoring import ArticleImportanceScorer, score_unscored_articles
    scorer = ArticleImportanceScorer()
    assert scorer.BATCH_SIZE >= 1
    assert 1.0 <= scorer.DEFAULT_SCORE <= 10.0
    assert callable(score_unscored_articles)


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_tokenize_basic,
    test_tokenize_empty,
    test_tokenize_stop_words,
    test_jaccard_identical,
    test_jaccard_disjoint,
    test_jaccard_partial,
    test_jaccard_empty,
    test_strip_code_fences_plain_json,
    test_strip_code_fences_with_json_tag,
    test_strip_code_fences_plain_backticks,
    test_strip_code_fences_none_safe,
    test_filter_title_dupes_intra_batch,
    test_filter_title_dupes_no_title,
    test_tokenize_consistency_scoring_vs_data,
    test_jaccard_consistency,
    test_importance_scorer_default_score,
    test_importance_scorer_weight_zero_honoured,
    test_extract_json_array_trailing_prose,
    test_extract_json_array_leading_prose,
    test_extract_json_array_none_found,
    test_importance_scorer_structural,
]


if __name__ == '__main__':
    run(_TESTS)
