# Article pipeline state & annotation map

Where an article is in the annotation pipeline, and which field is written by
what. This is the *article-level* companion to [pipeline.md](pipeline.md) (which
covers the stage machinery) and [data-model.md](data-model.md) (field schema).

## State is stored: `Article.stage`

`Article.stage` (indexed CharField) is the single pipeline-position field. The
stage predicates in `api/services/stages.py` are pure equality filters on it,
and the stage handlers are the only writers — so the census on the dashboard,
the pending counts, and what the dispatcher selects all read the same field.

| `stage` | Meaning | Next stage |
|---|---|---|
| `fetched` | ingested, awaiting analysis | `analyze` (if fresh + live) or `annotate` (backfill, or aged out) |
| `refine` | NLP-annotated (historical only), classification confidence < `ESCALATE_BELOW` | `refine` (the judge) |
| `annotated` | analyzed, confident — by `analyze` (live, cloud LLM) or `annotate` (on-prem NLP) | terminal — `aggregate` → `tag` → `route` (Event stages) if a location resolved |
| `refined` | re-judged by the refine stage (`refined_on`/`refined_by` stamped) | terminal — same as `annotated` |

`fetched` fans out to exactly one of `analyze`/`annotate`, never both: an
article is "live" (→ `analyze`) only if it's not backfill-tagged AND was
fetched within `LIVE_ANALYZE_FRESHNESS_HOURS` (`services/stages.py`, default
6h); everything else — explicit historical backfill, or a live article
`analyze` didn't reach before aging out of that window — goes to `annotate`
instead. `annotated` doesn't distinguish which stage produced it; check
`extra_data.llm.annotator` (`'nlp'`) vs. `llm_usage.provider` (a real LLM
provider name) if you need to know.

`annotation_deferred=True` (fetch-only backfill) parks an article off the live
pipeline regardless of stage — `Article.pipeline_state` surfaces it as
`deferred`; `reprocess_corpus_task` (scope=`deferred`) picks those up on demand.

**Location is an attribute, not a pipeline step.** A terminal article either
has a `location` (→ aggregates into events) or doesn't (terminal, but kept —
it's still a valid training sample). There is no geocode stage: geocoding is a
local `geonamescache` lookup done inline in `analyze`/`annotate`
(`services/processing/geocode.py`). A *failed* analysis/annotation leaves
`stage='fetched'` (and `processed_on` NULL) so the owning stage retries it —
or, for `analyze`, so the article falls through to `annotate` once it ages
out — instead of a degraded result masquerading as done; a failed refine
verdict leaves `stage='refine'` for the same reason.

`stage_status` (JSON) is the authoritative record of *why* a stage produced what
it did (per-stage `{ok, at, error}` under keys `analyze`/`annotate`/`refine`);
the `stage` field is authoritative for *what runs next*. Don't infer "next
stage" from `stage_status`.

## Field → stage → producer → LLM or local

`annotate` (historical/backfill) is entirely on-prem (pretrained models +
rules — no LLM, no network). `analyze` (live traffic) is a cloud LLM by
design — that's the whole point of splitting it out. `refine` is
provider-dependent, and only ever sees `annotate`'s low-confidence output
(never anything `analyze` touched).

| Field(s) | Stage | Producer | LLM? |
|---|---|---|---|
| `title, content, source_*, published_on, banner_image_url` | fetch | RSS / feedparser | — |
| `category, sub_category, geo, intensity` | analyze | `analyzer.py` via `LLM_ROUTES['analyzer_lite']` | **LLM** |
| `translations.en` (title/abstractive summary) | analyze | same cloud call | **LLM** |
| `category, sub_category` | annotate | prototype embeddings (`annotator.py` + `taxonomy.py`) | local |
| `location, latitude, longitude` | analyze / annotate | analyze: LLM-named, geocoded via `geocode.py`; annotate: NER (`wikineural`) → gazetteer (`geocode.py`) | analyze: LLM-named / annotate: local |
| `event_intensity` | annotate | priors + lexical cues (`rate_intensity`) | local |
| `importance_score, importance_source` | analyze / annotate | intensity→1–10 base + weight/corroboration/floors (`services/scoring/`) — same post-processing either way | local |
| `sentiment` | analyze / annotate | VADER (`services/processing/vader.py`) — same for both | local |
| `finbert_sentiment` | analyze / annotate | FinBERT (`services/processing/finbert.py`) — same for both | local |
| `translations.en` (title/summary) | annotate | extractive (leading sentences) | local |
| `translations.ar` | analyze / annotate | MarianMT (`services/translation/`) — same for both | local |
| `category, sub_category, geo, intensity` (re-judged) · `translations.en.summary` (cloud only) · `refined_by` | refine | `refiner.py` — zeroshot NLI / Ollama JSON-schema / cloud LLM | provider-dependent |
| `title_embedding` | aggregate | sentence-transformers | local |
| `entities` | — | **unused** (retained for schema stability; not populated) | — |

Event-level fields (`topics`, `affected_indicators`, aggregated metrics) are set
by the `tag` / `route` / `aggregate` stages — see [pipeline.md](pipeline.md).

## Training vs display annotation

The forecast training corpus needs only the **structured** fields
(`category, sub_category, event_intensity, location/lat/lon, sentiment,
finbert_sentiment`) — the vast majority of the corpus is historical/backfill,
produced on-prem by `annotate` at $0. Summaries and Arabic translations are
**UI-only**; lite (backfill) articles skip Arabic, `annotate`'s summaries are
extractive, and abstractive summaries exist only from `analyze` (live) or the
cloud refine provider (historical, rare).
