from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Scrape Wikipedia Current Events and upsert Topic objects'

    def add_arguments(self, parser):
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background Celery task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        from services.tasks import refresh_topics_task

        if kwargs['background']:
            from services.queue import enqueue
            enqueue(refresh_topics_task)
            self.stdout.write(self.style.SUCCESS('Enqueued refresh_topics_task'))
            return

        count = refresh_topics_task()
        self.stdout.write(self.style.SUCCESS(f'Topics refreshed: {count} active topic(s)'))
