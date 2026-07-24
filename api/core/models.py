from dataclasses import dataclass
from uuid import uuid4
from django.utils.translation import gettext_lazy as _
from django.db import models
from django_mongodb_backend.managers import MongoManager


class AliveMongoManager(MongoManager):
    """Default Article manager — hides soft-deleted rows (is_deleted=True) from
    every ordinary query, so junk (non-article pages, raw-URL/paywall stubs the
    processing path flags via services.data.bodies.is_junk_article) is excluded
    from the pipeline, the API, and the map without a per-query filter. Reach
    the full set (the code that SETS is_deleted, admin, debugging) through
    Article.all_objects."""

    def get_queryset(self):
        # exclude(is_deleted=True), NOT filter(is_deleted=False): in MongoDB the
        # latter ({is_deleted: false}) does not match documents where the field
        # is ABSENT, which would hide every pre-migration article until each was
        # rewritten. `$ne: true` matches absent-or-false, so the field
        # materializes lazily as (deferred) annotate writes it — no backfill,
        # no slow deploy.
        return super().get_queryset().exclude(is_deleted=True)


class SourceType(models.TextChoices):
    WEBSITE = 'website'
    API = 'api'
    RSS = 'rss'
    SOCIAL = 'social'
    EMAIL = 'email'
    NEWSLETTER = 'newsletter'
    DATABASE = 'database'


class Source(models.Model):
    code = models.CharField(max_length=64, unique=True, help_text=_('Unique identifier for the source'))
    type = models.CharField(max_length=64, choices=SourceType.choices)
    name = models.CharField(max_length=128, help_text=_('Display name of the source'))
    description = models.TextField(blank=True)
    url = models.URLField(max_length=255, default='', blank=True, help_text=_('URL of the source, used in website and RSS feeds'))
    sitemap_url = models.URLField(
        max_length=255, default='', blank=True,
        help_text=_(
            'Explicit sitemap URL for historical backfill, when it lives somewhere '
            "other than the standard paths (robots.txt directive, /sitemap.xml, "
            '/sitemap_index.xml, /news-sitemap.xml) or on a different domain than '
            "the feed URL above. Leave blank to use the standard discovery order."
        ),
    )
    author_slug = models.CharField(max_length=255, default='', blank=True, help_text=_('Author/slug of the source'))
    headers = models.JSONField(default=dict, blank=True)
    is_enabled = models.BooleanField(default=True, help_text=_('Uncheck to disable fetching from this source'))

    # Credibility multiplier applied at importance-scoring time (0.1–2.0).
    # weight_locked=True prevents the weekly auto-adjust task from nudging it.
    weight = models.FloatField(default=1.0, help_text=_('Importance score multiplier (0.1–2.0)'))
    weight_locked = models.BooleanField(default=False, help_text=_('Prevent auto-weight adjustment'))

    # Fetch cursor — start of the last *successful* live fetch. The fetch stage
    # fetches since this timestamp (clamped to a 24h floor) instead of a fixed
    # look-back window, so worker/cron downtime longer than the fetch interval
    # no longer silently drops articles published during the gap.
    last_fetched_at = models.DateTimeField(null=True, blank=True)

    updated_on = models.DateTimeField(auto_now=True)
    created_on = models.DateTimeField(auto_now_add=True)

    objects = models.Manager()

    class Meta:
        ordering = ['-created_on']
        indexes = [models.Index(fields=['created_on'])]

    def __str__(self):
        return self.name


class EventCategory(models.TextChoices):
    # Two-level taxonomy top-level (plan §Concepts). Sub-category does the work.
    CONFLICT = 'conflict', _('Conflict')
    DISASTER = 'disaster', _('Disaster')
    ECONOMIC = 'economic', _('Economic')
    POLITICAL = 'political', _('Political')
    HEALTH = 'health', _('Health')
    GENERAL = 'general', _('General')
    # Legacy flat categories — retained only so pre-redesign data still validates.
    # New data is never assigned these (protest→political, crime→conflict).
    PROTEST = 'protest', _('Protest')
    CRIME = 'crime', _('Crime')


