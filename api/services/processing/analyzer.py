import functools
import json
import logging

from services.llm import strip_code_fences
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Two-level taxonomy (plan §Concepts) — top-level stays small, sub-category does the work.
# protest → political/protest-policy; crime → conflict (terrorism/insurgency) or general.
_CATEGORIES = {'conflict', 'disaster', 'economic', 'political', 'health', 'general'}

# Neutral-low fallback when the LLM omits/garbles intensity on an otherwise-parsed
# article — keeps the event in play without overstating it.
_DEFAULT_INTENSITY = 0.3

_SUB_CATEGORIES: dict[str, set[str]] = {
    'conflict':  {'war', 'airstrike', 'insurgency', 'terrorism', 'border-clash', 'other'},
    'disaster':  {'earthquake', 'flood', 'storm', 'wildfire', 'industrial-accident', 'other'},
    'economic':  {'monetary-policy', 'energy', 'trade', 'tariffs', 'labor', 'markets', 'sanctions', 'other'},
    'political': {'election', 'legislation', 'diplomacy', 'leadership-change', 'protest-policy', 'other'},
    'health':    {'outbreak', 'pandemic', 'healthcare-system', 'other'},
    'general':   {'other'},
}

# English-only schema. Two things are no longer requested from the LLM, each
# handled by a purpose-built local model instead (see cleaner.py):
#   - entities   → services.processing.ner   (dslim/bert-base-NER)
#   - sentiment  → services.processing.vader (VADER, rule-based)
# Arabic localization is likewise generated locally, by services.translation
# (MarianMT), from the English fields below — see _add_arabic_translations.
# What's left needs real judgment (taxonomy classification, geo naming,
# severity rating), so it stays on the LLM.
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


@functools.lru_cache(maxsize=1)
def _city_index() -> dict[str, tuple[float, float]]:
    """Lowercase city name → (lat, lon) of the most populous city with that name."""
    import geonamescache
    # population → (lat, lon) per name; keeps highest-population entry on collision
    best: dict[str, tuple[int, float, float]] = {}
    for c in geonamescache.GeonamesCache().get_cities().values():
        name = c['name'].lower()
        pop = int(c.get('population') or 0)
        if name not in best or pop > best[name][0]:
            best[name] = (pop, float(c['latitude']), float(c['longitude']))
    return {name: (lat, lon) for name, (_, lat, lon) in best.items()}


@functools.lru_cache(maxsize=1)
def _country_index() -> dict[str, tuple[float, float]]:
    """
    Lowercase country name → (lat, lon).

    Strategy per country:
      1. Try the capital city name against the city index (fast, usually works).
      2. Fall back to the most populous city in that country by country code
         (robust against capital-name spelling mismatches like Kiev/Kyiv).
    """
    import geonamescache
    gc = geonamescache.GeonamesCache()
    city_idx = _city_index()

    # country_code → (population, lat, lon) for the most populous city
    top_by_cc: dict[str, tuple[int, float, float]] = {}
    for c in gc.get_cities().values():
        cc = c.get('countrycode', '')
        pop = int(c.get('population') or 0)
        lat, lon = float(c['latitude']), float(c['longitude'])
        if cc and (cc not in top_by_cc or pop > top_by_cc[cc][0]):
            top_by_cc[cc] = (pop, lat, lon)

    index: dict[str, tuple[float, float]] = {}
    for iso, cdata in gc.get_countries().items():
        country_lower = cdata['name'].lower()
        capital = (cdata.get('capital') or '').strip()

        # 1. try capital name match
        coords: tuple[float, float] | None = city_idx.get(capital.lower()) if capital else None

        # 2. fall back to most populous city in this country
        if not coords and iso in top_by_cc:
            _, lat, lon = top_by_cc[iso]
            coords = (lat, lon)

        if coords:
            index[country_lower] = coords

    return index


