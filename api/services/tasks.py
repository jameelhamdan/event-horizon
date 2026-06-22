"""Task functions for the ingestion and aggregation pipeline.

These are plain Python functions enqueued via django-rq (services.queue.enqueue).
"""

import os
from datetime import datetime, timedelta, timezone as dt_timezone

from services.workflow import Workflow

JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "1800"))
DEFAULT_FETCH_MINUTES = int(os.getenv("FETCH_INTERVAL_MINUTES", "10")) * 2
DEFAULT_PROCESS_LIMIT = int(os.getenv("PROCESS_LIMIT", "1000"))
DEFAULT_AGGREGATE_HOURS = int(os.getenv("AGGREGATE_HOURS", "24"))
DEFAULT_AGGREGATE_MIN_ARTICLES = int(os.getenv("AGGREGATE_MIN_ARTICLES", "1"))


# ── Text pipeline ─────────────────────────────────────────────────────────────

def fetch_articles_task(source_code: str | None = None, start_date: datetime | None = None) -> int:
    now = datetime.now(dt_timezone.utc)
    if start_date is None:
        start_date = now - timedelta(minutes=DEFAULT_FETCH_MINUTES)
    # Soft deadline 30 s before the hard RQ timeout fires (interval - 60 s).
    # Checked between sources so we stop gracefully rather than being force-killed mid-fetch.
    interval_seconds = int(os.getenv('FETCH_INTERVAL_MINUTES', '10')) * 60
    deadline = now + timedelta(seconds=interval_seconds - 30)
    return Workflow.fetch_articles(source_code, start_date, deadline=deadline)


def process_articles_task(
    limit: int = DEFAULT_PROCESS_LIMIT,
    source_code: str | None = None,
    reprocess: bool = False,
    only_failed: bool = False,
) -> int:
    return Workflow.process_articles(
        limit=limit, source_code=source_code, reprocess=reprocess, only_failed=only_failed,
    )


def aggregate_events_task(
    hours: int = DEFAULT_AGGREGATE_HOURS,
    min_articles: int = DEFAULT_AGGREGATE_MIN_ARTICLES,
) -> tuple[int, int]:
    return Workflow.aggregate_events(hours=hours, min_articles=min_articles)


def run_pipeline_task(
    fetch_hours: int = 2,
    process_limit: int = 500,
    aggregate_hours: int = 24,
    source_code: str | None = None,
    tag: bool = False,
) -> dict:
    """Run fetch -> process -> aggregate (-> tag) sequentially in ONE job.

    Enqueueing the three steps as separate jobs races them: aggregate can run before
    process/fetch finish, so no new events appear. This chains them in order so the admin
    'Run full pipeline' button reliably produces Events.
    """
    now = datetime.now(dt_timezone.utc)
    summary: dict = {}
    summary['fetched'] = fetch_articles_task(source_code, now - timedelta(hours=fetch_hours))
    summary['processed'] = process_articles_task(limit=process_limit)
    created, updated = aggregate_events_task(hours=aggregate_hours)
    summary['events_created'] = created
    summary['events_updated'] = updated
    if tag:
        summary['tagged'] = tag_topics_task(hours=aggregate_hours)
    return summary


# ── Topic tasks ────────────────────────────────────────────────────────────────

def refresh_topics_task() -> int:
    return Workflow.refresh_topics()


def tag_topics_task(hours: int = DEFAULT_AGGREGATE_HOURS, force_retag: bool = False) -> int:
    return Workflow.tag_events_with_topics(hours=hours, force_retag=force_retag)


def retroactive_tag_topic_task(slug: str, lookback_hours: int = 72) -> int:
    return Workflow.retroactive_tag_topic(slug=slug, lookback_hours=lookback_hours)


def discover_topics_task(hours: int = 6) -> int:
    return Workflow.discover_topics_from_events(hours=hours)


# ── Stream tasks ───────────────────────────────────────────────────────────────

def fetch_prices_task() -> int:
    from django.conf import settings
    if not settings.STREAM_PRICES_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('prices')


def fetch_notams_task() -> int:
    from django.conf import settings
    if not settings.STREAM_NOTAM_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('notam')


def fetch_earthquakes_task() -> int:
    from django.conf import settings
    if not settings.STREAM_EARTHQUAKE_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('earthquakes')


def fetch_forex_task() -> int:
    from django.conf import settings
    if not settings.STREAM_FOREX_ENABLED:
        return 0
    from services.streams import run_stream
    return run_stream('forex')


# ── Health monitoring (A1 / A5) ─────────────────────────────────────────────────

