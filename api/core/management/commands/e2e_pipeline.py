"""
End-to-end pipeline test command.

Runs each pipeline step in sequence (fetch → process → aggregate → tag),
captures counts and sample records at each stage, and writes a JSON report
to disk for manual inspection.

Usage:
    python manage.py e2e_pipeline
    python manage.py e2e_pipeline --source my_source --hours 12
    python manage.py e2e_pipeline --skip-fetch --skip-process --samples 10
    python manage.py e2e_pipeline --output /tmp/report.json
"""

import json
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone


def _default(obj):
    """JSON serializer for types that aren't natively serialisable."""
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"Not serialisable: {type(obj)!r}")


def _article_snapshot(article) -> dict:
    return {
        "id": str(article.id),
        "title": article.title[:120],
        "source_code": article.source_code,
        "published_on": article.published_on.isoformat() if article.published_on else None,
        "processed_on": article.processed_on.isoformat() if article.processed_on else None,
        "category": article.category,
        "sub_category": article.sub_category,
        "location": article.location,
        "latitude": article.latitude,
        "longitude": article.longitude,
        "sentiment": article.sentiment,
        "event_intensity": article.event_intensity,
        "importance_score": getattr(article, "importance_score", None),
        "importance_source": getattr(article, "importance_source", None),
        "entities": (article.entities or [])[:5],  # first 5 entities
        "banner_image_url": article.banner_image_url,
        "llm": (article.extra_data or {}).get("llm"),
    }


def _event_snapshot(event) -> dict:
    return {
        "id": str(event.id),
        "title": event.title[:120],
        "category": event.category,
        "location_name": event.location_name,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "started_at": event.started_at.isoformat() if event.started_at else None,
        "article_count": event.article_count,
        "avg_sentiment": event.avg_sentiment,
        "avg_intensity": event.avg_intensity,
        "source_codes": event.source_codes,
        "sub_categories": event.sub_categories,
        "topic_slugs": event.topic_slugs or [],
        "topics": event.topics or {},
    }