class Article(models.Model):
    id = models.UUIDField(default=uuid4, editable=False, primary_key=True)
    source_code = models.CharField(max_length=64)
    source_type = models.CharField(max_length=64, choices=SourceType.choices)
    source_url = models.URLField(max_length=512)

    author = models.CharField(max_length=100)
    author_slug = models.CharField(max_length=100)

    title = models.CharField(max_length=200)
    content = models.TextField()
    published_on = models.DateTimeField()

    related = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    # NLP fields — populated by annotate_articles (and, for judged fields, refine_articles)
    entities = models.JSONField(default=list, blank=True)  # unused — retained for schema stability; not populated
    # sentiment = VADER polarity [-1, 1] (local, rule-based).
    sentiment = models.FloatField(null=True, blank=True)
    # FinBERT signed sentiment [-1, 1] for news article text (domain-matched).
    # Both scores are exposed as separate downstream features; never the predictor.
    finbert_sentiment = models.FloatField(null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True)
    event_intensity = models.FloatField(null=True, blank=True)
    category = models.CharField(
        max_length=64,
        choices=EventCategory.choices,
        null=True,
        blank=True,
        help_text=_('Rule-based event category'),
    )
    sub_category = models.CharField(max_length=64, null=True, blank=True)
    processed_on = models.DateTimeField(null=True, blank=True)

    # Pipeline position — the single stored "where is this article" field,
    # advanced by the annotate/refine stage handlers (services/stages.py
    # predicates are pure equality filters on it):
    #   fetched   → ingested, awaiting the annotate stage
    #   annotated → NLP-annotated with confident classification — terminal
    #   refine    → NLP-annotated but low-confidence — queued for the judge
    #   refined   → re-judged by the refine stage — terminal
    STAGE_FETCHED = 'fetched'
    STAGE_ANNOTATED = 'annotated'
    STAGE_REFINE = 'refine'
    STAGE_REFINED = 'refined'
    stage = models.CharField(max_length=16, default='fetched', db_index=True)
    # When the refine stage re-judged this article (stage='refined').
    refined_on = models.DateTimeField(null=True, blank=True)
    # Which judge produced the current category/sub_category verdict —
    # 'zeroshot' | 'ollama' | 'cloud' (see settings.REFINE_PROVIDER /
    # services/processing/refiner.py). Set by refine_articles, overwritten on
    # every re-refine so it always reflects the provider that judged the
    # article's PRESENT fields, not necessarily the first judge it ever saw.
    refined_by = models.CharField(max_length=16, null=True, blank=True)

    # Which settings.ANNOTATOR_VERSION produced this article's current
    # category/sub_category/geo/intensity — stamped by annotate_articles and
    # refine_articles alongside stage/processed_on/refined_on. Lets
    # annotate_deferred_batch_task skip articles that are already terminal
    # AND already current, so overlapping/re-run reprocess_corpus_task passes
    # become cheap no-ops instead of redundant NLP.
    annotator_version = models.CharField(max_length=64, null=True, blank=True)

    # Set by the 'annotate' pipeline stage (services/stages.py) when a job is
    # enqueued for this article; not cleared explicitly — once the stage
    # advances past 'fetched' the article is excluded from selection regardless
    # of this value. Prevents re-dispatch while an earlier job for the same
    # article is still sitting in the queue (see stages.PROCESS_CLAIM_TTL_HOURS).
    process_queued_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Importance scoring — computed in the annotate stage (rules: intensity base
    # + source weight + corroboration + category floors).
    # importance_source: 'rules' | 'llm' (pre-migration rows) | 'default'
    importance_score = models.FloatField(null=True, blank=True, db_index=True)
    importance_source = models.CharField(max_length=16, null=True, blank=True)

    # Per-stage pipeline outcome tracking — written by each per-record worker.
    # Shape: {"analyze": {"ok": true, "at": "ISO-8601", "error": null},
    #         "annotate": {"ok": true, "at": "...", "error": null},
    #         "refine": {"ok": false, "at": "...", "error": "judge unavailable"}, ...}
    # Makes the *reason* a stage is missing visible (not just that it's missing).
    stage_status = models.JSONField(default=dict, blank=True)

    # Media — populated during fetch (RSS) or annotate_articles (OG image fallback)
    banner_image_url = models.URLField(max_length=512, null=True, blank=True)

    # Geocoding — populated by annotate_articles (NLPAnnotator's NER-extracted
    # country/city → services.processing.geocode.geocode(), a local geonamescache
    # lookup), and re-set by refine_articles if the judge's own place resolves.
    # NOT set by aggregate_events; the Event carries its own lat/lon separately.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    # i18n — subdocument keyed by language code, e.g.:
    # {"en": {"title": "...", "summary": "...", "country": "...", "city": "..."},
    #  "ar": {"title": "...", "summary": "...", "country": "...", "city": "..."}}
    translations = models.JSONField(default=dict, blank=True)

    # Annotation provenance, written by annotate_articles. On-prem annotation
    # is always {"provider": "nlp"}; only the refine stage's 'cloud' provider
    # (services/processing/analyzer.py) fills in model/token counts.
    # {"provider": "groq", "model": "llama-3.1-8b-instant",
    #  "prompt_tokens": 420, "completion_tokens": 180, "total_tokens": 600}
    llm_usage = models.JSONField(default=dict, blank=True)

    # Set on articles saved by a fetch-only backfill (BACKFILL_LLM_ENABLED=False):
    # they are fetched + stored but not annotated, and the live annotate pipeline
    # stage skips them (via .exclude(annotation_deferred=True)) so a large
    # historical backfill doesn't flood the annotator. reprocess_corpus_task
    # (scope=deferred) annotates them later and clears this flag.
    annotation_deferred = models.BooleanField(default=False)

    updated_on = models.DateTimeField(auto_now=True)
    created_on = models.DateTimeField(auto_now_add=True)
    extra_data = models.JSONField(default=dict, blank=True)

    # Cached title embedding for semantic clustering (aggregate_events) — computed
    # once and reused across runs instead of re-encoding on every aggregation pass
    # (the lookback window overlaps runs, so the same article gets re-embedded
    # repeatedly otherwise). title_embedding_model guards against stale vectors if
    # the clustering model is ever swapped.
    title_embedding = models.JSONField(default=list, blank=True)
    title_embedding_model = models.CharField(max_length=128, null=True, blank=True)

    # Soft-delete: junk rows (non-article pages, raw-URL/paywall stubs — see
    # services.data.bodies.is_junk_article) are kept as training data but hidden
    # from every ordinary query by the default manager (AliveMongoManager). The
    # processing path sets this lazily (only junk gets is_deleted=True written);
    # existing rows leave the field absent, matched as not-deleted. Nothing
    # hard-deletes (see the no-deletion policy). No db_index: the field is
    # low-selectivity (almost everything is not-deleted) so an index barely
    # helps, and skipping it keeps the AddField migration a no-op on Mongo.
    is_deleted = models.BooleanField(default=False)

    objects = AliveMongoManager()   # default — excludes is_deleted=True
    all_objects = MongoManager()    # full set, including soft-deleted

    class Meta:
        ordering = ['-created_on']
        # Internals (related-object fetches, refresh_from_db) use the unfiltered
        # manager so a soft-deleted row still resolves via FK and never breaks.
        base_manager_name = 'all_objects'
        indexes = [
            models.Index(fields=['created_on']),
            models.Index(fields=['source_code']),
            models.Index(fields=['author_slug']),
            models.Index(fields=['category']),
            models.Index(fields=['processed_on']),
            models.Index(fields=['location']),
            # reprocess_corpus_task (scope=deferred) selects the (small) deferred set;
            # the live annotate stage excludes it from its pending query.
            models.Index(fields=['annotation_deferred'], name='core_article_annot_defer_idx'),
            # aggregate_events' primary query filters processed_on + published_on range.
            models.Index(fields=['processed_on', 'published_on'], name='core_article_proc_pub_idx'),
            # Dashboard activity chart: month-range filter on published_on, then
            # sort+limit by importance_score to fetch the top N per month.
            models.Index(fields=['published_on', 'importance_score'], name='core_article_pub_imp_idx'),
            # id's only other index is the auto-generated unique constraint,
            # which is a *partial* index (partialFilterExpression id:$type
            # string — django-mongodb-backend's standard null-handling for a
            # unique field). Mongo's planner can't turn an id__in(...) query
            # into tight bounds against a partial index — confirmed locally
            # that even .hint()-forcing it produces an unbounded [MinKey,
            # MaxKey] index scan, no better than COLLSCAN. This plain,
            # non-partial index is what id__in batch lookups (e.g.
            # annotate_deferred_batch_task) actually seek against.
            models.Index(fields=['id'], name='core_article_id_plain_idx'),
        ]

    def __str__(self):
        return self.title

    @property
    def pipeline_state(self) -> str:
        """Where this article sits in the annotation pipeline — the stored
        ``stage`` field, except that backfill-deferred articles surface as
        'deferred' (annotation_deferred parks them off the live pipeline
        regardless of stage). See docs/pipeline-state.md.

        Location is an attribute of an annotated article, not a pipeline step:
        an annotated article with no resolved location is still terminal (it
        just never aggregates into an event).
        """
        return 'deferred' if self.annotation_deferred else self.stage


class Event(models.Model):
    """
    An aggregated event derived from one or more related articles
    at the same location within a time window.
    Populated by the aggregate_events management command.
    """
    title = models.CharField(max_length=512)
    content = models.TextField()
    category = models.CharField(
        max_length=64,
        choices=EventCategory.choices,
        default=EventCategory.GENERAL,
    )

    # Geographic fields
    location_name = models.CharField(max_length=255)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    # Temporal
    started_at = models.DateTimeField(help_text=_('Timestamp of the earliest article'))
    # Event-time for all as-of/point-in-time filtering = max(published_on) over the
    # constituent articles (plan §2). No article published after a forecast time t may
    # contribute to an event used at t — the as-of cut is on this field, not the day bucket.
    latest_article_at = models.DateTimeField(null=True, blank=True)

    # Aggregated metrics
    article_count = models.IntegerField(default=1)
    avg_sentiment = models.FloatField(null=True, blank=True)          # mean article sentiment
    avg_finbert_sentiment = models.FloatField(null=True, blank=True)  # FinBERT mean over articles
    avg_intensity = models.FloatField(null=True, blank=True)

    # Market indicators this event plausibly moves (a FEATURE/hypothesis, not a label).
    # Format: [{"symbol": "GC=F", "weight": 0.42}, ...] (weight signed; see routing.py)
    affected_indicators = models.JSONField(default=list, blank=True)
    # Which router produced affected_indicators — always 'rules' (deterministic, services/forecasting/routing.py).
    router_source = models.CharField(max_length=8, default='rules', blank=True)
    # Mirrors "affected_indicators is non-empty" as a real indexed field — the
    # route stage's pending query used to filter affected_indicators=[] directly,
    # which MongoDB can't serve from an index on a JSONField. Set wherever
    # affected_indicators is set.
    is_routed = models.BooleanField(default=False)

    # References
    article_ids = models.JSONField(default=list)
    source_codes = models.JSONField(default=list)
    sub_categories = models.JSONField(default=list, blank=True)

    # i18n — subdocument keyed by language code, e.g.:
    # {"en": {"title": "...", "location_name": "..."},
    #  "ar": {"title": "...", "location_name": "..."}}
    translations = models.JSONField(default=dict, blank=True)

    # Current global topics this event is connected to.
    # Format: {slug: confidence_score} e.g. {"ukraine-war": 0.92}
    topics = models.JSONField(default=dict, blank=True)

    # Flat slug list for queryable filtering (parallel to topics dict)
    topic_slugs = models.JSONField(default=list, blank=True)

    # Aggregated LLM usage across all constituent articles (written by aggregate_events).
    # {"total_tokens": 1800, "prompt_tokens": 1400, "completion_tokens": 400,
    #  "by_provider": {"groq": {"total_tokens": 1200, ...}, "openrouter": {...}},
    #  "article_count": 3}
    llm_usage = models.JSONField(default=dict, blank=True)

    # Which matcher produced `topics`: 'embed' (EmbeddingTopicMatcher succeeded) or
    # 'keyword' (embedding model was unavailable, fell back to keyword TopicMatcher).
    # Keyword-fallback tags are low-confidence and possibly wrong, so they are
    # re-evaluated on a later tag_events_with_topics run. Empty = untagged.
    topics_source = models.CharField(max_length=8, default='', blank=True)

    # Per-stage pipeline outcome tracking — see Article.stage_status. Stages here are
    # 'tag' and 'route'. Shape: {"route": {"ok": true, "at": "...", "error": null}, ...}
    stage_status = models.JSONField(default=dict, blank=True)

    updated_on = models.DateTimeField(auto_now=True)
    created_on = models.DateTimeField(auto_now_add=True)

    objects = MongoManager()

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['started_at']),
            models.Index(fields=['latest_article_at'], name='core_event_latest__idx'),
            models.Index(fields=['category']),
            models.Index(fields=['location_name']),
            models.Index(fields=['is_routed']),
            # tag stage coverage/selection: filter started_at window, then
            # exclude(topics_source='embed') to find events still needing tagging.
            models.Index(fields=['topics_source'], name='core_event_topics_src_idx'),
            # Dashboard activity chart: month-range filter on started_at, then
            # sort+limit by avg_intensity to fetch the top N per month.
            models.Index(fields=['started_at', 'avg_intensity'], name='core_event_start_int_idx'),
        ]

    def __str__(self):
        return f'{self.location_name} | {self.category} | {self.started_at:%Y-%m-%d}'


