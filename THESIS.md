# News Event-Fused Market Forecasting: A Real-Time Pipeline for Geolocated News Event Extraction and Financial Indicator Prediction

**Repository:** `news-events-market-forcasting`

---

## Abstract

Financial markets respond continuously to a stream of unstructured, multi-lingual,
geographically dispersed news. This thesis presents the design, implementation, and honest
evaluation of an end-to-end system that ingests real-time news from heterogeneous sources,
extracts and **geolocates** discrete events using a hybrid neural and large-language-model
(LLM) pipeline, links those events to a configurable panel of market indicators, and produces
calibrated **directional and magnitude forecasts** over short horizons (1 and 5 trading days).

The central methodological commitment is **leakage-free supervised learning**: the
event→indicator association produced by an LLM router is treated strictly as an *input feature*
— a hypothesis — while the supervised label is the *realized* price return between two observed
price nodes. A walk-forward backtest with four ablation arms (naïve persistence, price-only,
price + rule-routed events, price + LLM-routed events) quantifies whether news events add
predictive value over price history alone. On real daily data the system attains directional
accuracy of approximately **0.52** with ROC-AUC ≈ 0.52–0.53 — consistent with the
near-random-walk behaviour of liquid markets — and the contribution is therefore framed
relative to the naïve baseline rather than as a trading strategy. Alongside the forecasting
layer, the system delivers a live geospatial event map, automatic topic discovery, and a daily
generated news briefing, demonstrating that a single ingestion-and-understanding pipeline can
serve both situational-awareness and quantitative-forecasting use cases.

A secondary engineering contribution is a **provider-agnostic LLM orchestration layer** with
per-task routing, automatic fallback across self-hosted and hosted providers, and a daily
discovery service that probes and caches currently-available free models — addressing the
operational reality that hosted LLM availability and rate limits fluctuate continuously.

---

## Table of Contents

1. Introduction
2. Background and Related Work
3. System Overview
4. Data Ingestion
5. The Natural-Language Understanding Pipeline
6. Event Aggregation and Geolocation
7. Topic Discovery and Tagging
8. Event-to-Indicator Routing
9. The Forecasting Model
10. LLM Orchestration
11. System Engineering and Deployment
12. Evaluation
13. Discussion and Limitations
14. Conclusion and Future Work
- References
- Appendix A — Indicator Panel
- Appendix B — Reproduction Commands

---

## 1. Introduction

### 1.1 Motivation

News precedes, accompanies, and explains market movement, yet the link between the two is
buried in unstructured text spread across thousands of sources in many languages. Two distinct
audiences want to traverse that link in opposite directions: analysts and the public want a
**situational picture** ("what is happening, where"), while quantitative practitioners want a
**predictive picture** ("what will move, and which way"). This thesis argues that both can be
served by one pipeline whose intermediate representation — a stream of typed, geolocated,
topic-tagged **events** — is useful in its own right and is also a natural feature source for
forecasting.

### 1.2 Problem statement

Given a continuous feed of news items, the system must (i) extract discrete events with a
category, location, sentiment, and topical tags; (ii) associate each event with the market
indicators it plausibly affects and in which direction; and (iii) forecast, in a strictly
leakage-free manner, the short-horizon direction and magnitude of those indicators. The
forecasting claim must be evaluated honestly against baselines, acknowledging that public news
is lagged and largely priced-in.

### 1.3 Contributions

1. An **end-to-end real-time pipeline** from multi-source ingestion to geolocated event
   extraction, served as a live map and an event API.
2. An **event-fused forecasting layer** that cleanly separates the LLM-produced event→indicator
   *feature* from the realized-return *label*, trained per horizon with calibrated LightGBM
   models and validated by a leakage-checked walk-forward backtest with four ablation arms.
3. A **provider-agnostic LLM orchestration layer** with per-role routing, cross-provider
   fallback, and daily dynamic discovery of available free models.
4. An **honest empirical study** reporting near-baseline predictive performance, framed as a
   research-grade signal rather than alpha.

### 1.4 Scope

The system is explicitly *not* a trading system. It predicts direction and volatility of a
small indicator panel as a supervised-learning exercise; no order execution, position sizing,
or transaction-cost modelling is performed.

