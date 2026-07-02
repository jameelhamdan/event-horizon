import datetime

from django.contrib import admin, messages
from django.shortcuts import redirect
from import_export import resources
from import_export.admin import ImportExportModelAdmin

from . import models


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
    """Filter articles by which pipeline stage they are stuck at (WA3.6)."""
    title = "pipeline gap"
    parameter_name = "stage_gap"

    def lookups(self, request, model_admin):
        return [("unprocessed", "Unprocessed"), ("unlocated", "Processed but un-located")]

    def queryset(self, request, queryset):
        if self.value() == "unprocessed":
            return queryset.filter(processed_on__isnull=True)
        if self.value() == "unlocated":
            return queryset.filter(processed_on__isnull=False, location__isnull=True)
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
            return queryset.filter(affected_indicators=[])
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
            start_date = datetime.datetime(
                *map(int, start_raw.split("-")), tzinfo=datetime.timezone.utc
            )
            end_date = datetime.datetime(
                *map(int, end_raw.split("-")), tzinfo=datetime.timezone.utc
            )
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

        # Backfills can run for hours on multi-year ranges — use unlimited timeout.
        enqueue(
            backfill_history_task,
            start_date,
            end_date,
            source_code,
            top_n,
            queue="bulk",
            job_timeout=-1,
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
        "importance_score",
        "sentiment",
        "location",
        "published_on",
        "processed_on",
    ]
    date_hierarchy = "published_on"
    list_filter = ["source_type", "source_code", "category", ArticleStageFilter, ImportanceFilter]
    search_fields = ["title", "location", "category"]
    actions = ["reprocess_selected", "score_importance_selected"]

    @admin.action(description="Reprocess selected (NLP / geocode)")
    def reprocess_selected(self, request, queryset):
        from services.queue import enqueue
        from services.tasks import process_articles_chunk_task
        ids = [a.id for a in queryset]
        if ids:
            enqueue(process_articles_chunk_task, ids, True, queue="heavy")
        self.message_user(request, f"Reprocess enqueued for {len(ids)} article(s).", messages.SUCCESS)

    @admin.action(description="Score importance (LLM)")
    def score_importance_selected(self, request, queryset):
        from services.queue import enqueue
        from services.tasks import score_articles_task
        ids = [str(a.id) for a in queryset]
        if ids:
            enqueue(score_articles_task, article_ids=ids, queue="heavy")
        self.message_user(request, f"Importance scoring enqueued for {len(ids)} article(s).", messages.SUCCESS)

    readonly_fields = [
        "id",
        "entities",
        "sentiment",
        "location",
        "event_intensity",
        "category",
        "latitude",
        "longitude",
        "processed_on",
        "importance_score",
        "importance_source",
        "created_on",
        "updated_on",
    ]

    def changelist_view(self, request, extra_context=None):
        if request.method == "POST" and "pipeline_action" in request.POST:
            return self._handle_pipeline_action(request)
        return super().changelist_view(request, extra_context=extra_context)

    def _handle_pipeline_action(self, request):
        from services.queue import enqueue
        from services.tasks import (
            aggregate_events_task,
            dispatch_fetch_task,
            dispatch_process_articles_task,
        )

        action = request.POST["pipeline_action"]

        if action == "run_all":
            enqueue(dispatch_fetch_task, queue='default')
            enqueue(dispatch_process_articles_task, queue='default')
            # Bounded (was -1/no cap) — an uncapped job that deadlocks has no
            # watchdog at all and can sit in "started" forever with nothing to
            # show for it. 1h covers a full 168h backlog aggregation with margin.
            enqueue(aggregate_events_task, queue="heavy", job_timeout=3600)
            self.message_user(
                request,
                "Pipeline dispatchers enqueued (fetch → process → aggregate).",
                messages.SUCCESS,
            )
            return redirect(request.path)

        if action == "fetch":
            source_code = request.POST.get("fetch_source") or None
            enqueue(dispatch_fetch_task, queue='default')
            self.message_user(request, f"Fetch dispatched ({source_code or 'all sources'}).", messages.SUCCESS)
        elif action == "process":
            limit = max(1, int(request.POST.get("process_limit") or 500))
            enqueue(dispatch_process_articles_task, limit=limit, queue='default')
            self.message_user(request, f"Process dispatched (limit {limit}).", messages.SUCCESS)
        elif action == "reprocess_failed":
            limit = max(1, int(request.POST.get("process_limit") or 500))
            enqueue(dispatch_process_articles_task, limit=limit, only_failed=True, queue='default')
            self.message_user(
                request,
                f"Re-dispatched up to {limit} processed-but-unlocated articles.",
                messages.SUCCESS,
            )
        elif action == "aggregate":
            hours = max(1, int(request.POST.get("aggregate_hours") or 24))
            enqueue(aggregate_events_task, hours=hours, queue="heavy")
            self.message_user(request, f"Aggregate job enqueued - last {hours}h.", messages.SUCCESS)
        else:
            self.message_user(request, f"Unknown action: {action}", messages.ERROR)

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
        "started_at",
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

    @admin.action(description="Re-tag topics for selected events")
    def retag_selected(self, request, queryset):
        from services.queue import enqueue
        from services.tasks import tag_events_chunk_task
        ids = [e.pk for e in queryset]
        if ids:
            enqueue(tag_events_chunk_task, ids, queue="heavy")
        self.message_user(request, f"Re-tag enqueued for {len(ids)} event(s).", messages.SUCCESS)

    @admin.action(description="Re-route selected events to symbols")
    def reroute_selected(self, request, queryset):
        from services.queue import enqueue
        from services.tasks import route_events_chunk_task
        ids = [e.pk for e in queryset]
        if ids:
            enqueue(route_events_chunk_task, ids, queue="heavy")
        self.message_user(request, f"Re-route enqueued for {len(ids)} event(s).", messages.SUCCESS)


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
        count = queryset.update(
            is_pinned=True, is_current=True, is_active=True, is_top_level=True
        )
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
        from services.tasks import refresh_topics_task, dispatch_tag_topics_task
        action = request.POST["topic_action"]
        if action == "refresh":
            enqueue(refresh_topics_task)
            self.message_user(request, "Refresh topics job enqueued.", messages.SUCCESS)
        elif action == "tag":
            hours = max(1, int(request.POST.get("tag_hours") or 24))
            enqueue(dispatch_tag_topics_task, hours=hours, queue='default')
            self.message_user(request, f"Tag topics dispatched (last {hours}h).", messages.SUCCESS)
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
    list_display = [
        "task_name", "queue", "status", "items", "duration_ms",
        "started_at", "finished_at", "job_id",
    ]
    list_filter = ["status", "queue", "task_name"]
    search_fields = ["task_name", "job_id", "error"]
    readonly_fields = [
        "task_name", "queue", "status", "started_at", "finished_at",
        "duration_ms", "items", "error", "params", "job_id",
    ]

    def has_add_permission(self, request):
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
    return [
        path('dashboard/', admin.site.admin_view(dashboard_view), name='ops_dashboard'),
    ] + _orig_admin_get_urls()


admin.site.get_urls = _admin_get_urls


# ── Branding ──────────────────────────────────────────────────────────────────
from django.conf import settings as _settings  # noqa: E402
_app = getattr(_settings, 'APP_NAME', 'eventhorizonai.dev')
_version = getattr(_settings, 'VERSION_NUMBER', '')
admin.site.site_header = f'{_app} v{_version}' if _version else _app
admin.site.site_title = _app
admin.site.index_title = 'Administration'
