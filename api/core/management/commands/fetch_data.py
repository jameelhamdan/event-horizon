from datetime import datetime, timedelta

from django.utils.timezone import make_aware, now

from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Fetch messages from a Source into the Article model'

    def add_arguments(self, parser):
        parser.add_argument('source_code', type=str, nargs='?', default=None, help='Source.code to fetch from (omit to fetch all sources)')
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            '--hours', type=int, default=None,
            help='Fetch last N hours (default: each source\'s last_fetched_at cursor)',
        )
        group.add_argument(
            '--start-date', type=str, default=None, dest='start_date',
            help='Start datetime UTC, e.g. "2024-01-01 00:00:00" (clamped to the 24h cursor floor)',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background Celery task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        source_code = kwargs['source_code']
        # No explicit window → cursor-based fetch, same as the fetch stage.
        start = None
        if kwargs['hours'] is not None:
            start = now() - timedelta(hours=kwargs['hours'])
        elif kwargs['start_date']:
            start = make_aware(datetime.strptime(kwargs['start_date'], '%Y-%m-%d %H:%M:%S'))

        label = source_code or 'all sources'

        if kwargs['background']:
            from services.queue import enqueue
            from services.tasks import dispatch_stage_task, run_stage_chunk_task
            if source_code:
                enqueue(run_stage_chunk_task, 'fetch', [source_code], queue='default')
                self.stdout.write(self.style.SUCCESS(f'Enqueued fetch chunk for {source_code}'))
            else:
                enqueue(dispatch_stage_task, 'fetch', queue='default')
                self.stdout.write(self.style.SUCCESS('Enqueued fetch stage dispatch'))
            return

        from services.workflow import fetch_sources
        count = fetch_sources(source_code, start=start)
        self.stdout.write(self.style.SUCCESS(f'Fetched {count} new article(s) from {label}'))
