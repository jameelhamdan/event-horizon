from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the walk-forward backtest (4 ablation arms) and print the headline table'

    def add_arguments(self, parser):
        parser.add_argument('--years', type=int, default=2,
                            help='Backtest span in years (default: 2)')
        parser.add_argument('--step-days', type=int, default=21,
                            help='Rolling-origin step in days (default: 21)')
        parser.add_argument('--output', type=str, default=None,
                            help='Path for the JSON report (default: ./forecast_backtest_<ts>.json)')

    def handle(self, *args, **kwargs):
        from services.forecasting.backtest import run_backtest

        report = run_backtest(
            years=kwargs['years'], step_days=kwargs['step_days'], output_path=kwargs['output'],
        )
        if 'error' in report:
            self.stdout.write(self.style.ERROR(report['error']))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Backtest: {report['n_origins']} origins, {report['years']}y, "
            f"step {report['step_days']}d"))
        for hkey, arms in report['results'].items():
            self.stdout.write(f'\n  Horizon {hkey}:')
            self.stdout.write(f'    {"arm":<26} {"n":>6} {"acc":>7} {"f1":>7} {"auc":>7} {"brier":>7}')
            for arm, m in arms.items():
                self.stdout.write(
                    f'    {arm:<26} {m.get("n", 0):>6} '
                    f'{_fmt(m.get("accuracy")):>7} {_fmt(m.get("f1_macro")):>7} '
                    f'{_fmt(m.get("roc_auc")):>7} {_fmt(m.get("brier")):>7}')
        self.stdout.write(self.style.SUCCESS(f"\nReport: {report.get('_output_path')}"))


def _fmt(v):
    return f'{v:.3f}' if isinstance(v, (int, float)) else '-'
