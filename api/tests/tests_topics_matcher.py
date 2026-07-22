"""Dependency-light self-tests for the topic matchers: TopicMatcher (keyword
overlap) and EmbeddingTopicMatcher (local sentence-transformer similarity).

No database or model download required — EmbeddingTopicMatcher's embedding step
is exercised with a fake clusterer returning small hand-picked vectors, so the
real cosine-similarity math (sentence_transformers.util.cos_sim) runs for real
without needing the actual ~470MB multilingual model.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_topics_matcher
"""

import datetime

from tests._runner import bootstrap_django, run

bootstrap_django()


def _dt(y, m, d):
    return datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc)


class _FakeEvent:
    def __init__(self, pk, title='', location_name='', category='general', started_at=None):
        self.pk = pk
        self.title = title
        self.location_name = location_name
        self.category = category
        self.started_at = started_at


class _FakeTopic:
    def __init__(self, slug, name, keywords=None, description='', started_at=None):
        self.slug = slug
        self.name = name
        self.keywords = keywords or []
        self.description = description
        self.started_at = started_at


# ── TopicMatcher (keyword overlap) ────────────────────────────────────────────

def test_topic_matcher_overlaps_and_scores():
    from services.topics.matcher import TopicMatcher
    matcher = TopicMatcher()
    event = _FakeEvent('e1', title='Russia Ukraine war escalates', location_name='Kyiv')
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['ukraine', 'russia', 'war'])]
    result = matcher.match(event, topics)
    assert 'ukraine-war' in result
    assert 0.0 < result['ukraine-war'] <= 1.0


def test_topic_matcher_no_overlap_returns_empty():
    from services.topics.matcher import TopicMatcher
    matcher = TopicMatcher()
    event = _FakeEvent('e1', title='Completely unrelated zzqxv story', location_name='')
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['ukraine', 'russia', 'war'])]
    assert matcher.match(event, topics) == {}


def test_topic_matcher_empty_event_title_and_location():
    from services.topics.matcher import TopicMatcher
    matcher = TopicMatcher()
    event = _FakeEvent('e1', title='', location_name='')
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['ukraine'])]
    assert matcher.match(event, topics) == {}


def test_topic_matcher_topic_with_no_keywords_never_matches():
    from services.topics.matcher import TopicMatcher
    matcher = TopicMatcher()
    event = _FakeEvent('e1', title='Russia Ukraine war', location_name='')
    topics = [_FakeTopic('empty-topic', '', keywords=[])]
    assert matcher.match(event, topics) == {}


def test_topic_matcher_rejects_topic_that_postdates_event():
    """Confirmed live bug: an October 2023 event tagged with a topic for a
    crisis (2026) that hadn't happened yet — a topic's own started_at must
    not predate the event it's applied to."""
    from services.topics.matcher import TopicMatcher
    matcher = TopicMatcher()
    event = _FakeEvent('e1', title='Iran fuel crisis unrest', location_name='Tehran', started_at=_dt(2023, 10, 15))
    topics = [_FakeTopic(
        '2026-iran-war-fuel-crisis', 'Iran war fuel crisis',
        keywords=['iran', 'fuel', 'crisis'], started_at=_dt(2026, 1, 1),
    )]
    assert matcher.match(event, topics) == {}


def test_topic_matcher_allows_topic_with_no_started_at():
    """Legacy topics predating the started_at field (null) have nothing to
    check against and must still be eligible to match."""
    from services.topics.matcher import TopicMatcher
    matcher = TopicMatcher()
    event = _FakeEvent('e1', title='Russia Ukraine war', location_name='Kyiv', started_at=_dt(2023, 10, 15))
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['russia', 'ukraine', 'war'], started_at=None)]
    assert 'ukraine-war' in matcher.match(event, topics)


def test_topic_matcher_allows_topic_that_predates_event():
    from services.topics.matcher import TopicMatcher
    matcher = TopicMatcher()
    event = _FakeEvent('e1', title='Russia Ukraine war update', location_name='Kyiv', started_at=_dt(2023, 10, 15))
    topics = [_FakeTopic(
        'ukraine-war', 'Russia-Ukraine war', keywords=['russia', 'ukraine', 'war'],
        started_at=_dt(2022, 2, 24),
    )]
    assert 'ukraine-war' in matcher.match(event, topics)


