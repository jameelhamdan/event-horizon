"""
Backfill top-N historical articles per day window, across one or all RSS sources.

Each day window is fetched for every requested source via the sitemap-based
RSSHistoricalService, capped per-source by recency, merged, cross-source
title-deduped, and saved via Article.objects.get_or_create — fully idempotent.
Saved articles are NOT pre-scored or NLP-processed here; they land exactly
like live-fetched articles (importance_score left NULL) so the normal
score_articles_task / dispatch_process_articles_task cron jobs pick them up.

Examples:

    # One RSS source — 3 years, top 5 per source per day
    python manage.py backfill_history my_rss_feed \\
        --start-date 2022-01-01 --end-date 2025-01-01

    # All enabled RSS sources — 6 months, top 3 per source per day, dry run first
    python manage.py backfill_history \\
        --start-date 2023-06-01 --end-date 2023-12-31 \\
        --top-n 3 --dry-run

    # Resume an interrupted run (checkpoint stored in Django cache)
    python manage.py backfill_history \\
        --start-date 2022-01-01 --end-date 2025-01-01 --resume

    # Backfill from today backward until a specific date (no --end-date needed)
    python manage.py backfill_history my_rss_feed --until 2022-01-01

    # Enqueue as a background RQ job (heavy queue, no timeout) — for long runs
    python manage.py backfill_history \\
        --start-date 2022-01-01 --end-date 2025-01-01 --background

After a backfill, articles are picked up automatically by the normal cron
schedule (score_articles_task, dispatch_process_articles_task). To force it
immediately:
    python manage.py run_task score_articles_task --sync
    python manage.py process_articles --limit <N>
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
            help='Source.code to backfill (must be of type rss). '
                 'Omit to backfill all enabled RSS sources.',
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
            help='Skip days already completed; checkpoint stored in Django cache',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background RQ task (heavy queue, no timeout) instead '
                 'of running directly',
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

        if all_sources:
            count = m.Source.objects.filter(
                type=m.SourceType.RSS, is_enabled=True,
            ).count()
            if count == 0:
                self.stderr.write(self.style.ERROR('No enabled RSS sources to backfill.'))
                return
        else:
            try:
                m.Source.objects.get(code=source_code)
            except m.Source.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'Source "{source_code}" not found.'))
                return

        # ── Background: enqueue and return ────────────────────────────────────
        if kwargs['background']:
            if dry_run:
                self.stderr.write(self.style.ERROR('--dry-run cannot be combined with --background.'))
                return
            from services.queue import enqueue
            # bulk queue + no timeout (-1): multi-year / multi-source backfills outlast
            # the 30-min cap and must not block the live NLP pipeline on the heavy queue.
            enqueue(
                backfill_history_task,
                start_date, end_date, source_code,
                top_n=top_n, delay_seconds=delay, resume=resume,
                queue='bulk', job_timeout=-1,
            )
            label = f'all enabled RSS sources ({count})' if all_sources else f'"{source_code}"'
            self.stdout.write(self.style.SUCCESS(
                f'Enqueued backfill_history_task for {label} '
                f'({start_date.date()} → {end_date.date()}) on the bulk queue.'
            ))
            return

        # ── Foreground: run with per-day progress echoed to stdout ─────────────
        total_days = sum(1 for _ in iter_days(start_date, end_date))
        dry_label = '  [DRY RUN — nothing will be written]' if dry_run else ''

        if all_sources:
            top_n_label = f'{top_n} per source per day' if top_n is not None else '2–6 per source per day (by weight)'
            scope_label = f'all enabled RSS sources ({count})'
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

        def progress(result):
            line = (
                f'  {result.day.date()}  '
                f'candidates={result.fetched:>4}  '
                f'saved={result.saved:>3}'
            )
            if result.fetched == 0:
                self.stdout.write(self.style.WARNING(line + '  (no candidates)'))
            elif result.saved == 0 and not dry_run:
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
            f'{summary["fetched"]} candidates | {summary["saved"]} articles saved'
        )
        if dry_run:
            line += '  (dry run — nothing written)'
        self.stdout.write(self.style.SUCCESS(line))

        if not dry_run and summary['saved'] > 0:
            self.stdout.write(
                '\n  New articles are picked up automatically by the normal cron '
                'schedule (scoring, then NLP processing). To force it now:\n'
                '  python manage.py run_task score_articles_task --sync\n'
                f'  python manage.py process_articles --limit {summary["saved"] + 200}'
            )


def _parse_date(value: str) -> datetime.datetime:
    try:
        d = datetime.date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f'Invalid date {value!r} — expected YYYY-MM-DD format.'
        )
    return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
