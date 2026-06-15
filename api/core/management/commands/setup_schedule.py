"""Register all periodic jobs with rq-scheduler.

Run once at scheduler startup (the scheduler Docker service does this before
launching rqscheduler).  Clears any existing scheduled jobs first so
re-running is idempotent.

Queues
------
default  — light I/O tasks (fetchers, stream collectors)
heavy    — NLP / LLM tasks (processing, clustering, topic matching, forecasting)
           Heavy task intervals default to 5x the equivalent light interval.
"""

import os
from datetime import datetime, timezone

import django_rq
from django.core.management.base import BaseCommand
from rq_scheduler import Scheduler

from services.forecasting import is_enabled as forecasting_enabled


def _minutes(env_var: str, default: str) -> int:
    return int(os.getenv(env_var, default)) * 60


def _interval_timeout(interval_seconds: int) -> int:
    """Hard-kill timeout: 60 s before the next scheduled run (minimum 60 s)."""
    return max(interval_seconds - 60, 60)


class Command(BaseCommand):
    help = 'Register all periodic jobs with rq-scheduler (idempotent)'

    def handle(self, *args, **options):
        from newsletter.tasks import generate_newsletter_task
        from services.tasks import (
            aggregate_events_task,
            discover_topics_task,
            fetch_articles_task,
            fetch_earthquakes_task,
            fetch_forex_task,
            fetch_notams_task,
            fetch_prices_task,
            process_articles_task,
            refresh_topics_task,
            run_forecast_task,
            score_forecasts_task,
            tag_topics_task,
            train_forecaster_task,
        )

        conn = django_rq.get_connection('default')
        light = Scheduler(queue_name='default', connection=conn)
        heavy = Scheduler(queue_name='heavy', connection=conn)

        # Used only for cron (daily) jobs where no interval gives a natural bound.
        cron_timeout = int(os.getenv('JOB_TIMEOUT_SECONDS', '1800'))

        # Clear existing scheduled jobs so re-runs are idempotent
        for job in light.get_jobs():
            light.cancel(job)
        for job in heavy.get_jobs():
            heavy.cancel(job)
        self.stdout.write('Cleared existing scheduled jobs.')

        now = datetime.now(timezone.utc)

        # ── Light queue — fast I/O ─────────────────────────────────────────────
        fetch_interval = _minutes('FETCH_INTERVAL_MINUTES', '10')
        price_interval = _minutes('PRICE_FETCH_INTERVAL_MINUTES', '5')
        notam_interval = _minutes('NOTAM_FETCH_INTERVAL_MINUTES', '15')
        quake_interval = _minutes('EARTHQUAKE_FETCH_INTERVAL_MINUTES', '5')
        forex_interval = _minutes('FOREX_FETCH_INTERVAL_MINUTES', '15')

        light.schedule(now, fetch_articles_task,    interval=fetch_interval, repeat=None, timeout=_interval_timeout(fetch_interval))
        light.schedule(now, fetch_prices_task,      interval=price_interval, repeat=None, timeout=_interval_timeout(price_interval))
        light.schedule(now, fetch_notams_task,      interval=notam_interval, repeat=None, timeout=_interval_timeout(notam_interval))
        light.schedule(now, fetch_earthquakes_task, interval=quake_interval, repeat=None, timeout=_interval_timeout(quake_interval))
        light.schedule(now, fetch_forex_task,       interval=forex_interval, repeat=None, timeout=_interval_timeout(forex_interval))

        # ── Heavy queue — NLP / LLM (defaults are 5x the base interval) ───────
        process_interval  = _minutes('PROCESS_INTERVAL_MINUTES', '60')
        aggregate_interval = _minutes('AGGREGATE_INTERVAL_MINUTES', '60')
        tag_interval      = _minutes('TAG_TOPICS_INTERVAL_MINUTES', '75')
        discover_interval = _minutes('DISCOVER_TOPICS_INTERVAL_MINUTES', '150')
        forecast_interval = _minutes('FORECAST_INTERVAL_MINUTES', '300')
        score_interval    = _minutes('FORECAST_SCORE_INTERVAL_MINUTES', '300')

        forecasting_on = forecasting_enabled()

        heavy.schedule(now, process_articles_task,  interval=process_interval,   repeat=None, timeout=_interval_timeout(process_interval))
        heavy.schedule(now, aggregate_events_task,  interval=aggregate_interval, repeat=None, timeout=_interval_timeout(aggregate_interval))
        heavy.schedule(now, tag_topics_task,        interval=tag_interval,       repeat=None, timeout=_interval_timeout(tag_interval))
        heavy.schedule(now, discover_topics_task,   interval=discover_interval,  repeat=None, timeout=_interval_timeout(discover_interval))
        # Forecasting (run + score) — skipped entirely when FORECASTING_ENABLED is off.
        if forecasting_on:
            heavy.schedule(now, run_forecast_task,    interval=forecast_interval, repeat=None, timeout=_interval_timeout(forecast_interval))
            heavy.schedule(now, score_forecasts_task, interval=score_interval,    repeat=None, timeout=_interval_timeout(score_interval))

        # ── Cron jobs (heavy — daily LLM runs) ────────────────────────────────
        refresh_hour    = int(os.getenv('TOPICS_REFRESH_HOUR', '4'))
        newsletter_hour = int(os.getenv('NEWSLETTER_GENERATE_HOUR', '6'))
        train_hour      = int(os.getenv('FORECAST_TRAIN_HOUR', '3'))
        heavy.cron(f'0 {refresh_hour} * * *',    refresh_topics_task,      repeat=None, timeout=cron_timeout)
        heavy.cron(f'0 {newsletter_hour} * * *', generate_newsletter_task, repeat=None, timeout=cron_timeout)
        # v2 retrain — daily; no-op if lightgbm/numpy unavailable. Skipped when off.
        if forecasting_on:
            heavy.cron(f'0 {train_hour} * * *',  train_forecaster_task,    repeat=None, timeout=cron_timeout)

        # rq-scheduler keeps a single shared job registry (not split per queue), so
        # report the total actually registered — robust to the forecasting toggle.
        total = sum(1 for _ in heavy.get_jobs())
        note = '' if forecasting_on else ' (forecasting disabled)'
        self.stdout.write(self.style.SUCCESS(
            f'Schedule registered: {total} scheduled job(s).{note}'
        ))