# ── EmbeddingTopicMatcher ──────────────────────────────────────────────────────

def test_embedding_matcher_empty_events_short_circuits():
    from services.topics.matcher import EmbeddingTopicMatcher
    matcher = EmbeddingTopicMatcher()
    results, sources = matcher.match_batch([], [_FakeTopic('t1', 'Topic One')])
    assert results == {} and sources == {}


def test_embedding_matcher_empty_topics_short_circuits():
    from services.topics.matcher import EmbeddingTopicMatcher
    matcher = EmbeddingTopicMatcher()
    event = _FakeEvent('e1', title='Some event')
    results, sources = matcher.match_batch([event], [])
    assert results == {'e1': {}} and sources == {'e1': 'keyword'}


def test_embedding_matcher_keyword_prefilter_excludes_no_overlap_event():
    """An event with zero keyword overlap against every topic never reaches the
    embedding step at all — it stays 'keyword' with an empty result."""
    from services.topics.matcher import EmbeddingTopicMatcher
    matcher = EmbeddingTopicMatcher()
    event = _FakeEvent('e1', title='Something totally unrelated zzqxv')
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['ukraine', 'russia', 'war'])]
    results, sources = matcher.match_batch([event], topics)
    assert results == {'e1': {}}
    assert sources == {'e1': 'keyword'}


def test_embedding_matcher_tags_candidate_above_threshold():
    """An event that clears the keyword pre-filter and scores above SIM_THRESHOLD
    gets tagged with a rescaled [0.5, 1.0] confidence."""
    from services.topics.matcher import EmbeddingTopicMatcher
    from unittest.mock import patch
    import torch

    matcher = EmbeddingTopicMatcher()
    event = _FakeEvent('e1', title='Ukraine war update', location_name='Kyiv')
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['ukraine', 'war'])]

    class FakeClusterer:
        def encode(self, texts):
            # One vector per text; event/topic vectors identical → cos_sim == 1.0.
            return torch.tensor([[1.0, 0.0]] * len(texts))

    with patch('services.processing.clustering.get_clusterer', return_value=FakeClusterer()):
        results, sources = matcher.match_batch([event], topics)

    assert 'ukraine-war' in results['e1']
    assert 0.5 <= results['e1']['ukraine-war'] <= 1.0
    assert sources['e1'] == 'embed'


def test_embedding_matcher_rejects_topic_that_postdates_event_even_above_threshold():
    """Same confirmed bug as the TopicMatcher case, but for the embedding
    path: a topic that postdates the event must not be tagged even when its
    cosine score clears SIM_THRESHOLD. Two topics with keyword overlap (one
    anachronistic, one not) so the event still clears the keyword pre-filter
    and reaches the embedding loop, exercising that loop's own guard rather
    than just the pre-filter's."""
    from services.topics.matcher import EmbeddingTopicMatcher
    from unittest.mock import patch
    import torch

    matcher = EmbeddingTopicMatcher()
    event = _FakeEvent(
        'e1', title='Iran fuel crisis unrest', location_name='Tehran',
        started_at=_dt(2023, 10, 15),
    )
    future_topic = _FakeTopic(
        '2026-iran-war-fuel-crisis', 'Iran war fuel crisis',
        keywords=['iran', 'fuel', 'crisis'], started_at=_dt(2026, 1, 1),
    )
    valid_topic = _FakeTopic(
        'iran-unrest', 'Iran unrest', keywords=['iran', 'crisis'], started_at=_dt(2023, 9, 1),
    )
    topics = [future_topic, valid_topic]

    class FakeClusterer:
        def encode(self, texts):
            # Every vector identical → cos_sim == 1.0 for both topics, so only
            # the temporal guard (not the similarity score) can be excluding one.
            return torch.tensor([[1.0, 0.0]] * len(texts))

    with patch('services.processing.clustering.get_clusterer', return_value=FakeClusterer()):
        results, sources = matcher.match_batch([event], topics)

    assert '2026-iran-war-fuel-crisis' not in results['e1']
    assert 'iran-unrest' in results['e1']
    assert sources['e1'] == 'embed'


