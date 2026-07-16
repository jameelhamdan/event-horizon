"""Dependency-light self-tests for the deterministic event->symbol router
(services/forecasting/routing.py) — the production-default router
(FORECAST_ROUTER='rules').

No database or network required — get_panel_symbols() is patched to a fixed
panel so results don't depend on whether MarketSymbol/Mongo is reachable.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_forecasting_routing
"""

from unittest.mock import patch

from tests._runner import bootstrap_django, run

bootstrap_django()

_FIXED_PANEL = ['CL=F', 'GC=F', 'BTC-USD', 'SPY', 'EURUSD=X', '^VIX', '^TNX', 'DX-Y.NYB', 'NG=F', 'ZW=F']


def _patch_panel(panel=_FIXED_PANEL):
    return patch('services.forecasting.routing.get_panel_symbols', return_value=panel)


# ── route_event_to_symbols ─────────────────────────────────────────────────────

def test_route_by_topic_slug():
    from services.forecasting.routing import route_event_to_symbols
    with _patch_panel():
        symbols = route_event_to_symbols('conflict', '', ['ukraine-war'])
    assert 'NG=F' in symbols and 'CL=F' in symbols and 'GC=F' in symbols


def test_route_by_category_region_rule():
    from services.forecasting.routing import route_event_to_symbols
    with _patch_panel():
        symbols = route_event_to_symbols('conflict', 'Middle East tensions rise', [])
    assert set(symbols) == {'CL=F', 'GC=F', '^VIX'}


def test_route_region_rule_adds_to_generic_conflict_rule():
    """Region rules are cumulative with the generic ('conflict', '', ...) rule,
    not a replacement for it — a russia-specific event picks up NG=F on top of
    the generic conflict symbols (GC=F, ^VIX, CL=F)."""
    from services.forecasting.routing import route_event_to_symbols
    with _patch_panel():
        symbols = route_event_to_symbols('conflict', 'Russia border clash', [])
    assert 'NG=F' in symbols  # from the russia-specific rule
    assert set(symbols) == {'NG=F', 'GC=F', '^VIX', 'CL=F'}


def test_route_falls_back_to_category_default_when_no_rule_or_topic_matches():
    from services.forecasting.routing import route_event_to_symbols
    # 'health' has no region rules keyed by a specific region, so the '' (any) rule fires first;
    # use a category with only a CATEGORY_DEFAULTS entry and no matching region rule at all.
    with _patch_panel():
        symbols = route_event_to_symbols('general', 'Somewhere', [])
    assert symbols == []  # CATEGORY_DEFAULTS['general'] == []


def test_route_dedupes_symbols_across_topic_and_rule():
    from services.forecasting.routing import route_event_to_symbols
    # 'ukraine-war' topic and the 'ukraine' region rule both emit overlapping symbols.
    with _patch_panel():
        symbols = route_event_to_symbols('conflict', 'Ukraine front line', ['ukraine-war'])
    assert len(symbols) == len(set(symbols))


def test_route_never_emits_off_panel_symbol():
    from services.forecasting.routing import route_event_to_symbols
    with _patch_panel(['SPY']):
        symbols = route_event_to_symbols('conflict', 'Middle East', [])
    # Middle-east conflict rules only ever emit CL=F/GC=F/^VIX — none are on this
    # panel, so nothing survives the panel intersection (SPY itself was never a
    # candidate for this category/location).
    assert symbols == []


def test_route_unknown_topic_slug_contributes_nothing():
    from services.forecasting.routing import route_event_to_symbols
    with _patch_panel():
        symbols = route_event_to_symbols('general', '', ['no-such-topic-xyz'])
    assert symbols == []


# ── asymmetric_sentiment ───────────────────────────────────────────────────────

def test_asymmetric_sentiment_none_is_small_positive_default():
    from services.forecasting.routing import asymmetric_sentiment
    assert asymmetric_sentiment(None) == 0.5


def test_asymmetric_sentiment_negative_amplified_1_5x():
    from services.forecasting.routing import asymmetric_sentiment
    assert abs(asymmetric_sentiment(-0.4) - (-0.6)) < 1e-9


def test_asymmetric_sentiment_negative_clamped_at_neg_1_5():
    from services.forecasting.routing import asymmetric_sentiment
    assert asymmetric_sentiment(-1.0) == -1.5


def test_asymmetric_sentiment_positive_passthrough_above_floor():
    from services.forecasting.routing import asymmetric_sentiment
    assert asymmetric_sentiment(0.7) == 0.7


def test_asymmetric_sentiment_small_positive_floored_at_0_3():
    from services.forecasting.routing import asymmetric_sentiment
    assert asymmetric_sentiment(0.1) == 0.3


def test_asymmetric_sentiment_zero_floored_at_0_3():
    from services.forecasting.routing import asymmetric_sentiment
    assert asymmetric_sentiment(0.0) == 0.3


# ── select_route_sentiment ─────────────────────────────────────────────────────

