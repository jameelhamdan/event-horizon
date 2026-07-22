---
name: pipeline-eval-live
description: Evaluate NLP pipeline accuracy (category, sub_category, geo) across years and sources with NO Mongo/Redis/Docker required — samples one real article per (source, month) from a year range via the real backfill strategy dispatch (Wayback-frontpage / RSS-sitemap), fetching real articles over live HTTP, then classifies them with the on-prem annotator, and you (Claude) judge it against a 90% accuracy target. Use when asked to re-run the pipeline accuracy check, validate a change to annotator.py/geocode.py/taxonomy.py/bodies.py without a running Mongo, or get a quick accuracy read outside Docker.
---

# Live-fetch pipeline evaluation

Samples one real historical article per (RSS source, month) across a year
range and runs it through the real production path, unsaved: `Source` rows
are built in memory from `core/fixtures/initial_rss_sources.json` +
`additional_sources.json` instead of a Mongo query (the only DB-free part —
everything else is real), then `HistoricalBackfillService`'s strategy
dispatch (Wayback-frontpage for recency-only-sitemap publishers, RSS-sitemap
otherwise — same selection `_build_strategy` makes in production) →
`_hydrate_bodies` (real live HTTP fetch of the actual article page + Wayback
fallback, `services/data/bodies.py` extraction) → `NLPAnnotator` (on-prem) →
`LLMRefiner` (on-prem `zeroshot` by default, matching `settings.REFINE_PROVIDER`)
for any cell `annotate` flagged low-confidence (`stage: refine`). Nothing is
written to any database.

**Always let refine run** (the default) — most historical/backfill volume
lands below `annotator.ESCALATE_BELOW` on the first pass, so `annotate`'s raw
output is a draft, not what `Article.stage` ends up as in production. Pass
`--no-refine` only when you specifically want to isolate `annotate`'s
first-pass accuracy (e.g. debugging prototype embeddings in isolation).

## 1. Generate the sample

From `api/` (needs project deps — `uv run` installs/updates them
automatically from `pyproject.toml` on first invocation):

```bash
# Full grid: every enabled RSS source x every month, 2020-2026 (the "official"
# validation run — thousands of cells, one or more live HTTP fetches each; can
# take hours. Run it in the background and let it finish rather than blocking on it)
uv run python -m scripts.eval_pipeline_live --start-year 2020 --end-year 2026

# Local/fast check: capped, shuffled sample across the whole grid — 50 cells
# is the default fast local sanity check (not just the first N months of the
# first few sources — cells are shuffled first, so a small --limit still spans
# many sources)
uv run python -m scripts.eval_pipeline_live --start-year 2020 --end-year 2026 --seed 42 --limit 50

# Quick check: fewer months per year, or specific sources
uv run python -m scripts.eval_pipeline_live --start-year 2022 --end-year 2024 --months 3
uv run python -m scripts.eval_pipeline_live --source bbc-world --source brookings --start-year 2020 --end-year 2026

# A/B a different refine judge against the same seeded sample (Phase 3 of the
# accuracy-improvement plan — zeroshot is the settings.REFINE_PROVIDER default)
uv run python -m scripts.eval_pipeline_live --seed 42 --limit 50 --refine-provider ollama
uv run python -m scripts.eval_pipeline_live --seed 42 --limit 50 --refine-provider cloud
```

### Faster: sample already-ingested articles from production (`--from-prod-api`)

Instead of re-discovering + re-fetching articles from sources (slow, flaky —
Wayback frontpage mining, sitemap crawls, Wikipedia 429s), pull real
already-ingested articles straight from the deployed staff-only endpoint
(`GET /api/internal/articles/historical/`, `api/views/articles.py`). No
discovery, no rate limits — the fast path for a quick accuracy read.

Auth is HTTP Basic with the admin credentials in the **gitignored
`.env.claude`** at the repo root (`ARTICLE_API_ADMIN_EMAIL` /
`ARTICLE_API_ADMIN_PASSWORD`). Export them first; a `403` means the account
isn't staff (`IsAdminUser`).

