---
name: pipeline-validate
description: Validate that each pipeline stage and field (category, event_intensity, importance_score, affected_indicators) is actually doing real work, by backtesting known market-moving LANDMARK dates against random control dates over the last 5 years and checking them against the backfilled PriceBar data. Use when asked to verify the pipeline end-to-end, sanity-check whether importance/intensity/routing "matter", or confirm a stage is populated and predictive before trusting forecasts.
---

# Pipeline validation + importance/price backtest

Confirms the pipeline produces *real* signal — not just populated fields — by
asking, for a set of curated market-moving dates vs random dates: did each
stage fire, and does what it produced line up with what the market actually
did in `PriceBar`?

Runs `python manage.py validate_pipeline` (needs Mongo + PriceBar history).

```bash
# From api/ (needs Mongo with historical Events + PriceBars)
python manage.py validate_pipeline
python manage.py validate_pipeline --window-days 3 --random-samples 30 --seed 1
```

**Data requirement:** the intensity/importance/routing rows are only meaningful
against a DB that has historical **Events** across the landmark range
(2021–2026). A fresh dev box usually has only recent Events — run
`aggregate_history_task` over the backfill range first, or point
`DATABASE_URL` at production. The PriceBar-move rows work with any populated
price history. If `with_events` is 0%, you're measuring an un-aggregated DB,
not a broken pipeline.

## What it measures

Writes `results/validate_pipeline/validate_<ts>.json` and prints:

1. **Stage coverage on landmark dates** — the step-by-step "did it fire?"
   ladder: `with_articles → with_events → with_importance → with_routed →
   with_price_move`. The first stage that drops toward 0% is where the
   pipeline is failing for real, market-relevant days.
2. **Landmark vs random discrimination** — mean `event_intensity`,
   `importance_score`, and realized `|price move|` on landmark dates vs random
   control dates. Landmark values should be **materially higher** — that's the
   proof intensity/importance track real market impact rather than being noise.
   (Reference: on a validated price set, landmark dates move ~1.9× more than
   random — ~13% vs ~7% max move.)
3. **Spearman(intensity, |move|)** — rank correlation between an event's
   intensity and how much its symbols actually moved. Positive = intensity is
   predictive of magnitude.
4. **Directional hit-rate** — of routed (event→symbol) pairs, the fraction
   where the signed `affected_indicators` weight matched the realized return's
   sign. **> 0.50** means routing beats a coin flip on direction (the primary
   forecast target; see also `evaluate_forecasting`).

## How to judge

- **Coverage ladder healthy** = every step ≥ ~80% on landmark dates. A cliff at
  `with_events` → aggregation isn't clustering these; at `with_routed` →
  routing is emitting nothing (check the forecast panel / routing rules — see
  `services/forecasting/routing.py`); at `with_price_move` → the symbol has no
  PriceBar history.
- **Discrimination** = landmark clearly > random on intensity/importance/move.
  If landmark ≈ random, the field is not capturing importance — tune intensity
  priors (`taxonomy.py::PRIORS`, `annotator.py::rate_intensity`) or the
  importance scorer (`services/scoring`).
- **Spearman ≤ 0 or hit-rate ≤ 0.5** = the field/router is not predictive;
  don't trust downstream forecasts until it is. Cross-check against
  `evaluate_forecasting` (routing precision@k + return MAE) and the routing
  fixes in `services/forecasting/routing.py`.

## Extending

Add landmark rows to `LANDMARK_EVENTS` in
`core/management/commands/validate_pipeline.py` (`date`, `desc`, `category`,
`region`, and the `symbols` that *should* have moved). Keep them
unambiguous, high-impact days so they stay a clean ground-truth set.