class PriceTick(models.Model):
    """One price sample for a symbol, stored for up to 1 year (TTL index)."""
    symbol      = models.CharField(max_length=32)                  # "BTC-USD", "GC=F", "SPY" — covered by compound index prefix
    stream_key  = models.CharField(max_length=32)                  # "crypto", "commodity", "stock", "forex", "bond"
    name        = models.CharField(max_length=64)                  # "Bitcoin", "Gold"
    value       = models.FloatField()
    change_pct  = models.FloatField(null=True, blank=True)         # % vs previous close
    volume      = models.FloatField(null=True, blank=True)
    occurred_at = models.DateTimeField(db_index=True)              # standalone time-range queries

    objects = MongoManager()

    class Meta:
        ordering = ['-occurred_at']
        indexes = [models.Index(fields=['symbol', 'occurred_at']), models.Index(fields=['stream_key'])]

    def __str__(self):
        return f'{self.symbol} {self.value} @ {self.occurred_at:%Y-%m-%d %H:%M}'


class PriceBar(models.Model):
    """Daily OHLC bar for a panel symbol — the training + charting substrate.

    Distinct from PriceTick (high-frequency live samples): PriceBar is a clean daily
    candle backfilled from yfinance (non-crypto) / CoinGecko (crypto). Upserted on
    (symbol, interval, date); no TTL (history is the point).
    """
    symbol      = models.CharField(max_length=32)
    stream_key  = models.CharField(max_length=32)                  # crypto/stock/commodity/index/bond
    name        = models.CharField(max_length=64, blank=True)
    interval    = models.CharField(max_length=8, default='1d')
    open        = models.FloatField(null=True, blank=True)
    high        = models.FloatField(null=True, blank=True)
    low         = models.FloatField(null=True, blank=True)
    close       = models.FloatField()
    volume      = models.FloatField(null=True, blank=True)
    date        = models.DateTimeField()                           # day-anchored UTC midnight
    created_on  = models.DateTimeField(auto_now_add=True)

    objects = MongoManager()

    class Meta:
        ordering = ['-date']
        indexes = [models.Index(fields=['symbol', 'interval', 'date']), models.Index(fields=['date'])]

    def __str__(self):
        return f'{self.symbol} {self.close} @ {self.date:%Y-%m-%d}'


