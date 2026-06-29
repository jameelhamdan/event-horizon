"""Full-system end-to-end test with REAL data.

Drives every part of the platform from start to finish against the live MongoDB and
real external sources, asserting invariants at each stage. Unlike ``e2e_pipeline``
(which only reports counts) this command *checks* and exits non-zero on any hard
failure — it is the gradeable "does the whole thing work" test.

What it exercises (with real data):
  1. Config / symbols       — MarketSymbol seed, market_symbols helpers, 5-symbol panel
  2. Queue & helpers        — enqueue() sync return value, mark_stage behaviour
  3. Fan-out fetch          — real RSS via dispatch_fetch / fetch_source workers
  4. Fan-out process        — real NLP/LLM per-record workers + stage_status
  5. Aggregate events       — real semantic clustering
  6. Tag topics (LLM)       — real LLM matcher + stage_status
  7. Route events (rules)   — deterministic router → affected_indicators + stage_status
  8. Pipeline coverage      — pipeline_coverage() shape
  9. Forecasting            — real price backfill → train → run → score
 10. REST API               — /api/symbols, /api/events, /api/forecasts, /api/prices (Django test client)
 11. Ops dashboard          — throughput / coverage / forecast-status helpers
 12. Bootstrap guard        — idempotency of bootstrap_initial_data_task
 13. Scoring helpers        — utils tokenize/jaccard, strip_code_fences, ArticleImportanceScorer,
                             model fields (Article.importance_score, Source.weight), title dedup

Fan-out runs synchronously (this command forces ``TASK_QUEUE_ENABLED=False``) so the
dispatcher → per-record worker path is fully covered without live RQ workers.

Hard checks (must pass) are local/deterministic; soft checks (WARN) depend on live
network/LLM and won't fail the run when an environment is offline.

    python manage.py e2e_full
    python manage.py e2e_full --source guardian-world --years 2
    python manage.py e2e_full --fast            # skip network-heavy stages (structural checks only)
    python manage.py e2e_full --skip-forecast --output /tmp/e2e.json
"""
import json
import os
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class _Checks:
    """Collects PASS / FAIL / WARN results. FAIL ⇒ non-zero exit."""

    def __init__(self, stdout, style):
        self.results: list[dict] = []
        self._stdout = stdout
        self._style = style

    def _emit(self, status, name, detail):
        self.results.append({'check': name, 'status': status, 'detail': str(detail)[:300]})
        tag = {'PASS': self._style.SUCCESS, 'FAIL': self._style.ERROR,
               'WARN': self._style.WARNING}[status]
        self._stdout.write(tag(f'  [{status}] {name}') + (f' — {detail}' if detail else ''))

    def hard(self, name, ok, detail=''):
        self._emit('PASS' if ok else 'FAIL', name, detail)
        return ok

    def soft(self, name, ok, detail=''):
        self._emit('PASS' if ok else 'WARN', name, detail)
        return ok

    @property
    def failed(self):
        return [r for r in self.results if r['status'] == 'FAIL']

    @property
    def warned(self):
        return [r for r in self.results if r['status'] == 'WARN']


