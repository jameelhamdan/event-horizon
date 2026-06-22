"""One-shot, idempotent data-consistency pass — run BEFORE the historical backfill.

Reconciles pre-redesign data so the backfill doesn't amplify inconsistencies into the
event/topic layers (feature-roadmap §5). Safe to re-run; always `--dry-run` first.

  N1  legacy categories       protest→political, crime→conflict (Article/Event/Topic)
  N2  location canonicalize    USA/US→United States, Kiev→Kyiv, … (Article/Event) + dup report
  N3  null-fill                Event.latest_article_at, Event.affected_indicators
  N5  sub-category spelling    underscore variants → canonical hyphen form
  N4  translations             reports articles missing AR (LLM retry is out of scope here)
"""
from __future__ import annotations

import re
import uuid
from datetime import timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.db.models import Max

from core import models as core_models


# ── N1: legacy top-level categories ────────────────────────────────────────────
LEGACY_CATEGORY_MAP = {'protest': 'political', 'crime': 'conflict'}

# ── N2: location-string canonicalization (whole-word, case-insensitive) ─────────
LOCATION_CANON = {
    'usa': 'United States', 'u.s.': 'United States', 'u.s.a.': 'United States',
    'us': 'United States', 'united states of america': 'United States',
    'uk': 'United Kingdom', 'u.k.': 'United Kingdom', 'britain': 'United Kingdom',
    'kiev': 'Kyiv', 'peking': 'Beijing', 'bombay': 'Mumbai', 'calcutta': 'Kolkata',
}

# ── N5: canonical sub-category spelling (hyphen, per analyzer._SUB_CATEGORIES) ──
CANONICAL_SUBCATS = {
    'war', 'airstrike', 'insurgency', 'terrorism', 'border-clash',
    'earthquake', 'flood', 'storm', 'wildfire', 'industrial-accident',
    'monetary-policy', 'energy', 'trade', 'tariffs', 'labor', 'markets', 'sanctions',
    'election', 'legislation', 'diplomacy', 'leadership-change', 'protest-policy',
    'outbreak', 'pandemic', 'healthcare-system', 'other',
}
# underscore variant → canonical hyphen form
SUBCAT_SPELLING = {s.replace('-', '_'): s for s in CANONICAL_SUBCATS if '-' in s}


def _canon_location(value: str) -> str:
    """Replace whole-word location aliases; preserve the rest of the string."""
    if not value:
        return value
    out = value
    for alias, canon in LOCATION_CANON.items():
        out = re.sub(rf'(?<![\w.]){re.escape(alias)}(?![\w])', canon, out, flags=re.IGNORECASE)
    return out


