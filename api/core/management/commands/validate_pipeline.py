"""Step-by-step pipeline validation + importance/price backtest.

Answers "is each pipeline field actually doing something real?" by comparing
known market-moving LANDMARK dates against random control dates and checking,
at each stage, whether the pipeline produced signal that matches what the
backfilled PriceBar data actually did:

  1. ingest    — do articles exist near the date?
  2. annotate  — are category / event_intensity / importance_score populated?
  3. aggregate — did an Event form near the date?
  4. route     — did the Event emit affected_indicators?
  5. reality   — did those (and the landmark's expected) symbols actually MOVE
                 in PriceBar around the date, and does importance/intensity
                 rank-correlate with the realized move + predict its direction?

A healthy pipeline shows landmark dates scoring materially higher than random
dates on intensity/importance AND on realized price move, positive rank
correlation between the two, and directional hit-rate > 0.5.

Needs Mongo with historical Events (run aggregate_history_task over the
backfill range first, or point DATABASE_URL at production) and PriceBar
history. Writes JSON to results/validate_pipeline/. See the pipeline-validate
skill for how to read the scorecard.
"""

import json
import random
import statistics
from datetime import datetime, timedelta, timezone as dt_tz

from django.core.management.base import BaseCommand

# Curated market-moving events, 2021–2026. `symbols` = indicators that SHOULD
# have moved (the ground-truth the router is implicitly graded against);
# `region`/`category` = what annotate/aggregate should have produced.
LANDMARK_EVENTS = [
    {'date': '2022-02-24', 'desc': 'Russia invades Ukraine', 'category': 'conflict', 'region': 'ukraine', 'symbols': ['CL=F', 'NG=F', 'ZW=F', 'GC=F', '^VIX']},
    {'date': '2023-03-10', 'desc': 'SVB collapse', 'category': 'economic', 'region': 'united states', 'symbols': ['SPY', '^VIX', 'GC=F']},
    {'date': '2023-10-07', 'desc': 'Hamas attack on Israel', 'category': 'conflict', 'region': 'israel', 'symbols': ['CL=F', 'GC=F', '^VIX']},
    {'date': '2021-01-06', 'desc': 'US Capitol riot', 'category': 'political', 'region': 'united states', 'symbols': ['^VIX', 'SPY', 'GC=F']},
    {'date': '2022-09-23', 'desc': 'UK mini-budget / gilt crisis', 'category': 'economic', 'region': 'united kingdom', 'symbols': ['^TNX', 'EURUSD=X', '^VIX']},
    {'date': '2024-08-05', 'desc': 'Yen carry-trade unwind crash', 'category': 'economic', 'region': 'japan', 'symbols': ['^VIX', 'SPY', '^N225']},
    {'date': '2023-05-01', 'desc': 'First Republic seized', 'category': 'economic', 'region': 'united states', 'symbols': ['SPY', '^VIX']},
    {'date': '2024-04-13', 'desc': 'Iran strikes Israel', 'category': 'conflict', 'region': 'iran', 'symbols': ['CL=F', 'GC=F', '^VIX']},
    {'date': '2022-06-15', 'desc': 'Fed 75bp hike', 'category': 'economic', 'region': 'united states', 'symbols': ['^TNX', 'SPY', 'DX-Y.NYB']},
    {'date': '2025-04-02', 'desc': 'Trump reciprocal tariffs', 'category': 'economic', 'region': 'united states', 'symbols': ['SPY', 'DX-Y.NYB', '^VIX', 'GC=F']},
]

_MOVE_HORIZONS = (1, 3, 5)          # trading-ish days after the event
_SIGMA_WINDOW = 60                  # days of history for the ±1σ significance bar
# Fixed reference basket for measuring "what did the market do" on any date even
# when no event routed symbols (esp. random control dates) — keeps the
# landmark-vs-random price comparison on comparable footing.
_CORE_BASKET = ['SPY', 'GC=F', 'CL=F', '^VIX', 'BTC-USD']


