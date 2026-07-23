"""Cloud LLM prompt client for article analysis (route 'analyzer_lite').

One batched prompt extracts category/sub-category, country/city, intensity and
an abstractive EN title/summary. Two production callers, both orchestration —
this module is never an entry point itself: services.workflow.articles.
analyze_live_articles (the 'analyze' stage — every 3h, live-fetched articles
only) uses it as the primary analyzer, and services.processing.refiner's
'cloud' provider (the 'refine' stage) uses it as a second opinion on
low-confidence on-prem output from historical/backfill articles. The
'annotate' stage itself does everything on-prem and never touches this file.
"""

import json
import logging

from services.llm import strip_code_fences
from services.processing.geocode import geocode as _geocode
from services.processing.taxonomy import CATEGORIES as _CATEGORIES, SUB_CATEGORIES as _SUB_CATEGORIES
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Neutral-low fallback when the LLM omits/garbles intensity on an otherwise-parsed
# article — keeps the event in play without overstating it.
_DEFAULT_INTENSITY = 0.3

# English-only schema — covers only the fields that need real judgment (taxonomy
# classification, geo naming, severity rating). Sentiment and Arabic
# translation are the annotate stage's job (services.processing.annotator).
_OBJECT_SCHEMA = """\
{
  "category": conflict|disaster|economic|political|health|general,
  "sub_category": sub-category slug for the chosen category (see guide) or null,
  "country": English country name or null,
  "city": English city/region name or null,
  "intensity": float 0.0 to 1.0 (newsworthiness/severity — see guide),
  "translations": {
    "en": {"title": English title, "summary": 2-3 sentence factual English summary, "country": English country or null, "city": English city or null}
  }
}"""

_CATEGORY_GUIDE = """\
Category + sub-category:
- conflict [war|airstrike|insurgency|terrorism|border-clash|other]: deliberate armed/military action. Always conflict if an aggressor is present, even with casualties or explosions.
- disaster [earthquake|flood|storm|wildfire|industrial-accident|other]: natural or accidental — no armed aggressor.
- economic [monetary-policy|energy|trade|tariffs|labor|markets|sanctions|other]
- political [election|legislation|diplomacy|leadership-change|protest-policy|other]
- health [outbreak|pandemic|healthcare-system|other]
- general [other]: anything else, incl. ordinary crime.
Rule: deliberate armed action → conflict; accidental/natural → disaster.

Intensity (newsworthiness/severity of the event, not your opinion of it):
- 0.0-0.2: routine/minor — local notices, ordinary crime, scheduled procedure, opinion/analysis.
- 0.3-0.5: notable — regional impact, single-casualty incidents, policy proposals, market moves.
- 0.6-0.8: major — many casualties, national-scale crises, significant attacks/disasters, central-bank decisions.
- 0.9-1.0: severe/historic — mass-casualty events, wars, major disasters, globally market-moving shocks."""

def _batch_prompt(schema: str) -> str:
    return (
        'News article analyzer. JSON only (no markdown).\n\n'
        'You will receive a numbered list of articles. Return a JSON array with '
        'exactly one result object per article, in the same order — no other text.\n\n'
        'Result object schema:\n' + schema + '\n\n' + _CATEGORY_GUIDE
    )


# Batch prompt (array-in, array-out) — a single article is just a batch of one.
_SYSTEM_PROMPT = _batch_prompt(_OBJECT_SCHEMA)


@dataclass
class ArticleAnalysis:
    category: str             # one of _CATEGORIES
    sub_category: str | None  # sub-category slug within category, or None
    country: str | None       # e.g. "Ukraine"
    city: str | None          # e.g. "Kyiv"
    latitude: float | None
    longitude: float | None
    intensity: float          # 0.0..1.0 newsworthiness/severity — LLM-rated
    llm_data: dict            # raw parsed LLM response for storage in extra_data
    translations: dict        # i18n subdocument: {"en": {...}, "ar": {...}}
    llm_usage: dict           # {provider, model, prompt_tokens, completion_tokens, total_tokens}
    error: str | None = None  # set when analysis fell back to _empty() — surfaced via mark_stage


