"""Annotate a sample of articles with the two-stage analyzer and write a report
for human/Claude review — the data side of the .claude/skills/analyzer-eval
skill.

Two sample modes:
  live (default)  — fetch fresh items straight from enabled RSS sources
                    (in memory, nothing saved to the database)
  --from-db       — most recent stored articles instead (works offline)

Runs the on-prem annotate pass (services/processing/annotator.py) over every
sample article; with --refine, low-confidence articles are additionally judged
by the configured refine provider (settings.REFINE_PROVIDER — override per-run
with the env var). Writes JSON to results/eval_analyzer/ and prints a compact table.
"""

import json
from datetime import datetime, timedelta, timezone as dt_tz
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the two-stage analyzer over sample articles and write an eval report (see .claude/skills/analyzer-eval).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=24, help='max articles to classify')
        parser.add_argument('--source', help='restrict to one source code')
        parser.add_argument('--from-db', action='store_true', help='sample recent stored articles instead of fetching live RSS')
        parser.add_argument('--refine', action='store_true', help='also judge low-confidence articles with the configured REFINE_PROVIDER')

    def handle(self, *args, **options):
        from core.models import ArticleDocument
        from services.processing.annotator import ESCALATE_BELOW, NLPAnnotator

        limit = options['limit']
        items = (
            self._sample_db(limit, options.get('source'))
            if options['from_db']
            else self._sample_live(limit, options.get('source'))
        )
        if not items:
            self.stderr.write('No sample articles found.')
            return

        docs = [
            ArticleDocument(id=str(i), title=title, content=content, source_code=source, published_on='')
            for i, (source, title, content) in enumerate(items)
        ]
        features = NLPAnnotator().annotate_batch(docs, lite_flags=True)
        # Mirrors annotate_articles' own stage decision (services/workflow/
        # articles.py) so the report reflects what the live pipeline would do.
        stages = [
            'fetched' if f.llm_error is not None
            else 'annotated' if f.confidence >= ESCALATE_BELOW
            else 'refine'
            for f in features
        ]

        refine_provider = None
        verdicts = [None] * len(items)
        if options['refine']:
            from services.processing.refiner import LLMRefiner
            refiner = LLMRefiner()
            refine_provider = refiner.provider
            flagged = [i for i, stage in enumerate(stages) if stage == 'refine']
            judged = refiner.judge([(items[i][1], items[i][2]) for i in flagged])
            for i, verdict in zip(flagged, judged):
                verdicts[i] = verdict

        rows = []
        for (source_code, title, content), f, stage, verdict in zip(items, features, stages, verdicts):
            category, sub = f.category, f.sub_category
            refined_by = None
            if verdict:
                category, sub = verdict['category'], verdict['sub_category']
                refined_by = verdict['provider']
            rows.append({
                'source': source_code,
                'title': title,
                'content_lead': ' '.join((content or '').split())[:300],
                'category': category,
                'sub_category': sub,
                'country': (f.llm_data or {}).get('country'),
                'city': (f.llm_data or {}).get('city'),
                'located': f.latitude is not None,
                'stage': 'refined' if verdict else stage,
                'confidence': f.confidence,
                'refined_by': refined_by,
                'intensity': f.event_intensity,
                'summary': (f.translations.get('en') or {}).get('summary'),
                'error': f.llm_error,
            })

        flagged_n = sum(1 for r in rows if r['stage'] in ('refine', 'refined'))
        report = {
            'generated_at': datetime.now(dt_tz.utc).isoformat(),
            'refine_provider': refine_provider,
            'mode': 'db' if options['from_db'] else 'live',
            'count': len(rows),
            'located_fraction': round(sum(r['located'] for r in rows) / len(rows), 3),
            'flagged_for_refine': flagged_n,
            'articles': rows,
        }
        from services.utils import results_dir
        out_path = results_dir('eval_analyzer') / f'analyzer_eval_{datetime.now(dt_tz.utc):%Y%m%dT%H%M%SZ}.json'
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

        for r in rows:
            place = ', '.join(filter(None, [r['city'], r['country']])) or '?'
            cat = '/'.join(filter(None, [r['category'], r['sub_category']]))
            judge = f' ←{r["refined_by"]}' if r['refined_by'] else (' [refine]' if r['stage'] == 'refine' else '')
            self.stdout.write(f'{r["intensity"]:.2f}  {cat:<28} {place:<24} {r["title"][:66]}{judge}')
        refined_n = sum(1 for r in rows if r['refined_by'])
        self.stdout.write(self.style.SUCCESS(
            f'\n{len(rows)} article(s), located={report["located_fraction"]:.0%}, '
            f'flagged={flagged_n}, refined={refined_n}'
            f'{f" via {refine_provider}" if refine_provider else ""} → {out_path}'
        ))

    # ── sampling ─────────────────────────────────────────────────────────────

    def _sample_live(self, limit: int, source_code: str | None) -> list[tuple[str, str, str]]:
        """(source_code, title, content) fresh from enabled RSS feeds — spread
        evenly across sources, nothing persisted."""
        from core.models import Source
        from services.data.rss import RSSService

        qs = Source.objects.filter(is_enabled=True)
        if source_code:
            qs = qs.filter(code=source_code)
        sources = list(qs)
        if not sources:
            return []

        per_source = max(1, limit // len(sources))
        since = datetime.now(dt_tz.utc) - timedelta(hours=24)
        items: list[tuple[str, str, str]] = []
        for source in sources:
            try:
                for datum in RSSService(source).fetch_data(since):
                    items.append((source.code, datum['title'], datum.get('content') or ''))
                    if len([i for i in items if i[0] == source.code]) >= per_source:
                        break
            except Exception as exc:
                self.stderr.write(f'[{source.code}] fetch failed: {exc}')
            if len(items) >= limit:
                break
        return items[:limit]

    def _sample_db(self, limit: int, source_code: str | None) -> list[tuple[str, str, str]]:
        from core.models import Article

        qs = Article.objects.order_by('-created_on')
        if source_code:
            qs = qs.filter(source_code=source_code)
        return [(a.source_code, a.title, a.content or '') for a in qs[:limit]]
