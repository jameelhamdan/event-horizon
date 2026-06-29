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
            help='Fetch last N hours (default: 2)',
        )
        group.add_argument(
            '--start-date', type=str, default=None, dest='start_date',
            help='Start datetime UTC, e.g. "2024-01-01 00:00:00"',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background RQ task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        source_code = kwargs['source_code']
        if kwargs['hours'] is not None:
            start_date = now() - timedelta(hours=kwargs['hours'])
        elif kwargs['start_date']:
            start_date = make_aware(datetime.strptime(kwargs['start_date'], '%Y-%m-%d %H:%M:%S'))
        else:
            start_date = now() - timedelta(hours=2)

        label = source_code or 'all sources'

        if kwargs['background']:
            from services.queue import enqueue
            from services.tasks import dispatch_fetch_task, fetch_source_task
            if source_code:
                enqueue(fetch_source_task, source_code, start_date, queue='default')
                self.stdout.write(self.style.SUCCESS(f'Enqueued fetch_source_task for {source_code}'))
            else:
                enqueue(dispatch_fetch_task, queue='default')
                self.stdout.write(self.style.SUCCESS('Enqueued dispatch_fetch_task'))
            return

        from services.workflow import Workflow
        count = Workflow.fetch_articles(source_code, start_date)
        self.stdout.write(self.style.SUCCESS(f'Fetched {count} new article(s) from {label}'))
