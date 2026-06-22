import functools
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Two-level taxonomy (plan §Concepts) — top-level stays small, sub-category does the work.
# protest → political/protest-policy; crime → conflict (terrorism/insurgency) or general.
_CATEGORIES = {'conflict', 'disaster', 'economic', 'political', 'health', 'general'}

_SUB_CATEGORIES: dict[str, set[str]] = {
    'conflict':  {'war', 'airstrike', 'insurgency', 'terrorism', 'border-clash', 'other'},
    'disaster':  {'earthquake', 'flood', 'storm', 'wildfire', 'industrial-accident', 'other'},
    'economic':  {'monetary-policy', 'energy', 'trade', 'tariffs', 'labor', 'markets', 'sanctions', 'other'},
    'political': {'election', 'legislation', 'diplomacy', 'leadership-change', 'protest-policy', 'other'},
    'health':    {'outbreak', 'pandemic', 'healthcare-system', 'other'},
    'general':   {'other'},
}

_OBJECT_SCHEMA = """\
{
  "category": conflict|disaster|economic|political|health|general,
  "sub_category": sub-category slug for the chosen category (see guide) or null,
  "country": English country name or null,
  "city": English city/region name or null,
  "translations": {
    "en": {"title": English title, "summary": 2-3 sentence factual English summary, "country": English country or null, "city": English city or null},
    "ar": {"title": Arabic title, "summary": 2-3 sentence factual Arabic summary, "country": Arabic country or null, "city": Arabic city or null}
  }
}"""

_CATEGORY_GUIDE = """\
Pick the best top-level category, then the most specific sub-category:
- conflict  [war|airstrike|insurgency|terrorism|border-clash|other]: any deliberate armed/military action — strikes, drones, shelling, clashes, terrorism. Country A attacking Country B is ALWAYS conflict, even with explosions, fires, or mass casualties.
- disaster  [earthquake|flood|storm|wildfire|industrial-accident|other]: natural catastrophe OR a purely accidental industrial event (factory blast, spill, pipeline leak) with NO armed aggressor.
- economic  [monetary-policy|energy|trade|tariffs|labor|markets|sanctions|other]: finance, central-bank/rate decisions, trade, tariffs, labor, markets, energy policy, sanctions.
- political [election|legislation|diplomacy|leadership-change|protest-policy|other]: government decisions, summits, elections, legislation, leadership changes, coups, protests/strikes (use protest-policy).
- health    [outbreak|pandemic|healthcare-system|other]: disease outbreaks, epidemics, public-health/healthcare news.
- general   [other]: anything else, including ordinary crime not involving military actors.

conflict vs disaster: caused by a deliberate armed/military action? YES → conflict (even if buildings burned or people died); NO → disaster."""

_SYSTEM_PROMPT = (
    'You are a news article analyzer. Respond with a single valid JSON object — '
    'no markdown, no explanation, just JSON.\n\nSchema:\n'
    + _OBJECT_SCHEMA + '\n\n' + _CATEGORY_GUIDE
)

_BATCH_SYSTEM_PROMPT = (
    'You are a news article analyzer. You will receive several numbered articles. '
    'Respond with a JSON array of one object per article, IN THE SAME ORDER — '
    'no markdown, no explanation, just the JSON array.\n\nEach object schema:\n'
    + _OBJECT_SCHEMA + '\n\n' + _CATEGORY_GUIDE
)


