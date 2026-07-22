"""Live-fetch NLP pipeline accuracy eval — no Mongo, no Docker required.

Samples one real historical article per (RSS source, month) across a year
range, discovers + hydrates it through the SAME strategy dispatch and body
hydration the real backfill pipeline uses (HistoricalBackfillService's
Wayback-frontpage / RSS-sitemap strategy selection, services/data/bodies.py
extraction — real live HTTP fetches, real articles), classifies it with the
on-prem NLPAnnotator, then runs any low-confidence result through the SAME
refine-stage judge production uses (services.processing.refiner.LLMRefiner,
default provider settings.REFINE_PROVIDER) before reporting — annotate's raw
first pass is a draft, not what Article.stage ends up as for most
historical/backfill volume, so skipping refine (--no-refine) understates
accuracy. Writes a report for manual (Claude-as-judge) review against a 90%
accuracy target — see .claude/skills/pipeline-eval-live for the judging
rubric.

Only DB-free part is the source list: this script builds unsaved
core.models.Source instances straight from the fixture JSON
(core/fixtures/*_sources.json) instead of querying Mongo, so it runs with
nothing but outbound HTTP access. Everything downstream (strategy dispatch,
hydration, annotation) is the exact production code path, unsaved — nothing
is written anywhere.

Run from api/ (needs DJANGO_SETTINGS_MODULE, e.g. via .env.app or exporting it):
    uv run python -m scripts.eval_pipeline_live
    uv run python -m scripts.eval_pipeline_live --start-year 2023 --end-year 2024 --months 3
    uv run python -m scripts.eval_pipeline_live --source bbc-world --source brookings
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone as dt_tz
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.base')

import django  # noqa: E402

django.setup()


def _load_sources(only_codes: list[str] | None):
    """Same source set services.tasks.backfill_history_task dispatches against:
    the synthetic Wikipedia Current Events source (the PRIMARY discovery path
    — is_enabled=False since it's backfill-only, so it must be added
    explicitly) prepended to every enabled RSS source. Built in memory from
    the fixture JSON instead of a Mongo query — everything else downstream
    (strategy dispatch, hydration) is the real production code path."""
    import core.models as m
    from services.data.wikipedia import WIKIPEDIA_SOURCE_CODE

    fixtures_dir = Path(m.__file__).resolve().parent / 'fixtures'
    rows = []
    for name in ('initial_rss_sources.json', 'additional_sources.json'):
        rows += json.loads((fixtures_dir / name).read_text())

    sources = []
    seen = set()
    if not only_codes or WIKIPEDIA_SOURCE_CODE in only_codes:
        sources.append(m.Source(
            code=WIKIPEDIA_SOURCE_CODE, type=m.SourceType.WEBSITE,
            name='Wikipedia Current Events',
            url='https://en.wikipedia.org/wiki/Portal:Current_events',
            author_slug='wikipedia', is_enabled=False, weight=1.5,
        ))
        seen.add(WIKIPEDIA_SOURCE_CODE)

    for row in rows:
        f = row['fields']
        if f['type'] != m.SourceType.RSS or not f.get('is_enabled', True):
            continue
        if f['code'] in seen:
            continue
        if only_codes and f['code'] not in only_codes:
            continue
        seen.add(f['code'])
        sources.append(m.Source(
            code=f['code'], type=f['type'], name=f['name'], url=f['url'],
            sitemap_url=f.get('sitemap_url', ''), author_slug=f.get('author_slug', ''),
            headers=f.get('headers') or {}, weight=f.get('weight', 1.0),
            is_enabled=True,
        ))
    return sources


def _month_starts(start_year: int, end_year: int, months_per_year: int):
    """Skips months that haven't started yet (no articles can exist for them)
    and clamps the current in-progress month's end to now, instead of
    sampling a day that hasn't happened — a real gap when end_year is the
    current year."""
    now = datetime.now(dt_tz.utc)
    out = []
    step = max(1, 12 // months_per_year)
    for year in range(start_year, end_year + 1):
        for month in range(1, 13, step)[:months_per_year]:
            start = datetime(year, month, 1, tzinfo=dt_tz.utc)
            if start >= now:
                continue
            end = datetime(year + 1, 1, 1, tzinfo=dt_tz.utc) if month == 12 else datetime(year, month + 1, 1, tzinfo=dt_tz.utc)
            out.append((start, min(end, now)))
    return out


def _day_window(month_start: datetime, month_end: datetime, rng: random.Random):
    """A random calendar day within [month_start, month_end)."""
    from datetime import timedelta
    span_days = max(1, (month_end - month_start).days)
    day_start = month_start + timedelta(days=rng.randrange(span_days))
    return day_start, day_start + timedelta(days=1)


def _sample_from_prod_api(args):
    """Sample already-ingested articles from the deployed staff-only endpoint
    (GET /api/internal/articles/historical/, see api/views/articles.py) instead
    of re-discovering them from RSS/Wayback/Wikipedia. Returns (docs, meta,
    unhydrated_rows, empty_cells) in the same shape the discovery loop feeds the
    shared classify/report block.

    Auth: HTTP Basic with ARTICLE_API_ADMIN_EMAIL / ARTICLE_API_ADMIN_PASSWORD,
    which live in the gitignored .env.claude at the repo root — export them
    before running (`set -a && . ./.env.claude && set +a`). The account must be
    staff (IsAdminUser); a 403 means it isn't.

    Each returned article carries prod's own stored labels under
    fields['_prod_labels'] so the report can show prod's classification (the
    "before") next to the freshly-computed one. By default the article's stored
    content is reused as-is (no fetching — the whole point of using live data);
    with --rehydrate each source_url is re-fetched through the current
    services/data/bodies.py extractor (trafilatura) + Wayback fallback, so a
    body/extraction change can be A/B'd against prod's stored content.
    """
    import os
    import time
    import requests
    import core.models as m
    from services.data.bodies import fetch_article_page, fetch_wayback_page, is_junk_page_title

    email = os.environ.get('ARTICLE_API_ADMIN_EMAIL')
    password = os.environ.get('ARTICLE_API_ADMIN_PASSWORD')
    if not (email and password):
        print('--from-prod-api needs ARTICLE_API_ADMIN_EMAIL / ARTICLE_API_ADMIN_PASSWORD '
              '(from .env.claude) exported in the environment.', file=sys.stderr)
        sys.exit(1)

    url = args.api_base_url.rstrip('/') + '/api/internal/articles/historical/'
    params = {'year': args.year, 'month': args.month, 'limit': args.limit or 200}
    if args.day:
        params['day'] = args.day
    if args.source:
        params['source'] = args.source  # requests encodes a list as repeated params
    resp = requests.get(url, params=params, auth=(email, password), timeout=30)
    if resp.status_code == 403:
        print(f'403 from {url} — the account is authenticated but not staff (IsAdminUser).', file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()
    articles = resp.json().get('results', [])
    print(f'prod API {url} year={args.year} month={args.month}: {len(articles)} article(s)')

    docs, meta, unhydrated_rows, empty_cells = [], [], [], 0
    for a in articles:
        source_code = a.get('source_code') or 'prod'
        published = a.get('published_on') or ''
        try:
            month_start = datetime.fromisoformat(str(published).replace('Z', '+00:00'))
        except ValueError:
            month_start = datetime(args.year, args.month, 1, tzinfo=dt_tz.utc)
        prod_labels = {
            'category': a.get('category'), 'sub_category': a.get('sub_category'),
            'location': a.get('location'), 'stage': a.get('stage'), 'refined_by': a.get('refined_by'),
        }
        fields = {'source_url': a.get('source_url'), '_prod_labels': prod_labels}

        title = a.get('title')
        content = a.get('content')
        if args.rehydrate:
            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
            page_title, body = fetch_article_page(a.get('source_url'), source_code)
            if (not body or is_junk_page_title(page_title)):
                wb_title, wb_body = fetch_wayback_page(a.get('source_url'), around=month_start)
                if wb_body:
                    page_title, body = wb_title, wb_body
            if body:
                title, content = (page_title or title), body

        if not content:
            unhydrated_rows.append({
                'source': source_code, 'year': month_start.year, 'month': month_start.month,
                'url': a.get('source_url'), 'via': 'prod-api', 'title': title,
                'body_chars': 0, 'content_lead': '',
                'category': None, 'sub_category': None, 'country': None, 'city': None,
                'located': False, 'stage': 'unhydrated', 'confidence': 0.0,
                'intensity': None, 'summary': None, 'error': 'no content',
                'prod_category': prod_labels['category'], 'prod_sub_category': prod_labels['sub_category'],
                'prod_location': prod_labels['location'],
            })
            empty_cells += 1
            continue

        docs.append(m.ArticleDocument(
            id=str(len(docs)), title=title or '', content=content,
            source_code=source_code, published_on='',
        ))
        meta.append((source_code, month_start, fields, 'prod-api'))
    return docs, meta, unhydrated_rows, empty_cells


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-year', type=int, default=2020)
    parser.add_argument('--end-year', type=int, default=2026)
    parser.add_argument('--months', type=int, default=12, help='months per year to sample, evenly spaced')
    parser.add_argument('--source', action='append', help='restrict to this source code (repeatable)')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--delay-seconds', type=float, default=0.5, help='pause between discovery calls (per-publisher sitemap/Wayback hosts)')
    parser.add_argument('--wiki-delay-seconds', type=float, default=5.0, help='pause between Wikipedia API calls — its month-page endpoint 429s fast under a tight loop (unlike production, which spreads day-dispatches over the crontab schedule)')
    parser.add_argument('--limit', type=int, help='stop after this many (source, month) cells have been attempted — cells are shuffled first so a capped run still spans many sources, not just however many months the first source in the list happens to have')
    parser.add_argument('--hydrate-limit', type=int, help='stop as soon as this many cells have been successfully hydrated (unlike --limit, empty/failed cells do not count against this) — for a fast, bounded sample of usable articles')
    parser.add_argument('--refine-provider', help='override settings.REFINE_PROVIDER (zeroshot/ollama/cloud/off) for the second-opinion judge pass — lets you A/B providers against the same sample')
    parser.add_argument('--no-refine', action='store_true', help='report annotate\'s raw first-pass output only, skip the refine judge (old behavior — annotate alone is NOT what Article.stage ends up as in production for low-confidence rows)')
    parser.add_argument('--from-prod-api', action='store_true', help='sample already-ingested articles from the deployed staff-only endpoint (auth via .env.claude) instead of re-discovering from sources — needs --year/--month')
    parser.add_argument('--api-base-url', default='https://eventhorizonai.dev', help='base URL of the deployed API for --from-prod-api')
    parser.add_argument('--year', type=int, help='year for --from-prod-api')
    parser.add_argument('--month', type=int, help='month (1-12) for --from-prod-api')
    parser.add_argument('--day', type=int, help='optional day for --from-prod-api (narrows to one calendar day)')
    parser.add_argument('--rehydrate', action='store_true', help='with --from-prod-api, re-fetch each article URL through the current bodies.py extractor + Wayback fallback instead of reusing prod\'s stored content — to A/B an extraction change against prod')
    args = parser.parse_args()

    from services.data.historical import HistoricalBackfillService, _PendingSave
    from services.data.wayback import supports_wayback
    from services.data.wikipedia import WIKIPEDIA_SOURCE_CODE
    from services.data.bodies import is_junk_page_title
    import core.models as m
    # NLPAnnotator / LLMRefiner / geocode live in _finish (the shared classify path).

    def _via(source_code: str) -> str:
        if source_code == WIKIPEDIA_SOURCE_CODE:
            return 'wikipedia'
        return 'wayback' if supports_wayback(source_code) else 'rss-sitemap'

    rng = random.Random(args.seed)
    import time

    # ── prod-API sampling: skip discovery entirely, read already-ingested
    #    articles from the deployed staff-only endpoint (see _sample_from_prod_api). ──
    if args.from_prod_api:
        if not (args.year and args.month):
            print('--from-prod-api requires --year and --month.', file=sys.stderr)
            sys.exit(1)
        sources = []
        cells = []
        docs, meta, rows, empty_cells = _sample_from_prod_api(args)
        return _finish(args, sources, cells, docs, meta, rows, empty_cells)

    sources = _load_sources(args.source)
    if not sources:
        print('No matching sources.', file=sys.stderr)
        sys.exit(1)

    months = _month_starts(args.start_year, args.end_year, args.months)
    print(f'{len(sources)} source(s) x {len(months)} month(s) = {len(sources) * len(months)} cell(s)')
    print(f"  wikipedia strategy: {WIKIPEDIA_SOURCE_CODE in [s.code for s in sources]}")
    print(f"  wayback-frontpage strategy: {sorted(s.code for s in sources if supports_wayback(s.code))}")

    service = HistoricalBackfillService(sources, top_n=1, fetch_body=False)

    cells = [(source, month_start, month_end) for source in sources for month_start, month_end in months]
    rng.shuffle(cells)
    if args.limit:
        cells = cells[:args.limit]
        print(f'  --limit {args.limit}: sampling {len(cells)} shuffled cell(s) instead of the full grid')

    rows = []
    empty_cells = 0
    docs = []
    meta = []

    for source, month_start, month_end in cells:
        strategy = service._strategies[source.code]
        delay = args.wiki_delay_seconds if source.code == WIKIPEDIA_SOURCE_CODE else args.delay_seconds
        if delay > 0:
            time.sleep(delay)
        day_start, day_end = _day_window(month_start, month_end, rng)
        try:
            candidates = strategy.fetch_day(day_start, day_end)
        except Exception as exc:
            print(f'  {source.code} {month_start:%Y-%m}: discovery failed ({exc})')
            empty_cells += 1
            continue
        if not candidates:
            print(f'  {source.code} {month_start:%Y-%m}: no candidates')
            empty_cells += 1
            continue

        entry = dict(rng.choice(candidates))
        pending = _PendingSave(source.code, source.type, entry)
        service._hydrate_bodies([pending], around=day_start, deadline=None)
        title = pending.fields.get('title')
        content = pending.fields.get('content')
        via = _via(source.code)
        if not content or is_junk_page_title(title):
            rows.append({
                'source': source.code, 'year': month_start.year, 'month': month_start.month,
                'url': pending.fields.get('source_url'), 'via': via, 'title': title,
                'body_chars': 0, 'content_lead': '',
                'category': None, 'sub_category': None, 'country': None, 'city': None,
                'located': False, 'stage': 'unhydrated', 'confidence': 0.0,
                'intensity': None, 'summary': None, 'error': 'body fetch failed (live + wayback)',
            })
            empty_cells += 1
            print(f'  {source.code} {month_start:%Y-%m}: sampled but unhydrated ({pending.fields.get("source_url")})')
            continue

        print(f'  {source.code} {month_start:%Y-%m}: {pending.fields.get("source_url")}')
        docs.append(m.ArticleDocument(
            id=str(len(docs)), title=title or entry.get('title') or '', content=content,
            source_code=source.code, published_on='',
        ))
        meta.append((source.code, month_start, pending.fields, via))
        if args.hydrate_limit and len(docs) >= args.hydrate_limit:
            print(f'  --hydrate-limit {args.hydrate_limit}: reached, stopping early')
            break

    return _finish(args, sources, cells, docs, meta, rows, empty_cells)


def _finish(args, sources, cells, docs, meta, rows, empty_cells):
    """Shared classify + refine + report tail for both sampling paths (discovery
    and --from-prod-api): annotate every hydrated doc, run the low-confidence
    ones through the refine judge, then write/print the report. meta rows may
    carry prod's stored labels under fields['_prod_labels'] (prod-API path) —
    surfaced as prod_* columns so the report shows prod's classification next to
    the freshly-computed one; absent (None) for the discovery path."""
    from services.processing.annotator import ESCALATE_BELOW, NLPAnnotator
    from services.processing.geocode import geocode as geocode_place
    from services.processing.refiner import LLMRefiner

    if docs:
        features = NLPAnnotator().annotate_batch(docs, lite_flags=True)
        annotated_rows = []
        for doc, f, (source_code, month_start, fields, via) in zip(docs, features, meta):
            stage = (
                'fetched' if f.llm_error is not None
                else 'annotated' if f.confidence >= ESCALATE_BELOW
                else 'refine'
            )
            prod = fields.get('_prod_labels') or {}
            annotated_rows.append({
                'doc': doc,
                'source': source_code, 'year': month_start.year, 'month': month_start.month,
                'url': fields.get('source_url'), 'via': via, 'title': doc.title,
                'body_chars': len(doc.content or ''),
                'content_lead': ' '.join((doc.content or '').split())[:300],
                'category': f.category, 'sub_category': f.sub_category,
                'country': (f.llm_data or {}).get('country'), 'city': (f.llm_data or {}).get('city'),
                'located': f.latitude is not None, 'stage': stage, 'confidence': f.confidence,
                'intensity': f.event_intensity, 'summary': (f.translations.get('en') or {}).get('summary'),
                'error': f.llm_error, 'refined_by': None,
                'prod_category': prod.get('category'), 'prod_sub_category': prod.get('sub_category'),
                'prod_location': prod.get('location'),
            })

        # Refine stage — a bare majority of historical/backfill volume lands
        # here (confidence < ESCALATE_BELOW). Article.stage='annotated' is NOT
        # the pipeline's final answer for these; skipping this step (--no-refine)
        # reports the pre-judge draft, not what actually ships.
        if not args.no_refine:
            to_refine = [r for r in annotated_rows if r['stage'] == 'refine']
            if to_refine:
                refiner = LLMRefiner(provider=args.refine_provider)
                items = [(r['doc'].title, r['doc'].content) for r in to_refine]
                print(f"  refining {len(to_refine)} low-confidence cell(s) via provider={refiner.provider!r}")
                verdicts = refiner.judge(items)
                for r, verdict in zip(to_refine, verdicts):
                    if verdict is None:
                        continue  # judge unavailable/failed — stays at stage='refine', same as production
                    r['category'] = verdict['category']
                    r['sub_category'] = verdict['sub_category']
                    city, country = verdict.get('city'), verdict.get('country')
                    if city or country:
                        lat, _lon = geocode_place(city, country)
                        if lat is not None:
                            r['city'], r['country'], r['located'] = city, country, True
                    if verdict.get('intensity') is not None:
                        r['intensity'] = verdict['intensity']
                    r['stage'] = 'refined'
                    r['refined_by'] = verdict['provider']

        for r in annotated_rows:
            r.pop('doc')
        rows.extend(annotated_rows)

    rows.sort(key=lambda r: (r['source'], r['year'] or 0, r['month'] or 0))
    report = {
        'generated_at': datetime.now(dt_tz.utc).isoformat(),
        'mode': 'prod-api' if args.from_prod_api else 'discovery',
        'start_year': args.start_year, 'end_year': args.end_year, 'months_per_year': args.months,
        'sources': [s.code for s in sources],
        'cells_total': len(cells), 'cells_empty': empty_cells,
        'sampled': len(rows), 'hydrated': sum(1 for r in rows if r.get('body_chars')),
        'articles': rows,
    }
    from services.utils import results_dir
    out_path = results_dir('eval_pipeline_live') / f'pipeline_eval_{datetime.now(dt_tz.utc):%Y%m%dT%H%M%SZ}.json'
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    for r in rows:
        place = ', '.join(filter(None, [r.get('city'), r.get('country')])) or '?'
        cat = '/'.join(filter(None, [r.get('category'), r.get('sub_category')])) or '(no body)'
        prod_cat = '/'.join(filter(None, [r.get('prod_category'), r.get('prod_sub_category')]))
        drift = '' if not prod_cat or prod_cat == cat else f'  (prod: {prod_cat})'
        ym = f'{r["year"]}-{r["month"]:02d}' if r.get('year') else '?'
        print(f'{ym}  {r["source"]:<16} {cat:<28} {place:<24} {(r.get("title") or "")[:52]}{drift}')

    print(f'\n{len(rows)} article(s) sampled ({empty_cells} empty/unhydrated), '
          f'{report["hydrated"]}/{len(rows)} hydrated -> {out_path}')


if __name__ == '__main__':
    main()