def _geocode(city: str | None, country: str | None = None) -> tuple[float | None, float | None]:
    """
    Try city first; fall back to the country's main city if city is absent or unknown.
    Returns (None, None) if neither resolves.
    """
    if city:
        coords = _city_index().get(city.lower())
        if coords:
            return coords
    if country:
        coords = _country_index().get(country.lower())
        if coords:
            return coords
    return None, None


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
    # one call per article. Smaller than ArticleImportanceScorer.BATCH_SIZE
    # (30) because full article content is included here, not just titles.
    ANALYZE_BATCH_SIZE = 8
    # Per-article content cap inside a multi-article prompt — keeps an
    # 8-article batch prompt within safe context/latency bounds.
    _BATCH_MAX_CHARS = 550

    def __init__(self) -> None:
        from services.llm import get_llm_service
        self._get_llm_service = get_llm_service
        # Single route: the LLM only handles category/geo/intensity + EN
        # translation now — entities/sentiment are local (NER/VADER, see
        # cleaner.py) and Arabic is added locally afterward via
        # services.translation. Resolved lazily and cached.
        self._llm = None

    def _service(self):
        if self._llm is None:
            self._llm = self._get_llm_service('analyzer_lite')
        return self._llm

    @staticmethod
    def _empty() -> ArticleAnalysis:
        return ArticleAnalysis(
            category='general', sub_category=None,
            country=None, city=None,
            latitude=None, longitude=None,
            intensity=0.0,
            llm_data={}, translations={}, llm_usage={},
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

    def analyze(self, text: str, translate: bool = True) -> ArticleAnalysis:
        """Analyze a single article. Returns a zeroed-out ArticleAnalysis on failure."""
        return self.analyze_batch([text], translate=translate)[0]

    def analyze_batch(self, texts: list[str], translate: bool = True) -> list[ArticleAnalysis]:
        """
        Analyze articles in batches of one LLM call each. Returns one ArticleAnalysis
        per input text, in order — failures fall back to _empty() per-item or per-chunk.

        The LLM call itself only ever produces English output (category, geo,
        intensity, EN translation) — entities and sentiment never touch the LLM at
        all (see cleaner.py: NER + VADER, local, on every document regardless of
        this flag). translate=True additionally adds a locally-generated ('ar')
        translation block via services.translation (MarianMT) — no extra LLM cost
        either way. translate=False skips that local translation step entirely
        (used for backfilled historical articles where Arabic localization isn't
        needed).

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
            results.extend(self._analyze_chunk(texts[i : i + batch_size], translate, service))
        return results

    def _analyze_chunk(self, texts: list[str], translate: bool, service) -> list[ArticleAnalysis]:
        """Send one multi-article prompt (array-in, array-out) and parse it back."""
        user = '\n\n'.join(
            f'Article {i + 1}:\n{t[: self._BATCH_MAX_CHARS]}' for i, t in enumerate(texts)
        )
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
        except Exception:
            logger.exception('ArticleAnalyzer._analyze_chunk failed (%d article(s))', len(texts))
            return [self._empty() for _ in texts]

        per_article_usage = self._split_usage(usage, len(texts))
        results = [
            self._parse_obj(data[i], per_article_usage[i]) if i < len(data) and isinstance(data[i], dict) else self._empty()
            for i in range(len(texts))
        ]
        if translate:
            self._add_arabic_translations(results)
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
    def _add_arabic_translations(results: list[ArticleAnalysis]) -> None:
        """Add a locally-generated ('ar') translation block to each result's
        translations dict, derived from the LLM's ('en') block. Batches every
        field across every result into a single translation-model call.
        """
        from services.translation import translate_en_ar_batch

        fields = ('title', 'summary', 'country', 'city')
        flat_texts: list[str] = []
        slots: list[tuple[int, str]] = []  # (result_index, field)
        for i, r in enumerate(results):
            en = r.translations.get('en') if isinstance(r.translations, dict) else None
            if not isinstance(en, dict):
                continue
            for field in fields:
                val = en.get(field)
                if isinstance(val, str) and val.strip():
                    slots.append((i, field))
                    flat_texts.append(val)
        if not flat_texts:
            return

        translated = translate_en_ar_batch(flat_texts)
        ar_blocks: dict[int, dict] = {}
        for (i, field), tr in zip(slots, translated):
            if tr:
                ar_blocks.setdefault(i, {})[field] = tr
        for i, ar in ar_blocks.items():
            results[i].translations['ar'] = ar

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