@dataclass
class ArticleAnalysis:
    category: str             # one of _CATEGORIES
    sub_category: str | None  # sub-category slug within category, or None
    country: str | None       # e.g. "Ukraine"
    city: str | None          # e.g. "Kyiv"
    latitude: float | None
    longitude: float | None
    llm_data: dict            # raw parsed LLM response for storage in extra_data
    translations: dict        # i18n subdocument: {"en": {...}, "ar": {...}}


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
    Uses the LLM to extract category, country, city, and coordinates from article text.

    Requires OPENROUTER_API_KEYS to be configured.
    Falls back to ArticleAnalysis(category='general', ...) on any failure.

    Usage:
        analyzer = ArticleAnalyzer()
        result = analyzer.analyze('Explosions were reported near Kyiv overnight...')
        # ArticleAnalysis(category='conflict', country='Ukraine', city='Kyiv', latitude=50.45, longitude=30.52)
    """

    # Title + lead paragraph is enough for category/geo/summary; trimming the
    # tail keeps input tokens down without hurting classification quality.
    _MAX_CHARS = 1200
    # Articles per LLM call in analyze_batch — amortizes the static system prompt
    # across many articles (the dominant pipeline cost). Kept modest so the JSON
    # array output stays well within the model's response window.
    _BATCH_SIZE = 6
    # Per-article output ceiling. Generous on purpose: a truncated JSON array
    # fails the whole batch, and the Arabic summary is token-heavy. max_tokens is
    # only a cap, so short responses cost nothing — we size to avoid truncation.
    _OUTPUT_PER_ARTICLE = 480
    _MAX_OUTPUT = 3400

    def __init__(self) -> None:
        from services.llm import get_llm_service
        self._llm = get_llm_service('analyzer')

    @staticmethod
    def _empty() -> ArticleAnalysis:
        return ArticleAnalysis(
            category='general', sub_category=None,
            country=None, city=None,
            latitude=None, longitude=None,
            llm_data={}, translations={},
        )

    def analyze(self, text: str) -> ArticleAnalysis:
        """
        Analyze a single article. Returns a zeroed-out ArticleAnalysis on failure.
        """
        try:
            raw = self._llm.chat(
                messages=[
                    {'role': 'system', 'content': _SYSTEM_PROMPT},
                    {'role': 'user', 'content': text[: self._MAX_CHARS]},
                ],
                temperature=0,
                max_tokens=self._OUTPUT_PER_ARTICLE + 220,
            )
            return self._parse_obj(self._loads(raw))
        except Exception:
            logger.exception('ArticleAnalyzer.analyze failed')
            return self._empty()

    def analyze_batch(self, texts: list[str]) -> list[ArticleAnalysis]:
        """
        Analyze many articles, chunked into single multi-article LLM calls.
        Always returns one ArticleAnalysis per input text, in order; missing or
        malformed entries degrade to a 'general' ArticleAnalysis.
        """
        results: list[ArticleAnalysis] = []
        for start in range(0, len(texts), self._BATCH_SIZE):
            results.extend(self._analyze_chunk(texts[start: start + self._BATCH_SIZE]))
        return results

    def _analyze_chunk(self, chunk: list[str]) -> list[ArticleAnalysis]:
        user = '\n\n'.join(
            f'[{i + 1}]\n{t[: self._MAX_CHARS]}' for i, t in enumerate(chunk)
        )
        try:
            raw = self._llm.chat(
                messages=[
                    {'role': 'system', 'content': _BATCH_SYSTEM_PROMPT},
                    {'role': 'user', 'content': user},
                ],
                temperature=0,
                max_tokens=min(self._MAX_OUTPUT, self._OUTPUT_PER_ARTICLE * len(chunk) + 200),
            )
            objs = self._parse_array(raw)
        except Exception:
            logger.exception('ArticleAnalyzer.analyze_batch chunk failed')
            objs = []
        if len(objs) != len(chunk):
            logger.warning(
                'analyze_batch: expected %d objects, got %d', len(chunk), len(objs),
            )
        # Align strictly to input order; pad short responses with empties.
        return [objs[i] if i < len(objs) else self._empty() for i in range(len(chunk))]

    @staticmethod
    def _loads(raw: str):
        # Free/no-key models often wrap JSON in ```json fences — strip them first.
        cleaned = re.sub(r'^```(?:json)?\s*', '', raw.strip())
        cleaned = re.sub(r'\s*```$', '', cleaned)
        return json.loads(cleaned)

    def _parse_array(self, raw: str) -> list[ArticleAnalysis]:
        data = self._loads(raw)
        if isinstance(data, dict):
            # Tolerate {"results": [...]} / {"articles": [...]} envelopes.
            data = data.get('results') or data.get('articles') or []
        if not isinstance(data, list):
            return []
        return [self._parse_obj(o) if isinstance(o, dict) else self._empty() for o in data]

    def _parse_obj(self, data: dict) -> ArticleAnalysis:
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
            llm_data=llm_data,
            translations=translations,
        )
