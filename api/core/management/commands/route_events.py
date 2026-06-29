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
        task_kwargs = dict(hours=kwargs['hours'], source=kwargs['router'])
        if kwargs['background']:
            from services.queue import enqueue
            from services.tasks import dispatch_route_events_task
            enqueue(dispatch_route_events_task, **task_kwargs, queue='default')
            self.stdout.write(self.style.SUCCESS('Enqueued dispatch_route_events_task'))
            return

        from datetime import datetime, timedelta, timezone as dt_timezone
        from django.conf import settings
        from core import models as core_models
        from services.routing import route_events

        src = kwargs['router'] or settings.FORECAST_ROUTER
        start = datetime.now(dt_timezone.utc) - timedelta(hours=kwargs['hours'])
        events = list(core_models.Event.objects.filter(started_at__gte=start))
        updated = route_events(events, source=src)
        self.stdout.write(self.style.SUCCESS(f'Routed {updated} event(s)'))
