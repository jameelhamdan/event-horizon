# Event Horizon — Documentation

A real-time global event-intelligence platform that ingests news + market/geophysical
streams, reconstructs them into **events** and **topics**, and predicts the
**direction & magnitude of economic indicators from news data**.

This `docs/` set explains the system end to end. For day-to-day coding conventions,
recipes, and the file map, see [`../CLAUDE.md`](../CLAUDE.md) — this folder is the
*conceptual* companion to that *operational* guide.

## Contents

| Doc | What it covers |
|-----|----------------|
| [architecture.md](architecture.md) | Stack, Docker services, queues, data flow, storage |
| [pipeline.md](pipeline.md) | Every phase of the system, in order, with inputs/outputs |
| [forecasting.md](forecasting.md) | The news → economic-indicator prediction subsystem (the redesign) |
| [data-model.md](data-model.md) | Core MongoDB collections and their key fields |
| [symbols.md](symbols.md) | The `MarketSymbol` config model — curating fetched/forecast/UI symbols |
| [operations.md](operations.md) | Admin dashboard, fan-out pipeline, configurationless bootstrap |

## The system in one picture

```mermaid
flowchart TD
    SRC([RSS / web sources]) --> S1

    subgraph S1["Stage 1 · Fetch"]
        direction LR
        LIVE[live hourly] & BACK[historical backfill]
    end
    S1 --> ART[(Article)]

    ART --> S2
    subgraph S2["Stage 2 · Process"]
        NLP[LLM category/sub-category/geo/intensity · local NER + VADER<br/>FinBERT · local MarianMT i18n en→ar]
    end
    S2 --> ARTE[(Article · enriched)]

    ARTE --> S2B
    subgraph S2B["Stage 2b · Aggregate"]
        AGG[bucket city/country/category/day<br/>semantic sub-cluster · topic tag<br/>route_event_to_weighted_symbols]
    end
    S2B --> EVT[(Event<br/>+ affected_indicators)]

    EVT --> S3
    BARS[(PriceBar)] --> S3
    subgraph S3["Stage 3 · Predict (AI)"]
        FEAT[as-of features] --> V2[LightGBM clf + reg<br/>walk-forward validated]
    end
    S3 --> FC[(Forecast<br/>1d · 5d)]

    FC --> S3C
    subgraph S3C["Stage 3c · Score"]
        SCORE[realized close after horizon →<br/>realized_direction · is_correct]
    end
    S3C --> MET([metrics vs naive baselines])

    EVT -.daily.-> NL([Newsletter])
```

## Quick start

```bash
cp api/.env.example .env.app      # fill in SECRET_KEY etc.; LLM works out of the box
docker compose up                 # everything
cd api && python manage.py migrate
python manage.py run_task pipeline_tick_task --sync   # run one pipeline tick in-process
```

> **LLM**: free-tier cloud providers (`GROQ_API_KEYS`, `CEREBRAS_API_KEYS`) lead every route, with OpenRouter (`OPENROUTER_API_KEYS`) as the mid fallback and self-hosted Ollama (no API key; set `OLLAMA_BASE_URL` if it runs elsewhere, default `http://localhost:11434`) as the last resort. The LLM's job is narrower than it looks — entities, sentiment, Arabic translation, topic tagging, and event routing all run on local models instead. See [architecture.md → LLM providers & routing](architecture.md#llm-providers--routing).

See [`../CLAUDE.md` → Dev Commands](../CLAUDE.md) for the full command list.
</content>
</invoke>
