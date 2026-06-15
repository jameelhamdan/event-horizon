from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Train the v2 quantitative classifier (walk-forward) per (symbol, horizon)'

    def add_arguments(self, parser):
        parser.add_argument('--symbol', type=str, default=None,
                            help='Single symbol to train (default: all DEFAULT_SYMBOLS)')
        parser.add_argument('--horizon', type=int, default=None,
                            help='Single horizon in hours (1/24/168). Default: all applicable.')

    def handle(self, *args, **opts):
        from services.forecasting.model import train
        from services.forecasting.service import DEFAULT_SYMBOLS, _horizons_for

        if opts['symbol']:
            pairs = [(opts['symbol'], next(
                (sk for s, sk in DEFAULT_SYMBOLS if s == opts['symbol']), 'stock'))]
        else:
            pairs = DEFAULT_SYMBOLS

        for symbol, stream_key in pairs:
            horizons = ([(None, opts['horizon'])] if opts['horizon']
                        else _horizons_for(stream_key))
            for _label, hours in horizons:
                try:
                    result = train(symbol, hours)
                    self.stdout.write(self.style.SUCCESS(
                        f'{symbol} +{hours}h → {result}'))
                except RuntimeError as e:
                    self.stderr.write(self.style.ERROR(str(e)))
                    return
                except Exception as e:
                    self.stderr.write(self.style.WARNING(f'{symbol} +{hours}h failed: {e}'))
