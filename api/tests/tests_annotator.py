"""Dependency-light self-tests for the annotate stage's NLP service
(services/processing/annotator.py): taxonomy consistency, gazetteer scan
helpers, rule-based intensity, extractive summaries, prototype classification
(fake encoder — no model download), and the full annotate_batch flow.

No database or network required — the NER pipeline is only exercised via its
disabled/env-toggle path.

Run standalone:
    python -m tests.tests_annotator
"""

import os
from contextlib import contextmanager
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


def _doc(title: str, content: str = ''):
    from core.models import ArticleDocument
    return ArticleDocument(id='1', title=title, content=content, source_code='test', published_on='')


# ── taxonomy ──────────────────────────────────────────────────────────────────

def test_taxonomy_prototypes_cover_every_pair():
    from services.processing.taxonomy import CATEGORIES, PROTOTYPES, PRIORS, SUB_CATEGORIES

    pairs = {(c, s) for c, subs in SUB_CATEGORIES.items() for s in subs}
    assert set(SUB_CATEGORIES) == CATEGORIES
    assert set(PROTOTYPES) == pairs, 'every (category, sub) needs at least one prototype'
    assert all(isinstance(v, list) and v and all(isinstance(t, str) and t for t in v) for v in PROTOTYPES.values())
    assert set(PRIORS) == pairs, 'every (category, sub) needs exactly one intensity prior'
    assert all(0.0 <= v <= 1.0 for v in PRIORS.values())


def test_llm_analyzer_prompt_still_builds_from_shared_taxonomy():
    from services.processing.analyzer import _SYSTEM_PROMPT
    assert 'conflict' in _SYSTEM_PROMPT and 'border-clash' in _SYSTEM_PROMPT


# ── geocode helpers ───────────────────────────────────────────────────────────

def test_find_place_matches_country_in_headline():
    from services.processing.geocode import find_place
    assert find_place('Floods submerge villages across Pakistan overnight') == 'Pakistan'
    assert find_place('Nothing geographic here at all') is None


def test_find_place_skips_ambiguous_english_words():
    from services.processing.geocode import find_place
    # 'us' the pronoun must not match as the country (alias excluded from scan).
    assert find_place('He asked them to move him closer to us today') is None


def test_canonical_country_resolves_aliases_and_rejects_noise():
    from services.processing.geocode import canonical_country
    assert canonical_country('UK') == 'United Kingdom'
    assert canonical_country('Ukraine') == 'Ukraine'
    assert canonical_country('Gaza') == 'Gaza'  # extra-place territory counts
    assert canonical_country('Zzqxv Nowhere') is None


def test_is_city_covers_aliases():
    from services.processing.geocode import is_city
    assert is_city('Kyiv') and is_city('Kiev')
    assert not is_city('Zzqxv Nowhere')


def test_country_of_city_backfills_canonical_country():
    from services.processing.geocode import country_of_city
    assert country_of_city('Kyoto') == 'Japan'
    assert country_of_city('Kiev') == 'Ukraine'  # via city alias
    assert country_of_city('Zzqxv Nowhere') is None


def test_city_country_conflict_detects_mismatched_pair():
    """Confirmed live bug: a Kyiv/EU-diplomacy piece geocoded as "Kyiv,
    Russia" — a city paired with an unrelated country mentioned elsewhere."""
    from services.processing.geocode import city_country_conflict
    assert city_country_conflict('Kyiv', 'Russia') is True


def test_city_country_conflict_allows_matching_pair():
    from services.processing.geocode import city_country_conflict
    assert city_country_conflict('Kyiv', 'Ukraine') is False


def test_city_country_conflict_false_when_either_missing():
    from services.processing.geocode import city_country_conflict
    assert city_country_conflict(None, 'Russia') is False
    assert city_country_conflict('Kyiv', None) is False


def test_city_country_conflict_false_for_unresolvable_city():
    from services.processing.geocode import city_country_conflict
    assert city_country_conflict('Zzqxv Nowhere', 'Russia') is False


# ── rule-based intensity ──────────────────────────────────────────────────────

def test_intensity_starts_from_taxonomy_prior():
    from services.processing.annotator import rate_intensity
    from services.processing.taxonomy import PRIORS
    assert rate_intensity('conflict', 'war', 'Talks continue') == PRIORS[('conflict', 'war')]
    assert rate_intensity('general', 'other', 'A quiet day') == PRIORS[('general', 'other')]


def test_intensity_casualties_escalate_by_magnitude():
    from services.processing.annotator import rate_intensity
    base = rate_intensity('conflict', 'other', 'Clashes reported')
    few = rate_intensity('conflict', 'other', 'Clashes reported, 3 killed')
    many = rate_intensity('conflict', 'other', 'Clashes reported, 250 killed')
    assert base < few < many


