"""
Article importance scoring.

ArticleImportanceScorer sends batches of up to BATCH_SIZE headlines to the
LLM (role='scoring') for 1.0–10.0 significance ratings, then applies:
  - source.weight multiplier (DB-driven; weight=0 is honoured, not coerced to 1)
  - cross-source corroboration bonus (+0.5 per extra source, max +2.0)
  - category importance floor (for already-categorised articles only)

score_unscored_articles() is the main entry point, called by the 'score'
pipeline stage (services/stages.py).
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone as dt_tz

from services.utils import tokenize as _tokenize, jaccard as _jaccard

logger = logging.getLogger(__name__)

_SCORE_PROMPT_HEADER = (
    'Rate each headline 1.0–10.0 by global significance'
    ' (geopolitical impact, population affected, economic scale, novelty).\n\n'
)
_SCORE_PROMPT_FOOTER = (
    '\n\nReturn a JSON array of scores in order, one float per headline: [7.5, 4.0, ...]'
)

# Applied only when article.category is already set (i.e. after NLP has run).
# For fresh unscored articles the floor is 0.0 because category is None.
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


class ArticleImportanceScorer:
    BATCH_SIZE    = 30
    DEFAULT_SCORE = 5.0

    def score_articles(self, articles: list) -> dict[str, float]:
        """
        Score a list of Article instances.
        Returns {str(article.id): final_score} clamped to [1.0, 10.0].
        """
        if not articles:
            return {}

        from core import models as m

        source_codes = {a.source_code for a in articles}
        source_weights: dict[str, float] = {
            s.code: s.weight
            for s in m.Source.objects.filter(code__in=source_codes)
        }

        bonuses = self._corroboration_bonuses(articles)

        results: dict[str, float] = {}
        for i in range(0, len(articles), self.BATCH_SIZE):
            batch      = articles[i : i + self.BATCH_SIZE]
            titles     = [a.title for a in batch]
            llm_scores = self.score_batch_llm(titles)
            for article, llm_score in zip(batch, llm_scores):
                # weight=None means the source row wasn't found; treat as 1.0.
                # weight=0 is an explicit suppression by the operator — honour it.
                weight = source_weights.get(article.source_code)
                if weight is None:
                    weight = 1.0
                bonus = bonuses.get(str(article.id), 0.0)
                floor = _CATEGORY_FLOORS.get(article.category or '', 0.0)
                raw   = llm_score * weight + bonus
                # category floor only kicks in for categorised articles
                final = max(1.0, min(10.0, max(raw, floor)))
                results[str(article.id)] = round(final, 2)

        return results

    def score_batch_llm(self, titles: list[str], role: str = 'scoring') -> list[float]:
        """
        Send up to BATCH_SIZE titles to the LLM; return a parallel list of floats.
        Falls back to DEFAULT_SCORE on any error.
        role: LLM_ROUTES key — callers can pass 'historical' to use a different route.
        """
        from services.llm import LLMError, get_llm_service, strip_code_fences

        default = [self.DEFAULT_SCORE] * len(titles)
        if not titles:
            return default

        lines  = '\n'.join(f'{i + 1}. {title}' for i, title in enumerate(titles))
        prompt = _SCORE_PROMPT_HEADER + lines + _SCORE_PROMPT_FOOTER

        raw = ''
        try:
            llm = get_llm_service(role)
            raw   = strip_code_fences(llm.chat([{'role': 'user', 'content': prompt}]))
            array = self._extract_json_array(raw)
            if array is None:
                logger.warning(
                    'LLM importance score: no JSON array in response (%r); using %.1f',
                    raw[:200], self.DEFAULT_SCORE,
                )
                return default
            data = json.loads(array)
            if not isinstance(data, list):
                return default
            return [
                float(data[i]) if i < len(data) else self.DEFAULT_SCORE
                for i in range(len(titles))
            ]
        except LLMError as exc:
            logger.warning('LLM importance scoring failed (%s); using %.1f', exc, self.DEFAULT_SCORE)
            return default
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            fallback = self._recover_scores_from_text(raw, len(titles))
            if fallback is not None:
                logger.info(
                    'LLM importance score parse error (%s); recovered %d/%d scores via regex fallback',
                    exc, len(fallback), len(titles),
                )
                return fallback
            logger.warning(
                'LLM importance score parse error (%s); raw=%r; using %.1f',
                exc, raw[:200], self.DEFAULT_SCORE,
            )
            return default

    def _recover_scores_from_text(self, raw: str, count: int) -> list[float] | None:
        """
        Best-effort salvage when the LLM's array isn't valid JSON (trailing comma,
        truncated tail, stray prose). Pulls every bare number out of the response
        in order; only used if it yields exactly as many numbers as titles, so a
        garbled/unrelated response can't silently masquerade as real scores.
        """
        numbers = re.findall(r'-?\d+(?:\.\d+)?', raw)
        if len(numbers) != count:
            return None
        try:
            return [float(n) for n in numbers]
        except ValueError:
            return None

    @staticmethod
    def _extract_json_array(raw: str) -> str | None:
        """
        Find the first balanced top-level `[...]` block in raw text.
        Unlike a greedy `\\[.*\\]` regex, this stops at the matching close
        bracket instead of the LAST `]` in the whole response — so trailing
        prose (or a second bracketed aside) from the LLM doesn't get pulled
        into the "array" and break json.loads with "Extra data" errors.
        """
        start = raw.find('[')
        if start == -1:
            return None

        depth, in_string, escape = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
        return None

    def _corroboration_bonuses(self, articles: list) -> dict[str, float]:
        """
        For each article, count how many OTHER sources filed a similar title
        (Jaccard >= _CORROBORATION_THRESHOLD). Bonus: +0.5 per source, capped at +2.0.
        Uses the same tokenizer as the title dedup filter for consistency.

        Similar titles are looked for both in the last 24h of stored articles
        AND among the other members of this same batch — breaking news covered
        by several sources within one scoring window used to earn nobody a
        bonus, because the batch excluded itself from the comparison set.
        """
        from core import models as m

        cutoff      = datetime.now(dt_tz.utc) - timedelta(hours=24)
        article_ids = [a.id for a in articles]

        recent_pairs = list(
            m.Article.objects.filter(created_on__gte=cutoff)
            .exclude(id__in=article_ids)
            .values_list('title', 'source_code')[:2000]
        )
        recent_tokensets: list[tuple[frozenset, str]] = [
            (_tokenize(title), src) for title, src in recent_pairs
        ]
        batch_tokensets: list[tuple[frozenset, str, str]] = [
            (_tokenize(a.title), a.source_code, str(a.id)) for a in articles
        ]

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
            bonuses[my_id] = min(
                len(corroborating) * _CORROBORATION_BONUS,
                _CORROBORATION_MAX,
            )

        return bonuses


def score_unscored_articles(article_ids: list) -> int:
    """
    LLM-score exactly these Article rows (re-scores if already scored). Called
    by the 'score' stage (services/stages.py), which selects unscored article
    ids and passes them in chunks.
    """
    from core import models as m

    articles = list(m.Article.objects.filter(id__in=article_ids))

    if not articles:
        return 0

    scorer  = ArticleImportanceScorer()
    scores  = scorer.score_articles(articles)

    to_save = []
    for article in articles:
        score = scores.get(str(article.id))
        if score is not None:
            article.importance_score  = score
            article.importance_source = 'llm'
            to_save.append(article)
    if to_save:
        m.Article.objects.bulk_update(
            to_save, ['importance_score', 'importance_source'], batch_size=500,
        )

    logger.info('[scoring] scored %d/%d articles', len(to_save), len(articles))
    return len(to_save)
