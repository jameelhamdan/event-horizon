"""Pipeline stage registry — the single definition of the article→event pipeline.

Every pull-based pipeline step (fetch, score, process, aggregate, tag, route)
is declared here as a Stage: how to find pending work
(``pending_ids``/``pending_count``), how to do it (``handler``), and how it is
scheduled and chunked. Exactly two Celery tasks execute all of them —
``services.tasks.pipeline_tick_task`` (cron, every 10 min: dispatches every
stage that is due and has work) and ``services.tasks.run_stage_chunk_task``
(the only fan-out worker task).

Because the dashboard's coverage table, the admin "Reprocess" buttons, and the
dispatcher all read the same ``pending_*`` callables, the count shown, the
button's effect, and what the cron actually dispatches can never drift apart.

Time-of-day jobs (topics refresh/discovery, newsletter, forecast training,
maintenance cleanups) are NOT stages — they are genuinely scheduled work and
stay as standalone crontab tasks.

Ordering in REGISTRY is pipeline order (upstream first). A downstream stage
picks up upstream output on the next tick (≤10 min later) — stage sequencing is
eventually-consistent by design; there are no cron-offset dependencies.
"""

import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Callable

logger = logging.getLogger(__name__)

# Window an event stays eligible for tag/route repair — matches the aggregate
# stage's own look-back so events can't age out before being tagged.
EVENT_STAGE_WINDOW_HOURS = 168
# Claim lease for the process stage — an article claimed by a dispatch is not
# re-dispatched until this expires (protects against a backlogged heavy queue).
PROCESS_CLAIM_TTL_HOURS = 6


def _now() -> datetime:
    return datetime.now(dt_timezone.utc)


# ── Per-stage selection / handling ──────────────────────────────────────────
# All model imports are function-local: this module must stay importable
# before django.setup() finishes (tests, tooling).

def _fetch_pending():
    from core import models as m
    return m.Source.objects.filter(is_enabled=True)


def _fetch_ids(limit: int) -> list:
    return list(_fetch_pending().values_list('code', flat=True)[:limit])


def _fetch_handler(codes: list) -> int:
    from services.workflow.articles import fetch_source
    total = 0
    for code in codes:
        try:
            total += fetch_source(code)
        except Exception:
            logger.exception('[stage:fetch] %s failed', code)
    return total


def _score_pending():
    from core import models as m
    # exclude(annotation_deferred=True): fetch-only backfill articles
    # (BACKFILL_LLM_ENABLED=False) wait for annotate_deferred_articles_task, not
    # the live pipeline. exclude() matches False-or-unset, so pre-migration rows
    # (which have no such field) are still included.
    return (
        m.Article.objects.filter(processed_on__isnull=True, importance_score__isnull=True)
        .exclude(annotation_deferred=True)
    )


def _score_ids(limit: int) -> list:
    return list(_score_pending().order_by('-created_on').values_list('id', flat=True)[:limit])


def _score_handler(ids: list) -> int:
    from services.scoring import score_unscored_articles
    return score_unscored_articles(article_ids=ids)


def _live_llm_enabled() -> bool:
    """Dashboard-editable master switch for the live pipeline's LLM stages
    (score/process). Off ⇒ those stages don't dispatch; articles keep being
    fetched and accumulate as pending until it's turned back on."""
    from services.runtime_config import is_live_llm_enabled
    return is_live_llm_enabled()


def _score_enabled() -> bool:
    from django.conf import settings
    return bool(settings.ARTICLE_IMPORTANCE_SCORING_ENABLED) and _live_llm_enabled()


def _process_pending():
    from django.conf import settings
    from django.db.models import Q
    from core import models as m
    from services.workflow.articles import _apply_min_score_filter
    claim_cutoff = _now() - timedelta(hours=PROCESS_CLAIM_TTL_HOURS)
    qs = m.Article.objects.filter(processed_on__isnull=True)
    # Skip articles whose earlier dispatch is still (presumably) in flight.
    qs = qs.filter(Q(process_queued_at__isnull=True) | Q(process_queued_at__lt=claim_cutoff))
    # Fetch-only backfill articles (BACKFILL_LLM_ENABLED=False) are annotated by
    # annotate_deferred_articles_task, not the live pipeline.
    qs = qs.exclude(annotation_deferred=True)
    return _apply_min_score_filter(qs, settings.ARTICLE_MIN_IMPORTANCE_TO_PROCESS)


