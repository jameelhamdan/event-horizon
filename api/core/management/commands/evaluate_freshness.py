import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Preliminary capstone eval: fetch-to-map latency percentiles — time from an '
            'article being fetched to its event first appearing (Event.created_on)')

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Look-back window in days (default 30)')
        parser.add_argument('--output', type=str, default=None,
                            help='Report path (default <repo>/eval/freshness_report.json)')

    def handle(self, *args, **kwargs):
        import numpy as np
        from core.models import Article, Event

        cutoff = datetime.now(timezone.utc) - timedelta(days=kwargs['days'])
        events = list(Event.objects.filter(created_on__gte=cutoff)
                      .values('created_on', 'article_ids'))

        wanted = set()
        for ev in events:
            for aid in (ev['article_ids'] or []):
                wanted.add(aid)
        fetched_at = {}
        ids = [uuid.UUID(a) for a in wanted]
        for i in range(0, len(ids), 500):
            for row in Article.objects.filter(id__in=ids[i:i + 500]).values('id', 'created_on'):
                fetched_at[str(row['id'])] = row['created_on']

        # Per-article latency against the event's first appearance. Articles merged
        # into an already-existing event fetch *after* created_on — those are event
        # growth, not pipeline latency, so negatives are dropped.
        latencies = []
        for ev in events:
            for aid in (ev['article_ids'] or []):
                ts = fetched_at.get(aid)
                if ts is None:
                    continue
                minutes = (ev['created_on'] - ts).total_seconds() / 60
                if minutes >= 0:
                    latencies.append(minutes)

        if not latencies:
            self.stdout.write(self.style.WARNING('No event/article pairs in window'))
            return

        arr = np.asarray(latencies)
        report = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'window_days': kwargs['days'],
            'n_events': len(events),
            'n_article_event_pairs': int(len(arr)),
            'latency_minutes': {
                'p50': round(float(np.percentile(arr, 50)), 1),
                'p95': round(float(np.percentile(arr, 95)), 1),
                'p99': round(float(np.percentile(arr, 99)), 1),
                'mean': round(float(arr.mean()), 1),
            },
            'definition': 'Article.created_on (fetch) -> Event.created_on (first on map); '
                          'articles merged into pre-existing events excluded',
        }

        output = kwargs['output']
        if output is None:
            eval_dir = os.path.join(str(settings.BASE_DIR), 'eval')
            os.makedirs(eval_dir, exist_ok=True)
            output = os.path.join(eval_dir, 'freshness_report.json')
        with open(output, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, indent=2)

        lat = report['latency_minutes']
        self.stdout.write(f"Fetch->map latency over {kwargs['days']}d "
                          f"({report['n_article_event_pairs']} pairs, {report['n_events']} events): "
                          f"P50={lat['p50']}m P95={lat['p95']}m P99={lat['p99']}m")
        self.stdout.write(self.style.SUCCESS(f'Report -> {output}'))