---

## 2. Background and Related Work

The work sits at the intersection of three research areas:

- **Event and entity extraction** from news, traditionally addressed with sequence-labelling
  models (e.g. transformer-based NER) but, in this work, performed by **LLM prompting** for joint
  entity/category/sentiment extraction in a single pass.
- **Geoparsing**, the resolution of place mentions to coordinates, here handled by the same
  LLM call with gazetteer-assisted normalisation.
- **News-driven financial prediction**, a long literature relating textual sentiment and event
  signals to asset returns, with the persistent finding that public-news signals are weak,
  lagged, and partially priced-in. This motivates the thesis's baseline-first evaluation stance.

The system's novelty is integrative rather than algorithmic: it operationalises these strands
in a single, deployable, multilingual pipeline whose event representation is shared between a
geospatial product and a leakage-disciplined forecasting study.

---

## 3. System Overview

The system is a two-tier application: a Python backend (Django 6 + Django REST Framework) with a
document store (MongoDB) and an in-memory broker (Redis), and a single-page React 19 / Vite
frontend rendering a Leaflet map and a markets dashboard. Asynchronous work is executed by a
task queue (`django-rq`) split across a *light* queue (fast I/O) and a *heavy* queue (NLP/LLM),
scheduled by a cron daemon.

```
Sources ──▶ Ingestion ──▶ NLP understanding ──▶ Event aggregation ──▶ Topic tagging
                                                       │
                                                       ├──▶ Event→indicator routing ──▶ Forecasting
                                                       └──▶ Live map / API / daily briefing
```

The pipeline is fully automated through scheduled tasks; each stage records its status on the
record it processes, allowing partial reprocessing and health monitoring.

---

## 4. Data Ingestion

News is collected from two source families: RSS/Atom feeds (via a feed parser) and Telegram
channels (via a client library). Ingested items are normalised into a common article record,
filtered by a minimum word count, and deduplicated by a Jaccard similarity on titles within a
rolling window to suppress near-duplicate syndicated copies. Article identifiers are stored as
string UUIDs. Ingestion runs on the light queue at a short cadence; downstream NLP is dispatched
in a fan-out pattern so a slow stage never blocks collection.

---

## 5. The Natural-Language Understanding Pipeline

Each article is enriched by a hybrid pipeline in which most language understanding is performed
by a single large-language-model (LLM) call, complemented by one specialised local model:

- **Entity, category, sub-category, geolocation, sentiment, and EN/AR translation** are all
  obtained from one LLM analysis call. The model returns named entities (labelled
  PER/ORG/LOC/MISC) alongside the event's category and location, with code-fence stripping and
  robust JSON parsing applied to every response. An earlier design used a dedicated transformer
  NER model and a lexicon-based sentiment scorer for these steps; both were retired once the LLM
  call was found to produce entities and general sentiment of sufficient quality in a single
  pass, simplifying the pipeline.
- **Financial sentiment** is scored by a dedicated **FinBERT** model and retained as a numeric
  feature for forecasting — the one understanding component kept as a local specialised model,
  because a calibrated finance-domain sentiment score is more reliable than a general LLM
  judgement for this quantitative feature.
- **Importance scoring** (1–10) gates the pipeline: low-scoring articles are skipped before the
  expensive analysis stage and pruned after a grace period, conserving compute and LLM quota.

This division reflects a cost/quality trade-off: the LLM handles the open-ended, multilingual
understanding (entities, category, geolocation, translation) in one call, while FinBERT supplies
a deterministic, domain-calibrated sentiment feature for the forecasting layer.

---

## 6. Event Aggregation and Geolocation

Processed articles are aggregated into **events** by bucketing on `(city, country, category,
day)` and then semantically sub-clustering within each bucket using sentence-transformer
embeddings and a similarity threshold, so that distinct stories in the same place and category
on the same day are not conflated. An event is upserted on the key `(location_name, category,
day)` and carries aggregate sentiment, member article identifiers, and the timestamp of its
most recent article — the latter being the strict temporal cut used downstream to prevent
leakage. Geolocated events are the unit rendered on the map and consumed by the forecasting
feature builder.

