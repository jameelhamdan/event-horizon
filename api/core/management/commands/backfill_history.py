"""
Backfill top-N historical articles per day window — Wikipedia Current Events
(curated per-day world events with citations; the primary discovery path) plus
one or all RSS sources' sitemaps.

This command's ``backfill_history_task`` dispatches one ``backfill_day_chunk_task``
per (day, source-chunk) pair — each discovers that day's articles via its
source's strategy (WikipediaHistoricalService for the synthetic
'wikipedia-current-events' source, RSSHistoricalService sitemap discovery for
RSS sources), cross-source title-dedups within its chunk, fetches each
article's title+body (live page first, Wayback Machine capture as fallback),
saves via Article.objects.get_or_create (idempotent), and then immediately
runs NLP annotation (services.workflow.articles.annotate_articles) on the
newly-saved articles — see services/data/historical.py's and
services/data/wikipedia.py's module docstrings for the full chain and its
chunking trade-offs.

Examples:

    # One RSS source — 3 years, top 5 per source per day
    python manage.py backfill_history my_rss_feed \\
        --start-date 2022-01-01 --end-date 2025-01-01

    # All enabled RSS sources — 6 months, top 3 per source per day, dry run first
    python manage.py backfill_history \\
        --start-date 2023-06-01 --end-date 2023-12-31 \\
        --top-n 3 --dry-run

    # Resume an interrupted run (checkpoint stored in Redis, keyed by date range)
    python manage.py backfill_history \\
        --start-date 2022-01-01 --end-date 2025-01-01 --resume

    # Backfill from today backward until a specific date (no --end-date needed)
    python manage.py backfill_history my_rss_feed --until 2022-01-01

    # Enqueue as a background Celery job — for long runs
    python manage.py backfill_history \\
        --start-date 2022-01-01 --end-date 2025-01-01 --background

Each chunk runs the live pipeline's order on what it saves: LLM importance
the on-prem NLP annotation pass (importance included).

After a backfill's chunks finish, aggregate the range into Events (the live
aggregate stage only looks back 168h, so it will never reach them):
    python manage.py run_task aggregate_history_task \\
        start_date=2021-07-01 end_date=2026-07-01 --sync
"""
import datetime

