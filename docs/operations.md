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
- `backfill_history_task` — dispatches the article backfill (see the fan-out table
  below) for every enabled RSS source over `BOOTSTRAP_ARTICLE_YEARS` (default 1y)
- `train_forecast_model_task` + `run_forecast_task`

To re-run it manually: the admin dashboard **Re-run bootstrap** button (forces past the
guard), or `enqueue(bootstrap_initial_data_task, True)`.

## The stage-registry pipeline (tick → fan-out worker)

The pipeline is **not** a set of per-step dispatcher/worker task pairs — it's a
single registry (`services/stages.py`) executed by exactly two Celery tasks (see
[pipeline.md](pipeline.md) for the full stage list and cadences):

| Task | Role |
|------|------|
| `pipeline_tick_task` (cron, every 10m) | Dispatches every enabled stage that is due (past its own `every_minutes`) and has pending work — runs on `default` |
| `run_stage_chunk_task(stage_name, ids)` | The only fan-out worker — executes one stage's handler over one chunk of ids — runs on the stage's own queue (`default`/`heavy`) |
| `dispatch_stage_task(stage_name)` | Force-dispatches one stage, skipping the cadence gate (admin buttons, manual repair) |
| `backfill_history_task` → `backfill_day_chunk_task(day, source_codes)` | Historical backfill dispatcher (separate from the live-pipeline `fetch` stage) — one day × `BACKFILL_CHUNK_SIZE` sources, fetches+saves+NLP-processes inline; see `services/data/historical.py` |

Each stage in the registry declares its own `chunk_size`/`limit`/`queue`/
`every_minutes` — to change a stage's throughput, edit its entry in
`services/stages.py`, not env vars. Scale worker capacity by adding
`worker-heavy` replicas in `docker-compose.yml` (no code change).

**Coordination.** Downstream stages stay on their own cadence and operate on
whatever is ready (eventually-consistent; idempotent upserts mean nothing is
lost). The admin **"Run full pipeline"** button calls `pipeline_tick_task(force=True)`
— dispatches every enabled stage with pending work, skipping cadence gates.

**Robustness.** `run_stage_chunk_task` is a Celery task declared with
`autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={'max_retries': 3}`.
Saves are idempotent (`get_or_create` for articles, date-skip for prices), so a
retried or resumed run fills only gaps. The `process` stage claims ids at
dispatch time (`process_queued_at`, TTL `PROCESS_CLAIM_TTL_HOURS`) so a
backlogged heavy queue doesn't get the same articles re-dispatched every tick;
a mid-loop enqueue failure releases the claim on ids that never actually made
it onto the queue.

**Manual/CLI parity.** `manage.py fetch_data` and `manage.py process_articles`
select work through the exact same predicates as the dispatcher
(`services/stages.py::select_ids` / `fetch_source`) — a manual run can never
select a different record set than the pipeline would.

## Per-record stage tracking

Each stage handler records its outcome on the record's `stage_status` JSON
(`{stage: {ok, at, error}}`) via `services/utils.py::mark_stage`. Stages tracked
this way:

- **Article**: `process`, `geocode`
- **Event**: `route` (routing failures/no-indicator outcomes)

`pipeline_coverage()` (`services/workflow/events.py`) returns, per stage, the
count of records still pending there — built directly from the same
`pending_count` callable each stage declares in the registry, so the displayed
count, the Reprocess button's effect, and what the tick actually dispatches
cannot drift apart — plus a sample error, the data behind the dashboard's
coverage panel.

## Admin operations dashboard

`/admin/dashboard/` (server-rendered). Sections:

- **Health** — the last `pipeline_health_task` report (persisted to Redis every
  30m): per-stream freshness (articles, prices, earthquakes), a zero-current-topics
  check (the single-source Wikipedia scraper risk), and per-stage staleness
  (pending work piling up while the tick hasn't dispatched that stage in 3× its
  cadence — a stuck tick/queue signal, not just "slow"). Flags if the report
  itself is stale (health task not running).
- **Pipeline coverage** — per-stage "N need reprocessing" + last dispatch time +
  last error, each with a **Reprocess** button that force-dispatches just that
  stage.
- **Upcoming runs** — next scheduled time per task (from `api/crontab`).
- **Task queues** — per-queue workers, broker depth (unclaimed Redis-list
  length — persistent disagreement with the Queued count means lost/untracked
  messages), queued/running/failed-24h counts, linking into the task browser.
- **LLM providers** — per-provider ok/err/avg-ms + active debounce cooldowns.
- **Forecast model** — artifact mtimes, last forecast, live directional accuracy.
- **Actions** — run full sync, backfill prices/articles (incl. "until date"),
  retrain forecast, re-run bootstrap, and **cancel a job** by Celery task id
  (`app.control.revoke`).

Per-record reprocessing is also available from the `Article`/`Event` changelists: the
**"pipeline gap"** filter narrows to records stuck at a stage, and bulk actions
re-enqueue them (`Reprocess selected`, `Re-tag selected`, `Re-route selected`).

## Task browser (RQ-admin / Flower equivalent)

`/admin/core/taskrun/` — every enqueued task (queued, running, succeeded, failed,
cancelled), with args/kwargs (`params`), return value (`result`), retry count,
and error/traceback. Filter by status/queue/task name; the **"Cancel selected"**
admin action revokes queued or running tasks. `TaskRun` rows are created by
`enqueue()` at dispatch time and updated by Celery's `task_prerun`/`task_success`/
`task_failure`/`task_retry`/`task_revoked` signal handlers in `services/queue.py`
as the task moves through its lifecycle.