def _process_ids(limit: int) -> list:
    return list(_process_pending().order_by('-importance_score').values_list('id', flat=True)[:limit])


def _process_claim(ids: list) -> None:
    from core import models as m
    m.Article.objects.filter(id__in=ids).update(process_queued_at=_now())


def _process_release(ids: list) -> None:
    from core import models as m
    m.Article.objects.filter(id__in=ids).update(process_queued_at=None)


def _process_handler(ids: list) -> int:
    from services.workflow.articles import process_articles
    return process_articles(ids=ids)


def _aggregate_handler(_ids) -> int:
    from django.conf import settings
    from services.workflow import aggregate_events
    # Live stage clusters only the trailing AGGREGATE_LIVE_WINDOW_HOURS each tick;
    # aggregate_full_task sweeps the full EVENT_STAGE_WINDOW_HOURS daily.
    created, updated = aggregate_events(hours=settings.AGGREGATE_LIVE_WINDOW_HOURS)
    return created + updated


def _tag_pending_ids(limit: int | None) -> list:
    from core import models as m
    from services.workflow import event_needs_tagging
    lookback = _now() - timedelta(hours=EVENT_STAGE_WINDOW_HOURS)
    # DB-narrow to non-embed-tagged events (untagged / keyword-fallback / legacy);
    # an embed pass never needs re-tagging. The Python check stays only as a
    # legacy-list safety net over the already-narrowed set — no more full scan.
    qs = (
        m.Event.objects.filter(started_at__gte=lookback)
        .exclude(topics_source='embed')
        .only('pk', 'topics', 'topics_source')
    )
    ids = [e.pk for e in qs if event_needs_tagging(e)]
    return ids[:limit] if limit else ids


def _tag_pending_qs():
    """DB-narrowed pending set (untagged / keyword-fallback / legacy events) —
    mirrors _tag_pending_ids' DB predicate, minus the per-event Python check.
    Shared by the coverage count and the age-bucket breakdown."""
    from core import models as m
    lookback = _now() - timedelta(hours=EVENT_STAGE_WINDOW_HOURS)
    return m.Event.objects.filter(started_at__gte=lookback).exclude(topics_source='embed')


def _tag_pending_count() -> int:
    """Cheap DB-side count for the coverage table — mirrors _tag_pending_ids'
    DB predicate without materializing/filtering every event in Python."""
    return _tag_pending_qs().count()


def _tag_handler(ids: list) -> int:
    from services.workflow import tag_events_by_ids
    return tag_events_by_ids(ids)


def _route_pending():
    """Repair-only: events that missed inline routing at aggregation time
    (aggregate_events routes every event it touches) — NOT a periodic re-route
    of everything recent, which starved never-routed events past the limit."""
    from core import models as m
    lookback = _now() - timedelta(hours=EVENT_STAGE_WINDOW_HOURS)
    return m.Event.objects.filter(started_at__gte=lookback, is_routed=False)


def _route_ids(limit: int) -> list:
    return list(_route_pending().values_list('pk', flat=True)[:limit])


def _route_handler(ids: list) -> int:
    from core import models as m
    from services.routing import route_events
    events = list(m.Event.objects.filter(pk__in=list(ids)))
    return route_events(events)


def _forecast_enabled() -> bool:
    from django.conf import settings
    return bool(settings.FORECAST_ENABLED)


def _count(qs_fn) -> Callable[[], int]:
    def count() -> int:
        try:
            return qs_fn().count()
        except Exception:  # noqa: BLE001 — e.g. list-equality filter unsupported
            return 0
    return count


# ── Stage definition ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Stage:
    name: str                 # registry key; also TaskRun arg + dashboard action value
    label: str                # human-readable dashboard label
    model: str                # 'source' | 'article' | 'event' — display only
    queue: str                # 'default' | 'heavy'
    chunk_size: int           # records per run_stage_chunk_task job
    limit: int                # max records dispatched per tick
    every_minutes: int        # min interval between dispatches for this stage
    handler: Callable[[list], int]                 # process one chunk of ids
    pending_ids: Callable[[int], list] | None = None   # bounded id selection (None → singleton)
    pending_count: Callable[[], int] = lambda: 0       # dashboard/coverage count
    pending_qs: Callable[[], object] | None = None     # full pending queryset (age buckets)
    age_field: str = 'created_on'                      # record field used to bucket pending age
    claim: Callable[[list], None] | None = None        # mark ids in-flight at dispatch
    release: Callable[[list], None] | None = None      # undo claim for never-enqueued ids
    enabled: Callable[[], bool] = lambda: True
    job_timeout: int | None = None                     # per-job Celery time-limit override
    error_stage_key: str | None = None                 # stage_status key for coverage error samples
    coverage: bool = True                              # show in the dashboard coverage table

    @property
    def singleton(self) -> bool:
        return self.pending_ids is None


