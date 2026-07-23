"""Asset routing: map an event → affected market indicators with deterministic weights.

The router is a *first-class, auditable artifact* — its mapping quality bounds every
downstream forecasting metric (plan §2). No LLM is involved: the weight for each
(event, symbol) pair is a deterministic product

    weight = sign(sentiment) × sign(symbol_affinity)
             × |symbol_affinity| × sub_category_affinity × country_risk
             × |asymmetric_sentiment| × intensity

``asymmetric_sentiment`` carries the *direction* of the news (and **amplifies
negative** sentiment — negative news has larger, harder-to-predict impact), but the
final per-symbol direction also depends on the **sign of symbol_affinity**: a symbol
that moves WITH the news (risk asset, e.g. SPY: bad news → down) has positive
affinity, while one that moves AGAINST it (the VIX fear gauge, gold and other safe
havens, supply-shock commodities on conflict) has negative affinity, so the same
negative event pushes equities down but gold/oil/VIX up. |symbol_affinity| is the
strength of the move.

Two entry points:
  * ``route_event_to_symbols(...)``      → list[str]  (symbols only; used internally
    by ``route_event_to_weighted_symbols`` below)
  * ``route_event_to_weighted_symbols(...)`` → list[{'symbol', 'weight'}]  (preferred
    for callers that need per-symbol weights)
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
# SIGNED. The sign encodes DIRECTION relative to the news sentiment; the magnitude
# encodes STRENGTH.
#   positive → "risk asset", moves WITH sentiment (bad news → the symbol falls):
#              equities (SPY), yields/dollar on economic news, crypto.
#   negative → moves AGAINST sentiment (bad news → the symbol rises): the ^VIX fear
#              gauge (inverse in every category), gold and other safe havens, and
#              supply-shock commodities (oil/gas/wheat) on conflict/disaster where
#              the *threat itself* lifts the price.
# So a single negative conflict event correctly pushes SPY down but GC=F/CL=F/^VIX
# up — see route_event_to_weighted_symbols. Tuned to the well-documented crisis
# co-movements (geopolitical risk-off: equities↓, gold↑, oil↑, VIX↑).
SYMBOL_AFFINITY: dict[tuple[str, str], float] = {
    # conflict — havens & supply-shock commodities RISE on (negative) conflict news
    ('conflict', 'GC=F'): -0.8, ('conflict', 'CL=F'): -0.9, ('conflict', 'NG=F'): -0.8,
    ('conflict', 'ZW=F'): -0.7, ('conflict', '^VIX'): -0.9, ('conflict', 'SPY'): 0.6,
    # economic — risk assets move WITH sentiment; VIX inverse; gold a mild haven
    ('economic', 'SPY'): 0.9, ('economic', 'GC=F'): -0.4, ('economic', '^TNX'): 0.9,
    ('economic', 'DX-Y.NYB'): 0.8, ('economic', '^VIX'): -0.6,
    # political — gold & VIX inverse (haven / fear); equities & dollar with sentiment
    ('political', 'GC=F'): -0.6, ('political', 'SPY'): 0.7, ('political', '^VIX'): -0.7,
    ('political', 'DX-Y.NYB'): 0.6,
    # disaster — commodity supply disruption & haven bid lift these
    ('disaster', 'GC=F'): -0.5, ('disaster', 'CL=F'): -0.5,
    # health — VIX inverse (fear), equities with sentiment
    ('health', '^VIX'): -0.6, ('health', 'SPY'): 0.5,
}
# Unknown (category, symbol) pairs default to a risk-asset that moves WITH sentiment.
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


# Categories that are risk-off by nature. A *positive* sentiment reading on a
# conflict/disaster story is usually the sentiment model misreading escalation
# or aid/deal/production vocabulary as good news (measured live: "discusses
# weapons production" +0.62, "conflict beyond borders if Trump delivers threats"
# +0.22), not a genuine de-escalation. Damping the positive tail makes that
# likely-wrong risk-ON read a *smaller* bet without flipping direction — a real
# ceasefire still points the right way (havens down), just with less magnitude —
# while negative readings pass through unchanged (a war/disaster IS risk-off).
# Mirrors asymmetric_sentiment's own "amplify negative, floor positive" stance.
_RISK_OFF_CATEGORIES = frozenset({'conflict', 'disaster'})
_RISK_OFF_POSITIVE_DAMP = 0.5


def _category_adjusted_sentiment(category: str, sentiment: float | None) -> float | None:
    """Damp a positive sentiment on an inherently risk-off category; leave
    negatives and other categories untouched. See _RISK_OFF_CATEGORIES."""
    if sentiment is not None and sentiment > 0 and category in _RISK_OFF_CATEGORIES:
        return sentiment * _RISK_OFF_POSITIVE_DAMP
    return sentiment


def select_route_sentiment(avg_finbert: float | None, avg_sentiment: float | None) -> float | None:
    """Pick the sentiment that feeds routing: financial (FinBERT) if present, else
    general (VADER). Single source of truth so every routing call site agrees.

    Uses ``is not None`` — a genuine neutral 0.0 FinBERT reading must NOT fall
    through to the general sentiment (``avg_finbert or avg_sentiment`` would).
    """
    return avg_finbert if avg_finbert is not None else avg_sentiment


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
    intensity: float | None = None,
) -> list[dict]:
    """Return ``[{'symbol': str, 'weight': float}]`` for the affected indicators.

    ``weight`` is the deterministic product described in the module docstring,
    signed by ``asymmetric_sentiment``. Magnitude is clamped to [0, 1] after the
    sentiment sign is applied; the sign is retained. ``intensity`` (the event's
    0–1 severity, e.g. Event.avg_intensity) scales magnitude so a major event
    outweighs a routine one on the same symbol — a neutral event (intensity None)
    is unchanged.
    """
    symbols = route_event_to_symbols(category, location, topic_slugs)
    if not symbols:
        return []

    sub_categories = sub_categories or []
    # Best sub-category affinity among those present on the event (else neutral 0.6).
    sub_affinity = max((SUB_CATEGORY_AFFINITY.get(sc, 0.6) for sc in sub_categories), default=0.6)
    crisk = _country_risk(location)
    sent = asymmetric_sentiment(_category_adjusted_sentiment(category, sentiment))
    sentiment_sign = 1.0 if sent >= 0 else -1.0
    sent_mag = min(abs(sent), 1.0)
    # Severity scaler in [0.6, 1.0]: full weight for a severe event, damped for a
    # routine one; None (unknown) leaves weight unchanged so behavior is stable.
    intensity_factor = 1.0 if intensity is None else 0.6 + 0.4 * max(0.0, min(intensity, 1.0))

    weighted: list[dict] = []
    for sym in symbols:
        affinity = SYMBOL_AFFINITY.get((category, sym), _DEFAULT_SYMBOL_AFFINITY)
        # Direction is the sentiment sign times the symbol's polarity: a safe
        # haven / fear gauge (negative affinity) inverts, so a negative event
        # lifts it while equities fall. Magnitude uses |affinity|.
        polarity = 1.0 if affinity >= 0 else -1.0
        magnitude = sub_affinity * abs(affinity) * crisk * max(sent_mag, 0.1) * intensity_factor
        magnitude = min(magnitude, 1.0)
        weighted.append({'symbol': sym, 'weight': round(sentiment_sign * polarity * magnitude, 4)})
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
