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
) -> int:
    return Workflow.process_articles(limit=limit, source_code=source_code, reprocess=reprocess)


def aggregate_events_task(
    hours: int = DEFAULT_AGGREGATE_HOURS,
    min_articles: int = DEFAULT_AGGREGATE_MIN_ARTICLES,
) -> tuple[int, int]:
    return Workflow.aggregate_events(hours=hours, min_articles=min_articles)


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
    from services.streams import run_stream
    return run_stream('prices')


def fetch_notams_task() -> int:
    from services.streams import run_stream
    return run_stream('notam')


def fetch_earthquakes_task() -> int:
    from services.streams import run_stream
    return run_stream('earthquakes')


def fetch_forex_task() -> int:
    from services.streams import run_stream
    return run_stream('forex')


# ── Forecasting tasks ──────────────────────────────────────────────────────────

def run_forecast_task() -> int:
    from services.forecasting.service import run_forecasts
    return run_forecasts()


def score_forecasts_task() -> int:
    from services.forecasting.service import score_forecasts
    return score_forecasts()


# ── Backfill tasks ─────────────────────────────────────────────────────────────

def backfill_history_task(
    source_code: str,
    start_date: datetime,
    end_date: datetime,
    top_n: int = 10,
) -> dict:
    """
    Backfill top-N articles per ISO week for a source.

    Enqueue with job_timeout=-1 (no cap) since multi-year backtracks can take
    longer than the standard 30-minute task timeout.

    Returns {'weeks': int, 'fetched': int, 'saved': int}.
    """
    import core.models as m
    from services.data.historical import HistoricalBackfillService

    source = m.Source.objects.get(code=source_code)
    service = HistoricalBackfillService(
        source=source,
        start_date=start_date,
        end_date=end_date,
        top_n=top_n,
        delay_seconds=0.5,
    )

    total_weeks = total_fetched = total_saved = 0
    for result in service.run():
        total_weeks += 1
        total_fetched += result.fetched
        total_saved += result.saved

    return {'weeks': total_weeks, 'fetched': total_fetched, 'saved': total_saved}