class Forecast(models.Model):
    """A model-backed market forecast for one (symbol, horizon) at a point in time.

    The event→symbol router weights are FEATURES; the supervised label is the realized
    return between two real price nodes (close@t → close@t+horizon). realized_* fields
    are filled by score_forecasts_task once the horizon elapses.
    """
    symbol               = models.CharField(max_length=32)
    stream_key           = models.CharField(max_length=32, blank=True)
    generated_at         = models.DateTimeField()
    as_of_date           = models.DateTimeField()                  # feature cut time t
    horizon_days         = models.IntegerField(default=1)          # 1 or 5

    # Predictions
    direction            = models.CharField(max_length=8, default='neutral')  # up/down/neutral
    proba_up             = models.FloatField(default=0.5)          # calibrated P(up)
    predicted_change_pct = models.FloatField(default=0.0)          # regressor output (%)
    predicted_price      = models.FloatField(null=True, blank=True)
    band_low             = models.FloatField(null=True, blank=True)
    band_high            = models.FloatField(null=True, blank=True)
    confidence           = models.FloatField(default=0.0)          # |proba_up - 0.5| * 2
    current_value        = models.FloatField(null=True, blank=True)  # last close at t

    # Provenance
    router_source        = models.CharField(max_length=8, default='rules')  # llm/rules
    model_version        = models.CharField(max_length=64, blank=True)

    # Realized outcome (scoring)
    realized_direction   = models.CharField(max_length=8, null=True, blank=True)
    realized_change_pct  = models.FloatField(null=True, blank=True)
    is_correct           = models.BooleanField(null=True, blank=True)
    scored_at            = models.DateTimeField(null=True, blank=True)

    created_on           = models.DateTimeField(auto_now_add=True)

    objects = MongoManager()

    class Meta:
        ordering = ['-generated_at']
        indexes = [
            models.Index(fields=['symbol', 'horizon_days', 'generated_at']),
            models.Index(fields=['as_of_date']),
            models.Index(fields=['generated_at']),
            models.Index(fields=['realized_direction']),
        ]

    def __str__(self):
        return f'{self.symbol} h{self.horizon_days}d {self.direction} ({self.proba_up:.2f})'


