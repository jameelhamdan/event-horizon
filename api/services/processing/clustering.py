"""Semantic clustering of articles by title similarity.

Uses a lightweight sentence-transformer model to group articles that describe
the same real-world event, even when their wording differs. Runs AFTER the
primary geographic + calendar-day bucketing so that two unrelated events in the
same city on the same day are kept separate.
"""

import logging
from functools import cached_property

logger = logging.getLogger(__name__)

# Cosine-similarity threshold: pairs above this are considered the same event.
# Lower → larger clusters (more merging); higher → more splits.
DEFAULT_THRESHOLD = 0.55

# Compact multilingual model (~90 MB) — good balance of speed and accuracy.
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"


class SemanticClusterer:
    """Groups a list of Article objects by the semantic similarity of their titles.

    Also the shared embedding accessor for anything else that wants this exact
    multilingual model (e.g. services.topics.matcher.EmbeddingTopicMatcher) — call
    ``encode()`` rather than reaching into ``_model`` directly, so model-loading
    details stay internal to this class.
    """

    @cached_property
    def _model(self):
        from sentence_transformers import SentenceTransformer
        logger.info("[cluster] Loading sentence-transformer model: %s", _MODEL_NAME)
        return SentenceTransformer(_MODEL_NAME)

    def encode(self, texts: list[str]):
        """Embed *texts* with the shared multilingual model (normalized, tensor output)."""
        return self._model.encode(
            texts,
            convert_to_tensor=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

    def cluster(self, articles: list, threshold: float = DEFAULT_THRESHOLD) -> list[list]:
        """
        Split *articles* into semantic sub-clusters.

        Returns a list of article lists.  Every article appears in exactly one
        cluster.  Single-article inputs are returned as-is without model I/O.
        """
        if len(articles) <= 1:
            return [articles]

        from sentence_transformers import util

        titles = [a.title or "" for a in articles]
        embeddings = self.encode(titles)

        # community_detection groups sentences whose cosine similarity exceeds
        # *threshold*.  Each sentence appears in at most one community.
        communities = util.community_detection(
            embeddings,
            min_community_size=1,
            threshold=threshold,
        )

        assigned: set[int] = set()
        result: list[list] = []
        for community in communities:
            result.append([articles[i] for i in community])
            assigned.update(community)

        # Safety net: any article not assigned to a community becomes a singleton.
        for i in range(len(articles)):
            if i not in assigned:
                result.append([articles[i]])

        return result


# Module-level singleton — model is loaded once and reused across calls.
_clusterer: SemanticClusterer | None = None


def get_clusterer() -> SemanticClusterer:
    global _clusterer
    if _clusterer is None:
        _clusterer = SemanticClusterer()
    return _clusterer
