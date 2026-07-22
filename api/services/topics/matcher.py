"""
Topic matchers.

TopicMatcher          — keyword-overlap matching (no LLM, fast, used for retroactive tagging).
EmbeddingTopicMatcher — local sentence-transformer semantic matching (no LLM, used for the
                        regular tagging pipeline). Falls back to TopicMatcher if the model
                        can't be loaded.
"""
import logging

from services.utils import tokenize as _tokenize

logger = logging.getLogger(__name__)

# Minimum fraction of topic keywords that must match for a tag to apply
_MIN_OVERLAP = 0.1
# Minimum absolute keyword matches (whichever is higher wins)
_MIN_MATCHES = 1


def _is_anachronistic(topic, event) -> bool:
    """True if *topic* couldn't plausibly apply to *event* — the topic's own
    tracked start postdates the event (e.g. a topic discovered for 2026 news
    must never tag an October 2023 event; confirmed live: purely semantic
    cosine similarity was pairing events with topics for crises that hadn't
    happened yet). Topics without a started_at (legacy rows predating the
    field) have nothing to check against and are never excluded here."""
    return bool(topic.started_at and event.started_at and topic.started_at > event.started_at)


class TopicMatcher:

    def match(self, event, topics: list) -> dict[str, float]:
        """
        Match event against topics.

        Args:
            event: Event model instance (uses .title and .location_name)
            topics: list of Topic model instances

        Returns:
            dict mapping slug → confidence score (0.0–1.0) for matched topics
        """
        event_tokens = _tokenize(event.title or '') | _tokenize(event.location_name or '')
        if not event_tokens:
            return {}

        result: dict[str, float] = {}
        for topic in topics:
            if _is_anachronistic(topic, event):
                continue
            kw_tokens: set[str] = set()
            for kw in (topic.keywords or []):
                kw_tokens |= _tokenize(kw)
            kw_tokens |= _tokenize(topic.name)

            if not kw_tokens:
                continue

            overlap = event_tokens & kw_tokens
            n = len(overlap)
            if n < _MIN_MATCHES:
                continue

            frac = n / len(kw_tokens)
            if frac < _MIN_OVERLAP:
                continue

            score = round(min(1.0, 0.3 + frac), 3)
            result[topic.slug] = score

        return result


class EmbeddingTopicMatcher:
    """
    Local semantic matcher — pre-filters with keyword overlap, then encodes
    surviving candidates and topics with the same multilingual sentence-transformer
    used for article clustering
    (services.processing.clustering) and matches by cosine similarity. No LLM
    calls, no per-token cost.

    Falls back to TopicMatcher (keyword overlap) for the whole batch if embedding
    or similarity computation fails for any reason.
    """

    # Cosine similarity threshold for a confident topic match. Tuned against the
    # paraphrase-multilingual-MiniLM-L12-v2 model's typical short-text similarity
    # range (0.55 clustering threshold is for title-vs-title; topic-vs-event text
    # is less homogeneous, hence the lower bar here).
    SIM_THRESHOLD = 0.42

    @staticmethod
    def _topic_text(topic) -> str:
        parts = [topic.name]
        if topic.description:
            parts.append(topic.description[:200])
        if topic.keywords:
            parts.append(' '.join(topic.keywords[:15]))
        return ' — '.join(parts)

    @staticmethod
    def _event_text(event) -> str:
        return ' '.join(filter(None, [event.title or '', event.location_name or '', event.category or '']))

    def match_batch(
        self,
        events: list,
        topics: list,
    ) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
        """
        Match a list of Event objects against a list of Topic objects.

        Returns a ``(results, sources)`` tuple:
          - ``results``: {str(event.pk): {topic_slug: confidence}} for all events.
          - ``sources``: {str(event.pk): 'embed' | 'keyword'} — 'keyword' marks events
            tagged by the fallback (embedding model unavailable, or no keyword
            candidate at all — see pre-filter below).

        Pre-filters with a keyword-overlap gate: an event with zero keyword
        overlap against every topic essentially never matches semantically
        either, and skipping it keeps false-positive risk from the embedding
        threshold bounded to events that already show some lexical relation
        to a topic.
        """
        results: dict[str, dict[str, float]] = {str(e.pk): {} for e in events}
        sources: dict[str, str] = {str(e.pk): 'keyword' for e in events}
        if not events or not topics:
            return results, sources

        keyword = TopicMatcher()
        candidates = [e for e in events if keyword.match(e, topics)]
        if not candidates:
            return results, sources

        # Everything from embedding load through the similarity matrix is one unit of
        # work — any failure in it (not just the model failing to load) should degrade
        # to the keyword fallback rather than crash the tagging job.
        try:
            from services.processing.clustering import get_clusterer
            from sentence_transformers import util

            clusterer = get_clusterer()
            topic_texts = [self._topic_text(t) for t in topics]
            event_texts = [self._event_text(e) for e in candidates]
            topic_emb = clusterer.encode(topic_texts)
            event_emb = clusterer.encode(event_texts)
            sim = util.cos_sim(event_emb, topic_emb)  # [n_candidates, n_topics]
        except Exception as exc:
            logger.warning('[topics] embedding matching failed (%s) — falling back to keyword matcher', exc)
            for event in candidates:
                results[str(event.pk)] = keyword.match(event, topics)
            return results, sources

        for i, event in enumerate(candidates):
            key = str(event.pk)
            matched: dict[str, float] = {}
            for j, topic in enumerate(topics):
                if _is_anachronistic(topic, event):
                    continue
                score = float(sim[i][j])
                if score >= self.SIM_THRESHOLD:
                    # Rescale into a [0.5, 1.0] confidence band.
                    matched[topic.slug] = round(min(1.0, 0.5 + (score - self.SIM_THRESHOLD)), 3)
            if matched:
                logger.info(
                    '[topics] embedding tagged "%s" → %s',
                    (event.title or '')[:60],
                    ', '.join(f'{s}({c:.2f})' for s, c in matched.items()),
                )
            results[key] = matched
            sources[key] = 'embed'

        return results, sources
