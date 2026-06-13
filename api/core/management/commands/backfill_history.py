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

After a backfill, process newly imported articles through the NLP pipeline:
    python manage.py process_articles --limit <N>
"""
import datetime

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Backfill top-N historical articles per ISO week from a source'

    def add_arguments(self, parser):
        parser.add_argument(
            'source_code',
            type=str,
            help='Source.code to backfill (must be of type rss)',
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
            default=10,
            dest='top_n',
            metavar='N',
            help='Articles to keep per week (default: 10)',
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

    def handle(self, *args, **kwargs):
        import core.models as m
        from services.data.historical import (
            HistoricalBackfillService,
            HistoricalServiceError,
            iter_weeks,
        )

        source_code: str = kwargs['source_code']
        start_date = _parse_date(kwargs['start_date'])
        end_date = _parse_date(kwargs['end_date'])
        top_n: int = kwargs['top_n']
        delay: float = kwargs['delay']
        dry_run: bool = kwargs['dry_run']
        resume: bool = kwargs['resume']

        if start_date >= end_date:
            self.stderr.write(self.style.ERROR('--start-date must be before --end-date.'))
            return

        try:
            source = m.Source.objects.get(code=source_code)
        except m.Source.DoesNotExist:
            self.stderr.write(self.style.ERROR(f'Source "{source_code}" not found.'))
            return

        total_weeks = sum(1 for _ in iter_weeks(start_date, end_date))
        dry_label = '  [DRY RUN — nothing will be written]' if dry_run else ''

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f'\nBackfilling  {source.name}  ({source.type})\n'
                f'  Range   : {start_date.date()} → {end_date.date()}\n'
                f'  Weeks   : {total_weeks}\n'
                f'  Top-N   : {top_n} per week\n'
                f'  Delay   : {delay}s between weeks'
                + dry_label
            )
        )

        # Checkpoint — set of week_start ISO strings already completed
        resume_weeks: set[str] = set()
        checkpoint_key = (
            f'backfill:{source_code}:{start_date.date()}:{end_date.date()}:done'
        )
        if resume:
            from django.core.cache import cache
            resume_weeks = cache.get(checkpoint_key) or set()
            if resume_weeks:
                self.stdout.write(
                    f'  Resuming: {len(resume_weeks)} week(s) already done, skipping.\n'
                )

        # Validate strategy before starting the loop
        try:
            service = HistoricalBackfillService(
                source=source,
                start_date=start_date,
                end_date=end_date,
                top_n=top_n,
                delay_seconds=delay,
            )
        except HistoricalServiceError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        self.stdout.write('')
        total_fetched = total_saved = 0

        for result in service.run(resume_weeks=resume_weeks, dry_run=dry_run):
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

            total_fetched += result.fetched
            total_saved += result.saved

            if resume and not dry_run:
                from django.core.cache import cache
                resume_weeks.add(result.week_start.isoformat())
                cache.set(checkpoint_key, resume_weeks, timeout=None)

        # Summary
        self.stdout.write('')
        summary = (
            f'Done.  {total_weeks} weeks | '
            f'{total_fetched} candidates | '
            f'{total_saved} articles saved'
        )
        if dry_run:
            summary += '  (dry run — nothing written)'
        self.stdout.write(self.style.SUCCESS(summary))

        if not dry_run and total_saved > 0:
            self.stdout.write(
                '\n  New articles need NLP processing. Run:\n'
                f'  python manage.py process_articles --limit {total_saved + 200}'
            )


def _parse_date(value: str) -> datetime.datetime:
    try:
        d = datetime.date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f'Invalid date {value!r} — expected YYYY-MM-DD format.'
        )
    return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