def pipeline_health_task() -> dict:
    """Warn when pipeline outputs go stale. Logs only — surfaced via Sentry / log alerts.

    Covers the highest-risk silent failures (A1): no fetched articles, stale stream
    data on undocumented APIs, and — the single-source topic risk (A5) — zero current
    topics, which means the Wikipedia scraper has likely broken.
    """
    import logging
    from django.conf import settings
    from core import models as core_models

    log = logging.getLogger('pipeline.health')
    now = datetime.now(dt_timezone.utc)
    report: dict = {}

    def _aware(dt):
        return dt.replace(tzinfo=dt_timezone.utc) if dt and dt.tzinfo is None else dt

    def _check_stale(model, field, minutes, label):
        latest = _aware(model.objects.order_by(f'-{field}').values_list(field, flat=True).first())
        ok = latest is not None and latest >= now - timedelta(minutes=minutes)
        report[label] = {'latest': latest.isoformat() if latest else None, 'ok': ok}
        if not ok:
            log.warning('[health] %s stale — latest=%s (threshold=%dm)', label, latest, minutes)

    _check_stale(core_models.Article, 'created_on',
                 int(os.getenv('HEALTH_ARTICLE_STALE_MIN', '180')), 'articles')
    if settings.STREAM_PRICES_ENABLED:
        _check_stale(core_models.PriceTick, 'occurred_at',
                     int(os.getenv('HEALTH_PRICE_STALE_MIN', '60')), 'prices')
    if settings.STREAM_EARTHQUAKE_ENABLED:
        _check_stale(core_models.EarthquakeRecord, 'occurred_at',
                     int(os.getenv('HEALTH_QUAKE_STALE_MIN', '360')), 'earthquakes')

    # A5: Wikipedia is the only topic source — zero current topics ⇒ likely broken.
    current_topics = core_models.Topic.objects.filter(is_current=True).count()
    report['current_topics'] = current_topics
    if current_topics == 0:
        log.warning('[health] zero current topics — the Wikipedia topic source may be broken')

    return report


# ── Backfill tasks ─────────────────────────────────────────────────────────────

def backfill_history_task(
    source_code: str,
    start_date: datetime,
    end_date: datetime,
    top_n: int = 10,
    delay_seconds: float = 0.5,
    dry_run: bool = False,
    resume: bool = False,
    progress=None,
) -> dict:
    """
    Backfill top-N articles per ISO week for a source.

    Enqueue with job_timeout=-1 (no cap) since multi-year backtracks can take
    longer than the standard 30-minute task timeout.

    ``start_date`` / ``end_date`` accept either ``datetime`` objects or
    ``YYYY-MM-DD`` strings (the latter so the task is trivially enqueueable).
    ``resume`` skips ISO weeks already recorded in the Django cache checkpoint;
    ``progress`` is an optional ``callable(WeekResult)`` invoked per week (the
    management command passes one to echo per-week lines to stdout).

    Returns {'weeks': int, 'fetched': int, 'saved': int}.
    """
    import logging

    import core.models as m
    from django.core.cache import cache
    from services.data.historical import HistoricalBackfillService

    logger = logging.getLogger(__name__)

    start_date = _parse_backfill_date(start_date)
    end_date = _parse_backfill_date(end_date)

    source = m.Source.objects.get(code=source_code)
    service = HistoricalBackfillService(
        source=source,
        start_date=start_date,
        end_date=end_date,
        top_n=top_n,
        delay_seconds=delay_seconds,
    )

    checkpoint_key = f'backfill:{source_code}:{start_date.date()}:{end_date.date()}:done'
    resume_weeks: set[str] = (cache.get(checkpoint_key) or set()) if resume else set()

    total_weeks = total_fetched = total_saved = 0
    for result in service.run(resume_weeks=resume_weeks, dry_run=dry_run):
        total_weeks += 1
        total_fetched += result.fetched
        total_saved += result.saved
        if progress is not None:
            progress(result)
        if resume and not dry_run:
            resume_weeks.add(result.week_start.isoformat())
            cache.set(checkpoint_key, resume_weeks, timeout=None)

    summary = {'weeks': total_weeks, 'fetched': total_fetched, 'saved': total_saved}
    logger.info('backfill_history_task %s done: %s', source_code, summary)
    return summary


def backfill_all_sources_task(
    start_date: datetime,
    end_date: datetime,
    top_n: int = 10,
    delay_seconds: float = 0.5,
    dry_run: bool = False,
    resume: bool = False,
    progress=None,
    on_source_start=None,
) -> dict:
    """
    Backfill every enabled RSS source over the same date range, sequentially.

    Only ``SourceType.RSS`` sources are eligible (the historical backfill has no
    strategy for other types); disabled sources are skipped. Running sources one
    at a time keeps API rate-limit pressure bounded. ``on_source_start`` is an
    optional ``callable(Source)`` invoked before each source; ``progress`` is
    forwarded per week to :func:`backfill_history_task`.

    Enqueue with job_timeout=-1 — backfilling many sources can run for hours.

    Returns {'sources': int, 'weeks': int, 'fetched': int, 'saved': int,
             'per_source': {code: {weeks, fetched, saved}}}.
    """
    import logging

    import core.models as m

    logger = logging.getLogger(__name__)

    start_date = _parse_backfill_date(start_date)
    end_date = _parse_backfill_date(end_date)

    sources = list(
        m.Source.objects.filter(type=m.SourceType.RSS, is_enabled=True).order_by('code')
    )

    totals = {'sources': 0, 'weeks': 0, 'fetched': 0, 'saved': 0, 'per_source': {}}
    for source in sources:
        if on_source_start is not None:
            on_source_start(source)
        summary = backfill_history_task(
            source.code, start_date, end_date,
            top_n=top_n, delay_seconds=delay_seconds, dry_run=dry_run, resume=resume,
            progress=progress,
        )
        totals['per_source'][source.code] = summary
        totals['sources'] += 1
        for key in ('weeks', 'fetched', 'saved'):
            totals[key] += summary[key]

    logger.info(
        'backfill_all_sources_task done: %s source(s), %s saved',
        totals['sources'], totals['saved'],
    )
    return totals


