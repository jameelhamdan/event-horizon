from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Generate Forecast rows for the indicator panel from the trained models'

    def add_arguments(self, parser):
        parser.add_argument('--background', action='store_true',
                            help='Enqueue as a background Celery task instead of running directly')

    def handle(self, *args, **kwargs):
        from services.tasks import run_forecast_task

        if kwargs['background']:
            from services.queue import enqueue
            enqueue(run_forecast_task, queue='heavy')
            self.stdout.write(self.style.SUCCESS('Enqueued run_forecast_task'))
            return

        created = run_forecast_task()
        self.stdout.write(self.style.SUCCESS(f'Created {created} forecast(s)'))
