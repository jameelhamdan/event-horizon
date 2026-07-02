"""Trigger a background task by name — the cron entry point.

This is the single command ``api/crontab`` invokes for every periodic job.
Cron owns the schedule; this command only resolves a task by name, coerces its
params, and enqueues it on the right queue.

Usage::

    python manage.py run_task <task_name> [key=value ...] \
        [--queue default|heavy|bulk] [--require-flag SETTING_NAME] \
        [--job-timeout SECONDS] [--sync]

Examples::

    python manage.py run_task pipeline_tick_task
    python manage.py run_task dispatch_stage_task stage_name=process
    python manage.py run_task generate_newsletter_task --require-flag NEWSLETTER_ENABLED

Params are ``key=value`` pairs, coerced to bool / null / int / float / JSON /
str (in that order). The task's queue is auto-selected from the maps below
unless ``--queue`` overrides it. ``--require-flag`` makes the run a no-op when
the named ``settings`` flag is falsy, so feature-gated tasks can stay in the
crontab unconditionally.
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# Authoritative queue per task. Anything not listed runs on 'default' (light I/O).
# Pipeline stages pick their own queue in services/stages.py — pipeline_tick_task
# and dispatch_stage_task are light dispatchers and correctly default here.
HEAVY_TASKS = frozenset({
    'refresh_topics_task',
    'discover_topics_task',
    'generate_newsletter_task',
})
BULK_TASKS = frozenset({
    'backfill_prices_task',
    'train_forecast_model_task',
    'run_forecast_task',
})


def _coerce(value: str):
    """bool / null / int / float / JSON / str — first that parses wins."""
    low = value.lower()
    if low in ('true', 'false'):
        return low == 'true'
    if low in ('none', 'null'):
        return None
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    if value[:1] in ('[', '{'):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _resolve(name: str):
    """Find a *_task function in services.tasks or newsletter.tasks."""
    from newsletter import tasks as newsletter_tasks
    from services import tasks as service_tasks

    for module in (service_tasks, newsletter_tasks):
        func = getattr(module, name, None)
        if callable(func):
            return func
    return None


class Command(BaseCommand):
    help = 'Trigger a background task by name (cron entry point).'

    def add_arguments(self, parser):
        parser.add_argument('task_name', help='Task function name, e.g. pipeline_tick_task')
        parser.add_argument('params', nargs='*', help='key=value task kwargs')
        parser.add_argument('--queue', default=None, choices=['default', 'heavy', 'bulk'],
                            help='Override the auto-selected queue')
        parser.add_argument('--require-flag', default=None,
                            help='Skip the run unless this settings flag is truthy')
        parser.add_argument('--job-timeout', type=int, default=None,
                            help='Celery task time limit in seconds (-1 for no cap)')
        parser.add_argument('--sync', action='store_true',
                            help='Run inline instead of enqueueing (debugging)')

    def handle(self, *args, **options):
        name = options['task_name']

        flag = options['require_flag']
        if flag and not getattr(settings, flag, False):
            self.stdout.write(f'Skipping {name}: {flag} is disabled.')
            return

        func = _resolve(name)
        if func is None:
            raise CommandError(f'Unknown task: {name!r}')

        kwargs = {}
        for item in options['params']:
            key, sep, raw = item.partition('=')
            if not sep:
                raise CommandError(f'Bad param {item!r} — expected key=value')
            kwargs[key] = _coerce(raw)

        queue = options['queue'] or (
            'bulk' if name in BULK_TASKS else 'heavy' if name in HEAVY_TASKS else 'default'
        )

        if options['sync']:
            result = func(**kwargs)
            self.stdout.write(self.style.SUCCESS(f'{name} -> {result!r}'))
            return

        from services.queue import enqueue
        job = enqueue(func, queue=queue, job_timeout=options['job_timeout'], **kwargs)
        job_id = getattr(job, 'id', 'sync')
        self.stdout.write(self.style.SUCCESS(f'Enqueued {name} on {queue} (job {job_id}).'))