def test_intensity_earthquake_magnitude_and_clamp():
    from services.processing.annotator import rate_intensity
    big = rate_intensity('disaster', 'earthquake', 'Magnitude 7.8 earthquake, 1,200 dead, catastrophic damage')
    small = rate_intensity('disaster', 'earthquake', 'Magnitude 4.1 earthquake felt in region')
    assert big > small
    assert big <= 1.0


def test_intensity_opinion_framing_penalized():
    from services.processing.annotator import rate_intensity
    plain = rate_intensity('political', 'election', 'Election results due')
    opinion = rate_intensity('political', 'election', 'Opinion: Election results due')
    assert opinion < plain


# ── extractive summary ────────────────────────────────────────────────────────

def test_summary_takes_leading_sentences_bounded():
    from services.processing.annotator import _extract_summary
    content = 'First sentence here. Second one follows. Third is fine. Fourth must not appear.'
    out = _extract_summary(content)
    assert out.startswith('First sentence here.')
    assert 'Third is fine.' in out
    assert 'Fourth' not in out


def test_summary_empty_content_returns_empty():
    from services.processing.annotator import _extract_summary
    assert _extract_summary('') == ''
    assert _extract_summary('   \n  ') == ''


# ── prototype classification (fake encoder — no model download) ──────────────

class _FakeClusterer:
    """encode() maps a known text to the one-hot vector of a prototype index,
    and unknown text to all-zeros (maximally weak match)."""

    def __init__(self, mapping: dict[str, int], dim: int):
        self.mapping, self.dim = mapping, dim

    def encode(self, texts):
        import torch
        out = torch.zeros((len(texts), self.dim))
        for i, t in enumerate(texts):
            j = self.mapping.get(t)
            if j is not None:
                out[i][j] = 1.0
        return out


@contextmanager
def _fake_prototype_world():
    """Patch the shared clusterer so prototype texts embed to an identity
    matrix — classification becomes exact and deterministic."""
    from services.processing import annotator
    from services.processing.taxonomy import PROTOTYPES

    flat = [(pair, text) for pair, texts in PROTOTYPES.items() for text in texts]
    mapping = {text: i for i, (_, text) in enumerate(flat)}
    fake = _FakeClusterer(mapping, dim=len(flat))
    annotator._prototypes.cache_clear()
    try:
        with patch('services.processing.clustering.get_clusterer', return_value=fake):
            yield flat, mapping, fake
    finally:
        annotator._prototypes.cache_clear()


def test_classify_cosine_exact_prototype_match_with_confidence():
    # classify_batch's primary path is now zero-shot NLI; the nearest-prototype
    # cosine matcher is the fallback (_classify_cosine). This exercises that
    # fallback's exact-match property directly.
    from services.processing.annotator import NLPAnnotator
    from services.processing.taxonomy import PROTOTYPES

    with _fake_prototype_world():
        war_text = PROTOTYPES[('conflict', 'war')][0]
        flood_text = PROTOTYPES[('disaster', 'flood')][0]
        got = NLPAnnotator()._classify_cosine([war_text, flood_text])
    assert [(c, s) for c, s, _ in got] == [('conflict', 'war'), ('disaster', 'flood')]
    assert all(conf >= 0.99 for _, _, conf in got)


def test_best_sub_within_category():
    from services.processing.annotator import best_sub
    from services.processing.taxonomy import PROTOTYPES

    with _fake_prototype_world():
        flood_text = PROTOTYPES[('disaster', 'flood')][0]
        assert best_sub('disaster', [flood_text]) == ['flood']
        # Unknown text → all-zero similarity → below sub floor → 'other'
        assert best_sub('disaster', ['completely unknown text']) == ['other']


def test_annotate_batch_end_to_end_with_fakes():
    """Full annotation with NER disabled: classification via the fake encoder,
    geo via the regex country fallback, rule intensity, extractive summary —
    confidence reported for the caller to decide the next stage (NLPAnnotator
    itself doesn't know about Article/stage — see annotator.py's docstring)."""
    from services.processing import annotator
    from services.processing.annotator import ESCALATE_BELOW, NLPAnnotator

    doc = _doc(
        'Floods submerge villages across Pakistan',
        'Heavy monsoon rains flooded the region. Thousands were evacuated. Rescue efforts continue.',
    )
    with _fake_prototype_world():
        with _disabled('NER_ENABLED', annotator._ner_pipeline):
            with patch.object(NLPAnnotator, 'classify_batch', return_value=[('disaster', 'flood', 0.9)]):
                [f] = NLPAnnotator().annotate_batch([doc], lite_flags=True)

    assert f.llm_error is None
    assert f.confidence >= ESCALATE_BELOW
    assert (f.category, f.sub_category) == ('disaster', 'flood')
    assert f.llm_data['country'] == 'Pakistan'
    assert f.latitude is not None and f.longitude is not None
    assert 0.0 <= f.event_intensity <= 1.0
    en = f.translations['en']
    assert en['title'] == doc.title
    assert en['summary'].startswith('Heavy monsoon rains')
    assert f.llm_data['annotator'] == 'nlp'


