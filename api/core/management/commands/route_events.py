from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Route recent events to market symbols (LLMEventRouter, rules fallback)'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=168,
                            help='Lookback window in hours (default: 168 = 7 days)')
        parser.add_argument('--router', choices=['llm', 'rules'], default=None,
                            help='Routing source (default: settings.FORECAST_ROUTER)')
        parser.add_argument('--background', action='store_true',
                            help='Enqueue as a background RQ task instead of running directly')

    def handle(self, *args, **kwargs):
        from services.tasks import route_events_task

        task_kwargs = dict(hours=kwargs['hours'], source=kwargs['router'])
        if kwargs['background']:
            from services.queue import enqueue
            enqueue(route_events_task, queue='heavy', **task_kwargs)
            self.stdout.write(self.style.SUCCESS('Enqueued route_events_task'))
            return

        updated = route_events_task(**task_kwargs)
        self.stdout.write(self.style.SUCCESS(f'Routed {updated} event(s)'))