class Command(BaseCommand):
    help = "Run the full pipeline end-to-end and write a JSON report for manual inspection"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source", type=str, default=None,
            help="Limit fetch/process to a single source code",
        )
        parser.add_argument(
            "--fetch-hours", type=int, default=6,
            help="Look back N hours when fetching articles (default: 6)",
        )
        parser.add_argument(
            "--hours", type=int, default=24,
            help="Look back N hours for process/aggregate/tag steps (default: 24)",
        )
        parser.add_argument(
            "--process-limit", type=int, default=200,
            help="Max articles to process in step 2 (default: 200)",
        )
        parser.add_argument(
            "--samples", type=int, default=5,
            help="Number of sample records to capture per step (default: 5)",
        )
        parser.add_argument(
            "--output", type=str, default=None,
            help="Output JSON file path (default: ./e2e_report_<timestamp>.json)",
        )
        parser.add_argument(
            "--skip-fetch", action="store_true",
            help="Skip step 1 — fetch_articles",
        )
        parser.add_argument(
            "--skip-process", action="store_true",
            help="Skip step 2 — process_articles",
        )
        parser.add_argument(
            "--skip-aggregate", action="store_true",
            help="Skip step 3 — aggregate_events",
        )
        parser.add_argument(
            "--skip-tag", action="store_true",
            help="Skip step 4 — tag_topics",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        from core import models as core_models
        from services.workflow import Workflow

        source = options["source"]
        fetch_hours = options["fetch_hours"]
        hours = options["hours"]
        process_limit = options["process_limit"]
        n = options["samples"]

        timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%dT%H%M%S")
        output_path = Path(options["output"] or f"e2e_report_{timestamp}.json")

        report: dict = {
            "run_at": datetime.now(dt_timezone.utc).isoformat(),
            "params": {
                "source": source,
                "fetch_hours": fetch_hours,
                "hours": hours,
                "process_limit": process_limit,
                "samples": n,
                "skipped_steps": [
                    step for step, flag in [
                        ("fetch", options["skip_fetch"]),
                        ("process", options["skip_process"]),
                        ("aggregate", options["skip_aggregate"]),
                        ("tag", options["skip_tag"]),
                    ] if flag
                ],
            },
            "steps": {},
        }

        lookback = timezone.now() - timedelta(hours=hours)

        # ── Step 1: Fetch ──────────────────────────────────────────────────────
        step1: dict = {"skipped": options["skip_fetch"]}
        if not options["skip_fetch"]:
            self.stdout.write("→ Step 1: fetch_articles …")
            articles_before = core_models.Article.objects.count()
            start_date = datetime.now(dt_timezone.utc) - timedelta(hours=fetch_hours)

            try:
                fetched = Workflow.fetch_articles(source_code=source, start_date=start_date)
                articles_after = core_models.Article.objects.count()
                # Sample the most-recently-created articles
                recent_articles = list(
                    core_models.Article.objects.filter(created_on__gte=start_date).order_by("-created_on")[:n]
                )
                step1.update({
                    "ok": True,
                    "articles_before": articles_before,
                    "articles_after": articles_after,
                    "fetched_count": fetched,
                    "sample_fetched": [_article_snapshot(a) for a in recent_articles],
                })
                self.stdout.write(self.style.SUCCESS(f"  fetched {fetched} new article(s)"))
            except Exception as exc:
                step1.update({"ok": False, "error": str(exc)})
                self.stdout.write(self.style.ERROR(f"  FAILED: {exc}"))
        report["steps"]["1_fetch"] = step1

        # ── Step 2: Process ────────────────────────────────────────────────────
        step2: dict = {"skipped": options["skip_process"]}
        if not options["skip_process"]:
            self.stdout.write("→ Step 2: process_articles …")
            unprocessed_before = core_models.Article.objects.filter(processed_on__isnull=True).count()

            try:
                processed = Workflow.process_articles(
                    limit=process_limit,
                    source_code=source,
                    reprocess=False,
                )
                unprocessed_after = core_models.Article.objects.filter(processed_on__isnull=True).count()
                # Sample recently-processed articles
                recent_processed = list(
                    core_models.Article.objects.filter(processed_on__isnull=False)
                    .order_by("-processed_on")[:n]
                )
                step2.update({
                    "ok": True,
                    "unprocessed_before": unprocessed_before,
                    "unprocessed_after": unprocessed_after,
                    "processed_count": processed,
                    "sample_processed": [_article_snapshot(a) for a in recent_processed],
                })
                self.stdout.write(self.style.SUCCESS(f"  processed {processed} article(s)"))
            except Exception as exc:
                step2.update({"ok": False, "error": str(exc)})
                self.stdout.write(self.style.ERROR(f"  FAILED: {exc}"))
        report["steps"]["2_process"] = step2

        # ── Step 3: Aggregate ──────────────────────────────────────────────────
        step3: dict = {"skipped": options["skip_aggregate"]}
        if not options["skip_aggregate"]:
            self.stdout.write("→ Step 3: aggregate_events …")
            events_before = core_models.Event.objects.count()

            try:
                created, updated = Workflow.aggregate_events(hours=hours, min_articles=1)
                events_after = core_models.Event.objects.count()
                recent_events = list(
                    core_models.Event.objects.filter(started_at__gte=lookback).order_by("-started_at")[:n]
                )
                step3.update({
                    "ok": True,
                    "events_before": events_before,
                    "events_after": events_after,
                    "created_count": created,
                    "updated_count": updated,
                    "sample_events": [_event_snapshot(e) for e in recent_events],
                })
                self.stdout.write(self.style.SUCCESS(
                    f"  aggregated — {created} created, {updated} updated"
                ))
            except Exception as exc:
                step3.update({"ok": False, "error": str(exc)})
                self.stdout.write(self.style.ERROR(f"  FAILED: {exc}"))
        report["steps"]["3_aggregate"] = step3

        # ── Step 4: Tag topics ─────────────────────────────────────────────────
        step4: dict = {"skipped": options["skip_tag"]}
        if not options["skip_tag"]:
            self.stdout.write("→ Step 4: tag_events_with_topics …")
            topics_available = core_models.Topic.objects.filter(is_active=True).count()

            try:
                tagged = Workflow.tag_events_with_topics(hours=hours, force_retag=False)
                # Sample events that actually received topic tags
                tagged_events = list(
                    core_models.Event.objects.filter(
                        started_at__gte=lookback,
                        topic_slugs__isnull=False,
                    ).exclude(topic_slugs=[]).order_by("-started_at")[:n]
                )
                step4.update({
                    "ok": True,
                    "topics_available": topics_available,
                    "events_tagged_count": tagged,
                    "sample_tagged_events": [_event_snapshot(e) for e in tagged_events],
                })
                self.stdout.write(self.style.SUCCESS(f"  tagged {tagged} event(s)"))
            except Exception as exc:
                step4.update({"ok": False, "error": str(exc)})
                self.stdout.write(self.style.ERROR(f"  FAILED: {exc}"))
        report["steps"]["4_tag"] = step4

        # ── Summary ────────────────────────────────────────────────────────────
        summary = {
            "total_articles": core_models.Article.objects.count(),
            "processed_articles": core_models.Article.objects.filter(processed_on__isnull=False).count(),
            "total_events": core_models.Event.objects.count(),
            "events_with_topics": core_models.Event.objects.exclude(topic_slugs=[]).count(),
            "active_topics": core_models.Topic.objects.filter(is_active=True).count(),
            "current_topics": core_models.Topic.objects.filter(is_current=True, is_active=True).count(),
        }
        report["summary"] = summary

        # ── Write report ───────────────────────────────────────────────────────
        output_path.write_text(
            json.dumps(report, indent=2, default=_default),
            encoding="utf-8",
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Report written → {output_path.resolve()}"))
        self.stdout.write(f"  articles total/processed : {summary['total_articles']} / {summary['processed_articles']}")
        self.stdout.write(f"  events total/with topics : {summary['total_events']} / {summary['events_with_topics']}")
        self.stdout.write(f"  active topics            : {summary['active_topics']} ({summary['current_topics']} current)")
