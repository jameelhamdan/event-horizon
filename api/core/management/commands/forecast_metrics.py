import json

from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Print the two-head forecast evaluation report (vs naive baselines)'

    def add_arguments(self, parser):
        parser.add_argument('--symbol', type=str, default=None)
        parser.add_argument('--horizon', type=int, default=None)

    def handle(self, *args, **opts):
        from services.forecasting.metrics import evaluate_forecasts

        report = evaluate_forecasts(symbol=opts['symbol'], horizon_hours=opts['horizon'])
        if not report:
            self.stdout.write(self.style.WARNING('No scored forecasts yet.'))
            return
        self.stdout.write(json.dumps(report, indent=2))