class Command(BaseCommand):
    help = 'Full-system end-to-end test with real data (asserts invariants, exits non-zero on failure)'

    BASE_PANEL = {'CL=F', 'GC=F', 'BTC-USD', 'SPY', 'EURUSD=X'}

    def add_arguments(self, parser):
        parser.add_argument('--source', type=str, default='guardian-world',
                            help='RSS source code to fetch (default: guardian-world)')
        parser.add_argument('--fetch-hours', type=int, default=48)
        parser.add_argument('--process-limit', type=int, default=10)
        parser.add_argument('--hours', type=int, default=72,
                            help='Lookback window for aggregate/tag/route')
        parser.add_argument('--years', type=int, default=2, help='Price backfill years')
        parser.add_argument('--fast', action='store_true',
                            help='Skip all network/LLM-heavy stages; run structural checks only')
        parser.add_argument('--skip-fetch', action='store_true')
        parser.add_argument('--skip-process', action='store_true')
        parser.add_argument('--skip-forecast', action='store_true')
        parser.add_argument('--output', type=str, default=None)

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from core import models as core_models

        c = _Checks(self.stdout, self.style)
        report: dict = {'started_at': datetime.now(dt_timezone.utc).isoformat(),
                        'params': {k: opts[k] for k in
                                   ('source', 'fetch_hours', 'process_limit', 'hours',
                                    'years', 'fast', 'skip_fetch', 'skip_process', 'skip_forecast')}}

        fast = opts['fast']
        # Force synchronous fan-out so dispatcher → per-record workers run inline.
        prev_queue = getattr(settings, 'TASK_QUEUE_ENABLED', False)
        settings.TASK_QUEUE_ENABLED = False
        # Allow the Django test client through ALLOWED_HOSTS for the API stage.
        prev_hosts = list(getattr(settings, 'ALLOWED_HOSTS', []))
        if 'testserver' not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, 'testserver']

        try:
            self._stage_symbols(c)
            self._stage_tracking_and_helpers(c)
            if not fast and not opts['skip_fetch']:
                self._stage_fetch(c, opts)
            if not fast and not opts['skip_process']:
                self._stage_process(c, opts)
            if not fast:
                self._stage_aggregate(c, opts)
                self._stage_tag(c, opts)
            self._stage_route(c, opts, fast)
            self._stage_coverage(c)
            if not fast and not opts['skip_forecast']:
                self._stage_forecast(c, opts)
            self._stage_api(c)
            self._stage_dashboard(c)
            self._stage_bootstrap_guard(c)
            self._stage_scoring_helpers(c)
        finally:
            settings.TASK_QUEUE_ENABLED = prev_queue
            settings.ALLOWED_HOSTS = prev_hosts

        # ── Summary + report ──────────────────────────────────────────────
        report['checks'] = c.results
        report['summary'] = {
            'total': len(c.results),
            'passed': sum(1 for r in c.results if r['status'] == 'PASS'),
            'failed': len(c.failed),
            'warned': len(c.warned),
        }
        report['counts'] = {
            'articles': core_models.Article.objects.count(),
            'processed': core_models.Article.objects.filter(processed_on__isnull=False).count(),
            'events': core_models.Event.objects.count(),
            'price_bars': core_models.PriceBar.objects.count(),
            'forecasts': core_models.Forecast.objects.count(),
            'market_symbols': core_models.MarketSymbol.objects.count(),
        }
        report['finished_at'] = datetime.now(dt_timezone.utc).isoformat()

        ts = datetime.now(dt_timezone.utc).strftime('%Y%m%dT%H%M%S')
        out = Path(opts['output'] or os.path.join(os.getcwd(), f'e2e_full_{ts}.json'))
        out.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

        s = report['summary']
        self.stdout.write('')
        self.stdout.write(f"Report → {out.resolve()}")
        self.stdout.write(
            f"checks: {s['passed']} passed / {s['failed']} failed / {s['warned']} warned"
        )
        if c.failed:
            self.stdout.write(self.style.ERROR('E2E FAILED:'))
            for r in c.failed:
                self.stdout.write(self.style.ERROR(f"  - {r['check']}: {r['detail']}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS('E2E PASSED'))

    # ── Stage 1: config / symbols (WA1) ───────────────────────────────────

    def _stage_symbols(self, c):
        self.stdout.write('→ Stage 1: config & symbols')
        from core import models as core_models
        from services.market_symbols import (
            get_panel_symbols, get_symbol_meta, get_coingecko_ids,
            get_yahoo_symbols, get_backfill_symbols,
        )
        from services.forecasting.routing import get_panel_symbols as routing_panel

        total = core_models.MarketSymbol.objects.count()
        c.hard('symbols.seeded', total >= 20, f'{total} MarketSymbol rows')

        forecast_db = set(core_models.MarketSymbol.objects
                          .filter(is_forecast=True, is_active=True)
                          .values_list('symbol', flat=True))
        c.hard('symbols.forecast_nonempty', len(forecast_db) >= 1, f'{sorted(forecast_db)}')

        panel = set(get_panel_symbols())
        c.hard('symbols.panel_matches_db', panel == forecast_db,
               f'panel={sorted(panel)} db={sorted(forecast_db)}')
        c.hard('symbols.routing_panel_consistent', set(routing_panel()) == panel)
        is_base5 = panel == self.BASE_PANEL
        c.soft('symbols.panel_is_base5', is_base5,
               '' if is_base5 else f'expected {sorted(self.BASE_PANEL)}, got {sorted(panel)}')

        c.hard('symbols.meta_nonempty', len(get_symbol_meta()) > 0)
        c.hard('symbols.yahoo_nonempty', len(get_yahoo_symbols()) > 0)
        c.hard('symbols.coingecko_nonempty', len(get_coingecko_ids()) > 0)
        c.hard('symbols.backfill_nonempty', len(get_backfill_symbols()) > 0)

    # ── Stage 2: queue + stage helper ─────────────────────────────────────

    def _stage_tracking_and_helpers(self, c):
        self.stdout.write('→ Stage 2: queue & helpers')
        from services.queue import enqueue
        from services.utils import mark_stage

        # Verify enqueue() returns the function's value when TASK_QUEUE_ENABLED=False
        result = enqueue(lambda n: n, 7)
        c.hard('tracking.sync_result', result == 7)

        # mark_stage unit behaviour
        class _Rec:
            stage_status = {}
        rec = _Rec()
        mark_stage(rec, 'process', ok=True)
        mark_stage(rec, 'geocode', ok=False, error='boom')
        c.hard('stages.mark_ok', rec.stage_status.get('process', {}).get('ok') is True)
        c.hard('stages.mark_error', rec.stage_status.get('geocode', {}).get('error') == 'boom')

    # ── Stage 3: fan-out fetch (WA3) ──────────────────────────────────────

    def _stage_fetch(self, c, opts):
        self.stdout.write('→ Stage 3: fan-out fetch (real RSS)')
        from core import models as core_models
        from services.tasks import dispatch_fetch_task, fetch_source_task

        src = opts['source']
        if not core_models.Source.objects.filter(code=src, is_enabled=True).exists():
            c.soft('fetch.source_exists', False, f'source {src} not found/enabled — skipping fetch')
            return
        before = core_models.Article.objects.count()
        start = datetime.now(dt_timezone.utc) - timedelta(hours=opts['fetch_hours'])
        try:
            fetched = fetch_source_task(src, start)
            c.hard('fetch.returns_int', isinstance(fetched, int), f'{fetched}')
            after = core_models.Article.objects.count()
            c.soft('fetch.articles_present', after > 0, f'{after} total articles')
            c.soft('fetch.new_or_existing', after >= before)
            self.stdout.write(f'    fetched {fetched} new from {src} ({before}→{after})')
        except Exception as exc:  # noqa: BLE001
            c.soft('fetch.ran', False, f'fetch failed (offline?): {exc}')

        # Dispatcher returns a job-count even when nothing new is available.
        try:
            dispatched = dispatch_fetch_task(start)
            c.hard('fetch.dispatch_counts_sources', isinstance(dispatched, int) and dispatched >= 1,
                   f'{dispatched} sources dispatched')
        except Exception as exc:  # noqa: BLE001
            c.soft('fetch.dispatch_ran', False, str(exc))

    # ── Stage 4: fan-out process (WA3/3.6) ────────────────────────────────

    def _stage_process(self, c, opts):
        self.stdout.write('→ Stage 4: fan-out process (real NLP/LLM)')
        from core import models as core_models
        from services.tasks import dispatch_process_articles_task

        pending = core_models.Article.objects.filter(processed_on__isnull=True).count()
        if pending == 0:
            c.soft('process.has_pending', False, 'no unprocessed articles — skipping')
            return
        try:
            jobs = dispatch_process_articles_task(limit=opts['process_limit'])
            c.hard('process.dispatch_returns_int', isinstance(jobs, int))
            self.stdout.write(f'    dispatched {jobs} per-record process job(s)')
        except Exception as exc:  # noqa: BLE001
            c.soft('process.ran', False, f'process failed (LLM/NLP down?): {exc}')
            return

        # A processed article should carry stage_status from the per-record worker.
        sample = (core_models.Article.objects.filter(processed_on__isnull=False)
                  .order_by('-processed_on').first())
        c.soft('process.produced_processed', sample is not None)
        if sample is not None:
            ss = sample.stage_status or {}
            c.soft('process.stage_status_process', (ss.get('process') or {}).get('ok') is True,
                   f'stage_status={ss}')
            c.soft('process.stage_status_geocode_present', 'geocode' in ss)

    # ── Stage 5: aggregate ────────────────────────────────────────────────

    def _stage_aggregate(self, c, opts):
        self.stdout.write('→ Stage 5: aggregate events')
        from core import models as core_models
        from services.tasks import aggregate_events_task
        try:
            created, updated = aggregate_events_task(hours=opts['hours'])
            c.hard('aggregate.returns_tuple', isinstance(created, int) and isinstance(updated, int))
            self.stdout.write(f'    {created} created / {updated} updated')
        except Exception as exc:  # noqa: BLE001
            c.soft('aggregate.ran', False, str(exc))
            return
        c.soft('aggregate.events_present', core_models.Event.objects.count() > 0)

    # ── Stage 6: tag topics (LLM) ─────────────────────────────────────────

    def _stage_tag(self, c, opts):
        self.stdout.write('→ Stage 6: tag topics (real LLM)')
        from core import models as core_models
        from services.tasks import dispatch_tag_topics_task

        if core_models.Topic.objects.filter(is_active=True).count() == 0:
            c.soft('tag.topics_available', False, 'no active topics — skipping tag')
            return
        try:
            jobs = dispatch_tag_topics_task(hours=opts['hours'])
            c.hard('tag.dispatch_returns_int', isinstance(jobs, int))
            self.stdout.write(f'    dispatched {jobs} tag chunk(s)')
        except Exception as exc:  # noqa: BLE001
            c.soft('tag.ran', False, str(exc))
            return
        tagged = (core_models.Event.objects.exclude(topics_source='')
                  .order_by('-started_at').first())
        c.soft('tag.some_event_tagged', tagged is not None)
        if tagged is not None:
            c.soft('tag.stage_status', 'tag' in (tagged.stage_status or {}))

    # ── Stage 7: route events (deterministic rules) ───────────────────────

    def _stage_route(self, c, opts, fast):
        self.stdout.write('→ Stage 7: route events (rules router — deterministic)')
        from core import models as core_models
        from services.tasks import dispatch_route_events_task

        events = core_models.Event.objects.count()
        if events == 0:
            c.soft('route.events_present', False, 'no events to route')
            return
        try:
            jobs = dispatch_route_events_task(hours=max(opts['hours'], 24 * 14), source='rules')
            c.hard('route.dispatch_returns_int', isinstance(jobs, int), f'{jobs} chunk(s)')
        except Exception as exc:  # noqa: BLE001
            c.hard('route.ran', False, str(exc))
            return

        routed = (core_models.Event.objects.filter(router_source='rules')
                  .order_by('-started_at').first())
        # Rules router is deterministic (no network) → this is a hard check once events exist.
        c.hard('route.router_source_set', routed is not None, 'no event got router_source=rules')
        if routed is not None:
            c.hard('route.stage_status', 'route' in (routed.stage_status or {}),
                   f'stage_status={routed.stage_status}')
            # affected_indicators must be a list of {symbol, weight} within the panel.
            from services.market_symbols import get_panel_symbols
            panel = set(get_panel_symbols())
            inds = routed.affected_indicators or []
            shape_ok = all(isinstance(i, dict) and 'symbol' in i and 'weight' in i for i in inds)
            c.hard('route.indicator_shape', shape_ok, f'{inds[:3]}')
            within = all(i.get('symbol') in panel for i in inds)
            c.hard('route.indicators_in_panel', within, f'{[i.get("symbol") for i in inds]}')

    # ── Stage 8: pipeline coverage (WA3.6) ────────────────────────────────

    def _stage_coverage(self, c):
        self.stdout.write('→ Stage 8: pipeline coverage')
        from services.workflow import pipeline_coverage
        try:
            cov = pipeline_coverage()
        except Exception as exc:  # noqa: BLE001
            c.hard('coverage.runs', False, str(exc))
            return
        c.hard('coverage.runs', isinstance(cov, list) and len(cov) >= 1)
        stages = {row.get('stage') for row in cov}
        c.hard('coverage.has_stages', {'process', 'geocode', 'tag', 'route'} <= stages,
               f'{sorted(stages)}')
        shape_ok = all({'stage', 'label', 'need', 'action'} <= set(row) for row in cov)
        c.hard('coverage.row_shape', shape_ok)

    # ── Stage 9: forecasting (real data) ──────────────────────────────────

    def _stage_forecast(self, c, opts):
        self.stdout.write('→ Stage 9: forecasting (real backfill → train → run → score)')
        from core import models as core_models
        from services.tasks import (
            backfill_prices_task, train_forecast_model_task,
            run_forecast_task, score_forecasts_task,
        )
        if not getattr(settings, 'FORECAST_ENABLED', False):
            c.soft('forecast.enabled', False, 'FORECAST_ENABLED is false — skipping')
            return
        try:
            inserted = backfill_prices_task(years=opts['years'])
            c.hard('forecast.backfill_returns_int', isinstance(inserted, int))
            bars = core_models.PriceBar.objects.count()
            c.soft('forecast.bars_present', bars > 0, f'{bars} PriceBar rows')
            self.stdout.write(f'    backfilled {inserted} new bars ({bars} total)')
        except Exception as exc:  # noqa: BLE001
            c.soft('forecast.backfill_ran', False, f'backfill failed (offline?): {exc}')

        if core_models.PriceBar.objects.count() == 0:
            c.soft('forecast.has_bars', False, 'no PriceBar data — skipping train/run')
            return
        try:
            trained = train_forecast_model_task()
            c.soft('forecast.trained', isinstance(trained, int) and trained >= 0, f'{trained} horizons')
            created = run_forecast_task()
            c.soft('forecast.run', isinstance(created, int), f'{created} forecasts')
            scored = score_forecasts_task()
            c.soft('forecast.scored', isinstance(scored, int), f'{scored} scored')
        except Exception as exc:  # noqa: BLE001
            c.soft('forecast.train_run_ran', False, str(exc))
            return

        # Any produced forecast must be for a current panel symbol.
        from services.market_symbols import get_panel_symbols
        panel = set(get_panel_symbols())
        fc = core_models.Forecast.objects.order_by('-generated_at').first()
        if fc is not None:
            in_panel = fc.symbol in panel
            c.hard('forecast.symbol_in_panel', in_panel,
                   f'{fc.symbol}' if in_panel else f'{fc.symbol} not in {sorted(panel)}')
            c.hard('forecast.horizon_valid', fc.horizon_days in set(settings.FORECAST_HORIZONS_DAYS))

    # ── Stage 10: REST API (real DB via test client) ──────────────────────

    def _stage_api(self, c):
        self.stdout.write('→ Stage 10: REST API')
        from django.test import Client
        from core import models as core_models
        client = Client()

        def get(path):
            r = client.get(path)
            ctype = r.headers.get('Content-Type', '') if hasattr(r, 'headers') else r.get('Content-Type', '')
            body = None
            if 'application/json' in ctype:
                try:
                    body = r.json()
                except Exception:  # noqa: BLE001
                    body = None
            return r.status_code, body

        # /api/symbols/
        code, body = get('/api/symbols/')
        c.hard('api.symbols_200', code == 200, f'status {code}')
        c.hard('api.symbols_results', bool(body) and isinstance(body.get('results'), list)
               and body['count'] > 0, f'{body.get("count") if body else None} symbols')

        # forecast filter must agree with the DB
        code, body = get('/api/symbols/?forecast=true')
        db_forecast = core_models.MarketSymbol.objects.filter(is_forecast=True, is_active=True).count()
        c.hard('api.symbols_forecast_200', code == 200)
        c.hard('api.symbols_forecast_matches_db',
               bool(body) and body.get('count') == db_forecast,
               f'api={body.get("count") if body else None} db={db_forecast}')

        # group + stream_key filters
        code, body = get('/api/symbols/?group=top_crypto')
        c.hard('api.symbols_group_filter', code == 200 and bool(body)
               and all(s['group'] == 'top_crypto' for s in body['results']))

        # /api/events/
        code, body = get('/api/events/')
        c.hard('api.events_200', code == 200, f'status {code}')
        c.hard('api.events_shape', bool(body) and 'results' in body)

        # /api/forecasts/latest/
        code, body = get('/api/forecasts/latest/')
        c.hard('api.forecasts_200', code == 200, f'status {code}')

        # /api/forecasts/accuracy/
        code, _ = get('/api/forecasts/accuracy/')
        c.hard('api.accuracy_200', code == 200, f'status {code}')

        # /api/prices/latest/
        code, _ = get('/api/prices/latest/')
        c.hard('api.prices_latest_200', code == 200, f'status {code}')

        # /api/topics/
        code, _ = get('/api/topics/')
        c.hard('api.topics_200', code == 200, f'status {code}')

    # ── Stage 11: ops dashboard helpers (WA5) ─────────────────────────────

    def _stage_dashboard(self, c):
        self.stdout.write('→ Stage 11: ops dashboard helpers')
        from core import admin_dashboard as dash
        try:
            tp = dash._throughput()
            c.hard('dashboard.throughput', isinstance(tp, dict))
            fs = dash._forecast_status()
            c.hard('dashboard.forecast_status', isinstance(fs, dict) and 'artifacts' in fs)
            inflight = dash._in_flight()
            c.hard('dashboard.in_flight', isinstance(inflight, list))
            # _upcoming reads api/crontab — soft (file may be missing in some envs).
            up = dash._upcoming()
            c.soft('dashboard.upcoming', isinstance(up, list))
        except Exception as exc:  # noqa: BLE001
            c.hard('dashboard.helpers_run', False, str(exc))

    # ── Stage 12: bootstrap idempotency guard (WA4) ───────────────────────

    def _stage_bootstrap_guard(self, c):
        self.stdout.write('→ Stage 12: bootstrap idempotency guard')
        from django.core.cache import cache
        from services.tasks import bootstrap_initial_data_task
        # Pre-set the done flag so the task short-circuits WITHOUT enqueuing heavy backfills.
        cache.set('bootstrap:initial_data:done', True, timeout=None)
        try:
            result = bootstrap_initial_data_task()
            c.hard('bootstrap.guard_skips', result == 0, f'returned {result} (expected 0)')
        except Exception as exc:  # noqa: BLE001
            c.hard('bootstrap.guard_runs', False, str(exc))

    # ── Stage 13: scoring helpers & text utilities ────────────────────────

    def _stage_scoring_helpers(self, c):
        self.stdout.write('→ Stage 13: scoring helpers & text utilities')

        # ── utils ─────────────────────────────────────────────────────
        from services.utils import tokenize, jaccard, STOP_WORDS

        a = tokenize('Ukraine ceasefire deal signed')
        c.hard('utils.tokenize_works', 'ukraine' in a and 'ceasefire' in a,
               f'tokens={sorted(a)}')
        c.hard('utils.tokenize_stopwords', 'the' not in a and 'and' not in a,
               'stop words not filtered')
        c.hard('utils.tokenize_short_tokens_dropped', 'a' not in a,
               'single-char token slipped through')
        b = tokenize('Ukraine Russia peace negotiations')
        sim = jaccard(a, b)
        c.hard('utils.jaccard_positive', sim > 0.0, f'{sim:.3f}')
        c.hard('utils.jaccard_self', jaccard(a, a) == 1.0)
        c.hard('utils.jaccard_empty', jaccard(frozenset(), a) == 0.0)
        c.hard('utils.stop_words_nonempty', len(STOP_WORDS) >= 30,
               f'{len(STOP_WORDS)} stop words')

        # ── strip_code_fences ──────────────────────────────────────────────
        from services.llm import strip_code_fences
        c.hard('llm.strip_json_fence',
               strip_code_fences('```json\n[{"i":1}]\n```') == '[{"i":1}]')
        c.hard('llm.strip_plain_fence',
               strip_code_fences('```\n{"k":"v"}\n```') == '{"k":"v"}')
        c.hard('llm.strip_noop', strip_code_fences('[1,2,3]') == '[1,2,3]')
        c.hard('llm.strip_none_safe', strip_code_fences(None) == '')  # type: ignore[arg-type]

        # ── ArticleImportanceScorer structural ─────────────────────────────
        from services.scoring import ArticleImportanceScorer, score_unscored_articles
        scorer = ArticleImportanceScorer()
        c.hard('scoring.scorer_instantiates', isinstance(scorer, ArticleImportanceScorer))
        c.hard('scoring.batch_size_positive', scorer.BATCH_SIZE >= 1,
               f'BATCH_SIZE={scorer.BATCH_SIZE}')
        c.hard('scoring.default_score_in_range',
               1.0 <= scorer.DEFAULT_SCORE <= 10.0,
               f'DEFAULT_SCORE={scorer.DEFAULT_SCORE}')
        c.hard('scoring.score_unscored_callable', callable(score_unscored_articles))

        # ── Model fields exist ─────────────────────────────────────────────
        from core import models as core_models
        article_fields = {f.name for f in core_models.Article._meta.get_fields()}
        c.hard('model.article_importance_score', 'importance_score' in article_fields,
               f'fields={sorted(article_fields)}')
        c.hard('model.article_importance_source', 'importance_source' in article_fields)
        source_fields = {f.name for f in core_models.Source._meta.get_fields()}
        c.hard('model.source_weight', 'weight' in source_fields,
               f'fields={sorted(source_fields)}')
        c.hard('model.source_weight_locked', 'weight_locked' in source_fields)

        # ── Intra-batch title dedup (C1 fix) ──────────────────────────────
        from services.data import _filter_title_dupes
        datums = [
            {'title': 'Ukraine peace negotiations begin in Vienna'},
            {'title': 'Ukraine peace negotiations begin in Vienna today'},  # near-duplicate
            {'title': 'Earthquake strikes Turkey dozens killed'},           # different
        ]
        kept = _filter_title_dupes(datums, threshold=0.5, hours=1)
        c.hard('scoring.title_dedup_intra_batch', len(kept) == 2,
               f'expected 2 kept from 3, got {len(kept)}')
