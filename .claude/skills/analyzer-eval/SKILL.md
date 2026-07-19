---
name: analyzer-eval
description: Classify live articles from the project's RSS sources with the two-stage analyzer (on-prem annotate pass, optional refine judge), then act as the judge — score each classification, report accuracy per field, and propose concrete prototype/rule/alias fixes. Use when asked to evaluate, sanity-check, or tune the article analyzer.
---

# Analyzer evaluation

Run the analyzer over real articles, then evaluate its output yourself (you are the judge — read each headline and decide whether the analyzer's labels are right).

## 1. Generate the sample

From `api/` (needs Mongo running for the Source list):

```bash
python manage.py eval_analyzer --limit 30            # annotate pass only (on-prem NLP)
python manage.py eval_analyzer --limit 30 --refine   # + judge low-confidence rows (REFINE_PROVIDER)
python manage.py eval_analyzer --limit 30 --from-db  # offline: recent stored articles
REFINE_PROVIDER=cloud python manage.py eval_analyzer --limit 30 --refine  # compare judges
```

This writes `results/eval_analyzer/analyzer_eval_<timestamp>.json` — one row per article with `category`, `sub_category`, `country`, `city`, `located`, `intensity`, `summary`, plus `stage` ('annotated' = confident, 'refine' = flagged), the prototype `confidence`, and `refined_by` when a judge overrode the row.

First runs download the NER/zero-shot models; if that is unwanted, disable them for the run (`NER_ENABLED=false ZEROSHOT_ENABLED=false`) — geo then falls back to the regex country scan and zeroshot verdicts come back empty.

## 2. Judge every row

Read the report JSON. For each article, from its title/lead, decide independently what the correct labels are, then compare:

- **category/sub_category** — is the taxonomy pair right? (Taxonomy definition: `api/services/processing/taxonomy.py`; classification rule of thumb: deliberate armed action → conflict, accidental/natural → disaster, ordinary crime → general.)
- **geo** — is the country (and city, when present) the place the story is *about*, not merely a place mentioned? Is `located` false for an article that clearly names a place (a geocoding miss)?
- **intensity** — plausible under the rubric? (0.0–0.2 routine, 0.3–0.5 notable, 0.6–0.8 major, 0.9–1.0 severe/historic.)
- **summary** — for the local backend it is extractive (leading sentences); flag only if empty or nonsensical.

## 3. Report a scorecard

Present:

1. Per-field accuracy over the sample (category, sub_category, country, city), the located fraction, and an intensity plausible-rate.
2. A table of every miss: title → analyzer's label vs your label, one-line diagnosis.
3. **Concrete fixes**, mapped to where each lives:
   - misclassifications → prototype wording in `api/services/processing/taxonomy.py` (`PROTOTYPES`), the escalation threshold `ESCALATE_BELOW` in `annotator.py`, or the judge labels/conflict gate in `refiner.py`
   - geocoding misses → aliases in `api/services/processing/geocode.py` (`_COUNTRY_ALIASES`, `_EXTRA_PLACES`, `_CITY_ALIASES`)
   - intensity misrates → priors in `taxonomy.py` (`PRIORS`) or lexical cues in `annotator.py` (`rate_intensity`)

Only apply fixes when asked; the default deliverable is the scorecard. When comparing judges, run --refine under different REFINE_PROVIDER values and diff per-field agreement between the reports; also report the flagged/refined counts (escalation rate).