def test_annotate_batch_reports_low_confidence():
    from services.processing import annotator
    from services.processing.annotator import ESCALATE_BELOW, NLPAnnotator

    with _fake_prototype_world():
        with _disabled('NER_ENABLED', annotator._ner_pipeline):
            with patch.object(NLPAnnotator, 'classify_batch', return_value=[('general', 'other', 0.10)]):
                [f] = NLPAnnotator().annotate_batch([_doc('Some ambiguous headline')], lite_flags=True)
    assert f.llm_error is None
    assert f.confidence < ESCALATE_BELOW


# ── _locate: NER person-name collisions + city/country conflicts ────────────

def test_locate_drops_loc_entity_matching_person_name_token():
    """Confirmed live bug: "Trump endorses Jim Jordan for House Speaker" was
    geocoded to the country Jordan — the NER model mistagged the surname LOC.
    A same-document PER span for the full name ("Jim Jordan") excludes the
    single-token LOC reading of the same word."""
    from services.processing import annotator
    from services.processing.annotator import NLPAnnotator

    fake_raw = [[
        {'entity_group': 'PER', 'word': 'Jim Jordan', 'score': 0.99},
        {'entity_group': 'LOC', 'word': 'Jordan', 'score': 0.95},
    ]]

    with patch.object(annotator, '_ner_pipeline', return_value=(lambda texts, batch_size=8: fake_raw)):
        [(city, country)] = NLPAnnotator()._locate(['Trump endorses Jim Jordan for House Speaker'])
    assert city is None
    assert country is None


def test_locate_keeps_loc_entity_with_no_person_name_collision():
    from services.processing import annotator
    from services.processing.annotator import NLPAnnotator

    fake_raw = [[{'entity_group': 'LOC', 'word': 'Jordan', 'score': 0.95}]]

    with patch.object(annotator, '_ner_pipeline', return_value=(lambda texts, batch_size=8: fake_raw)):
        [(_city, country)] = NLPAnnotator()._locate(['Aid convoy crosses into Jordan from Syria'])
    assert country == 'Jordan'


def test_locate_resolves_city_country_conflict_to_citys_own_country():
    """Confirmed live bug: a Kyiv/EU-diplomacy piece geocoded as "Kyiv,
    Russia" — the real city's own country wins over a mismatched mention."""
    from services.processing import annotator
    from services.processing.annotator import NLPAnnotator

    fake_raw = [[
        {'entity_group': 'LOC', 'word': 'Kyiv', 'score': 0.95},
        {'entity_group': 'LOC', 'word': 'Russia', 'score': 0.9},
    ]]

    with patch.object(annotator, '_ner_pipeline', return_value=(lambda texts, batch_size=8: fake_raw)):
        [(city, country)] = NLPAnnotator()._locate(['Diplomatic push continues amid Kyiv EU talks'])
    assert city == 'Kyiv'
    assert country == 'Ukraine'


def test_annotate_batch_classifier_failure_marks_error_for_retry():
    from services.processing.annotator import NLPAnnotator

    with patch.object(NLPAnnotator, 'classify_batch', side_effect=RuntimeError('model exploded')):
        [f] = NLPAnnotator().annotate_batch([_doc('Title', 'Body.')], lite_flags=True)
    assert f.llm_error is not None and 'model exploded' in f.llm_error
    assert f.confidence == 0.0  # caller must not treat a failed batch as confident
    assert f.category == 'general'


_TESTS = [
    test_taxonomy_prototypes_cover_every_pair,
    test_llm_analyzer_prompt_still_builds_from_shared_taxonomy,
    test_find_place_matches_country_in_headline,
    test_find_place_skips_ambiguous_english_words,
    test_canonical_country_resolves_aliases_and_rejects_noise,
    test_is_city_covers_aliases,
    test_country_of_city_backfills_canonical_country,
    test_city_country_conflict_detects_mismatched_pair,
    test_city_country_conflict_allows_matching_pair,
    test_city_country_conflict_false_when_either_missing,
    test_city_country_conflict_false_for_unresolvable_city,
    test_intensity_starts_from_taxonomy_prior,
    test_intensity_casualties_escalate_by_magnitude,
    test_intensity_earthquake_magnitude_and_clamp,
    test_intensity_opinion_framing_penalized,
    test_summary_takes_leading_sentences_bounded,
    test_summary_empty_content_returns_empty,
    test_classify_cosine_exact_prototype_match_with_confidence,
    test_best_sub_within_category,
    test_annotate_batch_end_to_end_with_fakes,
    test_annotate_batch_reports_low_confidence,
    test_annotate_batch_classifier_failure_marks_error_for_retry,
    test_locate_drops_loc_entity_matching_person_name_token,
    test_locate_keeps_loc_entity_with_no_person_name_collision,
    test_locate_resolves_city_country_conflict_to_citys_own_country,
]


if __name__ == '__main__':
    run(_TESTS)
