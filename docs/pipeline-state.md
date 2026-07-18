# Article pipeline state & annotation map

Where an article is in the annotation pipeline, and which field is written by
what. This is the *article-level* companion to [pipeline.md](pipeline.md) (which
covers the stage machinery) and [data-model.md](data-model.md) (field schema).

## State is derived, not stored

An `Article` has **no status column**. Its state is computed from the same
signals the stage selectors in `api/services/stages.py` filter on, exposed as the
read-only `Article.pipeline_state` property (`core/models.py`). It is *derived on
read* on purpose — a stored status would drift from the null-ness signals the
dispatcher actually selects on.

| `pipeline_state` | Predicate (what makes an article "on" this step) | Next stage |
|---|---|---|
| `deferred` | `annotation_deferred=True` | `annotate_deferred_articles_task` (not the live pipeline) |
| `fetched` | `importance_score IS NULL` | `score` |
| `scored` | `importance_score` set · `processed_on IS NULL` *(incl. parked below `ARTICLE_MIN_IMPORTANCE_TO_PROCESS`)* | `process` |
| `processed` | `processed_on` set | terminal — `aggregate` → `tag` → `route` (Event stages) if a location resolved |

**Location is an attribute, not a pipeline step.** A `processed` article either
has a `location` (→ aggregates into events) or doesn't (terminal, but kept — it's
still a valid training sample). There is no geocode stage and no `geo_failed`
flag: geocoding is a local `geonamescache` lookup done inline in `process`
(`analyzer._geocode`). A *failed* analysis (LLM error) leaves `processed_on` NULL
so the `process` stage retries it, instead of a degraded result masquerading as
done.

`stage_status` (JSON) is the authoritative record of *why* a stage produced what
it did (per-stage `{ok, at, error}`); the null-ness signals above are
authoritative for *what runs next*. Don't infer "next stage" from `stage_status`.

## Field → stage → producer → LLM or local

| Field(s) | Stage | Producer | LLM? |
|---|---|---|---|
| `title, content, source_*, published_on, banner_image_url` | fetch | RSS / feedparser | — |
| `importance_score, importance_source` | score | `services/scoring/` — LLM 1–10 rating + local post-proc (source weight, corroboration, category floor) | **LLM** |
| `category, sub_category` | process | `services/processing/analyzer.py` | **LLM** |
| `location`, `event_intensity` | process | `analyzer.py` (country/city + severity) | **LLM** |
| `latitude, longitude` | process | `analyzer.py` → `_geocode()` — local `geonamescache` lookup of the LLM's country/city | local |
| `sentiment` | process | VADER (`services/processing/vader.py`) | local |
| `finbert_sentiment` | process | FinBERT (`services/processing/finbert.py`) | local |
| `translations.en` (title/summary) | process | `analyzer.py` | **LLM** |
| `translations.ar` | process | MarianMT (`services/translation/`) | local |
| `title_embedding` | aggregate | sentence-transformers | local |
| `entities` | — | **unused** (retained for schema stability; not populated) | — |

Event-level fields (`topics`, `affected_indicators`, aggregated metrics) are set
by the `tag` / `route` / `aggregate` stages — see [pipeline.md](pipeline.md).

## Training vs display annotation

The forecast training corpus needs only the **structured** fields
(`category, sub_category, event_intensity, location/lat/lon, sentiment,
finbert_sentiment`). Summaries and Arabic translations are **UI-only** and are
not required to annotate an article for training — so a bulk training-annotation
pass can skip the generative (LLM summary) step entirely.