class NotamRecord(models.Model):
    """NOTAM alert — append-only history."""
    notam_id       = models.CharField(max_length=128, unique=True)  # unique=True creates index
    source_region  = models.CharField(max_length=32)               # "FAA", "ICAO"
    notam_type     = models.CharField(max_length=32)               # "TFR", "prohibited", "restricted", "danger"
    status         = models.CharField(max_length=16)               # "active", "expired", "cancelled"
    effective_from = models.DateTimeField()
    effective_to   = models.DateTimeField(null=True, blank=True)
    geometry       = models.JSONField(default=dict)                # GeoJSON Feature
    altitude_min_ft = models.IntegerField(null=True, blank=True)
    altitude_max_ft = models.IntegerField(null=True, blank=True)
    location_name  = models.CharField(max_length=255, blank=True)
    country_code   = models.CharField(max_length=4, blank=True)
    raw_text       = models.TextField(blank=True)
    fetched_at     = models.DateTimeField(auto_now_add=True)

    objects = MongoManager()

    class Meta:
        ordering = ['-effective_from']
        indexes = [
            models.Index(fields=['effective_from', 'effective_to']),
            models.Index(fields=['status']),
            models.Index(fields=['country_code']),
        ]

    def __str__(self):
        return f'{self.notam_id} [{self.status}]'


