from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Train the LightGBM forecasting models (classifier + regressor) for all horizons'

    def add_arguments(self, parser):
        parser.add_argument('--background', action='store_true',
                            help='Enqueue as a background RQ task instead of running directly')

    def handle(self, *args, **kwargs):
        from services.tasks import train_forecast_model_task

        if kwargs['background']:
            from services.queue import enqueue
            enqueue(train_forecast_model_task, queue='bulk', job_timeout=-1)
            self.stdout.write(self.style.SUCCESS('Enqueued train_forecast_model_task'))
            return

        trained = train_forecast_model_task()
        if trained:
            self.stdout.write(self.style.SUCCESS(f'Trained models for {trained} horizon(s)'))
        else:
            self.stdout.write(self.style.WARNING(
                'No models trained — run backfill_prices first, or check FORECAST_ENABLED'))