def _parse_backfill_date(value) -> datetime:
    """Normalize a backfill bound to a UTC datetime (accepts YYYY-MM-DD strings)."""
    if isinstance(value, datetime):
        return value
    d = datetime.strptime(value, '%Y-%m-%d')
    return d.replace(tzinfo=dt_timezone.utc)


# ── Forecasting tasks (event-fused symbol prediction) ────────────────────────────

def backfill_prices_task(symbols: list[str] | None = None, years: int = 5) -> int:
    """Backfill daily OHLC PriceBar history for the indicator panel."""
    from services.forecasting.history import backfill_all
    results = backfill_all(symbols=symbols, years=years)
    return sum(results.values())


def route_events_task(hours: int = 168, source: str | None = None) -> int:
    """(Re)route recent events to market symbols, populating Event.affected_indicators."""
    from django.conf import settings
    from core import models as core_models
    from services.routing import route_events

    src = source or settings.FORECAST_ROUTER
    start = datetime.now(dt_timezone.utc) - timedelta(hours=hours)
    events = list(core_models.Event.objects.filter(started_at__gte=start))
    return route_events(events, source=src)


def train_forecast_model_task() -> int:
    """Train the LightGBM classifier + regressor for every configured horizon."""
    from django.conf import settings
    if not settings.FORECAST_ENABLED:
        return 0
    from services.forecasting import features, model

    end = datetime.now(dt_timezone.utc)
    start = end - timedelta(days=settings.FORECAST_TRAIN_WINDOW_DAYS + 60)
    frame = features.build_training_frame(
        start=start, end=end, horizons=settings.FORECAST_HORIZONS_DAYS, include_events=True,
    )
    if frame.empty:
        return 0
    trained = 0
    for h in settings.FORECAST_HORIZONS_DAYS:
        try:
            model.train(frame, h)
            trained += 1
        except RuntimeError as exc:
            import logging
            logging.getLogger(__name__).warning('[forecast] train h%d skipped: %s', h, exc)
    return trained


def run_forecast_task() -> int:
    """Write one Forecast row per (panel symbol, horizon) from the latest models."""
    from django.conf import settings
    if not settings.FORECAST_ENABLED:
        return 0
    from services.forecasting import features, model
    from services.forecasting.history import SYMBOL_META
    from core import models as core_models

    fm = features.build_feature_matrix(include_events=True)
    if fm.empty:
        return 0
    now = datetime.now(dt_timezone.utc)
    router = settings.FORECAST_ROUTER
    created = 0
    for h in settings.FORECAST_HORIZONS_DAYS:
        for p in model.predict(fm, h):
            stream_key = SYMBOL_META.get(p['symbol'], ('', ''))[0]
            core_models.Forecast.objects.create(
                symbol=p['symbol'], stream_key=stream_key, generated_at=now,
                as_of_date=p['as_of_date'], horizon_days=h, direction=p['direction'],
                proba_up=p['proba_up'], predicted_change_pct=p['predicted_change_pct'],
                predicted_price=p['predicted_price'], band_low=p['band_low'],
                band_high=p['band_high'], confidence=p['confidence'],
                current_value=p['current_value'], router_source=router,
                model_version=p['model_version'],
            )
            created += 1
    return created


def score_forecasts_task() -> int:
    """Fill realized outcomes for forecasts whose horizon has elapsed."""
    from core import models as core_models

    pending = core_models.Forecast.objects.filter(realized_direction__isnull=True)
    now = datetime.now(dt_timezone.utc)
    scored = 0
    for f in pending:
        if not f.current_value:
            continue
        future = list(
            core_models.PriceBar.objects.filter(
                symbol=f.symbol, interval='1d', date__gt=f.as_of_date,
            ).order_by('date').values_list('close', flat=True)[:f.horizon_days]
        )
        if len(future) < f.horizon_days:
            continue  # horizon not elapsed yet
        ret = future[-1] / f.current_value - 1
        f.realized_change_pct = round(ret * 100, 4)
        f.realized_direction = 'up' if ret > 0 else 'down'
        pred_up = f.proba_up > 0.5
        f.is_correct = (pred_up and ret > 0) or (not pred_up and ret <= 0)
        f.scored_at = now
        f.save(update_fields=[
            'realized_change_pct', 'realized_direction', 'is_correct', 'scored_at',
        ])
        scored += 1
    return scored
