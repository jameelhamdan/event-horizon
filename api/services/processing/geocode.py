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
            best[name] = (pop, float(c['latitude']), float(c['longitude']), countries.get(c.get('countrycode', '')))
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
    'jammu and kashmir': (33.7782, 76.5762), 'kashmir': (33.7782, 76.5762),
}

# City spellings the gazetteer lists under a different form.
_CITY_ALIASES: dict[str, str] = {'kiev': 'kyiv'}

# Demonym → canonical country, for the find_place() fallback: a headline that
# never names the country outright ("Russian journalist jailed", "Chinese
# authorities") still locates. Only unambiguous, single-country demonyms —
# "American"/"Indian"/"Korean" are excluded (region/ethnicity collisions).
_DEMONYMS: dict[str, str] = {
    'russian': 'Russia', 'ukrainian': 'Ukraine', 'chinese': 'China',
    'israeli': 'Israel', 'palestinian': 'Palestine', 'iranian': 'Iran',
    'syrian': 'Syria', 'iraqi': 'Iraq', 'afghan': 'Afghanistan',
    'japanese': 'Japan', 'german': 'Germany', 'french': 'France',
    'british': 'United Kingdom', 'spanish': 'Spain', 'italian': 'Italy',
    'turkish': 'Turkey', 'egyptian': 'Egypt', 'saudi': 'Saudi Arabia',
    'pakistani': 'Pakistan', 'mexican': 'Mexico', 'brazilian': 'Brazil',
    'nigerian': 'Nigeria', 'venezuelan': 'Venezuela', 'lebanese': 'Lebanon',
    'yemeni': 'Yemen', 'sudanese': 'Sudan', 'greek': 'Greece',
    'polish': 'Poland', 'dutch': 'Netherlands', 'swedish': 'Sweden',
    'portuguese': 'Portugal', 'taiwanese': 'Taiwan', 'vietnamese': 'Vietnam',
    'thai': 'Thailand', 'indonesian': 'Indonesia', 'filipino': 'Philippines',
    'australian': 'Australia', 'canadian': 'Canada', 'colombian': 'Colombia',
    'cuban': 'Cuba', 'ethiopian': 'Ethiopia', 'kenyan': 'Kenya',
    'somali': 'Somalia', 'libyan': 'Libya', 'algerian': 'Algeria',
    'moroccan': 'Morocco', 'tunisian': 'Tunisia', 'qatari': 'Qatar',
}

# Names shared by a sovereign country and a US state — when the article is
# clearly US-focused, the state reading wins (observed live: "Biden wins Georgia
# recount" geocoded to the country Georgia rather than the US state).
_US_STATE_COUNTRY_COLLISIONS = {'georgia'}
# US context signal. "US" matched case-sensitively (uppercase) so the pronoun
# "us" doesn't fire; the rest are case-insensitive.
_US_CONTEXT_RE = re.compile(r'\bUS\b|(?i:\bU\.S\.?A?\b|\bunited states\b|\bamerican\b|\bwashington\b)')


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


def city_country_conflict(city: str | None, country: str | None) -> bool:
    """True if *city* and *country* both resolve but disagree — e.g. a NER
    span found a real city ("Kyiv") paired with an unrelated country mention
    picked up elsewhere in the text ("Russia"), producing a self-contradictory
    location like "Kyiv, Russia". Callers should trust the city's own
    (gazetteer-verified) country over a stray/mismatched country mention when
    this returns True."""
    if not city or not country:
        return False
    resolved = country_of_city(city)
    paired = canonical_country(country)
    return bool(resolved and paired and resolved != paired)


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
    return re.compile(r'\b(' + '|'.join(re.escape(n) for n in ordered) + r')\b', re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _demonym_pattern() -> re.Pattern:
    """Alternation over every demonym (longest first)."""
    ordered = sorted(_DEMONYMS, key=len, reverse=True)
    return re.compile(r'\b(' + '|'.join(re.escape(d) for d in ordered) + r')\b', re.IGNORECASE)


def find_place(text: str) -> str | None:
    """First country/territory mentioned in *text* (gazetteer + alias scan),
    falling back to a demonym scan ("Russian" → Russia) so a headline that never
    names the country outright still locates.

    Cheap headline fallback for when NER is unavailable or finds no location —
    returns a name suitable for the ``country`` argument of ``geocode()``.
    """
    if not text:
        return None
    m = _country_pattern().search(text)
    return m.group(1) if m else find_demonym(text)


def find_demonym(text: str) -> str | None:
    """Country from the first unambiguous demonym in *text* ("Russian" → Russia),
    or None. Unlike find_place this scans demonyms *only* — no country-name pass —
    so it is safe to run even when NER already found (and we excluded) a
    place-name/surname collision: a demonym is never a person-name span, so it
    can't re-introduce that false positive."""
    if not text:
        return None
    dm = _demonym_pattern().search(text)
    return _DEMONYMS[dm.group(1).lower()] if dm else None


def resolve_state_country_collision(country: str | None, text: str | None) -> str | None:
    """Return 'United States' when *country* is a name shared by a US state and a
    foreign country (Georgia) and *text* is clearly US-focused; otherwise return
    *country* unchanged. Guards the NER/gazetteer against reading the US state
    "Georgia" as the Caucasus country."""
    if country and _norm(country) in _US_STATE_COUNTRY_COLLISIONS and _US_CONTEXT_RE.search(text or ''):
        return 'United States'
    return country
