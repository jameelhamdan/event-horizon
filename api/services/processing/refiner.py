"""The refine stage's service — second-opinion judging of low-confidence articles.

LLMRefiner re-judges articles the annotate stage flagged (Article.stage =
'refine': prototype-classification confidence below annotator.ESCALATE_BELOW).
It only touches the judgment fields — category, sub_category, country/city,
event_intensity (and, for the cloud provider, the EN summary) — everything
else (sentiment, translations, importance) stays as annotated.

One provider per deployment, selected by settings.REFINE_PROVIDER, each with a
batch size matched to its capacity:

  'zeroshot' (default) — mDeBERTa zero-shot NLI, on-prem, batches of 16.
                         Category-only verdict + prototype sub-pick; violent-
                         metaphor verdicts are gated by _CONFLICT_EVIDENCE.
  'ollama'             — local LLM, one article per call (the Ollama box serves
                         one generation at a time), JSON-schema constrained so
                         the output cannot fail to parse.
  'cloud'              — the existing LLM prompt client (analyzer.py) via
                         LLM_ROUTES['analyzer_lite'], batches of 8; also
                         refreshes the abstractive EN summary.
  'off'                — the refine stage never dispatches (stages.py).

A verdict of None for an article means "judge unavailable/failed" — the caller
leaves it at stage='refine' so the stage retries later.
"""

import json
import logging
import re

from services.processing._lazy import lazy_loader
from services.processing.taxonomy import CATEGORIES, SUB_CATEGORIES
from settings.model_names import ZEROSHOT_MODEL_NAME

logger = logging.getLogger(__name__)

#: dict per article: {'category', 'sub_category', 'country', 'city',
#: 'intensity' (optional), 'summary' (optional), 'provider'} — or None.
Verdict = dict | None

# ── zero-shot provider ────────────────────────────────────────────────────────

# Candidate labels (NLI hypothesis phrasing) → category slug. Several labels may
# map to the same slug — 'general' gets two so that both product/culture stories
# AND feature/opinion framing have a landing spot. Phrase labels as *what the
# story is about* and keep 'economic' scoped to macro/markets so company-
# adjacent tech news doesn't gravitate in (both tuned on live evals).
_ZEROSHOT_LABELS: dict[str, str] = {
    'a war, military strike or armed conflict that is happening': 'conflict',
    'a natural disaster or serious accident': 'disaster',
    'the economy: financial markets, trade, inflation, jobs or central banks': 'economic',
    'politics, government, elections or diplomacy': 'political',
    'health, disease or medicine': 'health',
    'consumer technology, gadgets, sports, culture, entertainment or lifestyle': 'general',
    'a feature story, product roundup, interview, podcast or opinion piece': 'general',
}
_ZEROSHOT_TEMPLATE = 'This news article is about {}.'
_ZEROSHOT_BATCH = 16

# Sanity gate for zero-shot 'conflict' verdicts: violent *metaphors* in soft
# news ("EVs killed off", "silencing a generation") read as conflict to the NLI
# model. A real conflict story virtually always carries at least one concrete
# military/violence term; a conflict verdict with none downgrades to general.
_CONFLICT_EVIDENCE = re.compile(
    r'\b(?:airstrike|air strike|attack\w*|missile|drone|troops|soldier\w*|army|military|militant\w*|'
    r'rebel\w*|insurgent\w*|gunmen|gunman|shelling|invasion|ceasefire|warplane\w*|'
    r'bomb\w*|explosion\w*|terror\w*|hostage\w*|casualt\w*|wounded|war\b|combat\b|'
    r'shot dead|killed in|kills?\b\s+\d)',
    re.I,
)

_zeroshot_pipeline = lazy_loader('zeroshot', 'ZEROSHOT_ENABLED', lambda: _build_zeroshot())


def _build_zeroshot():
    from transformers import pipeline
    return pipeline('zero-shot-classification', model=ZEROSHOT_MODEL_NAME)


# ── ollama provider ───────────────────────────────────────────────────────────

# JSON schema for Ollama constrained decoding — the model literally cannot emit
# an out-of-taxonomy category or unparseable output.
_OLLAMA_SCHEMA = {
    'type': 'object',
    'properties': {
        'category': {'type': 'string', 'enum': sorted(CATEGORIES)},
        'sub_category': {'type': ['string', 'null']},
        'country': {'type': ['string', 'null']},
        'city': {'type': ['string', 'null']},
        'intensity': {'type': 'number'},
    },
    'required': ['category', 'sub_category', 'country', 'city', 'intensity'],
}

_OLLAMA_PROMPT = (
    'Classify this news article. JSON only.\n'
    'category: one of ' + '|'.join(sorted(CATEGORIES)) + '.\n'
    'sub_category: '
    + '; '.join(f'{c}: {"|".join(sorted(s))}' for c, s in sorted(SUB_CATEGORIES.items()))
    + '.\n'
    'country/city: the place the story is about (English names) or null.\n'
    'intensity: severity 0.0-1.0 (routine 0.1, notable 0.4, major 0.7, historic 0.9).'
)


