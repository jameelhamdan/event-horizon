from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Aggregate processed articles into Events grouped by location and date'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours', type=int, default=24,
            help='Lookback window in hours (default: 24)',
        )
        parser.add_argument(
            '--min-articles', type=int, default=1,
            help='Minimum articles required to create an event (default: 1)',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background Celery task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        from services.tasks import aggregate_events_task

        task_kwargs = dict(hours=kwargs['hours'], min_articles=kwargs['min_articles'])

        if kwargs['background']:
            from services.queue import enqueue
            enqueue(aggregate_events_task, **task_kwargs)
            self.stdout.write(self.style.SUCCESS('Enqueued aggregate_events_task'))
            return

        created, updated = aggregate_events_task(**task_kwargs)
        self.stdout.write(self.style.SUCCESS(
            f'Aggregation complete: {created} created, {updated} updated'
        ))
