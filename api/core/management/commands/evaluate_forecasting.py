from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Preliminary capstone eval: routing Precision@k vs realized ±1σ moves, '
            'and walk-forward 1d-return MAE vs the zero-return baseline')

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=365, help='Evaluation window in days (default 365)')
        parser.add_argument('--top-k', type=int, default=3, help='Top-k routed symbols to score (default 3)')
        parser.add_argument('--step-days', type=int, default=30, help='Walk-forward fold size in days (default 30)')
        parser.add_argument('--output', type=str, default=None,
                            help='Report path (default <repo>/results/evaluate_forecasting/forecasting_report.json)')

    def handle(self, *args, **kwargs):
        from services.forecasting.evaluate import run_evaluation

        report = run_evaluation(days=kwargs['days'], top_k=kwargs['top_k'],
                                step_days=kwargs['step_days'], output_path=kwargs['output'])

        r = report['routing_precision']
        self.stdout.write(f"Routing: precision@{r['top_k']}={r['precision_at_k']} "
                          f"(random={r['random_baseline']}, events={r['n_events']}, "
                          f"panel={r['n_panel_symbols']} symbols)")
        m = report['return_mae']
        if 'error' in m:
            self.stdout.write(self.style.WARNING(f"MAE: {m['error']}"))
        else:
            self.stdout.write(f"Return MAE: model={m['mae_model']} vs zero={m['mae_zero_baseline']} "
                              f"({m['improvement_pct']}% better, {m['n_folds']} folds, "
                              f"{m['n_predictions']} predictions)")
        self.stdout.write(self.style.SUCCESS(f"Report -> {report['_output_path']}"))