class LLMRefiner:
    """Judge a batch of (title, content) items with the configured provider.

    ``judge(items)`` returns one Verdict (or None) per item, in order. Batching
    is internal per provider; the caller passes whatever chunk the stage
    dispatched.
    """

    def __init__(self, provider: str | None = None):
        from django.conf import settings
        self.provider = provider or getattr(settings, 'REFINE_PROVIDER', 'zeroshot')

    def judge(self, items: list[tuple[str, str]]) -> list[Verdict]:
        if not items:
            return []
        judge = {
            'zeroshot': self._judge_zeroshot,
            'ollama': self._judge_ollama,
            'cloud': self._judge_cloud,
        }.get(self.provider)
        if judge is None:
            logger.warning('[refine] unknown/off provider %r — no verdicts', self.provider)
            return [None] * len(items)
        return judge(items)

    # ── providers ─────────────────────────────────────────────────────────────

    def _judge_zeroshot(self, items: list[tuple[str, str]]) -> list[Verdict]:
        from services.processing.annotator import best_sub

        pipe = _zeroshot_pipeline()
        if pipe is None:
            return [None] * len(items)
        texts = [f'{title}. {content}'[:350] for title, content in items]
        verdicts: list[Verdict] = []
        for i in range(0, len(texts), _ZEROSHOT_BATCH):
            chunk = texts[i : i + _ZEROSHOT_BATCH]
            try:
                raw = pipe(chunk, candidate_labels=list(_ZEROSHOT_LABELS), hypothesis_template=_ZEROSHOT_TEMPLATE, batch_size=8)
                if isinstance(raw, dict):
                    raw = [raw]
            except Exception:
                logger.exception('[refine/zeroshot] failed (%d text(s))', len(chunk))
                verdicts.extend([None] * len(chunk))
                continue
            for text, r in zip(chunk, raw):
                category = _ZEROSHOT_LABELS.get(r['labels'][0])
                if category is None:
                    verdicts.append(None)
                    continue
                if category == 'conflict' and not _CONFLICT_EVIDENCE.search(text):
                    category = 'general'
                sub = best_sub(category, [text])[0]
                verdicts.append({'category': category, 'sub_category': sub, 'provider': 'zeroshot'})
        return verdicts

    def _judge_ollama(self, items: list[tuple[str, str]]) -> list[Verdict]:
        from services.llm import LLMError, get_provider, strip_code_fences

        service = get_provider('ollama_medium')
        if service is None:
            logger.warning('[refine/ollama] ollama_medium not configured')
            return [None] * len(items)
        verdicts: list[Verdict] = []
        for title, content in items:  # one generation at a time — see module docstring
            user = f'{title}\n\n{content[:550]}'
            try:
                raw = service.chat(
                    [{'role': 'system', 'content': _OLLAMA_PROMPT}, {'role': 'user', 'content': user}],
                    temperature=0, format=_OLLAMA_SCHEMA,
                )
                verdicts.append(self._parse_verdict(json.loads(strip_code_fences(raw)), provider='ollama'))
            except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning('[refine/ollama] %s', exc)
                verdicts.append(None)
        return verdicts

    def _judge_cloud(self, items: list[tuple[str, str]]) -> list[Verdict]:
        from services.processing.analyzer import ArticleAnalyzer

        analyses = ArticleAnalyzer().analyze_batch([f'{title} {content}' for title, content in items])
        verdicts: list[Verdict] = []
        for a in analyses:
            if a.error is not None:
                verdicts.append(None)
                continue
            en = a.translations.get('en') or {}
            verdicts.append({
                'category': a.category, 'sub_category': a.sub_category,
                'country': a.country, 'city': a.city,
                'intensity': a.intensity,
                'summary': en.get('summary'),
                'provider': 'cloud', 'llm_usage': a.llm_usage,
            })
        return verdicts

    # ── verdict application ───────────────────────────────────────────────────

    @staticmethod
    def apply(article, verdict: dict) -> None:
        """Apply a (non-None) verdict onto an Article in place — the single
        place refine-domain field semantics live. Does not save and does not
        touch stage/refined_on bookkeeping (the workflow owns persistence and
        stage transitions).

        Rules: category/sub always taken from the verdict; geo replaced only if
        the verdict's place actually geocodes (otherwise the annotator's geo
        stands); intensity from the verdict when present, else re-rated for the
        new category; summary merged only for non-lite articles when the judge
        produced one (cloud provider); refined_by always overwritten with this
        verdict's provider, so a re-refine (see refine_articles) correctly
        reflects whichever judge most recently decided the article's fields.
        """
        from services.processing.annotator import rate_intensity
        from services.processing.geocode import geocode

        article.category = verdict['category']
        article.sub_category = verdict['sub_category']

        city, country = verdict.get('city'), verdict.get('country')
        if city or country:
            lat, lon = geocode(city, country)
            if lat is not None:
                article.location = ', '.join(filter(None, [city, country]))
                article.latitude, article.longitude = lat, lon

        article.event_intensity = verdict.get('intensity') if verdict.get('intensity') is not None else rate_intensity(
            article.category, article.sub_category, f'{article.title}. {(article.content or "")[:500]}',
        )

        summary = verdict.get('summary')
        if summary and not (article.extra_data or {}).get('backfill_day'):
            translations = dict(article.translations or {})
            translations.setdefault('en', {})['summary'] = summary
            article.translations = translations

        article.refined_by = verdict['provider']

        llm_block = dict((article.extra_data or {}).get('llm') or {})
        llm_block.update({'category': article.category, 'sub_category': article.sub_category})
        article.extra_data = {**(article.extra_data or {}), 'llm': llm_block}

    # ── parsing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_verdict(data: dict, provider: str) -> Verdict:
        category = data.get('category')
        if category not in CATEGORIES:
            return None
        sub = data.get('sub_category') or None
        if sub not in SUB_CATEGORIES[category]:
            sub = None
        verdict: dict = {
            'category': category, 'sub_category': sub,
            'country': data.get('country') or None, 'city': data.get('city') or None,
            'provider': provider,
        }
        try:
            if data.get('intensity') is not None:
                verdict['intensity'] = round(max(0.0, min(1.0, float(data['intensity']))), 4)
        except (TypeError, ValueError):
            pass
        return verdict
