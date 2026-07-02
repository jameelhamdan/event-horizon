from django.core.management.base import BaseCommand

from newsletter.tasks import generate_newsletter_task


class Command(BaseCommand):
    help = 'Generate the daily newsletter for a given date (or today).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            type=str,
            default=None,
            help='Date to generate newsletter for (YYYY-MM-DD). Defaults to today.',
        )
        parser.add_argument(
            '--background',
            action='store_true',
            help='Enqueue as a background Celery task instead of running in the foreground.',
        )

    def handle(self, *args, **options):
        date_str = options.get('date')
        if options['background']:
            from services.queue import enqueue
            enqueue(generate_newsletter_task, date_str=date_str)
            self.stdout.write(self.style.SUCCESS(f'Enqueued newsletter generation for {date_str or "today"}.'))
        else:
            result = generate_newsletter_task(date_str=date_str)
            self.stdout.write(self.style.SUCCESS(result))
