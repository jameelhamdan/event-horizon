from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Backfill daily OHLC PriceBar history for the indicator panel (yfinance + CoinGecko)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--symbols', type=str, default='',
            help='Comma-separated symbols (default: all panel symbols)',
        )
        parser.add_argument(
            '--years', type=int, default=5,
            help='How many years of history to fetch (default: 5)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Fetch and report counts without writing to the database',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background RQ task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        from services.forecasting.history import backfill_all

        symbols = [s.strip() for s in kwargs['symbols'].split(',') if s.strip()] or None

        if kwargs['background']:
            from services.queue import enqueue
            from services.tasks import backfill_prices_task
            enqueue(backfill_prices_task, symbols=symbols, years=kwargs['years'], queue='default')
            self.stdout.write(self.style.SUCCESS('Enqueued backfill_prices_task'))
            return

        results = backfill_all(symbols=symbols, years=kwargs['years'], dry_run=kwargs['dry_run'])
        total = sum(results.values())
        for sym, n in results.items():
            self.stdout.write(f'  {sym}: {n} new bars')
        verb = 'would insert' if kwargs['dry_run'] else 'inserted'
        self.stdout.write(self.style.SUCCESS(f'Backfill complete: {verb} {total} bars'))