def test_embedding_matcher_below_threshold_not_tagged():
    from services.topics.matcher import EmbeddingTopicMatcher
    from unittest.mock import patch
    import torch

    matcher = EmbeddingTopicMatcher()
    event = _FakeEvent('e1', title='Ukraine war update', location_name='Kyiv')
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['ukraine', 'war'])]

    topic_text = EmbeddingTopicMatcher._topic_text(topics[0])
    event_text = EmbeddingTopicMatcher._event_text(event)

    class FakeClusterer:
        """Return orthogonal vectors keyed by which text is being embedded (the
        known topic text vs. the known event text) rather than by call order,
        so this stays correct even if match_batch's internal encode() call
        order changes — cos_sim(topic_emb, event_emb) == 0.0, below SIM_THRESHOLD."""
        def encode(self, texts):
            return torch.tensor([[0.0, 1.0] if t == topic_text else [1.0, 0.0] for t in texts])

    assert event_text != topic_text  # sanity: the two texts must actually differ

    with patch('services.processing.clustering.get_clusterer', return_value=FakeClusterer()):
        results, sources = matcher.match_batch([event], topics)

    assert results['e1'] == {}
    assert sources['e1'] == 'embed'


def test_embedding_matcher_falls_back_to_keyword_on_encode_failure():
    """A failure anywhere in the embed/similarity step (not just model load)
    degrades to the keyword matcher instead of raising."""
    from services.topics.matcher import EmbeddingTopicMatcher
    from unittest.mock import patch

    matcher = EmbeddingTopicMatcher()
    event = _FakeEvent('e1', title='Ukraine war update', location_name='Kyiv')
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['ukraine', 'war'])]

    class ExplodingClusterer:
        def encode(self, texts):
            raise RuntimeError('embedding backend exploded')

    with patch('services.processing.clustering.get_clusterer', return_value=ExplodingClusterer()):
        results, sources = matcher.match_batch([event], topics)

    # Falls back to TopicMatcher — event has real keyword overlap, so it's tagged.
    assert 'ukraine-war' in results['e1']
    assert sources['e1'] == 'keyword'


def test_embedding_matcher_falls_back_when_clusterer_import_fails():
    from services.topics.matcher import EmbeddingTopicMatcher
    from unittest.mock import patch

    matcher = EmbeddingTopicMatcher()
    event = _FakeEvent('e1', title='Ukraine war update', location_name='Kyiv')
    topics = [_FakeTopic('ukraine-war', 'Russia-Ukraine war', keywords=['ukraine', 'war'])]

    with patch('services.processing.clustering.get_clusterer', side_effect=RuntimeError('no model')):
        results, sources = matcher.match_batch([event], topics)

    assert 'ukraine-war' in results['e1']
    assert sources['e1'] == 'keyword'


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_topic_matcher_overlaps_and_scores,
    test_topic_matcher_no_overlap_returns_empty,
    test_topic_matcher_empty_event_title_and_location,
    test_topic_matcher_topic_with_no_keywords_never_matches,
    test_topic_matcher_rejects_topic_that_postdates_event,
    test_topic_matcher_allows_topic_with_no_started_at,
    test_topic_matcher_allows_topic_that_predates_event,
    test_embedding_matcher_empty_events_short_circuits,
    test_embedding_matcher_empty_topics_short_circuits,
    test_embedding_matcher_keyword_prefilter_excludes_no_overlap_event,
    test_embedding_matcher_tags_candidate_above_threshold,
    test_embedding_matcher_rejects_topic_that_postdates_event_even_above_threshold,
    test_embedding_matcher_below_threshold_not_tagged,
    test_embedding_matcher_falls_back_to_keyword_on_encode_failure,
    test_embedding_matcher_falls_back_when_clusterer_import_fails,
]


if __name__ == '__main__':
    run(_TESTS)