class ArticleAnalyzer:
    """
    Uses the LLM to extract category, sub-category, country, city, and intensity from
    article text (route 'analyzer_lite' — see settings.LLM_ROUTES).

    Falls back to ArticleAnalysis(category='general', ...) on any failure.

    Usage:
        analyzer = ArticleAnalyzer()
        result = analyzer.analyze('Explosions were reported near Kyiv overnight...')
        # ArticleAnalysis(category='conflict', country='Ukraine', city='Kyiv', latitude=50.45, longitude=30.52)
    """

    # Multi-article batching — cuts LLM call count roughly by this factor vs.
    # one call per article. Kept small because full article content is
    # included in the prompt, not just titles.
    ANALYZE_BATCH_SIZE = 8
    # Per-article content cap inside a multi-article prompt — keeps an
    # 8-article batch prompt within safe context/latency bounds.
    _BATCH_MAX_CHARS = 550

    def __init__(self) -> None:
        from services.llm import get_llm_service
        self._get_llm_service = get_llm_service
        self._llm = None  # resolved lazily and cached

    def _service(self):
        if self._llm is None:
            self._llm = self._get_llm_service('analyzer_lite')
        return self._llm

    @staticmethod
    def _empty(error: str | None = None) -> ArticleAnalysis:
        return ArticleAnalysis(
            category='general', sub_category=None,
            country=None, city=None,
            latitude=None, longitude=None,
            intensity=0.0,
            llm_data={}, translations={}, llm_usage={},
            error=error,
        )

    @staticmethod
    def _parse_intensity(raw, default: float = _DEFAULT_INTENSITY) -> float:
        """Coerce the LLM intensity field to a float clamped to [0.0, 1.0].

        Falls back to ``default`` (neutral-low) when the field is missing or
        unparseable, so an omitted value never zeroes out an otherwise real event.
        """
        if raw is None:
            return default
        try:
            return round(max(0.0, min(1.0, float(raw))), 4)
        except (TypeError, ValueError):
            return default

    def analyze(self, text: str) -> ArticleAnalysis:
        """Analyze a single article. Returns a zeroed-out ArticleAnalysis on failure."""
        return self.analyze_batch([text])[0]

    def analyze_batch(self, texts: list[str]) -> list[ArticleAnalysis]:
        """
        Analyze articles in batches of one LLM call each. Returns one ArticleAnalysis
        per input text, in order — failures fall back to _empty() per-item or per-chunk.

        Output is English-only (category, geo, intensity, EN title/summary) —
        sentiment and the Arabic translation belong to the annotate stage
        (services.processing.annotator), never to this prompt client.

        Batch size depends on which provider will actually serve the call: Ollama is
        a single local model server (one request at a time), so a route that
        effectively resolves to Ollama (no cloud keys configured, or all of them
        exhausted/unconfigured) degrades to one article per call instead of risking a
        big multi-article prompt timing out and losing the whole chunk.
        """
        if not texts:
            return []
        from services.llm import resolved_provider_names

        service = self._service()
        primary = (resolved_provider_names(service) or ['unknown'])[0]
        batch_size = 1 if primary.startswith('ollama') else self.ANALYZE_BATCH_SIZE

        results: list[ArticleAnalysis] = []
        for i in range(0, len(texts), batch_size):
            results.extend(self._analyze_chunk(texts[i : i + batch_size], service))
        return results

    def _analyze_chunk(self, texts: list[str], service) -> list[ArticleAnalysis]:
        """Send one multi-article prompt (array-in, array-out) and parse it back."""
        user = '\n\n'.join(f'Article {i + 1}:\n{t[: self._BATCH_MAX_CHARS]}' for i, t in enumerate(texts))
        try:
            raw, usage = service.chat_with_usage(
                messages=[
                    {'role': 'system', 'content': _SYSTEM_PROMPT},
                    {'role': 'user', 'content': user},
                ],
                temperature=0,
            )
            data = self._loads(raw)
            if not isinstance(data, list):
                data = [data]  # tolerate a bare object for a batch of one
        except Exception as exc:
            logger.exception('ArticleAnalyzer._analyze_chunk failed (%d article(s))', len(texts))
            err = f'LLM analysis failed: {exc}'[:300]
            return [self._empty(error=err) for _ in texts]

        per_article_usage = self._split_usage(usage, len(texts))
        results = [
            self._parse_obj(data[i], per_article_usage[i]) if i < len(data) and isinstance(data[i], dict)
            else self._empty(error='LLM response missing/malformed result object for this article')
            for i in range(len(texts))
        ]
        return results

    @staticmethod
    def _split_usage(usage: dict, n: int) -> list[dict]:
        """Split one batch call's token usage evenly across the ``n`` articles it
        covered, so per-article llm_usage sums back to the true batch total instead
        of every article being stamped with the whole batch's token count.

        Token counts divide with the remainder going to the first few articles
        (so the split always sums exactly to the original); ``provider``/``model``
        are copied as-is since they're the same for every article in the call.
        """
        if not usage or n <= 0:
            return [dict(usage or {}) for _ in range(max(n, 0))]
        token_fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
        shares = [dict(usage) for _ in range(n)]
        for field in token_fields:
            total = int(usage.get(field) or 0)
            base, remainder = divmod(total, n)
            for i, share in enumerate(shares):
                share[field] = base + (1 if i < remainder else 0)
        return shares

    @staticmethod
    def _loads(raw: str):
        # Free/no-key models often wrap JSON in ```json fences — strip them first.
        return json.loads(strip_code_fences(raw))

    def _parse_obj(self, data: dict, llm_usage: dict | None = None) -> ArticleAnalysis:
        category = data.get('category', 'general')
        if category not in _CATEGORIES:
            category = 'general'
        raw_sub = data.get('sub_category') or None
        valid_subs = _SUB_CATEGORIES.get(category, {'other'})
        sub_category = raw_sub if raw_sub in valid_subs else None
        country = data.get('country') or None
        city = data.get('city') or None
        lat, lon = _geocode(city, country)
        translations = data.get('translations') or {}
        # Sanitise: ensure each language entry is a dict
        if not isinstance(translations, dict):
            translations = {}
        for lang_key in list(translations.keys()):
            if not isinstance(translations[lang_key], dict):
                del translations[lang_key]
        intensity = self._parse_intensity(data.get('intensity'))
        llm_data = {
            'category': category,
            'sub_category': sub_category,
            'country': country,
            'city': city,
        }
        return ArticleAnalysis(
            category=category, sub_category=sub_category,
            country=country, city=city,
            latitude=lat, longitude=lon,
            intensity=intensity,
            llm_data=llm_data,
            translations=translations,
            llm_usage=llm_usage or {},
        )
