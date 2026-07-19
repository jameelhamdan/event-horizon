"""Local gazetteer geocoding (geonamescache) — shared by both analyzers.

Resolves an extracted (city, country) pair to coordinates entirely offline:
city index → alias maps → territory fallbacks → country index. Used by the
annotate stage (services.processing.annotator), the refine stage's verdict
application (services.processing.refiner), and the LLM prompt client
(services.processing.analyzer); also provides ``find_place()``, a regex scan
for country/territory mentions used when NER yields nothing.
"""

import functools
import re


@functools.lru_cache(maxsize=1)
def _city_index() -> dict[str, tuple[float, float, str | None]]:
    """Lowercase city name → (lat, lon, canonical country name) of the most
    populous city with that name (collisions like Paris/Texas vs Paris/France
    resolve to the biggest one)."""
    import geonamescache
    gc = geonamescache.GeonamesCache()
    countries = {iso: c['name'] for iso, c in gc.get_countries().items()}
    best: dict[str, tuple[int, float, float, str | None]] = {}
    for c in gc.get_cities().values():
        name = c['name'].lower()
        pop = int(c.get('population') or 0)
        if name not in best or pop > best[name][0]:
            best[name] = (
                pop, float(c['latitude']), float(c['longitude']),
                countries.get(c.get('countrycode', '')),
            )
    return {name: (lat, lon, country) for name, (_, lat, lon, country) in best.items()}


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
        record = city_idx.get(capital.lower()) if capital else None
        coords: tuple[float, float] | None = record[:2] if record else None

        # 2. fall back to most populous city in this country
        if not coords and iso in top_by_cc:
            _, lat, lon = top_by_cc[iso]
            coords = (lat, lon)

        if coords:
            index[country_lower] = coords

    return index


# Common LLM/name variants → the canonical geonamescache country name. Sources
# (LLM output, NER spans, headlines) routinely say "USA"/"UK"/"Russian
# Federation"/"Türkiye" etc.; geonamescache only keys the canonical form, so
# without this map a correctly-identified country silently fails to geocode
# (the root cause of the large un-located backlog).
# Every value here is a verified canonical geonamescache country name.
_COUNTRY_ALIASES: dict[str, str] = {
    'usa': 'United States', 'us': 'United States', 'u.s.': 'United States',
    'u.s.a.': 'United States', 'america': 'United States',
    'united states of america': 'United States',
    'uk': 'United Kingdom', 'u.k.': 'United Kingdom', 'britain': 'United Kingdom',
    'great britain': 'United Kingdom', 'england': 'United Kingdom',
    'scotland': 'United Kingdom', 'wales': 'United Kingdom',
    'northern ireland': 'United Kingdom',
    'russian federation': 'Russia',
    'czech republic': 'Czechia',
    'turkiye': 'Turkey', 'türkiye': 'Turkey',
    'burma': 'Myanmar',
    'dr congo': 'Democratic Republic of the Congo',
    'drc': 'Democratic Republic of the Congo',
    'congo-kinshasa': 'Democratic Republic of the Congo',
    'congo (kinshasa)': 'Democratic Republic of the Congo',
    'congo-brazzaville': 'Republic of the Congo',
    'cape verde': 'Cabo Verde',
    'swaziland': 'Eswatini',
    'macedonia': 'North Macedonia',
    'vatican city': 'Vatican', 'holy see': 'Vatican',
    'uae': 'United Arab Emirates', 'u.a.e.': 'United Arab Emirates',
    'the emirates': 'United Arab Emirates',
    'south korea': 'South Korea', 'north korea': 'North Korea',
}

# Places geonamescache has no country entry for — direct coordinate fallback so
# high-frequency conflict geographies still resolve. Keyed by normalized name;
# matched against either the city or the country field.
_EXTRA_PLACES: dict[str, tuple[float, float]] = {
    'palestine': (31.9522, 35.2332), 'palestinian territories': (31.9522, 35.2332),
    'west bank': (31.9522, 35.2332),
    'gaza': (31.5, 34.47), 'gaza strip': (31.5, 34.47), 'gaza city': (31.5, 34.47),
    'kosovo': (42.6026, 20.9030),
}

# City spellings the gazetteer lists under a different form.
_CITY_ALIASES: dict[str, str] = {'kiev': 'kyiv'}


def _norm(name: str) -> str:
    """Normalize a place string for lookup: lowercase, trim, drop a leading
    'the ', and strip surrounding quotes/whitespace. Cheap and allocation-light."""
    n = name.strip().strip('"\'').lower()
    if n.startswith('the '):
        n = n[4:]
    return n


def _country_key(country: str) -> str:
    """Alias-resolved, lowercased key into _country_index() for a country name."""
    n = _norm(country)
    canonical = _COUNTRY_ALIASES.get(n)
    return canonical.lower() if canonical else n


def canonical_country(name: str) -> str | None:
    """Title-cased canonical country/territory name for *name*, or None."""
    n = _norm(name)
    canonical = _COUNTRY_ALIASES.get(n)
    if canonical:
        return canonical
    if n in _country_index() or n in _EXTRA_PLACES:
        return name.strip().strip('"\'')
    return None


def is_city(name: str) -> bool:
    """True if *name* resolves via the city index (aliases included)."""
    n = _norm(name)
    return n in _city_index() or _CITY_ALIASES.get(n, n) in _city_index()


def country_of_city(city: str) -> str | None:
    """Canonical country name a city belongs to, or None if unknown."""
    n = _norm(city)
    record = _city_index().get(_CITY_ALIASES.get(n, n))
    return record[2] if record else None


def geocode(city: str | None, country: str | None = None) -> tuple[float | None, float | None]:
    """
    Try city first; fall back to the country's main city if city is absent or unknown.
    Applies alias/normalization so common name variants (USA, UK, Türkiye, …)
    and gazetteer-less territories (Palestine, Gaza) still resolve.
    Returns (None, None) if neither resolves.
    """
    if city:
        n = _norm(city)
        record = _city_index().get(n) or _city_index().get(_CITY_ALIASES.get(n, n))
        if record:
            return record[0], record[1]
    # Territories geonamescache lacks — check both fields before the country index.
    for raw in (city, country):
        if raw:
            coords = _EXTRA_PLACES.get(_norm(raw))
            if coords:
                return coords
    if country:
        coords = _country_index().get(_country_key(country))
        if coords:
            return coords
    return None, None


# Aliases that collide with ordinary English words when scanned case-insensitively
# in free text ('us' the pronoun matched as the country in a live eval). They stay
# valid for direct geocode()/canonical_country() lookups — only the regex scan
# skips them.
_SCAN_EXCLUDED = {'us'}


@functools.lru_cache(maxsize=1)
def _country_pattern() -> re.Pattern:
    """One alternation regex over every country name, alias, and extra territory —
    longest names first so 'United Arab Emirates' beats 'UAE' beats 'U.S.'."""
    names = (set(_country_index()) | set(_COUNTRY_ALIASES) | set(_EXTRA_PLACES)) - _SCAN_EXCLUDED
    ordered = sorted(names, key=len, reverse=True)
    return re.compile(
        r'\b(' + '|'.join(re.escape(n) for n in ordered) + r')\b',
        re.IGNORECASE,
    )


def find_place(text: str) -> str | None:
    """First country/territory mentioned in *text* (gazetteer + alias scan).

    Cheap headline fallback for when NER is unavailable or finds no location —
    returns a name suitable for the ``country`` argument of ``geocode()``.
    """
    if not text:
        return None
    m = _country_pattern().search(text)
    return m.group(1) if m else None
