"""
Backfill top-N historical articles per ISO week from a single source.

Each week's candidates are fetched via the RSS historical strategy
(RSSHistoricalService), ranked by LLM significance score, and the top-N
are saved via Article.objects.get_or_create — fully idempotent.

Examples:

    # RSS source — 3 years, top 10 per week
    python manage.py backfill_history my_rss_feed \\
        --start-date 2022-01-01 --end-date 2025-01-01

    # RSS source — 6 months, top 5 per week, dry run first
    python manage.py backfill_history my_rss_feed \\
        --start-date 2023-06-01 --end-date 2023-12-31 \\
        --top-n 5 --dry-run

    # Resume an interrupted run (checkpoint stored in Django cache)
    python manage.py backfill_history my_rss_feed \\
        --start-date 2022-01-01 --end-date 2025-01-01 --resume

    # Enqueue as a background RQ job (heavy queue, no timeout) — for long runs
    python manage.py backfill_history my_rss_feed \\
        --start-date 2022-01-01 --end-date 2025-01-01 --background

    # Backfill ALL enabled RSS sources (omit the source code); --background works too
    python manage.py backfill_history \\
        --start-date 2022-01-01 --end-date 2025-01-01 --background

After a backfill, process newly imported articles through the NLP pipeline:
    python manage.py process_articles --limit <N>
"""
import datetime

from core.management.base import BaseTaskCommand


class Command(BaseTaskCommand):
    help = 'Backfill top-N historical articles per ISO week from a source'

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
            required=True,
            dest='start_date',
            metavar='YYYY-MM-DD',
            help='Start date (UTC, inclusive) — backfill begins on the ISO week containing this date',
        )
        parser.add_argument(
            '--end-date',
            required=True,
            dest='end_date',
            metavar='YYYY-MM-DD',
            help='End date (UTC, exclusive)',
        )
        parser.add_argument(
            '--top-n',
            type=int,
            default=None,
            dest='top_n',
            metavar='N',
            help='Articles to keep per week. Default: derived from each source\'s '
                 'weight (10–25 by priority).',
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=1.0,
            metavar='SECONDS',
            help='Seconds to wait between weeks — reduces API rate-limit pressure (default: 1.0)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            dest='dry_run',
            help='Rank and display results without writing to the database',
        )
        parser.add_argument(
            '--resume',
            action='store_true',
            help='Skip weeks already completed; checkpoint stored in Django cache',
        )
        parser.add_argument(
            '--background', action='store_true',
            help='Enqueue as a background RQ task (heavy queue, no timeout) instead '
                 'of running directly',
        )

    def handle(self, *args, **kwargs):
        import core.models as m
        from services.data.historical import iter_weeks
        from services.tasks import backfill_all_sources_task, backfill_history_task

        source_code: str | None = kwargs['source_code']
        start_date = _parse_date(kwargs['start_date'])
        end_date = _parse_date(kwargs['end_date'])
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
            if all_sources:
                enqueue(
                    backfill_all_sources_task,
                    start_date, end_date,
                    top_n=top_n, delay_seconds=delay, resume=resume,
                    queue='bulk', job_timeout=-1,
                )
                label = f'all enabled RSS sources ({count})'
                task_name = 'backfill_all_sources_task'
            else:
                enqueue(
                    backfill_history_task,
                    source_code, start_date, end_date,
                    top_n=top_n, delay_seconds=delay, resume=resume,
                    queue='bulk', job_timeout=-1,
                )
                label = f'"{source_code}"'
                task_name = 'backfill_history_task'
            self.stdout.write(self.style.SUCCESS(
                f'Enqueued {task_name} for {label} '
                f'({start_date.date()} → {end_date.date()}) on the bulk queue.'
            ))
            return

        # ── Foreground: run with per-week progress echoed to stdout ────────────
        total_weeks = sum(1 for _ in iter_weeks(start_date, end_date))
        dry_label = '  [DRY RUN — nothing will be written]' if dry_run else ''

        def header(source):
            from services.tasks import _weighted_top_n
            top_n_label = (
                f'{top_n} per week' if top_n is not None
                else f'{_weighted_top_n(source.weight)} per week (weight {source.weight})'
            )
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f'\nBackfilling  {source.name}  ({source.type})\n'
                    f'  Range   : {start_date.date()} → {end_date.date()}\n'
                    f'  Weeks   : {total_weeks}\n'
                    f'  Top-N   : {top_n_label}\n'
                    f'  Delay   : {delay}s between weeks'
                    + dry_label
                )
            )
            self.stdout.write('')

        def progress(result):
            line = (
                f'  {result.week_start.date()}  '
                f'candidates={result.fetched:>4}  '
                f'saved={result.saved:>3}'
            )
            if result.fetched == 0:
                self.stdout.write(self.style.WARNING(line + '  (no candidates)'))
            elif result.saved == 0 and not dry_run:
                self.stdout.write(self.style.WARNING(line + '  (all already imported)'))
            else:
                self.stdout.write(line)

        if all_sources:
            summary = backfill_all_sources_task(
                start_date, end_date,
                top_n=top_n, delay_seconds=delay, dry_run=dry_run, resume=resume,
                progress=progress, on_source_start=header,
            )
            scope = f'{summary["sources"]} source(s) | '
        else:
            header(m.Source.objects.get(code=source_code))
            summary = backfill_history_task(
                source_code, start_date, end_date,
                top_n=top_n, delay_seconds=delay, dry_run=dry_run, resume=resume,
                progress=progress,
            )
            scope = ''

        self.stdout.write('')
        line = (
            f'Done.  {scope}{summary["weeks"]} weeks | '
            f'{summary["fetched"]} candidates | '
            f'{summary["saved"]} articles saved'
        )
        if dry_run:
            line += '  (dry run — nothing written)'
        self.stdout.write(self.style.SUCCESS(line))

        if not dry_run and summary['saved'] > 0:
            self.stdout.write(
                '\n  New articles need NLP processing. Run:\n'
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