class NotamZone(models.Model):
    """Current live NOTAM zone — upserted on every fetch, not appended."""
    notam_id        = models.CharField(max_length=128, unique=True)  # unique=True creates index
    notam_type      = models.CharField(max_length=32)
    geometry        = models.JSONField(default=dict)               # GeoJSON Feature
    effective_from  = models.DateTimeField()
    effective_to    = models.DateTimeField(null=True, blank=True)
    is_active       = models.BooleanField(default=True)            # indexed via Meta.indexes
    location_name   = models.CharField(max_length=255, blank=True)
    country_code    = models.CharField(max_length=4, blank=True)
    altitude_min_ft = models.IntegerField(null=True, blank=True)
    altitude_max_ft = models.IntegerField(null=True, blank=True)
    updated_at      = models.DateTimeField(auto_now=True)

    objects = MongoManager()

    class Meta:
        ordering = ['-effective_from']
        indexes = [models.Index(fields=['is_active']), models.Index(fields=['effective_to'])]

    def __str__(self):
        return f'{self.notam_id} ({"active" if self.is_active else "inactive"})'


class EarthquakeRecord(models.Model):
    """USGS earthquake event."""
    usgs_id        = models.CharField(max_length=32, unique=True)   # unique=True creates index
    magnitude      = models.FloatField()                           # indexed via Meta.indexes
    magnitude_type = models.CharField(max_length=8, blank=True)   # "ml", "mb", "mw"
    depth_km       = models.FloatField(null=True, blank=True)
    location_name  = models.CharField(max_length=255)
    latitude       = models.FloatField()
    longitude      = models.FloatField()
    occurred_at    = models.DateTimeField()                        # indexed via Meta.indexes
    tsunami_alert  = models.BooleanField(default=False)
    alert_level    = models.CharField(max_length=16, blank=True)  # "green", "yellow", "orange", "red"
    fetched_at     = models.DateTimeField(auto_now_add=True)

    objects = MongoManager()

    class Meta:
        ordering = ['-occurred_at']
        indexes = [models.Index(fields=['occurred_at']), models.Index(fields=['magnitude'])]

    def __str__(self):
        return f'M{self.magnitude} {self.location_name} {self.occurred_at:%Y-%m-%d}'


class Topic(models.Model):
    """
    A news topic — either currently active (is_current=True) or historical
    (is_current=False, populated from Wikipedia "On This Day" or aged-off current topics).

    Events are tagged with matching topics via the tag_events_with_topics workflow.
    """
    slug        = models.CharField(max_length=128, unique=True)
    name        = models.CharField(max_length=255)
    keywords    = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True)
    category    = models.CharField(
        max_length=64,
        choices=EventCategory.choices,
        blank=True,
    )
    source_url  = models.URLField(max_length=512, blank=True)

    # Multi-source tracking — which adapters have confirmed this topic
    source_ids  = models.JSONField(default=list, blank=True)

    # is_current=True  → actively in today's news cycle (from Portal:Current_events)
    # is_current=False → historical topic (from "On This Day" or deactivated current topic)
    is_current  = models.BooleanField(default=True)

    # is_active → operational flag; False means suppressed/soft-deleted
    is_active   = models.BooleanField(default=True)

    # Lifecycle (current topics)
    started_at = models.DateTimeField(null=True, blank=True, help_text='When this topic first appeared in the news')
    ended_at    = models.DateTimeField(
        null=True, blank=True,
        help_text='When this topic resolved or faded. Null means still ongoing.',
    )
    fetched_at  = models.DateTimeField(auto_now=True)

    # Topic hierarchy — optional parent topic slug
    parent_slug = models.CharField(max_length=128, null=True, blank=True)

    # Calendar anchor for historical topics (from "On This Day")
    historical_month = models.IntegerField(null=True, blank=True)  # 1-12
    historical_day   = models.IntegerField(null=True, blank=True)  # 1-31
    historical_year  = models.IntegerField(null=True, blank=True)  # e.g. 1989

    # Denormalized count of events tagged with this topic (updated by tag_topics_task)
    event_count = models.IntegerField(default=0)

    # Scoring and top-level promotion (updated by tag_topics_task)
    topic_score  = models.FloatField(default=0.0, help_text='Composite ranking score, updated by tag_topics_task.')
    is_pinned    = models.BooleanField(default=False, help_text='Admin override: always shown in header, never auto-demoted.')
    is_top_level = models.BooleanField(default=False, help_text='Auto-set: shown in UI header when score passes threshold.')

    objects = MongoManager()

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_current']),
            models.Index(fields=['is_active']),
            models.Index(fields=['is_top_level']),
            models.Index(fields=['category']),
            models.Index(fields=['started_at']),
            models.Index(fields=['ended_at']),
            models.Index(fields=['parent_slug']),
            models.Index(fields=['historical_month', 'historical_day']),
        ]

    def __str__(self):
        return self.name


class StaticPointType(models.TextChoices):
    EXCHANGE           = 'exchange',           _('Stock Exchange')
    COMMODITY_EXCHANGE = 'commodity_exchange', _('Commodity Exchange')
    PORT               = 'port',               _('Major Port')
    CENTRAL_BANK       = 'central_bank',       _('Central Bank')