---

## 7. Topic Discovery and Tagging

A topic layer gives events a higher-level, curated semantics. Candidate topics are sourced from
the Wikipedia *Current events* portal subpages over a lookback window, deduplicated, merged, and
enriched by an LLM. Events are tagged to topics by an LLM matcher (batched), and new topics are
periodically auto-discovered from the event stream. Topics carry a score and flags distinguishing
those currently in the news cycle from those shown in the UI; the highest-signal topic tags
(e.g. conflict, central-bank rates, inflation, energy cartels, bilateral-trade tensions) later
serve as the **most curated feature** for forecasting.

---

## 8. Event-to-Indicator Routing

Routing associates each event with a signed weight in [-1, 1] per indicator on the panel,
stored as `Event.affected_indicators`. **This is the thesis's most important conceptual
distinction: the routed weight is a feature (a hypothesis about influence), never the
supervised label.** Two interchangeable routers produce it:

- An **LLM router** (primary) batches events, prompts with the panel description and the event's
  text, category, and topic tags, and returns signed per-indicator weights; it caches by event
  and falls back to the rule router on any error.
- A **deterministic rule router** (fallback and reproducible backtest baseline) computes the
  weight as a product of sub-category affinity, symbol affinity, country risk, and an asymmetric
  sentiment term; it intersects its output with the live panel so it can never emit an
  off-panel symbol.

---

## 9. The Forecasting Model

### 9.1 Targets and labels

The prediction panel is database-driven (indicators flagged for forecasting; the seeded default
is Oil, Gold, Bitcoin, the S&P 500 proxy, and EUR/USD) over horizons of **1 and 5 trading
days**. The supervised label for `(symbol, t, h)` is the realized return `close@t →
close@t+h`, taken from daily OHLC price bars backfilled from public market data.

### 9.2 Features

The feature builder emits one row per `(symbol, date)` under a strict **as-of `t`** rule: no
datum dated after `t` may enter the row (events are cut on their latest-article timestamp, bars
on their date). Features span price dynamics (multi-horizon log returns, realized volatility,
momentum, RSI, volume z-score), routed-event aggregates (decayed signed-weight sums, touch
counts), event sentiment, category taxonomy one-hots, and high-signal topic-tag presence.

### 9.3 Model

Per horizon, two pooled gradient-boosted models are trained: a **classifier** producing a
probability of an upward move, isotonically **calibrated**, and a **regressor** producing the
predicted percentage change, from which a predicted price and a confidence band are derived for
the UI's forward projection. Artifacts are persisted per horizon and loaded lazily.

### 9.4 Honest framing

Because public news is lagged and largely priced-in, the forecasting layer is positioned as a
research-grade signal. Leakage — not model capacity — is identified as the principal threat, and
is mitigated by the as-of discipline and an automated self-check in the backtest.

---

## 10. LLM Orchestration

LLM access is mediated by a provider-agnostic layer exposing a uniform chat interface over
self-hosted (Ollama, three model tiers) and hosted OpenAI-compatible providers (OpenRouter,
Groq, Cerebras). Each *role* (e.g. analyzer, scoring, topics, routing, newsletter) maps to an
ordered fallback chain in configuration; unconfigured providers are skipped silently, and a
failure on one provider transparently advances to the next. Per-provider timeouts are tuned to
the provider's latency profile (notably a short, model-size-scaled timeout for the slow CPU-bound
local models so they fail fast as a last resort).

Because hosted free-tier model availability and rate limits fluctuate continuously, a daily
**discovery service** queries the provider's model catalogue, filters to free text models, probes
the top candidates with a minimal request — rejecting those that are rate-limited or that leak
reasoning tokens instead of answering — and caches the working set in Redis for the routing layer
to consume, with a static configured list as the fallback.

---

## 11. System Engineering and Deployment

The system is containerised with Docker Compose: an ASGI API server, separate light and heavy
queue workers, a cron scheduler, a built frontend served behind a reverse proxy, and the Redis
and MongoDB backing stores. Migrations are centralised; the application bootstraps without manual
configuration. Live updates (price ticks, aviation notices, earthquakes) reach the frontend over
a Server-Sent Events endpoint backed by Redis pub/sub. All user-facing strings are
internationalised (English and Arabic).