def test_select_route_sentiment_prefers_finbert_when_present():
    from services.forecasting.routing import select_route_sentiment
    assert select_route_sentiment(0.4, -0.9) == 0.4


def test_select_route_sentiment_keeps_neutral_zero_finbert():
    # Regression: a genuine 0.0 FinBERT reading must NOT fall through to the
    # general sentiment (the old `avg_finbert or avg_sentiment` bug did).
    from services.forecasting.routing import select_route_sentiment
    assert select_route_sentiment(0.0, -0.9) == 0.0


def test_select_route_sentiment_falls_back_when_finbert_missing():
    from services.forecasting.routing import select_route_sentiment
    assert select_route_sentiment(None, -0.9) == -0.9
    assert select_route_sentiment(None, None) is None


# ── route_event_to_weighted_symbols ────────────────────────────────────────────

def test_weighted_symbols_empty_when_no_symbols_routed():
    from services.forecasting.routing import route_event_to_weighted_symbols
    with _patch_panel():
        weighted = route_event_to_weighted_symbols('general', '', [], sub_categories=[], sentiment=0.0)
    assert weighted == []


def test_weighted_symbols_shape_and_sign_for_negative_sentiment():
    from services.forecasting.routing import route_event_to_weighted_symbols
    with _patch_panel():
        weighted = route_event_to_weighted_symbols(
            'conflict', 'Russia', ['ukraine-war'], sub_categories=['war'], sentiment=-0.8,
        )
    assert weighted
    for row in weighted:
        assert set(row.keys()) == {'symbol', 'weight'}
        assert row['weight'] < 0  # negative sentiment → negative-signed weight
        assert -1.0 <= row['weight'] <= 1.0


def test_weighted_symbols_positive_sentiment_gives_positive_weight():
    from services.forecasting.routing import route_event_to_weighted_symbols
    with _patch_panel():
        weighted = route_event_to_weighted_symbols(
            'economic', '', [], sub_categories=['monetary-policy'], sentiment=0.6,
        )
    assert weighted
    assert all(row['weight'] > 0 for row in weighted)


def test_weighted_symbols_magnitude_never_exceeds_one():
    from services.forecasting.routing import route_event_to_weighted_symbols
    # High-affinity sub-category + high country risk + max sentiment — magnitude
    # is a product of factors ≤ 1.0 each, so it should clamp at 1.0, never exceed it.
    with _patch_panel():
        weighted = route_event_to_weighted_symbols(
            'conflict', 'United States', ['ukraine-war'], sub_categories=['war'], sentiment=-1.0,
        )
    assert all(abs(row['weight']) <= 1.0 for row in weighted)


def test_weighted_symbols_higher_country_risk_increases_magnitude():
    from services.forecasting.routing import route_event_to_weighted_symbols
    with _patch_panel():
        low_risk = route_event_to_weighted_symbols(
            'conflict', 'Nowhereland', ['ukraine-war'], sub_categories=['war'], sentiment=-0.5,
        )
        high_risk = route_event_to_weighted_symbols(
            'conflict', 'Russia', ['ukraine-war'], sub_categories=['war'], sentiment=-0.5,
        )
    low_by_symbol = {r['symbol']: abs(r['weight']) for r in low_risk}
    high_by_symbol = {r['symbol']: abs(r['weight']) for r in high_risk}
    shared = set(low_by_symbol) & set(high_by_symbol)
    assert shared
    assert all(high_by_symbol[s] >= low_by_symbol[s] for s in shared)


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_route_by_topic_slug,
    test_route_by_category_region_rule,
    test_route_region_rule_adds_to_generic_conflict_rule,
    test_route_falls_back_to_category_default_when_no_rule_or_topic_matches,
    test_route_dedupes_symbols_across_topic_and_rule,
    test_route_never_emits_off_panel_symbol,
    test_route_unknown_topic_slug_contributes_nothing,
    test_asymmetric_sentiment_none_is_small_positive_default,
    test_asymmetric_sentiment_negative_amplified_1_5x,
    test_asymmetric_sentiment_negative_clamped_at_neg_1_5,
    test_asymmetric_sentiment_positive_passthrough_above_floor,
    test_asymmetric_sentiment_small_positive_floored_at_0_3,
    test_asymmetric_sentiment_zero_floored_at_0_3,
    test_select_route_sentiment_prefers_finbert_when_present,
    test_select_route_sentiment_keeps_neutral_zero_finbert,
    test_select_route_sentiment_falls_back_when_finbert_missing,
    test_weighted_symbols_empty_when_no_symbols_routed,
    test_weighted_symbols_shape_and_sign_for_negative_sentiment,
    test_weighted_symbols_positive_sentiment_gives_positive_weight,
    test_weighted_symbols_magnitude_never_exceeds_one,
    test_weighted_symbols_higher_country_risk_increases_magnitude,
]


if __name__ == '__main__':
    run(_TESTS)