class StaticPoint(models.Model):
    """Static geographic reference point (exchange, port, central bank, etc.)."""
    code         = models.CharField(max_length=32, unique=True)    # unique=True creates index
    point_type   = models.CharField(max_length=32, choices=StaticPointType.choices)  # indexed via Meta.indexes
    name         = models.CharField(max_length=128)
    country      = models.CharField(max_length=64)
    country_code = models.CharField(max_length=4)
    latitude     = models.FloatField()
    longitude    = models.FloatField()
    metadata     = models.JSONField(default=dict)   # timezone, website, symbols, currencies, etc.
    is_active    = models.BooleanField(default=True)

    objects = MongoManager()

    class Meta:
        ordering = ['point_type', 'name']
        indexes = [models.Index(fields=['point_type']), models.Index(fields=['country_code'])]

    def __str__(self):
        return f'{self.name} ({self.code})'


class MarketSymbol(models.Model):
    """A curated market symbol — the single source of truth for what the price
    streams fetch, what the forecasting layer targets, and what the Markets UI
    shows. Replaces the hardcoded symbol lists previously scattered across
    streams/prices.py, forecasting/history.py, forecasting/routing.py, and
    ui/src/lib/symbols.ts. Seeded with defaults by migration 0006.
    """

    class Provider(models.TextChoices):
        YAHOO     = 'yahoo',     _('Yahoo Finance')
        COINGECKO = 'coingecko', _('CoinGecko')
        ECB       = 'ecb',       _('ECB (forex)')

    class StreamKey(models.TextChoices):
        STOCK     = 'stock',     _('Stock')
        CRYPTO    = 'crypto',    _('Crypto')
        COMMODITY = 'commodity', _('Commodity')
        FOREX     = 'forex',     _('Forex')
        BOND      = 'bond',      _('Bond')
        INDEX     = 'index',     _('Index')

    class Group(models.TextChoices):
        TOP_STOCK  = 'top_stock',  _('Top Stock')
        TOP_CRYPTO = 'top_crypto', _('Top Crypto')
        RESOURCE   = 'resource',   _('Resource / Commodity')
        FOREX      = 'forex',      _('Forex')
        BOND       = 'bond',       _('Bond')
        INDEX      = 'index',      _('Index')
        OTHER      = 'other',      _('Other')

    symbol       = models.CharField(max_length=32, unique=True)   # "GC=F", "BTC-USD"
    name         = models.CharField(max_length=128)
    stream_key   = models.CharField(max_length=16, choices=StreamKey.choices, default=StreamKey.STOCK)
    provider     = models.CharField(max_length=16, choices=Provider.choices, default=Provider.YAHOO)
    provider_id  = models.CharField(max_length=64, blank=True)    # CoinGecko id, blank otherwise
    group        = models.CharField(max_length=16, choices=Group.choices, default=Group.OTHER)

    is_active    = models.BooleanField(default=True)   # fetched by the price streams
    is_forecast  = models.BooleanField(default=False)  # a forecasting target (panel symbol)
    is_popular   = models.BooleanField(default=False)  # surfaced in "most popular" lists
    rank         = models.IntegerField(default=0)      # ordering within is_popular
    display_order = models.IntegerField(default=0)     # ordering within a group

    metadata     = models.JSONField(default=dict, blank=True)
    created_on   = models.DateTimeField(auto_now_add=True)
    updated_on   = models.DateTimeField(auto_now=True)

    objects = MongoManager()

    class Meta:
        ordering = ['group', 'display_order', 'symbol']
        indexes = [
            models.Index(fields=['stream_key', 'is_active']),
            models.Index(fields=['is_forecast']),
            models.Index(fields=['group']),
        ]

    def __str__(self):
        return f'{self.symbol} ({self.name})'


class TaskRun(models.Model):
    """One recorded execution of a pipeline/stream task — the data source for the
    admin operations dashboard's throughput stats and the task browser (the
    Django admin change list at /admin/core/taskrun/ doubles as our RQ-admin /
    Flower equivalent). Written centrally by services/queue.py — enqueue() creates
    the row, Celery's task_prerun/task_success/task_failure/task_retry/task_revoked
    signals update it from the worker process — so every run is tracked with no
    per-task boilerplate.
    """

    class Status(models.TextChoices):
        QUEUED    = 'queued',    _('Queued')
        RUNNING   = 'running',   _('Running')
        SUCCESS   = 'success',   _('Success')
        FAILED    = 'failed',    _('Failed')
        CANCELLED = 'cancelled', _('Cancelled')

    task_name    = models.CharField(max_length=128)
    queue        = models.CharField(max_length=16, default='default')
    status       = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    started_at   = models.DateTimeField()  # enqueue time (or call time in sync mode)
    picked_up_at = models.DateTimeField(null=True, blank=True)  # task_prerun — worker actually started it
    finished_at  = models.DateTimeField(null=True, blank=True)
    duration_ms  = models.IntegerField(null=True, blank=True)
    items        = models.IntegerField(null=True, blank=True)   # result count where applicable
    result       = models.JSONField(default=None, null=True, blank=True)  # safe-truncated return value
    retries      = models.IntegerField(default=0)
    error        = models.TextField(blank=True)
    traceback    = models.TextField(blank=True)
    params       = models.JSONField(default=dict, blank=True)
    job_id       = models.CharField(max_length=64, blank=True)  # Celery task id, blank in sync mode

    objects = MongoManager()

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['task_name', 'started_at']),
            models.Index(fields=['status']),
            models.Index(fields=['started_at']),
            # resume_deferred_backfill_task's in-flight dedupe check and admin
            # job-status lookups both filter by job_id — without this, every
            # such lookup COLLSCANs the whole collection.
            models.Index(fields=['job_id'], name='core_taskrun_job_id_idx'),
        ]

    def __str__(self):
        return f'{self.task_name} [{self.status}] {self.started_at:%Y-%m-%d %H:%M}'


