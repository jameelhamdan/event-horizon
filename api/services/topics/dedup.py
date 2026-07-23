"""Deduplicate TopicDicts by slug — keep the entry with the most keywords."""
from services.topics.types import TopicDict


def deduplicate_topics(topics: list[TopicDict]) -> list[TopicDict]:
    seen: dict[str, TopicDict] = {}
    for t in topics:
        slug = t.get('slug', '')
        if not slug:
            continue
        existing = seen.get(slug)
        if existing is None or len(t.get('keywords') or []) > len(existing.get('keywords') or []):
            seen[slug] = t
    return list(seen.values())


class _NameProxy:
    """Minimal proxy so SemanticClusterer.cluster() can read .title from a TopicDict."""
    __slots__ = ('title', '_topic')

    def __init__(self, topic: TopicDict):
        self.title = topic.get('name') or topic.get('slug') or ''
        self._topic = topic


def semantic_merge_topics(topics: list[TopicDict], threshold: float = 0.85) -> list[TopicDict]:
    """
    Merge semantically near-duplicate topics using the sentence-transformer clusterer.

    For each cluster the canonical entry is the one with the most keywords; all
    keywords and source_ids from the cluster are unioned into the canonical entry.
    Returns a deduplicated list.
    """
    if len(topics) <= 1:
        return topics

    from services.processing.clustering import get_clusterer

    proxies = [_NameProxy(t) for t in topics]
    clusters = get_clusterer().cluster(proxies, threshold=threshold)

    merged: list[TopicDict] = []
    for cluster in clusters:
        if len(cluster) == 1:
            merged.append(cluster[0]._topic)
            continue

        # Canonical = entry with most keywords
        canonical: TopicDict = max((p._topic for p in cluster), key=lambda t: len(t.get('keywords') or []))

        # Union keywords and source_ids from all members (skip canonical to avoid duplication)
        all_keywords: list[str] = list(canonical.get('keywords') or [])
        all_source_ids: list[str] = list(canonical.get('source_ids') or [])
        if canonical.get('source_id') and canonical['source_id'] not in all_source_ids:
            all_source_ids.append(canonical['source_id'])

        for proxy in cluster:
            t = proxy._topic
            if t is canonical:
                continue
            for kw in (t.get('keywords') or []):
                if kw not in all_keywords:
                    all_keywords.append(kw)
            for sid in (t.get('source_ids') or []):
                if sid not in all_source_ids:
                    all_source_ids.append(sid)
            single = t.get('source_id')
            if single and single not in all_source_ids:
                all_source_ids.append(single)

        result: TopicDict = {**canonical, 'keywords': all_keywords, 'source_ids': all_source_ids}
        merged.append(result)

    return merged
