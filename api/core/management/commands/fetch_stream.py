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

        stream = kwargs['stream']

        if kwargs['background']:
            from services.queue import enqueue
            from services.tasks import run_stream_task
            enqueue(run_stream_task, stream)
            self.stdout.write(self.style.SUCCESS(f'Enqueued stream: {stream}'))
            return

        self.stdout.write(f'Running stream: {stream}')
        count = run_stream(stream)
        self.stdout.write(self.style.SUCCESS(f'Stream "{stream}" saved {count} record(s)'))
