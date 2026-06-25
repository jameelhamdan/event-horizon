"""Register all periodic jobs with rq-scheduler.

Run once at scheduler startup (the scheduler Docker service does this before
launching rqscheduler).  Clears any existing scheduled jobs first so
re-running is idempotent.

Queues
------
default  — light I/O tasks (fetchers, stream collectors)
heavy    — NLP / LLM tasks (processing, clustering, topic matching)
           Heavy task intervals default to 5x the equivalent light interval.
"""

import os
from datetime import datetime, timezone

import django_rq
from django.conf import settings
from django.core.management.base import BaseCommand
from rq_scheduler import Scheduler


def _minutes(env_var: str, default: str) -> int:
    return int(os.getenv(env_var, default)) * 60


def _interval_timeout(interval_seconds: int) -> int:
    """Hard-kill timeout: 60 s before the next scheduled run (minimum 60 s)."""
    return max(interval_seconds - 60, 60)


class Command(BaseCommand):
    help = 'Register all periodic jobs with rq-scheduler (idempotent)'

    def handle(self, *args, **options):
        from newsletter.tasks import generate_newsletter_task
        from services.queue import enqueue  # noqa: F401 — used below for bootstrap
        from services.tasks import (
            aggregate_events_task,
            backfill_articles_task,
            backfill_prices_task,
            bootstrap_initial_data_task,
            discover_topics_task,
            dispatch_fetch_task,
            dispatch_process_articles_task,
            dispatch_route_events_task,
            dispatch_tag_topics_task,
            fetch_earthquakes_task,
            fetch_forex_task,
            fetch_notams_task,
            fetch_prices_task,
            pipeline_health_task,
            refresh_topics_task,
            run_forecast_task,
            score_forecasts_task,
            train_forecast_model_task,
        )

        conn = django_rq.get_connection('default')
        light = Scheduler(queue_name='default', connection=conn)
        heavy = Scheduler(queue_name='heavy', connection=conn)

        cron_timeout = int(os.getenv('JOB_TIMEOUT_SECONDS', '1800'))

        def sched(scheduler, when, func, *, interval=None, repeat=None,
                  timeout=None, kwargs=None):
            scheduler.schedule(when, func, kwargs=kwargs or {},
                               interval=interval, repeat=repeat, timeout=timeout)

        def cron(scheduler, cron_string, func, *, repeat=None, timeout=None):
            scheduler.cron(cron_string, func, repeat=repeat, timeout=timeout)

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

        # Fetch is a light dispatcher: enqueues one fetch_source_task per enabled source.
        sched(light, now, dispatch_fetch_task, interval=fetch_interval, timeout=_interval_timeout(fetch_interval))
        # Stream collectors — each gated by its feature flag.
        if settings.STREAM_PRICES_ENABLED:
            sched(light, now, fetch_prices_task,      interval=price_interval, timeout=_interval_timeout(price_interval))
        if settings.STREAM_NOTAM_ENABLED:
            sched(light, now, fetch_notams_task,      interval=notam_interval, timeout=_interval_timeout(notam_interval))
        if settings.STREAM_EARTHQUAKE_ENABLED:
            sched(light, now, fetch_earthquakes_task, interval=quake_interval, timeout=_interval_timeout(quake_interval))
        if settings.STREAM_FOREX_ENABLED:
            sched(light, now, fetch_forex_task,       interval=forex_interval, timeout=_interval_timeout(forex_interval))
        health_interval = _minutes('HEALTH_CHECK_INTERVAL_MINUTES', '30')
        sched(light, now, pipeline_health_task, interval=health_interval, timeout=_interval_timeout(health_interval))

        # ── NLP / LLM — light dispatchers fan out to per-record heavy workers ──
        process_interval  = _minutes('PROCESS_INTERVAL_MINUTES', '60')
        aggregate_interval = _minutes('AGGREGATE_INTERVAL_MINUTES', '60')
        tag_interval      = _minutes('TAG_TOPICS_INTERVAL_MINUTES', '75')
        discover_interval = _minutes('DISCOVER_TOPICS_INTERVAL_MINUTES', '150')
        recover_interval  = _minutes('STUCK_RECOVERY_INTERVAL_MINUTES', '360')

        sched(light, now, dispatch_process_articles_task, interval=process_interval, timeout=_interval_timeout(process_interval))
        sched(heavy, now, aggregate_events_task,          interval=aggregate_interval, timeout=_interval_timeout(aggregate_interval))
        sched(light, now, dispatch_tag_topics_task,       interval=tag_interval,   timeout=_interval_timeout(tag_interval))
        sched(heavy, now, discover_topics_task,           interval=discover_interval, timeout=_interval_timeout(discover_interval))
        sched(light, now, dispatch_process_articles_task, kwargs={'only_failed': True},
              interval=recover_interval, timeout=_interval_timeout(recover_interval))

        # ── Cron jobs (heavy — daily LLM runs) ────────────────────────────────
        refresh_hour    = int(os.getenv('TOPICS_REFRESH_HOUR', '4'))
        newsletter_hour = int(os.getenv('NEWSLETTER_GENERATE_HOUR', '6'))
        cron(heavy, f'0 {refresh_hour} * * *',    refresh_topics_task,      timeout=cron_timeout)
        if settings.NEWSLETTER_ENABLED:
            cron(heavy, f'0 {newsletter_hour} * * *', generate_newsletter_task, timeout=cron_timeout)

        # ── Forecasting ────────────────────────────────────────────────────────
        week = 7 * 24 * 60 * 60
        sched(heavy, now, backfill_articles_task, interval=week, timeout=cron_timeout)

        if settings.FORECAST_ENABLED:
            sched(light, now, backfill_prices_task, interval=week, timeout=cron_timeout)
            route_interval = _minutes('ROUTE_EVENTS_INTERVAL_MINUTES', '90')
            sched(light, now, dispatch_route_events_task, interval=route_interval,
                  timeout=_interval_timeout(route_interval))
            train_hour = int(os.getenv('FORECAST_TRAIN_HOUR', '5'))
            cron(heavy, f'0 {train_hour} * * *',  train_forecast_model_task, timeout=cron_timeout)
            cron(heavy, f'30 {train_hour} * * *', run_forecast_task,         timeout=cron_timeout)
            cron(light, '0 7 * * *',              score_forecasts_task,      timeout=cron_timeout)

        # ── Configurationless first-load bootstrap (WA4) ──────────────────────
        # Enqueue once on every scheduler start; the task is idempotent (cache flag +
        # PriceBar-presence heuristic) so it only backfills on a fresh deployment.
        try:
            enqueue(bootstrap_initial_data_task, queue='default')
            self.stdout.write('Triggered first-load bootstrap (idempotent).')
        except Exception as exc:  # noqa: BLE001 — never block schedule registration
            self.stdout.write(self.style.WARNING(f'Bootstrap trigger skipped: {exc}'))

        # rq-scheduler keeps a single shared job registry (not split per queue),
        # so report the total actually registered.
        total = sum(1 for _ in heavy.get_jobs())
        self.stdout.write(self.style.SUCCESS(
            f'Schedule registered: {total} scheduled job(s).'
        ))
