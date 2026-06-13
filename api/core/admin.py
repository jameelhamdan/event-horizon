import datetime
from datetime import timedelta

from django.contrib import admin, messages
from django.shortcuts import redirect
from import_export import resources
from import_export.admin import ImportExportModelAdmin

from . import models


class SourceResource(resources.ModelResource):
    class Meta:
        model = models.Source
        fields = ("code", "type", "name", "description", "url", "author_slug", "is_enabled")
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
    list_display = ["code", "name", "type", "author_slug", "is_enabled", "created_on"]
    list_filter = ["type", "is_enabled"]
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
        top_n = max(1, min(100, int(request.POST.get("backfill_top_n") or 10)))

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

        weeks = (end_date - start_date).days // 7 + 1

        # Backfills can run for hours on multi-year ranges — use unlimited timeout.
        enqueue(
            backfill_history_task,
            source_code,
            start_date,
            end_date,
            top_n,
            queue="heavy",
            job_timeout=-1,
        )
        self.message_user(
            request,
            (
                f'Backfill enqueued for "{source.name}" '
                f'({start_date.date()} → {end_date.date()}, ~{weeks} weeks, top-{top_n}/week). '
                f'Monitor progress at /admin/django-rq/.'
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
        "sentiment",
        "location",
        "published_on",
        "processed_on",
    ]
    list_filter = ["source_type", "source_code", "category"]
    search_fields = ["title", "location", "category"]
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
        "created_on",
        "updated_on",
    ]

    def changelist_view(self, request, extra_context=None):
        if request.method == "POST" and "pipeline_action" in request.POST:
            return self._handle_pipeline_action(request)
        return super().changelist_view(request, extra_context=extra_context)

    def _handle_pipeline_action(self, request):
        from django.utils.timezone import now
        from services.queue import enqueue
        from services.tasks import aggregate_events_task, fetch_articles_task, process_articles_task

        action = request.POST["pipeline_action"]

        if action in ("fetch", "run_all"):
            hours = max(1, int(request.POST.get("fetch_hours") or 2))
            source_code = request.POST.get("fetch_source") or None
            start_date = now() - timedelta(hours=hours)
            enqueue(fetch_articles_task, source_code, start_date)
            self.message_user(request, f"Fetch job enqueued - {source_code}, last {hours}h.", messages.SUCCESS)

        if action in ("process", "run_all"):
            limit = max(1, int(request.POST.get("process_limit") or 500))
            enqueue(process_articles_task, limit=limit)
            self.message_user(request, f"Process job enqueued - limit {limit}.", messages.SUCCESS)

        if action in ("aggregate", "run_all"):
            hours = max(1, int(request.POST.get("aggregate_hours") or 24))
            enqueue(aggregate_events_task, hours=hours)
            self.message_user(request, f"Aggregate job enqueued - last {hours}h.", messages.SUCCESS)

        if action not in ("fetch", "process", "aggregate", "run_all"):
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
    list_filter = ["category"]
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
        count = 0
        for topic in queryset:
            topic.is_pinned = True
            topic.is_current = True
            topic.is_active = True
            topic.is_top_level = True
            topic.save(update_fields=['is_pinned', 'is_current', 'is_active', 'is_top_level'])
            count += 1
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
        from services.tasks import refresh_topics_task, tag_topics_task
        action = request.POST["topic_action"]
        if action == "refresh":
            enqueue(refresh_topics_task)
            self.message_user(request, "Refresh topics job enqueued.", messages.SUCCESS)
        elif action == "tag":
            hours = max(1, int(request.POST.get("tag_hours") or 24))
            enqueue(tag_topics_task, hours=hours)
            self.message_user(request, f"Tag topics job enqueued (last {hours}h).", messages.SUCCESS)
        return redirect(request.path)


@admin.register(models.StaticPoint)
class StaticPointAdmin(admin.ModelAdmin):
    list_display = ["code", "point_type", "name", "country", "country_code", "is_active"]
    list_filter = ["point_type", "is_active", "country_code"]
    search_fields = ["code", "name", "country"]

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return [*self.readonly_fields, "code"]
        return self.readonly_fields


@admin.register(models.Forecast)
class ForecastAdmin(admin.ModelAdmin):
    list_display = [
        'symbol', 'stream_key', 'direction', 'confidence',
        'horizon_hours', 'generated_at', 'actual_value',
    ]
    list_filter = ['stream_key', 'direction']
    search_fields = ['symbol']
    readonly_fields = [
        'symbol', 'stream_key', 'generated_at', 'horizon_hours',
        'direction', 'confidence', 'model_name', 'reasoning',
        'event_ids', 'feature_vector', 'actual_value', 'predicted_value',
    ]