REGISTRY: dict[str, Stage] = {s.name: s for s in [
    Stage(
        name='fetch', label='Fetch enabled sources', model='source',
        queue='default', chunk_size=1, limit=500, every_minutes=10,
        handler=_fetch_handler, pending_ids=_fetch_ids,
        pending_count=_count(_fetch_pending),
        # "Pending" here is just "enabled sources" — every source is re-fetched
        # each cadence, so it's not a stuck-records signal for the coverage table.
        coverage=False,
    ),
    Stage(
        # chunk_size matches ArticleImportanceScorer.BATCH_SIZE — one chunk =
        # one batched LLM scoring call. Selection is simply "no score yet"
        # (no created_on window), so articles that missed a scoring run are
        # recovered automatically on a later tick.
        name='score', label='Unprocessed & unscored', model='article',
        queue='heavy', chunk_size=30, limit=300, every_minutes=60,
        handler=_score_handler, pending_ids=_score_ids,
        pending_count=_count(_score_pending), pending_qs=_score_pending,
        enabled=_score_enabled,
    ),
    Stage(
        # chunk_size matches ArticleAnalyzer.ANALYZE_BATCH_SIZE — one chunk =
        # one batched LLM analysis call.
        name='process', label='Unprocessed & eligible (awaiting dispatch)', model='article',
        queue='heavy', chunk_size=8, limit=500, every_minutes=30,
        handler=_process_handler, pending_ids=_process_ids,
        pending_count=_count(_process_pending), pending_qs=_process_pending,
        claim=_process_claim, release=_process_release,
        enabled=_live_llm_enabled,   # dashboard master switch for live LLM
        error_stage_key='process',
        # When Ollama is the effective primary (no cloud keys, or all exhausted),
        # analyzer.py degrades analyze_batch to one LLM call per article instead
        # of one per 8-article chunk — the heavy queue's 600s default would
        # SIGKILL such a chunk mid-flight and silently drop its progress, so give
        # the process job extra headroom. Geocoding is done inline here (a local
        # geonamescache lookup of the LLM's country/city — analyzer._geocode);
        # there is no separate geocode stage.
        job_timeout=1200,
    ),
    Stage(
        name='aggregate', label='Aggregate articles into events', model='event',
        queue='heavy', chunk_size=1, limit=1, every_minutes=30,
        handler=_aggregate_handler,     # singleton — pending_ids=None
        job_timeout=3600,               # bounded: covers a full 168h window with margin
    ),
    Stage(
        name='tag', label='Untagged / keyword-fallback events', model='event',
        queue='heavy', chunk_size=10, limit=500, every_minutes=60,
        handler=_tag_handler, pending_ids=_tag_pending_ids,
        pending_count=_tag_pending_count, pending_qs=_tag_pending_qs,
        error_stage_key='tag',
    ),
    Stage(
        name='route', label='Unrouted events (repair)', model='event',
        queue='heavy', chunk_size=10, limit=500, every_minutes=360,
        handler=_route_handler, pending_ids=_route_ids,
        pending_count=_count(_route_pending), pending_qs=_route_pending,
        enabled=_forecast_enabled,
        error_stage_key='route',
    ),
]}


def select_ids(stage_name: str, limit: int, source_code: str | None = None) -> list:
    """Select pending ids for a stage using the SAME predicate the dispatcher
    uses — CLI/e2e entry point, so manual runs can't drift from the pipeline.

    source_code narrows to one source (article stages only; currently just
    'process' needs it).
    """
    stage = REGISTRY[stage_name]
    if stage.singleton:
        return []
    if source_code:
        if stage_name != 'process':
            raise ValueError(f'source filtering not supported for stage {stage_name!r}')
        qs = _process_pending().filter(source_code=source_code)
        return list(qs.order_by('-importance_score').values_list('id', flat=True)[:limit])
    return stage.pending_ids(limit)


# ── Execution ───────────────────────────────────────────────────────────────

def _last_dispatch_key(stage: Stage) -> str:
    from services.cache import key_stage_last_dispatch
    return key_stage_last_dispatch(stage.name)


