"""Asset routing: map an event → affected market indicators with deterministic weights.

The router is a *first-class, auditable artifact* — its mapping quality bounds every
downstream forecasting metric (plan §2). No LLM is involved: the weight for each
(event, symbol) pair is a deterministic product

    weight = sub_category_affinity × symbol_affinity × country_risk × asymmetric_sentiment

where ``asymmetric_sentiment`` keeps the *sign* of the news and **amplifies negative**
sentiment (negative news has larger, harder-to-predict market impact).

Two entry points:
  * ``route_event_to_symbols(...)``      → list[str]  (legacy; symbols only)
  * ``route_event_to_weighted_symbols(...)`` → list[{'symbol', 'weight'}]  (new)
"""


# ── Indicator panel ───────────────────────────────────────────────────────────
# The forecasting target panel is now DB-driven (MarketSymbol.is_forecast). The
# literal below is only a fallback when the table is empty/unreachable. Read the
# live panel via get_panel_symbols(); rules intersect their emitted symbols with it
# so the router never emits a non-panel symbol.
PANEL_SYMBOLS: list[str] = ['CL=F', 'GC=F', 'BTC-USD', 'SPY', 'EURUSD=X']


def get_panel_symbols() -> list[str]:
    """Live forecasting panel (MarketSymbol.is_forecast). Falls back to PANEL_SYMBOLS."""
    from services.market_symbols import get_panel_symbols as _gp
    return _gp()

# topic slug → affected symbols (highest-signal routing)
TOPIC_TO_SYMBOLS: dict[str, list[str]] = {
    'ukraine-war':          ['NG=F', 'ZW=F', 'CL=F', 'GC=F', '^VIX'],
    'russia-ukraine':       ['NG=F', 'ZW=F', 'CL=F', 'GC=F', '^VIX'],
    'middle-east-conflict': ['CL=F', 'GC=F', '^VIX'],
    'iran':                 ['CL=F', 'GC=F', '^VIX'],
    'opec':                 ['CL=F'],
    'fed-rates':            ['^TNX', 'SPY', 'GC=F', 'DX-Y.NYB'],
    'us-economy':           ['SPY', 'DX-Y.NYB', '^VIX'],
    'china-economy':        ['CL=F', 'SPY'],
    'us-china-trade':       ['SPY', 'DX-Y.NYB', '^VIX'],
    'inflation':            ['GC=F', '^TNX', 'DX-Y.NYB'],
    'crypto':               ['BTC-USD', 'ETH-USD'],
    'bitcoin':              ['BTC-USD'],
}

# (category, region_keyword, symbols) — evaluated top-down, region='' matches all.
# VIX/SPY rules added (plan §2 — currently absent from the default set).
CATEGORY_REGION_RULES: list[tuple[str, str, list[str]]] = [
    ('conflict',  'middle east', ['CL=F', 'GC=F', '^VIX']),
    ('conflict',  'russia',      ['NG=F', 'GC=F', '^VIX']),
    ('conflict',  'ukraine',     ['NG=F', 'ZW=F', '^VIX']),
    ('conflict',  'iran',        ['CL=F', 'GC=F', '^VIX']),
    ('conflict',  'israel',      ['CL=F', 'GC=F', '^VIX']),
    ('conflict',  '',            ['GC=F', '^VIX', 'CL=F']),
    ('disaster',  'gulf',        ['CL=F']),
    ('disaster',  '',            ['GC=F']),
    ('economic',  '',            ['SPY', 'GC=F', '^TNX', 'DX-Y.NYB']),
    ('political', 'us',          ['SPY', 'DX-Y.NYB', '^VIX']),
    ('political', '',            ['GC=F', 'SPY', '^VIX']),
    ('health',    '',            ['^VIX', 'SPY']),
]

# fallback if no rules match
CATEGORY_DEFAULTS: dict[str, list[str]] = {
    'conflict':  ['GC=F', 'CL=F', '^VIX'],
    'economic':  ['SPY', 'GC=F', '^TNX'],
    'political': ['GC=F', 'SPY'],
    'disaster':  ['GC=F'],
    'health':    ['^VIX', 'SPY'],
    'general':   [],
    # legacy flat categories — still routed for backward compatibility with old data
    'protest':   ['GC=F'],
    'crime':     [],
}

# ── Sub-category affinity (plan two-level taxonomy) ───────────────────────────
# Multiplier in [0, 1] capturing how market-moving a sub-category is. Sub-categories
# absent here default to 0.6.
SUB_CATEGORY_AFFINITY: dict[str, float] = {
    # conflict
    'war': 1.0, 'airstrike': 0.9, 'strike': 0.9, 'insurgency': 0.6,
    'terrorism': 0.8, 'border-clash': 0.7, 'border_clash': 0.7,
    'ground_offensive': 0.9, 'naval': 0.8, 'siege': 0.7, 'casualties': 0.7,
    'ceasefire': 0.8,
    # disaster
    'earthquake': 0.7, 'flood': 0.5, 'storm': 0.6, 'wildfire': 0.5,
    'industrial-accident': 0.7, 'industrial_accident': 0.7, 'explosion': 0.7,
    'epidemic': 0.6,
    # economic
    'monetary-policy': 1.0, 'monetary_policy': 1.0, 'energy': 0.9,
    'trade': 0.8, 'tariffs': 0.8, 'trade/tariffs': 0.8, 'labor': 0.6,
    'markets': 0.9, 'sanctions': 0.8, 'sanction': 0.8, 'inflation': 0.9,
    'fiscal': 0.7, 'supply_chain': 0.7,
    # political
    'election': 0.7, 'legislation': 0.6, 'diplomacy': 0.6,
    'leadership-change': 0.8, 'leadership_change': 0.8, 'coup': 0.9,
    'protest-policy': 0.5,
    # health
    'outbreak': 0.7, 'pandemic': 0.9, 'healthcare-system': 0.4,
}

