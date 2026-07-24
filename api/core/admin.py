import datetime
import logging

from django.contrib import admin, messages
from django.shortcuts import redirect
from import_export import resources
from import_export.admin import ImportExportModelAdmin

from . import models

logger = logging.getLogger(__name__)


def _enqueue_stage_for_selected(modeladmin, request, queryset, stage: str, noun: str, verb: str) -> None:
    """Shared body of the admin's "re-run this pipeline stage on the selected
    rows" actions (reannotate/rerefine on Article, retag/reroute on Event):
    collect ids, dispatch run_stage_chunk_task on the heavy queue, report the
    count. *stage* is a services/stages.py registry name; *noun*/*verb* only
    change the user-facing message.
    """
    from services.queue import enqueue
    from services.tasks import run_stage_chunk_task
    ids = [obj.pk for obj in queryset]
    if ids:
        enqueue(run_stage_chunk_task, stage, ids, queue="heavy")
    modeladmin.message_user(request, f"{verb} enqueued for {len(ids)} {noun}(s).", messages.SUCCESS)


class ImportanceFilter(admin.SimpleListFilter):
    title = "importance"
    parameter_name = "importance"

    def lookups(self, request, model_admin):
        return [
            ("unscored", "Unscored"),
            ("high",     "High ≥ 7"),
            ("medium",   "Medium 4–7"),
            ("low",      "Low < 4"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "unscored":
            return queryset.filter(importance_score__isnull=True)
        if self.value() == "high":
            return queryset.filter(importance_score__gte=7.0)
        if self.value() == "medium":
            return queryset.filter(importance_score__gte=4.0, importance_score__lt=7.0)
        if self.value() == "low":
            return queryset.filter(importance_score__lt=4.0)
        return queryset


class ArticleStageFilter(admin.SimpleListFilter):
    """Filter articles by pipeline position (the stored Article.stage field)."""
    title = "pipeline stage"
    parameter_name = "stage"

    def lookups(self, request, model_admin):
        return [
            ("fetched", "Fetched — awaiting annotation"),
            ("refine", "Awaiting judge (refine)"),
            ("annotated", "Annotated"),
            ("refined", "Refined"),
            ("unlocated", "Annotated but un-located"),
        ]

    def queryset(self, request, queryset):
        from core.models import Article
        if self.value() == "unlocated":
            return queryset.filter(stage__in=[Article.STAGE_ANNOTATED, Article.STAGE_REFINED], location__isnull=True)
        if self.value():
            return queryset.filter(stage=self.value())
        return queryset


class EventStageFilter(admin.SimpleListFilter):
    """Filter events by which pipeline stage they are stuck at (WA3.6)."""
    title = "pipeline gap"
    parameter_name = "stage_gap"

    def lookups(self, request, model_admin):
        return [
            ("untagged", "Untagged"),
            ("keyword", "Keyword-fallback tags"),
            ("unrouted", "Unrouted"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "untagged":
            return queryset.filter(topics_source="")
        if self.value() == "keyword":
            return queryset.filter(topics_source="keyword")
        if self.value() == "unrouted":
            return queryset.filter(is_routed=False)
        return queryset


class SourceResource(resources.ModelResource):
    class Meta:
        model = models.Source
        fields = ("code", "type", "name", "description", "url", "sitemap_url", "author_slug", "is_enabled")
        import_id_fields = ("code",)


class ArticleResource(resources.ModelResource):
    class Meta:
        model = models.Article
        fields = (
            "id", "source_code", "source_type", "title", "content",
            "author", "published_on", "processed_on",
            "sentiment", "location", "latitude", "longitude",
            "event_intensity", "category", "sub_category",
        )
        import_id_fields = ("id",)


class EventResource(resources.ModelResource):
    class Meta:
        model = models.Event
        fields = (
            "id", "title", "location_name", "category", "sub_category",
            "latitude", "longitude", "started_at",
            "article_count", "avg_sentiment", "avg_intensity",
            "source_codes",
        )
        import_id_fields = ("id",)


@admin.register(models.Source)
class SourceAdmin(ImportExportModelAdmin):
    resource_classes = [SourceResource]
    change_list_template = "admin/core/source/change_list.html"
    list_display = ["code", "name", "type", "author_slug", "weight", "weight_locked", "is_enabled", "created_on"]
    list_filter = ["type", "is_enabled", "weight_locked"]
    search_fields = ["name", "code", "author_slug"]

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return [*self.readonly_fields, "code"]
        return self.readonly_fields

    def changelist_view(self, request, extra_context=None):
        if request.method == "POST" and "pipeline_action" in request.POST:
            return self._handle_backfill_action(request)
        extra_context = extra_context or {}
        extra_context["backfill_sources"] = models.Source.objects.filter(
            is_enabled=True,
            type__in=[models.SourceType.RSS],
        ).order_by("name")
        return super().changelist_view(request, extra_context=extra_context)

    def _handle_backfill_action(self, request):
        from services.queue import enqueue
        from services.tasks import backfill_history_task

        source_code = request.POST.get("backfill_source", "").strip()
        start_raw = request.POST.get("backfill_start", "").strip()
        end_raw = request.POST.get("backfill_end", "").strip()
        top_n = max(1, min(100, int(request.POST.get("backfill_top_n") or 5)))

        if not source_code or not start_raw or not end_raw:
            self.message_user(request, "Source, start date, and end date are all required.", messages.ERROR)
            return redirect(request.path)

        try:
            start_date = datetime.datetime(*map(int, start_raw.split("-")), tzinfo=datetime.timezone.utc)
            end_date = datetime.datetime(*map(int, end_raw.split("-")), tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            self.message_user(request, "Invalid date format — use YYYY-MM-DD.", messages.ERROR)
            return redirect(request.path)

        if start_date >= end_date:
            self.message_user(request, "Start date must be before end date.", messages.ERROR)
            return redirect(request.path)

        try:
            source = models.Source.objects.get(code=source_code)
        except models.Source.DoesNotExist:
            self.message_user(request, f'Source "{source_code}" not found.', messages.ERROR)
            return redirect(request.path)

        days = (end_date - start_date).days

        # backfill_history_task is a pure dispatcher (fans out bounded per-day-chunk
        # workers on the heavy queue) — cheap enough not to need job_timeout=-1.
        enqueue(
            backfill_history_task,
            start_date,
            end_date,
            source_code,
            top_n,
            queue="bulk",
        )
        self.message_user(
            request,
            (
                f'Backfill enqueued for "{source.name}" '
                f'({start_date.date()} → {end_date.date()}, ~{days} days, top-{top_n}/day). '
                f'Monitor progress at /admin/dashboard/.'
            ),
            messages.SUCCESS,
        )
        return redirect(request.path)


@admin.register(models.Article)
class ArticleAdmin(ImportExportModelAdmin):
    resource_classes = [ArticleResource]
    change_list_template = "admin/core/article/change_list.html"

    list_display = [
        "id",
        "title",
        "source_code",
        "source_type",
        "category",
        "stage",
        "importance_score",
        "sentiment",
        "location",
        "published_on",
        "created_on",
    ]
    date_hierarchy = "published_on"
    list_filter = ["source_type", "source_code", "category", ArticleStageFilter, "refined_by", "annotator_version", ImportanceFilter]
    search_fields = ["title", "location", "category"]
    autocomplete_fields = ["related"]
    actions = ["reannotate_selected", "rerefine_selected"]

    @admin.action(description="Re-annotate selected (on-prem NLP, importance included)")
    def reannotate_selected(self, request, queryset):
        _enqueue_stage_for_selected(self, request, queryset, 'annotate', 'article', 'Re-annotation')

    @admin.action(description="Re-refine selected (judge again with the current REFINE_PROVIDER)")
    def rerefine_selected(self, request, queryset):
        """Manual override of the refine stage's own stage='refine' selection —
        works on articles in any stage (annotated, refine, or already refined),
        via services.workflow.articles.refine_articles' id-driven contract. Use
        to re-judge with a newly-configured REFINE_PROVIDER, or to redo a
        judgment now considered wrong; Article.refined_by is overwritten with
        whichever provider produced the new verdict."""
        _enqueue_stage_for_selected(self, request, queryset, 'refine', 'article', 'Re-refine')

    readonly_fields = [
        "id",
        "related_events",
        "entities",
        "sentiment",
        "location",
        "event_intensity",
        "category",
        "latitude",
        "longitude",
        "processed_on",
        "stage",
        "refined_on",
        "refined_by",
        "importance_score",
        "importance_source",
        "created_on",
        "updated_on",
    ]

    @admin.display(description="Events built from this article")
    def related_events(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html, format_html_join

        # Event.article_ids is a JSON array of string UUIDs; Mongo equality on
        # an array field matches membership, so this finds every containing event.
        events = models.Event.objects.filter(article_ids=str(obj.id)).only(
            "id", "title", "location_name", "category", "started_at"
        )
        rows = format_html_join(
            "",
            '<li><a href="{}">{}</a> — {} [{}] {}</li>',
            (
                (
                    reverse("admin:core_event_change", args=[e.pk]),
                    e.title,
                    e.location_name,
                    e.category,
                    e.started_at.strftime("%Y-%m-%d %H:%M") if e.started_at else "",
                )
                for e in events
            ),
        )
        if not rows:
            return "— (no event references this article)"
        return format_html('<ul style="margin:0;padding-left:1.2em">{}</ul>', rows)

    def changelist_view(self, request, extra_context=None):
        if request.method == "POST" and "pipeline_action" in request.POST:
            return self._handle_pipeline_action(request)
        return super().changelist_view(request, extra_context=extra_context)

    def _handle_pipeline_action(self, request):
        from services.queue import enqueue
        from services.tasks import dispatch_stage_task, pipeline_tick_task

        action = request.POST["pipeline_action"]

        if action == "run_all":
            # force=True — dispatch every enabled stage with pending work,
            # skipping the per-stage cadence gates (see services/stages.py).
            enqueue(pipeline_tick_task, True, queue='default')
            self.message_user(request, "Pipeline tick enqueued (all due stages).", messages.SUCCESS)
            return redirect(request.path)

        # Stage names map 1:1 onto services/stages.py REGISTRY entries.
        stage = {
            "fetch": "fetch",
            "analyze": "analyze",
            "annotate": "annotate",
            "refine": "refine",
            "aggregate": "aggregate",
        }.get(action)
        if stage is None:
            self.message_user(request, f"Unknown action: {action}", messages.ERROR)
        else:
            enqueue(dispatch_stage_task, stage, queue='default')
            self.message_user(request, f'Stage "{stage}" dispatch enqueued.', messages.SUCCESS)

        return redirect(request.path)


@admin.register(models.Event)
class EventAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "title",
        "location_name",
        "category",
        "article_count",
        "avg_sentiment",
        "avg_intensity",
        "published_on",
        "created_on",
    ]
    date_hierarchy = "started_at"
    list_filter = ["category", EventStageFilter]
    search_fields = ["title", "location_name"]
    readonly_fields = [
        "article_count",
        "avg_sentiment",
        "avg_intensity",
        "article_ids",
        "source_codes",
        "created_on",
        "updated_on",
    ]
    actions = ["retag_selected", "reroute_selected"]

    @admin.display(description="Published", ordering="started_at")
    def published_on(self, obj):
        return obj.started_at

    @admin.action(description="Re-tag topics for selected events")
    def retag_selected(self, request, queryset):
        _enqueue_stage_for_selected(self, request, queryset, 'tag', 'event', 'Re-tag')

    @admin.action(description="Re-route selected events to symbols")
    def reroute_selected(self, request, queryset):
        _enqueue_stage_for_selected(self, request, queryset, 'route', 'event', 'Re-route')


@admin.register(models.PriceTick)
class PriceTickAdmin(admin.ModelAdmin):
    list_display = ["symbol", "stream_key", "name", "value", "change_pct", "volume", "occurred_at"]
    list_filter = ["stream_key"]
    search_fields = ["symbol", "name"]
    readonly_fields = ["occurred_at"]


@admin.register(models.NotamRecord)
class NotamRecordAdmin(admin.ModelAdmin):
    list_display = [
        "notam_id", "notam_type", "status", "location_name",
        "country_code", "effective_from", "effective_to",
    ]
    list_filter = ["status", "notam_type", "source_region", "country_code"]
    search_fields = ["notam_id", "location_name", "country_code"]
    readonly_fields = ["notam_id", "geometry", "raw_text", "fetched_at"]


@admin.register(models.NotamZone)
class NotamZoneAdmin(admin.ModelAdmin):
    list_display = [
        "notam_id", "notam_type", "is_active", "location_name",
        "country_code", "effective_from", "effective_to", "updated_at",
    ]
    list_filter = ["is_active", "notam_type", "country_code"]
    search_fields = ["notam_id", "location_name"]
    readonly_fields = ["notam_id", "geometry", "updated_at"]


@admin.register(models.EarthquakeRecord)
class EarthquakeRecordAdmin(admin.ModelAdmin):
    list_display = [
        "usgs_id", "magnitude", "magnitude_type", "location_name",
        "alert_level", "tsunami_alert", "depth_km", "occurred_at",
    ]
    list_filter = ["alert_level", "tsunami_alert", "magnitude_type"]
    search_fields = ["usgs_id", "location_name"]
    readonly_fields = ["usgs_id", "fetched_at"]


@admin.register(models.Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = [
        "slug", "name", "category", "is_current", "is_active",
        "is_pinned", "is_top_level", "topic_score",
        "source_ids_display", "started_at", "ended_at", "fetched_at",
    ]
    list_filter = ["category", "is_current", "is_active", "is_pinned", "is_top_level"]
    search_fields = ["slug", "name", "description", "keywords"]
    readonly_fields = ["slug", "fetched_at", "source_ids", "topic_score", "is_top_level"]
    actions = ["mark_active", "mark_inactive", "retroactive_tag", "pin_topics"]

    def source_ids_display(self, obj):
        return ", ".join(obj.source_ids or [])
    source_ids_display.short_description = "Sources"

    def save_model(self, request, obj, form, change):
        if obj.is_pinned:
            obj.is_current = True
            obj.is_active = True
            obj.is_top_level = True
        super().save_model(request, obj, form, change)

    @admin.action(description="Mark selected topics as active")
    def mark_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} topic(s) marked active.")

    @admin.action(description="Mark selected topics as inactive")
    def mark_inactive(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} topic(s) marked inactive.")

    @admin.action(description="Pin selected topics (always shown in header)")
    def pin_topics(self, request, queryset):
        count = queryset.update(is_pinned=True, is_current=True, is_active=True, is_top_level=True)
        self.message_user(request, f"{count} topic(s) pinned.")

    @admin.action(description="Retroactively tag events for selected topics")
    def retroactive_tag(self, request, queryset):
        from services.queue import enqueue
        from services.tasks import retroactive_tag_topic_task
        count = 0
        for topic in queryset:
            enqueue(retroactive_tag_topic_task, slug=topic.slug)
            count += 1
        self.message_user(request, f"Enqueued retroactive tagging for {count} topic(s).")

    def changelist_view(self, request, extra_context=None):
        if request.method == "POST" and "topic_action" in request.POST:
            return self._handle_topic_action(request)
        return super().changelist_view(request, extra_context=extra_context)

    def _handle_topic_action(self, request):
        from services.queue import enqueue
        from services.tasks import refresh_topics_task, dispatch_stage_task
        action = request.POST["topic_action"]
        if action == "refresh":
            enqueue(refresh_topics_task)
            self.message_user(request, "Refresh topics job enqueued.", messages.SUCCESS)
        elif action == "tag":
            enqueue(dispatch_stage_task, 'tag', queue='default')
            self.message_user(request, "Tag stage dispatch enqueued.", messages.SUCCESS)
        return redirect(request.path)


class MarketSymbolResource(resources.ModelResource):
    class Meta:
        model = models.MarketSymbol
        fields = (
            "symbol", "name", "stream_key", "provider", "provider_id", "group",
            "is_active", "is_forecast", "is_popular", "rank", "display_order",
        )
        import_id_fields = ("symbol",)


@admin.register(models.MarketSymbol)
class MarketSymbolAdmin(ImportExportModelAdmin):
    resource_classes = [MarketSymbolResource]
    list_display = [
        "symbol", "name", "stream_key", "provider", "group",
        "is_active", "is_forecast", "is_popular", "rank", "display_order",
    ]
    list_filter = ["stream_key", "provider", "group", "is_active", "is_forecast", "is_popular"]
    list_editable = ["is_active", "is_forecast", "is_popular", "rank", "display_order"]
    search_fields = ["symbol", "name", "provider_id"]
    actions = ["enable_active", "disable_active", "mark_forecast", "unmark_forecast"]

    @admin.action(description="Mark selected as active (fetched by streams)")
    def enable_active(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_active=True)} symbol(s) activated.")

    @admin.action(description="Mark selected as inactive")
    def disable_active(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_active=False)} symbol(s) deactivated.")

    @admin.action(description="Add to forecast panel (is_forecast=True)")
    def mark_forecast(self, request, queryset):
        n = queryset.update(is_forecast=True)
        self.message_user(
            request,
            f"{n} symbol(s) added to the forecast panel. Retrains on the next daily "
            f"train_forecast_model_task.",
            messages.WARNING,
        )

    @admin.action(description="Remove from forecast panel")
    def unmark_forecast(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_forecast=False)} symbol(s) removed from panel.")


@admin.register(models.TaskRun)
class TaskRunAdmin(admin.ModelAdmin):
    """The task browser — individual tasks with args/kwargs, result, status,
    retries, and error/traceback. Our RQ-admin / Flower equivalent, backed by
    TaskRun rows that services/queue.py's enqueue() + Celery signal handlers
    keep up to date (see core/models.py::TaskRun and services/queue.py)."""

    list_display = [
        "task_name", "queue", "status", "retries", "result_preview", "duration_ms",
        "started_at", "picked_up_at", "finished_at", "job_id",
    ]
    list_filter = ["status", "queue", "task_name"]
    search_fields = ["task_name", "job_id", "error"]
    readonly_fields = [
        "task_name", "queue", "status", "started_at", "picked_up_at", "finished_at",
        "duration_ms", "items", "result", "retries", "error", "traceback", "params", "job_id",
    ]
    actions = ["cancel_selected"]
    ordering = ["-started_at"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Result")
    def result_preview(self, obj):
        if obj.result is None:
            return "—"
        text = str(obj.result)
        return text if len(text) <= 60 else text[:60] + "…"

    @admin.action(description="Cancel selected (queued/running)")
    def cancel_selected(self, request, queryset):
        from app.celery import app as celery_app

        targets = queryset.filter(status__in=[models.TaskRun.Status.QUEUED, models.TaskRun.Status.RUNNING])
        job_ids = [run.job_id for run in targets if run.job_id]
        if not job_ids:
            self.message_user(request, "Cancel requested for 0 task(s).")
            return
        try:
            # revoke() accepts a list — one control-bus broadcast for the whole
            # selection instead of one round trip per row.
            celery_app.control.revoke(job_ids, terminate=True, signal="SIGTERM")
            cancelled = len(job_ids)
        except Exception:  # noqa: BLE001
            logger.exception("[admin] failed to revoke tasks %s", job_ids)
            cancelled = 0
        self.message_user(request, f"Cancel requested for {cancelled} task(s).")


@admin.register(models.RuntimeConfig)
class RuntimeConfigAdmin(admin.ModelAdmin):
    """Singleton runtime config (LLM master switches). Normally toggled from the
    operations dashboard's Actions section; also editable here for audit/direct
    access. Adding a second row is blocked — RuntimeConfig.load() reads the
    oldest, so extra rows would be ignored and just cause confusion."""

    list_display = ["__str__", "live_llm_enabled", "backfill_llm_enabled", "updated_on"]
    readonly_fields = ["created_on", "updated_on"]

    def has_add_permission(self, request):
        return not models.RuntimeConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.StaticPoint)
class StaticPointAdmin(admin.ModelAdmin):
    list_display = ["code", "point_type", "name", "country", "country_code", "is_active"]
    list_filter = ["point_type", "is_active", "country_code"]
    search_fields = ["code", "name", "country"]

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return [*self.readonly_fields, "code"]
        return self.readonly_fields


# ── Operations dashboard URL (WA5) ───────────────────────────────────────────
# Register a custom admin view at /admin/dashboard/ by wrapping the default site's
# get_urls (consistent with the project's custom changelist templates).
_orig_admin_get_urls = admin.site.get_urls


def _admin_get_urls():
    from django.urls import path
    from .admin_dashboard import dashboard_view

    return [path('dashboard/', admin.site.admin_view(dashboard_view), name='ops_dashboard')] + _orig_admin_get_urls()


admin.site.get_urls = _admin_get_urls

# Make /admin/dashboard/ the default landing page instead of the stock app-list index.
def _admin_index(request, extra_context=None):
    return redirect('admin:ops_dashboard')


admin.site.index = _admin_index


# ── Branding ──────────────────────────────────────────────────────────────────
from django.conf import settings as _settings  # noqa: E402
_app = getattr(_settings, 'APP_NAME', 'eventhorizonai.dev')
_version = getattr(_settings, 'VERSION_NUMBER', '')
admin.site.site_header = f'{_app} v{_version}' if _version else _app
admin.site.site_title = _app
admin.site.index_title = 'Administration'