def _is_due(stage: Stage) -> bool:
    from services.cache import cache_get
    try:
        last = cache_get(_last_dispatch_key(stage))
    except Exception:  # noqa: BLE001 — no Redis in dev → always due
        return True
    return last is None or (_time.time() - float(last)) >= stage.every_minutes * 60


def _mark_dispatched(stage: Stage) -> None:
    from services.cache import cache_set
    try:
        cache_set(_last_dispatch_key(stage), _time.time(), timeout=None)
    except Exception:  # noqa: BLE001
        pass


def last_dispatched_at(stage: Stage) -> datetime | None:
    from services.cache import cache_get
    try:
        ts = cache_get(_last_dispatch_key(stage))
    except Exception:  # noqa: BLE001
        return None
    return datetime.fromtimestamp(float(ts), tz=dt_timezone.utc) if ts else None


def stage_age_buckets(stage: Stage) -> dict | None:
    """Break a stage's pending records into age buckets by ``age_field``
    (default ``created_on``): how long each record has been waiting at this step.

    Buckets: <1h, 1h–24h, 24h–1w, >1w, plus ``total``. Returns None for stages
    with no queryset (singletons like aggregate). Four index-backed count()
    queries — cheap enough for the dashboard, computed by boundary subtraction
    so the buckets always sum to ``total``.
    """
    if stage.pending_qs is None:
        return None
    now = _now()
    field = stage.age_field
    try:
        qs = stage.pending_qs()
        total = qs.count()
        older_1h = qs.filter(**{f'{field}__lt': now - timedelta(hours=1)}).count()
        older_1d = qs.filter(**{f'{field}__lt': now - timedelta(days=1)}).count()
        older_1w = qs.filter(**{f'{field}__lt': now - timedelta(weeks=1)}).count()
    except Exception:  # noqa: BLE001 — same defensive stance as _count()
        return None
    return {
        'lt_1h': total - older_1h,
        'h1_24h': older_1h - older_1d,
        'd1_1w': older_1d - older_1w,
        'gt_1w': older_1w,
        'total': total,
    }


def dispatch_stage(stage_name: str, force: bool = False) -> int:
    """Select pending work for one stage and fan it out. Returns jobs enqueued.

    force=True skips the cadence gate (admin buttons / manual runs) but still
    respects ``enabled`` and only dispatches when there is pending work.
    """
    stage = REGISTRY[stage_name]
    if not stage.enabled():
        return 0
    if not force and not _is_due(stage):
        return 0

    if stage.singleton:
        _mark_dispatched(stage)
        _enqueue_chunk(stage, None)
        return 1

    ids = stage.pending_ids(stage.limit)
    if not ids:
        return 0
    _mark_dispatched(stage)
    if stage.claim is not None:
        stage.claim(ids)

    enqueued: set = set()
    jobs = 0
    try:
        for i in range(0, len(ids), stage.chunk_size):
            chunk = ids[i:i + stage.chunk_size]
            _enqueue_chunk(stage, chunk)
            enqueued.update(chunk)
            jobs += 1
    except Exception:
        # Release the claim on ids a mid-loop failure never actually enqueued,
        # so the next tick can pick them up instead of waiting out the lease.
        if stage.release is not None:
            unclaimed = [x for x in ids if x not in enqueued]
            if unclaimed:
                stage.release(unclaimed)
        raise
    return jobs


def _enqueue_chunk(stage: Stage, chunk: list | None) -> None:
    from services.queue import enqueue
    from services.tasks import run_stage_chunk_task
    kwargs = {'job_timeout': stage.job_timeout} if stage.job_timeout else {}
    enqueue(run_stage_chunk_task, stage.name, chunk, queue=stage.queue, **kwargs)


def run_chunk(stage_name: str, ids: list | None) -> int:
    """Worker entry point — execute one chunk (or a singleton run) of a stage."""
    stage = REGISTRY[stage_name]
    return stage.handler(ids)


def run_due_stages(force: bool = False) -> dict:
    """One scheduler tick: dispatch every enabled stage that is due and has
    pending work, in pipeline order. Returns {stage_name: jobs_enqueued}."""
    results: dict[str, int] = {}
    for name in REGISTRY:
        try:
            jobs = dispatch_stage(name, force=force)
        except Exception:
            logger.exception('[tick] stage %r dispatch failed', name)
            jobs = -1  # visible in the tick's TaskRun result
        if jobs:
            results[name] = jobs
    return results