class RuntimeConfig(models.Model):
    """Singleton runtime configuration, editable live from the admin dashboard's
    Actions section so operators can flip pipeline behaviour without a redeploy or
    worker restart. Read at execution time (via services/runtime_config.py), so a
    change takes effect on the next tick / next backfill chunk — including an
    already-dispatched, in-flight backfill.

    Deliberately a single row: use RuntimeConfig.load() (never construct rows
    directly) so there is always exactly one to read and mutate.
    """

    # Master LLM switches. The annotate stage is always fully on-prem and is
    # NOT gated by either flag. live_llm_enabled gates the 'analyze' stage
    # (full cloud-LLM analysis of freshly-fetched live articles, every 3h) and
    # also pauses the refine stage when REFINE_PROVIDER is an actual LLM
    # ('ollama'/'cloud') — see services/stages.py::_live_llm_enabled /
    # _refine_enabled; the default 'zeroshot' judge and annotation itself keep
    # running regardless. When live_llm_enabled is off, live articles aren't
    # stranded — they fall through to the free on-prem annotate stage once
    # they age past LIVE_ANALYZE_FRESHNESS_HOURS (services/stages.py).
    # backfill_llm_enabled pauses historical-backfill annotation entirely
    # (fetch/save still happens, annotation is deferred) — see
    # services/tasks.py::backfill_day_chunk_task. The flag is read at
    # execution time in both cases (no restart needed).
    live_llm_enabled = models.BooleanField(default=True)       # 'analyze' stage + refine's LLM providers
    backfill_llm_enabled = models.BooleanField(default=True)   # historical backfill annotation

    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    objects = models.Manager()

    @classmethod
    def load(cls) -> 'RuntimeConfig':
        """Return the singleton row, creating it (with field defaults) on first use."""
        obj = cls.objects.order_by('created_on').first()
        if obj is None:
            obj = cls.objects.create()
        return obj

    def __str__(self):
        return f'RuntimeConfig(live_llm={self.live_llm_enabled}, backfill_llm={self.backfill_llm_enabled})'


# ---------------------------------------------------------------------------
# NLP data transfer objects — used by services/processing/annotator.py
# (ArticleDocument in, ArticleFeatures out) and services/workflow/articles.py
# ---------------------------------------------------------------------------

@dataclass
class ArticleDocument:
    """Input DTO for the NLP pipeline, built from an Article instance."""
    id: str
    title: str
    content: str
    source_code: str
    published_on: str  # ISO-8601 string

    @property
    def full_text(self) -> str:
        return f'{self.title} {self.content}'


@dataclass
class ArticleFeatures:
    """NLP output for a single article, returned by NLPAnnotator."""
    id: str
    sentiment: float        # VADER polarity [-1, 1] (local, rule-based)
    finbert_sentiment: float | None  # FinBERT signed sentiment [-1, 1], news-domain
    location: str | None    # 'City, Country' from NER + gazetteer
    latitude: float | None  # from geonamescache city lookup
    longitude: float | None # from geonamescache city lookup
    event_intensity: float  # rule-rated newsworthiness/severity [0, 1]
    category: str           # classifier-assigned category slug
    sub_category: str | None  # classifier-assigned sub-category slug within category
    llm_data: dict          # annotation summary — stored in article.extra_data['llm']
    translations: dict      # i18n subdocument — stored in article.translations
    llm_usage: dict         # {provider, ...} — provenance of the annotation
    confidence: float = 1.0  # classifier confidence [0, 1] — the caller (services.
    # workflow.articles) compares this against NLPAnnotator.ESCALATE_BELOW to
    # decide whether Article.stage becomes 'annotated' or 'refine'. Deciding
    # the *stage* is a pipeline-orchestration concern, not an annotation one —
    # NLPAnnotator reports confidence and stays free of any Article import.
    llm_error: str | None = None  # set when annotation failed — see mark_stage('annotate')


