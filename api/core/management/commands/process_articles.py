from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Run the on-prem NLP annotate stage (classification + geo + sentiment + importance) on fetched articles'

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
            help='Enqueue as a background Celery task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        if kwargs['background']:
            from services.queue import enqueue
            from services.tasks import dispatch_stage_task
            enqueue(dispatch_stage_task, 'annotate', queue='default')
            self.stdout.write(self.style.SUCCESS('Enqueued annotate stage dispatch'))
            return

        source_code = kwargs.get('source_code')
        limit = kwargs['limit']

        if kwargs['reprocess']:
            # Deliberate re-run over already-processed rows — the only selection
            # the stage predicate can't express (it selects pending work only).
            from core.models import Article
            qs = Article.objects.all()
            if source_code:
                qs = qs.filter(source_code=source_code)
            ids = list(qs.values_list('id', flat=True)[:limit])
        else:
            # Same predicate the pipeline dispatcher uses (services/stages.py).
            from services.stages import select_ids
            ids = select_ids('annotate', limit, source_code=source_code)

        from services.workflow import annotate_articles
        count = annotate_articles(ids=ids) if ids else 0
        self.stdout.write(self.style.SUCCESS(f'Annotated {count} articles'))