---

## 12. Evaluation

### 12.1 Methodology

Forecasting is evaluated by a **walk-forward (rolling-origin) backtest**: at each origin `t` the
model is retrained on data up to `t` and asked to predict `t+h`, never peeking past `t`. Four
**ablation arms** isolate the contribution of news events:

1. naïve persistence,
2. price-only,
3. price + rule-routed events,
4. price + LLM-routed events.

Reported metrics are directional accuracy, macro-F1, ROC-AUC, and the Brier score with a
reliability (calibration) curve. A built-in assertion verifies that every feature row's maximum
event/bar date does not exceed its as-of date, guaranteeing the absence of look-ahead leakage.

### 12.2 Results

On a real end-to-end run over roughly two years of daily bars across the indicator panel, with
the model trained on thousands of `(symbol, date)` samples per horizon, the system achieves
**directional accuracy ≈ 0.52** and **ROC-AUC ≈ 0.52–0.53**. These figures are close to the
naïve baseline, consistent with the near-random-walk behaviour of liquid markets. The honest
reading is that the event signal provides at most a small edge over price history alone; the
value of the contribution lies in the leakage-disciplined methodology and the reproducible
ablation framework, not in a large accuracy gain.

*(Tables of per-arm metrics and the reliability curve are to be inserted from the backtest's
JSON report for the final manuscript.)*

---

## 13. Discussion and Limitations

- **News is lagged and priced-in.** The predictive ceiling for public-news features on liquid
  instruments is low by construction; results should be read against the naïve baseline.
- **LLM non-determinism.** Routing and extraction via LLMs are non-deterministic; for the live
  system results are cached per event, and for the backtest the rule router provides a fully
  reproducible baseline arm.
- **Coverage and source bias.** Source selection, language coverage, and feed latency bias which
  events are seen at all.
- **Geolocation ambiguity.** Place disambiguation is imperfect; mis-geolocated events introduce
  noise into both the map and the location-based features.
- **Free-tier operational constraints.** Hosted LLM quotas cap sustained throughput; the
  orchestration and discovery layers manage but do not eliminate this constraint.

---

## 14. Conclusion and Future Work

This thesis demonstrated that a single real-time pipeline can extract geolocated news events and
reuse them as features for leakage-free short-horizon market forecasting, serving both a
situational-awareness product and a quantitative study. The empirical contribution is a careful,
baseline-anchored negative-to-marginal result that is honest about the weak predictive power of
public news.

Future work includes: incorporating higher-frequency and alternative data; extending horizons
and the indicator panel; replacing the pooled models with per-symbol or sequence models; adding
significance testing to the ablation comparison; and richer event modelling (causal chains,
entity-level resolution). The LLM orchestration layer also invites study as a contribution in its
own right, e.g. cost/quality-aware routing under fluctuating provider availability.

---

## References

*(To be completed in the final manuscript — NER, FinBERT, sentence-transformer, gradient-boosted
trees, calibration, geoparsing, and news-driven prediction literature.)*

---

## Appendix A — Indicator Panel

The default forecast panel comprises five liquid, macro-sensitive instruments: Crude Oil,
Gold, Bitcoin, an S&P 500 proxy, and EUR/USD. The panel is database-configurable; changing it
triggers a retrain because feature columns are one-hot encoded over the active panel.

## Appendix B — Reproduction Commands

```bash
# Forecasting layer
python manage.py backfill_prices --years 5      # seed daily OHLC bars
python manage.py route_events --router llm       # associate events with indicators
python manage.py train_forecast                  # fit calibrated clf + reg per horizon
python manage.py run_forecast                    # write current forecasts
python manage.py evaluate_forecast               # walk-forward backtest → JSON report
python manage.py forecast_e2e --years 3 --backtest

# Dependency-light self-tests (leakage / fallback / train-predict roundtrip)
DJANGO_SETTINGS_MODULE=settings.base python -m services.forecasting.tests_forecast
```
