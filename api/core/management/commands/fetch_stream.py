from core.management.base import BaseTaskCommand
from services.streams import STREAM_CLASSES


class Command(BaseTaskCommand):
    help = 'Fetch a named data stream (prices, notam, earthquakes, forex)'

    def add_arguments(self, parser):
        parser.add_argument(
            'stream',
            type=str,
            choices=list(STREAM_CLASSES.keys()),
            help=f'Stream to run: {", ".join(STREAM_CLASSES.keys())}',
        )
        parser.add_argument(
            '--background',
            action='store_true',
            help='Enqueue as a background Celery task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        from services.streams import run_stream
        from services.tasks import (
            fetch_forex_task,
            fetch_prices_task,
            fetch_notams_task,
            fetch_earthquakes_task,
        )

        stream = kwargs['stream']

        if kwargs['background']:
            from services.queue import enqueue
            job_map = {
                'forex':       fetch_forex_task,
                'prices':      fetch_prices_task,
                'notam':       fetch_notams_task,
                'earthquakes': fetch_earthquakes_task,
            }
            enqueue(job_map[stream])
            self.stdout.write(self.style.SUCCESS(f'Enqueued stream: {stream}'))
            return

        self.stdout.write(f'Running stream: {stream}')
        count = run_stream(stream)
        self.stdout.write(self.style.SUCCESS(f'Stream "{stream}" saved {count} record(s)'))