```bash
# From repo root: load the creds, then run from api/
set -a && . ./.env.claude && set +a
cd api

# Classify prod's STORED content (zero fetching) and show each row's fresh
# label next to prod's stored label (a "(prod: ...)" suffix flags drift)
uv run python -m scripts.eval_pipeline_live --from-prod-api --year 2025 --month 7 --limit 30

# A/B an extraction change (e.g. bodies.py): --rehydrate re-fetches each URL
# through the CURRENT bodies.py extractor + Wayback fallback instead of reusing
# prod's stored content — diff this run's labels against the non-rehydrate run
# above to see what the extractor change fixes.
uv run python -m scripts.eval_pipeline_live --from-prod-api --year 2025 --month 7 --limit 30 --rehydrate

# Narrow to a day and/or specific sources
uv run python -m scripts.eval_pipeline_live --from-prod-api --year 2025 --month 7 --day 28 --source npr-world --source brookings
```

Writes `results/eval_pipeline_live/pipeline_eval_<timestamp>.json` — one row
per (source, month) with `year`/`month`/`url`/`via` (`rss-sitemap` |
`wayback` | `wikipedia`), the extracted `title`/`content_lead`/`body_chars`,
then the classification fields: `category`, `sub_category`, `country`,
`city`, `located`, `stage` (`annotated` | `refine` | `refined` | `unhydrated`),
`confidence`, `intensity`, `summary`, `refined_by` (the judge provider that
last touched the row, or `null` if it never needed refining / the judge
failed).

Only RSS-typed, enabled sources from the fixtures are sampled (the synthetic
`wikipedia-current-events` source isn't in the fixtures, so it's out of scope
here). A source picked up by `services/data/wayback.py`'s `FRONTPAGES`
registry uses the Wayback-frontpage strategy (`via: wayback`); everything
else uses sitemap discovery (`via: rss-sitemap`) — most sources are
recency-only there, so older months for most sources will come up empty
(`cells_empty` in the report header) — that's expected, not a bug.

## 2. Judge every row

For each row with a body (`body_chars > 0`), read `title`/`content_lead`,
decide independently what's correct (taxonomy in
`api/services/processing/taxonomy.py`; conflict = deliberate armed action,
disaster = accidental/natural, ordinary crime = general), then compare
against the pipeline's `category`/`sub_category`/geo. Also flag the
boilerplate check — does `content_lead` read as real article prose, or
nav/menu/"related articles"/cookie-banner chrome? That's a `bodies.py`
extraction regression, not a classification miss, and should be called out
separately. Rows with `body_chars == 0` (`stage: unhydrated`) are fetch
failures — a coverage gap, not an accuracy miss; don't count them in the
denominator.

## 3. Report a scorecard

1. **Coverage**: `sampled`/`cells_total` and `hydrated`/`sampled` from the
   report header.
2. **Per-field accuracy** over hydrated rows: category/sub_category correct,
   geo correct, boilerplate-clean fraction. Target **≥90%** on
   category/sub_category and geo (baseline: 100 Oct-2023 events manually
   reviewed — 55% correct / 18% partial / 27% wrong on category+sub; ~9% geo
   error rate).
3. A table of every miss: `year-month source` → title → pipeline's label vs
   your label, one-line diagnosis (extraction contamination vs
   classification/prototype miss vs geocoding miss).
4. **Concrete fixes**, mapped to where each lives:
   - category/sub_category prototype or threshold miss →
     `services/processing/taxonomy.py`
   - boilerplate leaking into `content_lead` → `services/data/bodies.py`
     (`_CHROME_TAG_RE`/`_BOILERPLATE_CLASS_RE`/`_is_boilerplate_paragraph`)
   - a real (city, country) mismatch → `services/processing/geocode.py`'s
     `city_country_conflict`
   - NER/gazetteer miss on the located place → `services/processing/geocode.py`

Below 90% on any field: don't guess — point at the specific miss rows and the
file/function above, and only change what the misses actually implicate, not
the whole taxonomy.

## Re-running after a change

Meant to be re-run, not one-shot: after any change to `bodies.py`,
`annotator.py`, `geocode.py`, or `taxonomy.py`, re-run the full grid (or at
least `--months 3` across a few years) and diff the per-field accuracy
against the last run's report in `results/eval_pipeline_live/` to confirm no
regression.
