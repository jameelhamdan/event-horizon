from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Run NLP pipeline (LLM analysis + FinBERT + categorization) on unprocessed articles'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-code', type=str, default=None,
            help='Restrict to articles from a specific source',
        )
        parser.add_argument(
            '--limit', type=int, default=500,
            help='Max articles to process per run (default: 500)',
        )
        parser.add_argument(
            '--reprocess', action='store_true', default=False,
            help='Re-process already-processed articles',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background RQ task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        task_kwargs = dict(
            limit=kwargs['limit'],
            source_code=kwargs.get('source_code'),
            reprocess=kwargs['reprocess'],
        )

        if kwargs['background']:
            from services.queue import enqueue
            from services.tasks import dispatch_process_articles_task
            enqueue(dispatch_process_articles_task, limit=task_kwargs['limit'], queue='default')
            self.stdout.write(self.style.SUCCESS('Enqueued dispatch_process_articles_task'))
            return

        from services.workflow import Workflow
        count = Workflow.process_articles(**task_kwargs)
        self.stdout.write(self.style.SUCCESS(f'Processed {count} articles'))
