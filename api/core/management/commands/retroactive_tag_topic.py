from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Retroactively tag historical events with a single topic slug'

    def add_arguments(self, parser):
        parser.add_argument(
            'slug',
            help='The Topic slug to retroactively apply',
        )
        parser.add_argument(
            '--hours', type=int, default=72,
            help='Lookback window in hours (default: 72)',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background Celery task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        from services.tasks import retroactive_tag_topic_task

        task_kwargs = dict(slug=kwargs['slug'], lookback_hours=kwargs['hours'])

        if kwargs['background']:
            from services.queue import enqueue
            enqueue(retroactive_tag_topic_task, **task_kwargs)
            self.stdout.write(self.style.SUCCESS(
                f"Enqueued retroactive_tag_topic_task for '{kwargs['slug']}'"
            ))
            return

        tagged = retroactive_tag_topic_task(**task_kwargs)
        self.stdout.write(self.style.SUCCESS(
            f"Done: {tagged} event(s) tagged with '{kwargs['slug']}'"
        ))
