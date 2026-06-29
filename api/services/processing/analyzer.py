import functools
import json
import logging
import re

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

_OBJECT_SCHEMA = """\
{
  "category": conflict|disaster|economic|political|health|general,
  "sub_category": sub-category slug for the chosen category (see guide) or null,
  "country": English country name or null,
  "city": English city/region name or null,
  "entities": [{"text": named entity surface form, "label": PER|ORG|LOC|MISC}],
  "sentiment": float -1.0 (very negative) to 1.0 (very positive),
  "intensity": float 0.0 to 1.0 (newsworthiness/severity — see guide),
  "translations": {
    "en": {"title": English title, "summary": 2-3 sentence factual English summary, "country": English country or null, "city": English city or null},
    "ar": {"title": Arabic title, "summary": 2-3 sentence factual Arabic summary, "country": Arabic country or null, "city": Arabic city or null}
  }
}"""

# Lean schema for backfill: English-only (drops the token-heavy Arabic summary).
# Geocoding + category still work, articles still render on the map; we just skip
# the Arabic localization for historical records.
_OBJECT_SCHEMA_LITE = """\
{
  "category": conflict|disaster|economic|political|health|general,
  "sub_category": sub-category slug for the chosen category (see guide) or null,
  "country": English country name or null,
  "city": English city/region name or null,
  "entities": [{"text": named entity surface form, "label": PER|ORG|LOC|MISC}],
  "sentiment": float -1.0 (very negative) to 1.0 (very positive),
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

def _single_prompt(schema: str) -> str:
    return (
        'News article analyzer. JSON only (no markdown).\n\nSchema:\n'
        + schema + '\n\n' + _CATEGORY_GUIDE
    )


# Full prompts carry EN+AR translations; lite prompts are EN-only (backfill).
_SYSTEM_PROMPT = _single_prompt(_OBJECT_SCHEMA)
_SYSTEM_PROMPT_LITE = _single_prompt(_OBJECT_SCHEMA_LITE)


@dataclass
class ArticleAnalysis:
    category: str             # one of _CATEGORIES
    sub_category: str | None  # sub-category slug within category, or None
    country: str | None       # e.g. "Ukraine"
    city: str | None          # e.g. "Kyiv"
    latitude: float | None
    longitude: float | None
    entities: list            # [{"text": ..., "label": PER|ORG|LOC|MISC}] — LLM-extracted
    sentiment: float          # -1.0..1.0 polarity — LLM-extracted
    intensity: float          # 0.0..1.0 newsworthiness/severity — LLM-rated
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

    _MAX_CHARS = 1200

    def __init__(self) -> None:
        from services.llm import get_llm_service
        self._get_llm_service = get_llm_service
        # Two routes by complexity: full (EN+AR) → 'analyzer' (OpenRouter);
        # lite (English-only backfill) → 'analyzer_lite' (local Ollama 7B, OR fallback).
        # Resolved lazily and cached so an unused route never instantiates a client.
        self._llm_full = None
        self._llm_lite = None

    def _service(self, translate: bool):
        if translate:
            if self._llm_full is None:
                self._llm_full = self._get_llm_service('analyzer')
            return self._llm_full
        if self._llm_lite is None:
            self._llm_lite = self._get_llm_service('analyzer_lite')
        return self._llm_lite

    @staticmethod
    def _empty() -> ArticleAnalysis:
        return ArticleAnalysis(
            category='general', sub_category=None,
            country=None, city=None,
            latitude=None, longitude=None,
            entities=[], sentiment=0.0, intensity=0.0,
            llm_data={}, translations={},
        )

    @staticmethod
    def _parse_entities(raw) -> list:
        """Normalise the LLM entities field to [{'text','label'}] with valid labels.

        Accepts a list of {text,label} dicts (preferred) or bare strings (labelled
        MISC). Drops malformed/empty entries. Output shape is
        [{'text','label'}] — stored on Article.entities.
        """
        if not isinstance(raw, list):
            return []
        valid = {'PER', 'ORG', 'LOC', 'MISC'}
        out = []
        for e in raw:
            if isinstance(e, str):
                text, label = e.strip(), 'MISC'
            elif isinstance(e, dict):
                text = str(e.get('text') or '').strip()
                label = str(e.get('label') or 'MISC').upper()
            else:
                continue
            if not text:
                continue
            out.append({'text': text, 'label': label if label in valid else 'MISC'})
        return out

    @staticmethod
    def _parse_sentiment(raw) -> float:
        """Coerce the LLM sentiment field to a float clamped to [-1.0, 1.0]."""
        try:
            return round(max(-1.0, min(1.0, float(raw))), 4)
        except (TypeError, ValueError):
            return 0.0

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
        """
        Analyze a single article. Returns a zeroed-out ArticleAnalysis on failure.

        translate=False uses the lite (English-only) schema — used for backfilled
        historical articles where the Arabic localization isn't worth the tokens.
        """
        system = _SYSTEM_PROMPT if translate else _SYSTEM_PROMPT_LITE
        try:
            raw = self._service(translate).chat(
                messages=[
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': text[: self._MAX_CHARS]},
                ],
                temperature=0,
            )
            return self._parse_obj(self._loads(raw))
        except Exception:
            logger.exception('ArticleAnalyzer.analyze failed')
            return self._empty()

    def analyze_batch(self, texts: list[str], translate: bool = True) -> list[ArticleAnalysis]:
        """Analyze each article individually. Returns one ArticleAnalysis per input text."""
        return [self.analyze(t, translate) for t in texts]

    @staticmethod
    def _loads(raw: str):
        # Free/no-key models often wrap JSON in ```json fences — strip them first.
        return json.loads(strip_code_fences(raw))

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
        entities = self._parse_entities(data.get('entities'))
        sentiment = self._parse_sentiment(data.get('sentiment'))
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
            entities=entities, sentiment=sentiment, intensity=intensity,
            llm_data=llm_data,
            translations=translations,
        )