class Command(BaseCommand):
    help = 'Pre-backfill data normalization (N1-N5). Idempotent. Use --dry-run first.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without writing.')
        parser.add_argument('--merge-dups', action='store_true',
                            help='N2: merge duplicate events (same canonical location/category/day). '
                                 'Destructive — off by default.')
        parser.add_argument('--retry-translations', action='store_true',
                            help='N4: re-run the LLM analyzer to fill missing AR translations.')
        parser.add_argument('--limit', type=int, default=200,
                            help='Max articles to re-translate in one run (--retry-translations).')

    def handle(self, *args, **opts):
        self.dry = opts['dry_run']
        self.merge = opts['merge_dups']
        self.retry_tr = opts['retry_translations']
        self.limit = opts['limit']
        mode = 'DRY-RUN' if self.dry else 'APPLY'
        self.stdout.write(self.style.WARNING(f'normalize_data [{mode}]'))

        self._n1_categories()
        self._n2_locations()
        self._n3_null_fill()
        self._n5_subcategories()
        self._n4_translations()

        self.stdout.write(self.style.SUCCESS(
            f'\nDone [{mode}].' + ('' if not self.dry else ' Re-run without --dry-run to apply.')))

    # ── N1 ─────────────────────────────────────────────────────────────────────
    def _n1_categories(self):
        self.stdout.write('\n[N1] legacy categories protest→political, crime→conflict')
        for model in (core_models.Article, core_models.Event, core_models.Topic):
            for legacy, new in LEGACY_CATEGORY_MAP.items():
                qs = model.objects.filter(category=legacy)
                n = qs.count()
                if n and not self.dry:
                    qs.update(category=new)
                if n:
                    self.stdout.write(f'    {model.__name__}: {n} {legacy}→{new}')

    # ── N2 ─────────────────────────────────────────────────────────────────────
    def _n2_locations(self):
        self.stdout.write('\n[N2] canonicalize locations + dup-event report')
        a_changed = self._canon_field(core_models.Article, 'location')
        e_changed = self._canon_field(core_models.Event, 'location_name')
        self.stdout.write(f'    Article.location: {a_changed} changed')
        self.stdout.write(f'    Event.location_name: {e_changed} changed')

        # Group events by the (canonical location, category, day) merge key.
        buckets: dict[tuple, list] = {}
        for ev in core_models.Event.objects.all():
            loc = _canon_location(ev.location_name or '')
            day = ev.started_at.date().isoformat() if ev.started_at else '?'
            buckets.setdefault((loc, ev.category, day), []).append(ev)
        dups = {k: evs for k, evs in buckets.items() if len(evs) > 1}
        n_events = sum(len(v) for v in dups.values())

        if not self.merge:
            self.stdout.write(f'    duplicate (location, category, day) buckets: {len(dups)} '
                              f'({n_events} events) — pass --merge-dups to merge')
            return

        merged = self._merge_dup_events(dups)
        self.stdout.write(f'    merged {merged} duplicate events into {len(dups)} survivors')

    def _merge_dup_events(self, dups: dict) -> int:
        """Merge each duplicate bucket into one survivor (the most-sourced event)."""
        removed = 0
        for evs in dups.values():
            survivor = max(evs, key=lambda e: (e.article_count or 0, -(e.started_at.timestamp() if e.started_at else 0)))
            others = [e for e in evs if e.id != survivor.id]
            article_ids = list(survivor.article_ids or [])
            source_codes = list(survivor.source_codes or [])
            sub_categories = list(survivor.sub_categories or [])
            topic_slugs = list(survivor.topic_slugs or [])
            started = survivor.started_at
            latest = survivor.latest_article_at
            for o in others:
                article_ids += [a for a in (o.article_ids or []) if a not in article_ids]
                source_codes += [s for s in (o.source_codes or []) if s not in source_codes]
                sub_categories += [s for s in (o.sub_categories or []) if s not in sub_categories]
                topic_slugs += [s for s in (o.topic_slugs or []) if s not in topic_slugs]
                if o.started_at and (started is None or o.started_at < started):
                    started = o.started_at
                if o.latest_article_at and (latest is None or o.latest_article_at > latest):
                    latest = o.latest_article_at
            removed += len(others)
            if self.dry:
                continue
            survivor.article_ids = article_ids
            survivor.source_codes = source_codes
            survivor.sub_categories = sub_categories
            survivor.topic_slugs = topic_slugs
            survivor.article_count = len(article_ids)
            survivor.started_at = started
            survivor.latest_article_at = latest
            survivor.save(update_fields=[
                'article_ids', 'source_codes', 'sub_categories', 'topic_slugs',
                'article_count', 'started_at', 'latest_article_at',
            ])
            for o in others:
                o.delete()
        return removed

    def _canon_field(self, model, field: str) -> int:
        changed = 0
        for obj in model.objects.exclude(**{f'{field}__isnull': True}).iterator():
            cur = getattr(obj, field) or ''
            new = _canon_location(cur)
            if new != cur:
                changed += 1
                if not self.dry:
                    setattr(obj, field, new)
                    obj.save(update_fields=[field])
        return changed

    # ── N3 ─────────────────────────────────────────────────────────────────────
    def _n3_null_fill(self):
        self.stdout.write('\n[N3] null-fill latest_article_at + affected_indicators')
        from services.forecasting.routing import route_event_to_weighted_symbols

        filled_dt = filled_ind = 0
        qs = core_models.Event.objects.all()
        for ev in qs.iterator():
            fields = []
            if ev.latest_article_at is None and ev.article_ids:
                try:
                    ids = [uuid.UUID(a) for a in ev.article_ids]
                except (ValueError, AttributeError):
                    ids = []
                if ids:
                    latest = (core_models.Article.objects.filter(id__in=ids)
                              .aggregate(m=Max('published_on'))['m'])
                    if latest:
                        if latest.tzinfo is None:
                            latest = latest.replace(tzinfo=dt_timezone.utc)
                        ev.latest_article_at = latest
                        fields.append('latest_article_at')
                        filled_dt += 1
            if not ev.affected_indicators:
                weighted = route_event_to_weighted_symbols(
                    ev.category or '', ev.location_name or '',
                    ev.topic_slugs or [], ev.sub_categories or [], ev.avg_sentiment,
                )
                if weighted:
                    ev.affected_indicators = weighted
                    fields.append('affected_indicators')
                    filled_ind += 1
            if fields and not self.dry:
                ev.save(update_fields=fields)
        self.stdout.write(f'    latest_article_at filled: {filled_dt}')
        self.stdout.write(f'    affected_indicators filled: {filled_ind}')

    # ── N5 ─────────────────────────────────────────────────────────────────────
    def _n5_subcategories(self):
        self.stdout.write('\n[N5] sub-category spelling (underscore→hyphen)')
        a_changed = 0
        for art in core_models.Article.objects.exclude(sub_category__isnull=True).iterator():
            new = SUBCAT_SPELLING.get(art.sub_category)
            if new:
                a_changed += 1
                if not self.dry:
                    art.sub_category = new
                    art.save(update_fields=['sub_category'])
        e_changed = 0
        for ev in core_models.Event.objects.iterator():
            subs = ev.sub_categories or []
            new_subs = [SUBCAT_SPELLING.get(s, s) for s in subs]
            if new_subs != subs:
                e_changed += 1
                if not self.dry:
                    ev.sub_categories = new_subs
                    ev.save(update_fields=['sub_categories'])
        self.stdout.write(f'    Article.sub_category: {a_changed} changed')
        self.stdout.write(f'    Event.sub_categories: {e_changed} changed')

    # ── N4 ─────────────────────────────────────────────────────────────────────
    def _n4_translations(self):
        self.stdout.write('\n[N4] translations coverage')
        total = core_models.Article.objects.count()
        missing = [a for a in core_models.Article.objects.only('id', 'translations').iterator()
                   if not isinstance(a.translations, dict) or not a.translations.get('ar')]
        self.stdout.write(f'    Articles missing AR translation: {len(missing)}/{total}')

        if not self.retry_tr:
            self.stdout.write('    (pass --retry-translations to backfill via the LLM analyzer)')
            return

        targets = missing[: self.limit]
        self.stdout.write(f'    retrying {len(targets)} (limit={self.limit})…')
        if self.dry:
            return

        from services.processing.analyzer import ArticleAnalyzer
        try:
            analyzer = ArticleAnalyzer()
        except Exception as exc:  # LLM unavailable — degrade gracefully
            self.stdout.write(self.style.WARNING(f'    LLM analyzer unavailable: {exc}'))
            return

        fixed = 0
        for art in targets:
            full = core_models.Article.objects.filter(id=art.id).values('title', 'content').first()
            if not full:
                continue
            text = f"{full['title']}\n\n{full['content']}"
            try:
                result = analyzer.analyze(text)
            except Exception:
                continue
            tr = result.translations or {}
            if tr.get('ar'):
                merged = dict(art.translations or {})
                merged.update(tr)
                core_models.Article.objects.filter(id=art.id).update(translations=merged)
                fixed += 1
        self.stdout.write(f'    AR translations filled: {fixed}/{len(targets)}')
