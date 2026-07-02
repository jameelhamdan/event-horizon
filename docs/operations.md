# Operations — dashboard, fan-out pipeline, bootstrap

## Configurationless deployment

`docker compose up` self-seeds and self-backfills — there is **no manual
post-deploy step** (the old `bootstrap_static_points` command is no longer required;
static points are seeded by migration `0001`, symbols by `0006`).

On `api` container start, `start_api.sh` launches **supercronic** with `api/crontab`,
which dispatches jobs via `manage.py run_task`. `bootstrap_initial_data_task` is
**idempotent** (guarded by a cache flag and a
`PriceBar`-presence heuristic) and, on a fresh deployment, enqueues:

- `backfill_prices_task` — daily OHLC for every active symbol
- `backfill_all_sources_task` — top-10/week articles for every enabled RSS source over
  `BOOTSTRAP_ARTICLE_YEARS` (default 1y)
- `train_forecast_model_task` + `run_forecast_task`

To re-run it manually: the admin dashboard **Re-run bootstrap** button (forces past the
guard), or `enqueue(bootstrap_initial_data_task, True)`.

## The fan-out pipeline (dispatcher → per-record worker)

Heavy pipeline steps are split into a light **dispatcher** (selects pending records,
enqueues one worker job per record/chunk, runs on the `default` queue) and idempotent
**per-record workers** (run on the `heavy` queue). This spreads work across all
`worker-heavy` replicas instead of one job hogging a worker.

| Dispatcher (light) | Worker (heavy) |
|--------------------|----------------|
| `dispatch_fetch_task` | `fetch_source_task(source_code, start_date)` |
| `dispatch_process_articles_task` | `process_article_task(id)` / `process_articles_chunk_task(ids)` |
| `dispatch_tag_topics_task` | `tag_events_chunk_task(event_ids)` (chunks of 10) |
| `dispatch_route_events_task` | `route_events_chunk_task(event_ids)` (chunks of 10) |

Scale throughput by adding `worker-heavy` replicas in `docker-compose.yml` (no code
change). Tuning env vars: `PROCESS_CHUNK_SIZE`, `PROCESS_DISPATCH_LIMIT`,
`TAG_DISPATCH_LIMIT`, `ROUTE_DISPATCH_LIMIT`, `STUCK_RECOVERY_INTERVAL_MINUTES`.

**Coordination.** Downstream steps stay on their own schedule and operate on whatever
is ready (eventually-consistent; idempotent upserts mean nothing is lost). The admin **"Run full pipeline"** button enqueues `dispatch_fetch_task`,
`dispatch_process_articles_task`, and `aggregate_events_task` independently — eventual
consistency; results appear as each stage completes.

**Robustness.** Network/LLM workers are Celery tasks declared with
`autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 3}`.
Saves are idempotent (`get_or_create` for articles, date-skip for prices), so a
retried or resumed run fills only gaps. A low-frequency safety net re-dispatches
processed-but-unlocated articles.

## Per-record stage tracking

Each worker records its outcome on the record's `stage_status` JSON
(`{stage: {ok, at, error}}`) via `services/stages.py::mark_stage`. Stages:

- **Article**: `process`, `geocode` (the known g4f-outage gap)
- **Event**: `tag`, `route`

`Workflow.pipeline_coverage()` returns, per stage, the count of records stuck there
plus a sample error — the data behind the dashboard's coverage panel.

## Admin operations dashboard

`/admin/dashboard/` (server-rendered). Sections:

- **Throughput** — per-task item/run counts for today/yesterday from `TaskRun`, last
  success time, last error. Every run (scheduled, dispatched, or per-record) is recorded
  by the `_execute_tracked` wrapper in `services/queue.py`.
- **Pipeline coverage** — per-stage "N need reprocessing" + last error, each with a
  **Reprocess** button that re-dispatches only the stuck records.
- **Upcoming runs** — next scheduled time per task (from `api/crontab`).
- **In-flight** — currently `running` `TaskRun`s.
- **Forecast model** — artifact mtimes, last forecast, live directional accuracy.
- **Actions** — run full sync, backfill prices/articles, retrain forecast, re-run
  bootstrap, and **cancel a job** by Celery task id (`app.control.revoke`).

Per-record reprocessing is also available from the `Article`/`Event` changelists: the
**"pipeline gap"** filter narrows to records stuck at a stage, and bulk actions
re-enqueue them (`Reprocess selected`, `Re-tag selected`, `Re-route selected`).