# ── Symbol affinity per top-level category ────────────────────────────────────
# How strongly a category, when it touches a symbol, actually moves it.
SYMBOL_AFFINITY: dict[tuple[str, str], float] = {
    ('conflict', 'GC=F'): 0.8, ('conflict', 'CL=F'): 0.9, ('conflict', 'NG=F'): 0.8,
    ('conflict', 'ZW=F'): 0.7, ('conflict', '^VIX'): 0.9, ('conflict', 'SPY'): 0.6,
    ('economic', 'SPY'): 0.9, ('economic', 'GC=F'): 0.7, ('economic', '^TNX'): 0.9,
    ('economic', 'DX-Y.NYB'): 0.8, ('economic', '^VIX'): 0.6,
    ('political', 'GC=F'): 0.6, ('political', 'SPY'): 0.7, ('political', '^VIX'): 0.7,
    ('political', 'DX-Y.NYB'): 0.6,
    ('disaster', 'GC=F'): 0.5, ('disaster', 'CL=F'): 0.5,
    ('health', '^VIX'): 0.6, ('health', 'SPY'): 0.5,
}
_DEFAULT_SYMBOL_AFFINITY = 0.6

# ── Country risk weight ───────────────────────────────────────────────────────
# "Important" / systemically-relevant countries amplify market impact.
COUNTRY_RISK: dict[str, float] = {
    'united states': 1.0, 'usa': 1.0, 'us': 1.0,
    'china': 0.95, 'russia': 0.9, 'ukraine': 0.85, 'iran': 0.85,
    'israel': 0.8, 'saudi arabia': 0.8, 'european union': 0.85,
    'germany': 0.75, 'united kingdom': 0.7, 'uk': 0.7, 'japan': 0.75,
    'india': 0.7, 'taiwan': 0.8, 'north korea': 0.7,
}
_DEFAULT_COUNTRY_RISK = 0.5


def _country_risk(location: str) -> float:
    loc = (location or '').lower()
    best = _DEFAULT_COUNTRY_RISK
    for country, risk in COUNTRY_RISK.items():
        if country in loc and risk > best:
            best = risk
    return best


def asymmetric_sentiment(sentiment: float | None) -> float:
    """Signed sentiment multiplier with negative amplification.

    Returns a value in roughly [-1.5, 1.0]. Magnitude is what feeds the weight;
    the sign is preserved so downstream consumers know directionality. Negative
    sentiment is amplified by 1.5× (negative news has outsized market impact).
    A missing/neutral sentiment maps to a small positive baseline (0.5) so events
    are never zeroed out purely for lacking a sentiment score.
    """
    if sentiment is None:
        return 0.5
    if sentiment < 0:
        return max(-1.5, sentiment * 1.5)
    return max(0.3, sentiment)


def route_event_to_weighted_symbols(
    category: str,
    location: str,
    topic_slugs: list[str],
    sub_categories: list[str] | None = None,
    sentiment: float | None = None,
) -> list[dict]:
    """Return ``[{'symbol': str, 'weight': float}]`` for the affected indicators.

    ``weight`` is the deterministic product described in the module docstring,
    signed by ``asymmetric_sentiment``. Magnitude is clamped to [0, 1] after the
    sentiment sign is applied; the sign is retained.
    """
    symbols = route_event_to_symbols(category, location, topic_slugs)
    if not symbols:
        return []

    sub_categories = sub_categories or []
    # Best sub-category affinity among those present on the event (else neutral 0.6).
    sub_affinity = max(
        (SUB_CATEGORY_AFFINITY.get(sc, 0.6) for sc in sub_categories),
        default=0.6,
    )
    crisk = _country_risk(location)
    sent = asymmetric_sentiment(sentiment)
    sign = 1.0 if sent >= 0 else -1.0
    sent_mag = min(abs(sent), 1.0)

    weighted: list[dict] = []
    for sym in symbols:
        sym_affinity = SYMBOL_AFFINITY.get((category, sym), _DEFAULT_SYMBOL_AFFINITY)
        magnitude = sub_affinity * sym_affinity * crisk * max(sent_mag, 0.1)
        magnitude = min(magnitude, 1.0)
        weighted.append({'symbol': sym, 'weight': round(sign * magnitude, 4)})
    return weighted


def route_event_to_symbols(
    category: str,
    location: str,
    topic_slugs: list[str],
) -> list[str]:
    """Return a deduplicated list of symbols this event likely affects.

    Emitted symbols are intersected with the live forecasting panel
    (``get_panel_symbols()``) so the router never emits a non-panel symbol.
    """
    symbols: list[str] = []

    for slug in topic_slugs:
        symbols.extend(TOPIC_TO_SYMBOLS.get(slug, []))

    loc_lower = (location or '').lower()
    for cat, region, syms in CATEGORY_REGION_RULES:
        if cat == category and (not region or region in loc_lower):
            symbols.extend(syms)

    if not symbols:
        symbols.extend(CATEGORY_DEFAULTS.get(category, []))

    panel = set(get_panel_symbols())
    seen: set[str] = set()
    result: list[str] = []
    for s in symbols:
        if s in panel and s not in seen:
            seen.add(s)
            result.append(s)
    return result
