"""End-to-end forecasting flow runner — backfill → route → train → run → score → backtest.

Mirrors ``e2e_pipeline`` for the prediction layer: runs each stage in order, reports per-stage
counts/ok flags, and writes a JSON report for manual inspection. Use ``--skip-*`` to re-run a
subset (e.g. skip the slow backfill once PriceBar is seeded).

    python manage.py forecast_e2e --years 3 --backtest
    python manage.py forecast_e2e --skip-backfill --skip-route   # train+run+score only
"""
import json
from datetime import datetime, timezone

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the forecasting flow end-to-end and write a JSON report'

    def add_arguments(self, parser):
        parser.add_argument('--symbols', type=str, default='', help='Comma-separated (default: all panel)')
        parser.add_argument('--years', type=int, default=3, help='Backfill years (default: 3)')
        parser.add_argument('--route-hours', type=int, default=720, help='Event routing lookback hours')
        parser.add_argument('--backtest', action='store_true', help='Also run the walk-forward backtest')
        parser.add_argument('--skip-backfill', action='store_true')
        parser.add_argument('--skip-route', action='store_true')
        parser.add_argument('--skip-train', action='store_true')
        parser.add_argument('--output', type=str, default=None)

    def handle(self, *args, **opts):
        from datetime import timedelta, timezone as dt_timezone
        from core import models as core_models
        from services.tasks import (
            backfill_prices_task,
            train_forecast_model_task, run_forecast_task, score_forecasts_task,
        )
        from services.routing import route_events as _route_events

        def _route_events_direct(hours):
            start = datetime.now(dt_timezone.utc) - timedelta(hours=hours)
            events = list(core_models.Event.objects.filter(started_at__gte=start))
            return _route_events(events)

        symbols = [s.strip() for s in opts['symbols'].split(',') if s.strip()] or None
        report = {'started_at': datetime.now(timezone.utc).isoformat(), 'steps': {}}

        def step(name, fn):
            self.stdout.write(f'-> {name} ...')
            try:
                result = fn()
                report['steps'][name] = {'ok': True, 'result': result}
                self.stdout.write(self.style.SUCCESS(f'  {name}: {result}'))
            except Exception as exc:  # noqa: BLE001
                report['steps'][name] = {'ok': False, 'error': str(exc)}
                self.stdout.write(self.style.ERROR(f'  {name} FAILED: {exc}'))

        if not opts['skip_backfill']:
            step('backfill', lambda: backfill_prices_task(symbols=symbols, years=opts['years']))
        if not opts['skip_route']:
            step('route_events', lambda: _route_events_direct(hours=opts['route_hours']))
        if not opts['skip_train']:
            step('train', train_forecast_model_task)
        step('run_forecast', run_forecast_task)
        step('score', score_forecasts_task)

        # Snapshot the current state for the report.
        report['counts'] = {
            'price_bars': core_models.PriceBar.objects.count(),
            'forecasts': core_models.Forecast.objects.count(),
            'forecasts_scored': core_models.Forecast.objects.filter(is_correct__isnull=False).count(),
        }
        sample = list(core_models.Forecast.objects.values(
            'symbol', 'horizon_days', 'direction', 'proba_up', 'predicted_change_pct',
        )[:10])
        report['sample_forecasts'] = sample

        if opts['backtest']:
            from services.forecasting.backtest import run_backtest
            step('backtest', lambda: run_backtest(symbols=symbols, years=min(opts['years'], 2)).get('results'))

        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        from services.utils import results_dir
        out = opts['output'] or str(results_dir('forecast_e2e') / f'forecast_e2e_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")}.json')
        with open(out, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, indent=2, default=str)
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. bars={report['counts']['price_bars']} "
            f"forecasts={report['counts']['forecasts']} → report: {out}"))
