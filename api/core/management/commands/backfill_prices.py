from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Backfill daily OHLC PriceBar history for the indicator panel (yfinance + CoinGecko)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--symbols', type=str, default='',
            help='Comma-separated symbols (default: all panel symbols)',
        )
        parser.add_argument(
            '--years', type=int, default=10,
            help='How many years of history to fetch (default: 10)',
        )
        parser.add_argument(
            '--full', action='store_true',
            help='Re-pull the whole window instead of only the missing tail '
                 '(incremental is the default)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Fetch and report counts without writing to the database',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background Celery task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        from services.forecasting.history import backfill_all

        symbols = [s.strip() for s in kwargs['symbols'].split(',') if s.strip()] or None

        if kwargs['background']:
            from services.queue import enqueue
            from services.tasks import backfill_prices_task
            enqueue(
                backfill_prices_task,
                symbols=symbols, years=kwargs['years'], full=kwargs['full'],
                queue='bulk', job_timeout=-1,
            )
            self.stdout.write(self.style.SUCCESS('Enqueued backfill_prices_task'))
            return

        results = backfill_all(
            symbols=symbols, years=kwargs['years'], dry_run=kwargs['dry_run'], full=kwargs['full'],
        )
        total = sum(results.values())
        for sym, n in results.items():
            self.stdout.write(f'  {sym}: {n} new bars')
        verb = 'would insert' if kwargs['dry_run'] else 'inserted'
        self.stdout.write(self.style.SUCCESS(f'Backfill complete: {verb} {total} bars'))
