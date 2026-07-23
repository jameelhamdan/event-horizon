"""
Article importance post-processing.

The annotate stage computes a rules base score per article (its 0-1 intensity
mapped onto 1-10 — see services.workflow.articles.annotate_articles) and
ImportanceScorer applies the DB-driven adjustments on top:
  - source.weight multiplier (weight=0 is honoured, not coerced to 1)
  - cross-source corroboration bonus (+0.5 per extra source, max +2.0)
  - category importance floor (for already-categorised articles only)

There is no LLM anywhere in importance scoring — importance_source is 'rules'.
"""

import logging
from datetime import datetime, timedelta, timezone as dt_tz

from services.utils import tokenize as _tokenize, jaccard as _jaccard

logger = logging.getLogger(__name__)

# Applied only when article.category is already set (i.e. after NLP has run).
_CATEGORY_FLOORS: dict[str, float] = {
    'conflict':  6.0,
    'disaster':  6.0,
    'health':    5.0,
    'political': 4.0,
    'economic':  4.0,
}

_CORROBORATION_THRESHOLD = 0.5  # Jaccard token overlap
_CORROBORATION_BONUS     = 0.5  # per corroborating source
_CORROBORATION_MAX       = 2.0  # cap


class ImportanceScorer:
    DEFAULT_BASE = 5.0

    def score_from_intensity(self, articles: list, intensities: dict[str, float]) -> dict[str, float]:
        """Final importance per article from its 0-1 event intensity — the
        annotate stage's entry point. Maps intensity onto the 1-10 base scale,
        then applies the weight/corroboration/floor adjustments (see score()).
        Articles missing from ``intensities`` are skipped (their annotation
        failed — they'll be rescored on retry)."""
        bases = {aid: 1.0 + 9.0 * intensity for aid, intensity in intensities.items()}
        return self.score(articles, bases)

    def score(self, articles: list, bases: dict[str, float]) -> dict[str, float]:
        """Final importance per article: ``bases[article_id]`` (rules base,
        1-10) adjusted by source weight, corroboration and category floors.
        Articles missing from ``bases`` are skipped. Returns
        {str(article.id): score} clamped to [1.0, 10.0].
        """
        if not articles:
            return {}

        from core import models as m

        source_codes = {a.source_code for a in articles}
        source_weights: dict[str, float] = {s.code: s.weight for s in m.Source.objects.filter(code__in=source_codes)}
        bonuses = self._corroboration_bonuses(articles)

        results: dict[str, float] = {}
        for article in articles:
            base = bases.get(str(article.id))
            if base is None:
                continue
            # weight=None means the source row wasn't found; treat as 1.0.
            # weight=0 is an explicit suppression by the operator — honour it.
            weight = source_weights.get(article.source_code)
            if weight is None:
                weight = 1.0
            bonus = bonuses.get(str(article.id), 0.0)
            floor = _CATEGORY_FLOORS.get(article.category or '', 0.0)
            raw   = base * weight + bonus
            # category floor only kicks in for categorised articles
            results[str(article.id)] = round(max(1.0, min(10.0, max(raw, floor))), 2)

        return results

    def _corroboration_bonuses(self, articles: list) -> dict[str, float]:
        """
        For each article, count how many OTHER sources filed a similar title
        (Jaccard >= _CORROBORATION_THRESHOLD). Bonus: +0.5 per source, capped at +2.0.
        Uses the same tokenizer as the title dedup filter for consistency.

        Similar titles are looked for both in the last 24h of stored articles
        AND among the other members of this same batch — breaking news covered
        by several sources within one window used to earn nobody a bonus,
        because the batch excluded itself from the comparison set.
        """
        from core import models as m

        cutoff      = datetime.now(dt_tz.utc) - timedelta(hours=24)
        article_ids = [a.id for a in articles]

        recent_pairs = list(
            m.Article.objects.filter(created_on__gte=cutoff)
            .exclude(id__in=article_ids)
            .values_list('title', 'source_code')[:2000]
        )
        recent_tokensets: list[tuple[frozenset, str]] = [(_tokenize(title), src) for title, src in recent_pairs]
        batch_tokensets: list[tuple[frozenset, str, str]] = [(_tokenize(a.title), a.source_code, str(a.id)) for a in articles]

        bonuses: dict[str, float] = {}
        for my_tokens, my_source, my_id in batch_tokensets:
            corroborating: set[str] = set()
            for tokens, src in recent_tokensets:
                if src == my_source:
                    continue
                if _jaccard(my_tokens, tokens) >= _CORROBORATION_THRESHOLD:
                    corroborating.add(src)
            for tokens, src, other_id in batch_tokensets:
                if other_id == my_id or src == my_source:
                    continue
                if _jaccard(my_tokens, tokens) >= _CORROBORATION_THRESHOLD:
                    corroborating.add(src)
            bonuses[my_id] = min(len(corroborating) * _CORROBORATION_BONUS, _CORROBORATION_MAX)

        return bonuses
