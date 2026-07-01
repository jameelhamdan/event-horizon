"""Semantic clustering of articles by title similarity.

Uses a lightweight sentence-transformer model to group articles that describe
the same real-world event, even when their wording differs. Runs AFTER the
primary geographic + calendar-day bucketing so that two unrelated events in the
same city on the same day are kept separate.
"""

import logging
import time
from functools import cached_property

from settings.model_names import CLUSTER_MODEL_NAME as _MODEL_NAME

logger = logging.getLogger(__name__)

# Cosine-similarity threshold: pairs above this are considered the same event.
# Lower → larger clusters (more merging); higher → more splits.
DEFAULT_THRESHOLD = 0.55

# CPU batch size for encode() — larger batches amortize per-call Python/tensor
# overhead better than the sentence-transformers default (32) for our short,
# headline-length inputs.
_BATCH_SIZE = 64


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
        try:
            model = SentenceTransformer(_MODEL_NAME, backend="onnx")
            logger.info("[cluster] Loaded sentence-transformer model (onnx backend): %s", _MODEL_NAME)
        except Exception:
            logger.exception(
                "[cluster] ONNX backend unavailable for %s — falling back to torch backend", _MODEL_NAME,
            )
            model = SentenceTransformer(_MODEL_NAME)
        return model

    def encode(self, texts: list[str], batch_size: int = _BATCH_SIZE):
        """Embed *texts* with the shared multilingual model (normalized, tensor output)."""
        if not texts:
            import torch
            return torch.empty(0)
        started = time.monotonic()
        embeddings = self._model.encode(
            texts,
            batch_size=batch_size,
            convert_to_tensor=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        elapsed = time.monotonic() - started
        if elapsed > 1.0:
            logger.info('[cluster] encoded %d text(s) in %.2fs', len(texts), elapsed)
        return embeddings

    def encode_articles(self, articles: list) -> tuple:
        """Embed *articles* by title, reusing ``Article.title_embedding`` where present.

        Only articles missing a cached embedding (or cached under a different
        model — see ``title_embedding_model``) are sent through the model; the
        rest are read straight off the object. Freshly computed embeddings are
        written back onto the passed-in objects (``title_embedding`` /
        ``title_embedding_model``).

        Returns ``(embeddings, newly_computed)`` — a tensor aligned 1:1 with
        *articles*, and the subset of *articles* that got a fresh embedding
        (for the caller to persist — see ``_flush_embedding_cache``).
        """
        import torch

        vectors: list = [None] * len(articles)
        miss_idx: list[int] = []
        miss_titles: list[str] = []
        for i, a in enumerate(articles):
            cached = getattr(a, 'title_embedding', None)
            cached_model = getattr(a, 'title_embedding_model', None)
            if cached and cached_model == _MODEL_NAME:
                vectors[i] = torch.tensor(cached, dtype=torch.float32)
            else:
                miss_idx.append(i)
                miss_titles.append(a.title or "")

        newly_computed: list = []
        if miss_titles:
            computed = self.encode(miss_titles)
            for pos, idx in enumerate(miss_idx):
                vec = computed[pos]
                vectors[idx] = vec
                articles[idx].title_embedding = vec.tolist()
                articles[idx].title_embedding_model = _MODEL_NAME
                newly_computed.append(articles[idx])
            logger.info(
                '[cluster] embedding cache: %d/%d article(s) hit, %d computed',
                len(articles) - len(miss_titles), len(articles), len(miss_titles),
            )

        return torch.stack(vectors), newly_computed

    def cluster(self, articles: list, threshold: float = DEFAULT_THRESHOLD) -> list[list]:
        """
        Split *articles* into semantic sub-clusters.

        Returns a list of article lists.  Every article appears in exactly one
        cluster.  Single-article inputs are returned as-is without model I/O.
        """
        if len(articles) <= 1:
            return [articles]

        titles = [a.title or "" for a in articles]
        embeddings = self.encode(titles)
        return self._communities(articles, embeddings, threshold)

    def cluster_many(self, groups: list[list], threshold: float = DEFAULT_THRESHOLD) -> list[list[list]]:
        """Cluster several independent article groups in a single batched embedding pass.

        Equivalent to calling ``cluster()`` once per group, but embeds every
        title across all groups in one model call (far fewer, larger batches —
        the dominant cost on CPU is per-call overhead, not raw FLOPs) and
        reuses ``Article.title_embedding`` where cached. Returns one sub-cluster
        list per input group, in the same order.
        """
        flat: list = []
        bounds: list[tuple[int, int]] = []
        start = 0
        for group in groups:
            flat.extend(group)
            bounds.append((start, start + len(group)))
            start += len(group)

        if not flat:
            return [[] for _ in groups]

        embeddings, newly_computed = self.encode_articles(flat)
        self._flush_embedding_cache(newly_computed)

        results: list[list[list]] = []
        for group, (s, e) in zip(groups, bounds):
            if e - s <= 1:
                results.append([group] if group else [])
                continue
            results.append(self._communities(group, embeddings[s:e], threshold))
        return results

    @staticmethod
    def _flush_embedding_cache(newly_computed: list) -> None:
        """Persist freshly computed ``title_embedding``/``title_embedding_model`` values."""
        if not newly_computed:
            return
        from core.models import Article

        try:
            Article.objects.bulk_update(newly_computed, ['title_embedding', 'title_embedding_model'], batch_size=500)
        except Exception:
            logger.exception('[cluster] failed to persist %d cached embedding(s)', len(newly_computed))

    @staticmethod
    def _communities(articles: list, embeddings, threshold: float) -> list[list]:
        from sentence_transformers import util

        # community_detection groups sentences whose cosine similarity exceeds
        # *threshold*.  Each sentence appears in at most one community.
        detect_started = time.monotonic()
        communities = util.community_detection(
            embeddings,
            min_community_size=1,
            threshold=threshold,
        )
        detect_elapsed = time.monotonic() - detect_started
        if detect_elapsed > 1.0:
            logger.info(
                '[cluster] community_detection over %d article(s) took %.2fs -> %d community/ies',
                len(articles), detect_elapsed, len(communities),
            )

        assigned: set[int] = set()
        result: list[list] = []
        for community in communities:
            result.append([articles[i] for i in community])
            assigned.update(community)

        # Safety net: any article not assigned to a community becomes a singleton.
        singleton_count = 0
        for i in range(len(articles)):
            if i not in assigned:
                result.append([articles[i]])
                singleton_count += 1
        if singleton_count:
            logger.debug('[cluster] %d article(s) fell back to singleton clusters', singleton_count)

        return result


# Module-level singleton — model is loaded once and reused across calls.
_clusterer: SemanticClusterer | None = None


def get_clusterer() -> SemanticClusterer:
    global _clusterer
    if _clusterer is None:
        _clusterer = SemanticClusterer()
    return _clusterer