class Command(BaseCommand):
    help = 'Validate each pipeline stage + backtest importance/intensity against realized PriceBar moves.'

    def add_arguments(self, parser):
        parser.add_argument('--window-days', type=int, default=3, help='± days around a date to gather events')
        parser.add_argument('--random-samples', type=int, default=20, help='number of random control dates')
        parser.add_argument('--seed', type=int, default=0)

    def handle(self, *args, **opts):
        rng = random.Random(opts['seed'])
        window = opts['window_days']

        landmarks = [self._score_date(d['date'], d, window) for d in LANDMARK_EVENTS]
        randoms = [self._score_date(dt, None, window) for dt in self._random_dates(rng, opts['random_samples'])]

        report = {
            'generated_at': datetime.now(dt_tz.utc).isoformat(),
            'window_days': window,
            'coverage': self._coverage(landmarks),
            'discrimination': self._discrimination(landmarks, randoms),
            'correlation': self._correlation(landmarks + randoms),
            'directional_hit_rate': self._directional(landmarks + randoms),
            'landmarks': landmarks,
            'randoms': randoms,
        }
        from services.utils import results_dir
        out = results_dir('validate_pipeline') / f'validate_{datetime.now(dt_tz.utc):%Y%m%dT%H%M%SZ}.json'
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        self._print(report, out)

    # ── per-date scoring ──────────────────────────────────────────────────────

    def _score_date(self, date_str, landmark, window):
        from core.models import Article, Event

        day = datetime.fromisoformat(str(date_str)).replace(tzinfo=dt_tz.utc)
        lo, hi = day - timedelta(days=window), day + timedelta(days=window)

        articles = Article.objects.filter(published_on__gte=lo, published_on__lt=hi)
        events = list(Event.objects.filter(latest_article_at__gte=lo, latest_article_at__lt=hi))

        importances = [a.importance_score for a in articles if a.importance_score is not None]
        intensities = [e.avg_intensity for e in events if e.avg_intensity is not None]
        routed = [e for e in events if e.affected_indicators]

        # realized move: the landmark's expected symbols if known, else the union
        # of symbols the events actually routed to, else the fixed core basket so
        # the measurement is never empty (random dates have no events to derive
        # symbols from — they still get a market-move reading for comparison).
        symbols = landmark['symbols'] if landmark else sorted({i['symbol'] for e in routed for i in e.affected_indicators})
        if not symbols:
            symbols = _CORE_BASKET
        moves = {s: self._realized_move(s, day) for s in symbols}
        moves = {s: m for s, m in moves.items() if m is not None}
        max_move = max((abs(m['ret']) for m in moves.values()), default=None)

        # directional pairs: signed indicator weight vs realized signed return
        pairs = []
        for e in routed:
            for ind in e.affected_indicators:
                m = self._realized_move(ind['symbol'], day)
                if m is not None and ind.get('weight'):
                    pairs.append({'symbol': ind['symbol'], 'weight': ind['weight'], 'ret': m['ret']})

        return {
            'date': date_str,
            'desc': landmark['desc'] if landmark else 'random',
            'is_landmark': landmark is not None,
            'n_articles': articles.count(),
            'n_events': len(events),
            'n_routed': len(routed),
            'max_importance': max(importances, default=None),
            'max_intensity': max(intensities, default=None),
            'max_abs_move': max_move,
            'moves': moves,
            'dir_pairs': pairs,
        }

    def _realized_move(self, symbol, day):
        """Signed close-to-close return from the last bar on/before `day` to the
        first bar ≥ its horizon, plus whether it cleared ±1σ of recent history."""
        from core.models import PriceBar

        before = PriceBar.objects.filter(symbol=symbol, date__lte=day).order_by('-date').first()
        if before is None or not before.close:
            return None
        best = None
        for h in _MOVE_HORIZONS:
            after = (PriceBar.objects.filter(symbol=symbol, date__gt=day, date__lte=day + timedelta(days=h + 3))
                     .order_by('date').first())
            if after and after.close:
                ret = (after.close - before.close) / before.close
                if best is None or abs(ret) > abs(best):
                    best = ret
        if best is None:
            return None
        hist = list(PriceBar.objects.filter(
            symbol=symbol, date__gt=day - timedelta(days=_SIGMA_WINDOW), date__lte=day,
        ).order_by('date').values_list('close', flat=True))
        sigma = self._daily_sigma(hist)
        return {'ret': round(best, 5), 'significant': bool(sigma and abs(best) > sigma)}

    @staticmethod
    def _daily_sigma(closes):
        rets = [(b - a) / a for a, b in zip(closes, closes[1:]) if a]
        return statistics.pstdev(rets) if len(rets) > 2 else None

    # ── aggregate metrics ─────────────────────────────────────────────────────

    @staticmethod
    def _coverage(landmarks):
        n = len(landmarks) or 1
        return {
            'landmarks': len(landmarks),
            'with_articles': round(sum(bool(l['n_articles']) for l in landmarks) / n, 3),
            'with_events': round(sum(bool(l['n_events']) for l in landmarks) / n, 3),
            'with_importance': round(sum(l['max_importance'] is not None for l in landmarks) / n, 3),
            'with_routed': round(sum(bool(l['n_routed']) for l in landmarks) / n, 3),
            'with_price_move': round(sum(l['max_abs_move'] is not None for l in landmarks) / n, 3),
        }

    @staticmethod
    def _mean(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.mean(xs), 4) if xs else None

    def _discrimination(self, landmarks, randoms):
        return {
            'intensity_landmark': self._mean([l['max_intensity'] for l in landmarks]),
            'intensity_random': self._mean([r['max_intensity'] for r in randoms]),
            'importance_landmark': self._mean([l['max_importance'] for l in landmarks]),
            'importance_random': self._mean([r['max_importance'] for r in randoms]),
            'move_landmark': self._mean([l['max_abs_move'] for l in landmarks]),
            'move_random': self._mean([r['max_abs_move'] for r in randoms]),
        }

    @staticmethod
    def _correlation(rows):
        """Spearman rank corr between max intensity and realized max abs move."""
        pts = [(r['max_intensity'], r['max_abs_move']) for r in rows
               if r['max_intensity'] is not None and r['max_abs_move'] is not None]
        if len(pts) < 4:
            return {'spearman_intensity_move': None, 'n': len(pts)}

        def ranks(vals):
            order = sorted(range(len(vals)), key=lambda i: vals[i])
            rk = [0.0] * len(vals)
            for pos, i in enumerate(order):
                rk[i] = pos
            return rk
        xr, yr = ranks([p[0] for p in pts]), ranks([p[1] for p in pts])
        n = len(pts)
        d2 = sum((a - b) ** 2 for a, b in zip(xr, yr))
        rho = 1 - (6 * d2) / (n * (n * n - 1))
        return {'spearman_intensity_move': round(rho, 3), 'n': n}

    @staticmethod
    def _directional(rows):
        pairs = [p for r in rows for p in r['dir_pairs']]
        hits = sum(1 for p in pairs if (p['weight'] >= 0) == (p['ret'] >= 0))
        return {'hit_rate': round(hits / len(pairs), 3) if pairs else None, 'n_pairs': len(pairs)}

    # ── helpers / output ──────────────────────────────────────────────────────

    @staticmethod
    def _random_dates(rng, k):
        start = datetime(2021, 1, 1, tzinfo=dt_tz.utc)
        span = (datetime(2026, 1, 1, tzinfo=dt_tz.utc) - start).days
        return [(start + timedelta(days=rng.randrange(span))).strftime('%Y-%m-%d') for _ in range(k)]

    def _print(self, report, out):
        c, d = report['coverage'], report['discrimination']
        self.stdout.write('\n─ Stage coverage on landmark dates ─')
        for k in ('with_articles', 'with_events', 'with_importance', 'with_routed', 'with_price_move'):
            self.stdout.write(f'  {k:<18} {c[k]:.0%}')
        self.stdout.write('\n─ Landmark vs random (higher on landmark = signal is real) ─')
        for label, lk, rk in (('intensity', 'intensity_landmark', 'intensity_random'),
                              ('importance', 'importance_landmark', 'importance_random'),
                              ('abs price move', 'move_landmark', 'move_random')):
            self.stdout.write(f'  {label:<15} landmark={d[lk]}  random={d[rk]}')
        self.stdout.write(f"\n  Spearman(intensity, |move|) = {report['correlation']['spearman_intensity_move']} (n={report['correlation']['n']})")
        hr = report['directional_hit_rate']
        self.stdout.write(f"  Directional hit-rate = {hr['hit_rate']} (n_pairs={hr['n_pairs']})")
        self.stdout.write(self.style.SUCCESS(f'\nreport -> {out}'))
