from rest_framework import serializers
from core.models import (
    Article, Event, Source,
    PriceTick, NotamRecord, NotamZone, EarthquakeRecord, StaticPoint,
    Topic,
)
from newsletter.models import DailyNewsletter


class ArticleSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)
    title_ar = serializers.SerializerMethodField()

    def get_title_ar(self, obj):
        return (getattr(obj, 'translations', None) or {}).get('ar', {}).get('title') or obj.title

    class Meta:
        model = Article
        fields = [
            'id',
            'title',
            'title_ar',
            'source_code',
            'source_url',
            'category',
            'sentiment',
            'location',
            'published_on',
        ]


class EventSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)
    title_ar = serializers.SerializerMethodField()
    location_name_ar = serializers.SerializerMethodField()
    source_names = serializers.SerializerMethodField()

    def get_title_ar(self, obj):
        return (getattr(obj, 'translations', None) or {}).get('ar', {}).get('title') or obj.title

    def get_location_name_ar(self, obj):
        return (getattr(obj, 'translations', None) or {}).get('ar', {}).get('location_name') or obj.location_name

    def get_source_names(self, obj):
        source_map = self.context.get('source_map', {})
        return [source_map.get(code, code) for code in (obj.source_codes or [])]

    class Meta:
        model = Event
        fields = [
            'id',
            'title',
            'title_ar',
            'category',
            'sub_categories',
            'location_name',
            'location_name_ar',
            'latitude',
            'longitude',
            'started_at',
            'article_count',
            'avg_sentiment',
            'avg_finbert_sentiment',
            'avg_intensity',
            'affected_indicators',
            'source_codes',
            'source_names',
            'topics',
            'topic_slugs',
        ]


class SourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Source
        fields = ['code', 'name', 'type', 'url']


class PriceTickSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = PriceTick
        fields = ['id', 'symbol', 'stream_key', 'name', 'value', 'change_pct', 'volume', 'occurred_at']


class NotamRecordSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = NotamRecord
        fields = [
            'id', 'notam_id', 'source_region', 'notam_type', 'status',
            'effective_from', 'effective_to', 'geometry',
            'altitude_min_ft', 'altitude_max_ft',
            'location_name', 'country_code', 'raw_text', 'fetched_at',
        ]


class NotamZoneSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = NotamZone
        fields = [
            'id', 'notam_id', 'notam_type', 'geometry', 'is_active',
            'effective_from', 'effective_to',
            'altitude_min_ft', 'altitude_max_ft',
            'location_name', 'country_code', 'updated_at',
        ]


class EarthquakeRecordSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = EarthquakeRecord
        fields = [
            'id', 'usgs_id', 'magnitude', 'magnitude_type', 'depth_km',
            'location_name', 'latitude', 'longitude', 'occurred_at',
            'tsunami_alert', 'alert_level',
        ]


class StaticPointSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = StaticPoint
        fields = [
            'id', 'code', 'point_type', 'name', 'country', 'country_code',
            'latitude', 'longitude', 'metadata', 'is_active',
        ]


class NewsletterListSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = DailyNewsletter
        fields = ['id', 'date', 'subject', 'sent_at', 'event_count', 'status']


class NewsletterDetailSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = DailyNewsletter
        fields = [
            'id', 'date', 'subject', 'body',
            'articles', 'cover_image_url', 'cover_image_credit',
            'generated_at', 'sent_at', 'sent_count', 'event_count', 'status',
        ]


class TopicSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = Topic
        fields = [
            'id', 'slug', 'name', 'keywords', 'description', 'category',
            'source_url', 'source_ids', 'parent_slug',
            'is_current', 'is_active', 'is_pinned', 'is_top_level',
            'started_at', 'ended_at', 'fetched_at',
            'historical_month', 'historical_day', 'historical_year',
            'event_count', 'topic_score',
        ]


class ForecastSerializer(serializers.Serializer):
    """Placeholder market forecast — neutral / zero diff.

    The real prediction layer was removed and is being reworked; this is a stable
    response shape so the UI forecast surface keeps working in the meantime. Not
    backed by a model — synthesized per-symbol from the latest price tick.
    """
    symbol = serializers.CharField()
    stream_key = serializers.CharField()
    generated_at = serializers.DateTimeField()
    horizon_hours = serializers.IntegerField()
    direction = serializers.CharField()
    predicted_change_pct = serializers.FloatField()
    current_value = serializers.FloatField(allow_null=True)
    placeholder = serializers.BooleanField()


class SubscribeSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)

    def validate_email(self, value):
        return value.lower().strip()