from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Backfill top-N historical articles per day window from one or all RSS sources'

    def add_arguments(self, parser):
        parser.add_argument(
            'source_code',
            type=str,
            nargs='?',
            default=None,
            help='Source.code to backfill (an RSS source, or '
                 '"wikipedia-current-events"). Omit to backfill Wikipedia '
                 'Current Events + all enabled RSS sources.',
        )
        parser.add_argument(
            '--start-date',
            dest='start_date',
            metavar='YYYY-MM-DD',
            help='Start date (UTC, inclusive). Required unless --until is given.',
        )
        parser.add_argument(
            '--end-date',
            dest='end_date',
            metavar='YYYY-MM-DD',
            help='End date (UTC, exclusive). Required unless --until is given.',
        )
        parser.add_argument(
            '--until',
            dest='until',
            metavar='YYYY-MM-DD',
            help='Backfill from today backward until this date, without computing '
                 '--start-date/--end-date yourself. Shorthand for '
                 '--start-date <this date> --end-date <today>; cannot be combined '
                 'with --start-date/--end-date.',
        )
        parser.add_argument(
            '--top-n',
            type=int,
            default=None,
            dest='top_n',
            metavar='N',
            help='Articles to keep per source per day. Default: derived from each '
                 'source\'s weight (2–6 by priority).',
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=0.5,
            metavar='SECONDS',
            help='Seconds to wait between sources within a day — reduces API '
                 'rate-limit pressure (default: 0.5)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry_run',
            help='Discover and display results without writing to the database',
        )
        parser.add_argument(
            '--resume',
            action='store_true',
            help='Skip (day, source-chunk) pairs already completed; checkpoint stored in Redis',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue the dispatcher as a background Celery task instead of running directly',
        )

    def handle(self, *args, **kwargs):
        import core.models as m
        from services.data.historical import iter_days
        from services.tasks import backfill_history_task

        source_code: str | None = kwargs['source_code']
        until: str | None = kwargs['until']

        if until is not None:
            if kwargs['start_date'] or kwargs['end_date']:
                self.stderr.write(self.style.ERROR(
                    '--until cannot be combined with --start-date/--end-date.'
                ))
                return
            start_date = _parse_date(until)
            end_date = datetime.datetime.now(datetime.timezone.utc)
        elif kwargs['start_date'] and kwargs['end_date']:
            start_date = _parse_date(kwargs['start_date'])
            end_date = _parse_date(kwargs['end_date'])
        else:
            self.stderr.write(self.style.ERROR(
                'Either --until, or both --start-date and --end-date, are required.'
            ))
            return

        top_n: int = kwargs['top_n']
        delay: float = kwargs['delay']
        dry_run: bool = kwargs['dry_run']
        resume: bool = kwargs['resume']
        all_sources = source_code is None

        if start_date >= end_date:
            self.stderr.write(self.style.ERROR('--start-date must be before --end-date.'))
            return

        from services.data.wikipedia import WIKIPEDIA_SOURCE_CODE, ensure_wikipedia_source

        if all_sources:
            # +1: the wikipedia-current-events source is always included
            # (created on demand by backfill_history_task).
            count = m.Source.objects.filter(
                type=m.SourceType.RSS, is_enabled=True,
            ).count() + 1
        elif source_code == WIKIPEDIA_SOURCE_CODE:
            ensure_wikipedia_source()
        else:
            try:
                m.Source.objects.get(code=source_code)
            except m.Source.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'Source "{source_code}" not found.'))
                return

        # ── Background: enqueue the dispatcher and return ───────────────────────
        if kwargs['background']:
            if dry_run:
                self.stderr.write(self.style.ERROR('--dry-run cannot be combined with --background.'))
                return
            from services.queue import enqueue
            # backfill_history_task is a pure dispatcher (fans out bounded per-day-chunk
            # workers on the heavy queue) — cheap enough not to need job_timeout=-1.
            enqueue(
                backfill_history_task,
                start_date, end_date, source_code,
                top_n=top_n, delay_seconds=delay, resume=resume,
                queue='bulk',
            )
            label = f'wikipedia + all enabled RSS sources ({count})' if all_sources else f'"{source_code}"'
            self.stdout.write(self.style.SUCCESS(
                f'Enqueued backfill_history_task for {label} '
                f'({start_date.date()} → {end_date.date()}) on the bulk queue.'
            ))
            return

        # ── Foreground: run with per-day progress echoed to stdout ─────────────
        total_days = sum(1 for _ in iter_days(start_date, end_date))
        dry_label = '  [DRY RUN — nothing will be written]' if dry_run else ''

        if all_sources:
            top_n_label = (f'{top_n} per source per day' if top_n is not None
                           else '2–6 per source per day (by weight); 25 for wikipedia')
            scope_label = f'wikipedia + all enabled RSS sources ({count})'
        else:
            source = m.Source.objects.get(code=source_code)
            from services.tasks import _weighted_top_n
            top_n_label = (
                f'{top_n} per day' if top_n is not None
                else f'{_weighted_top_n(source.weight)} per day (weight {source.weight})'
            )
            scope_label = f'{source.name} ({source.type})'

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f'\nBackfilling  {scope_label}\n'
                f'  Range   : {start_date.date()} → {end_date.date()}\n'
                f'  Days    : {total_days}\n'
                f'  Top-N   : {top_n_label}\n'
                f'  Delay   : {delay}s between sources within a day'
                + dry_label
            )
        )
        self.stdout.write('')

        # Foreground = TASK_QUEUE_ENABLED=False, so every backfill_day_chunk_task the
        # dispatcher enqueues actually runs synchronously right there and its result
        # dict flows back through enqueue() to this callback — one line per (day,
        # source-chunk), the real unit of work now (was one line per day).
        totals = {'fetched': 0, 'saved': 0, 'scored': 0, 'processed': 0, 'chunks': 0}

        def progress(result: dict):
            totals['fetched'] += result['fetched']
            totals['saved'] += result['saved']
            totals['scored'] += result.get('scored', 0)
            totals['processed'] += result['processed']
            totals['chunks'] += 1
            sources_label = ','.join(result['sources'])
            line = (
                f'  {result["day"]}  [{sources_label}]  '
                f'candidates={result["fetched"]:>4}  '
                f'saved={result["saved"]:>3}  '
                f'processed={result["processed"]:>3}'
            )
            if result['fetched'] == 0:
                self.stdout.write(self.style.WARNING(line + '  (no candidates)'))
            elif result['saved'] == 0 and not dry_run:
                self.stdout.write(self.style.WARNING(line + '  (all already imported)'))
            else:
                self.stdout.write(line)

        summary = backfill_history_task(
            start_date, end_date, source_code,
            top_n=top_n, delay_seconds=delay, dry_run=dry_run, resume=resume,
            progress=progress,
        )

        self.stdout.write('')
        line = (
            f'Done.  {summary["sources"]} source(s) | {summary["days"]} days | '
            f'{totals["chunks"]} chunk(s) | {totals["fetched"]} candidates | '
            f'{totals["saved"]} articles saved | {totals["scored"]} scored | '
            f'{totals["processed"]} processed'
        )
        if dry_run:
            line += '  (dry run — nothing written)'
        self.stdout.write(self.style.SUCCESS(line))

        if not dry_run and totals['saved'] > 0:
            self.stdout.write(
                '\n  Articles are saved and NLP-processed already. To surface them as '
                'Events on the map, aggregate the range:\n'
                f'  python manage.py run_task aggregate_history_task '
                f'start_date={start_date.date()} end_date={end_date.date()} --sync'
            )


def _parse_date(value: str) -> datetime.datetime:
    try:
        d = datetime.date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f'Invalid date {value!r} — expected YYYY-MM-DD format.'
        )
    return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
