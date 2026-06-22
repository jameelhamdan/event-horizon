"""
Consolidated initial migration for the core app.

Squash of 0001–0004 (a prior squash 0001–0010 plus the pipeline-redesign follow-ups
0002–0004). Combines all schema operations and data seeds into a single file so fresh
installs run one migration. Pre-production squash: the replaced migrations were deleted
(no deployed history to honor), so this carries no `replaces`.

Index rules applied throughout:
  - unique=True implies an index; no separate db_index=True or Meta.Index on the same field.
  - db_index=True is omitted on fields already covered by a Meta.Index entry.
  - A compound Meta.Index on (A, B) covers prefix queries on A, so a standalone
    db_index=True on A alone is redundant when that compound index exists.
"""
import django.db.models.deletion
import django_mongodb_backend.fields
import json
import os
import uuid
from django.db import migrations, models


_FIXTURES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'core', 'fixtures')
)

_NEW_SOURCE_CODES = [
    'reuters-world', 'ap-top', 'voa-world',
    'politico-eu', 'the-hindu', 'japan-times', 'africa-news', 'bbc-middle-east',
    'straits-times', 'foreign-policy',
    'ft-world', 'bloomberg-markets', 'forbes-business', 'wsj-markets', 'business-insider',
    'imf-news', 'world-bank-blog', 'project-syndicate', 'brookings', 'oecd-news',
    'guardian-crime', 'reuters-crime', 'interpol-news', 'un-crime', 'propublica', 'occrp',
    'venturebeat', 'zdnet', 'ieee-spectrum', 'engadget', 'cnet-tech',
]

_NEW_POINT_CODES = [
    'NYSE', 'NASDAQ', 'LSE', 'TSE', 'SSE', 'HKEX', 'XETRA', 'EURONEXT-PAR', 'TSX', 'BSE',
    'SGX', 'KRX', 'SIX', 'MOEX', 'ASX', 'DFM', 'JSE', 'B3', 'TADAWUL', 'BIST',
    'CME', 'NYMEX', 'LME', 'ICE-LON', 'DCE', 'SHFE', 'TOCOM',
    'PORT-ROTTERDAM', 'PORT-SINGAPORE', 'PORT-SHANGHAI', 'PORT-SHENZHEN', 'PORT-HONGKONG',
    'PORT-NINGBO', 'PORT-BUSAN', 'PORT-JEBEL-ALI', 'PORT-LA', 'PORT-HAMBURG',
    'PORT-ANTWERP', 'PORT-NY', 'PORT-GUANGZHOU', 'PORT-KAOHSIUNG', 'PORT-SANTOS',
    'PORT-DURBAN', 'PORT-MUMBAI', 'PORT-FELIXSTOWE', 'PORT-SUEZ', 'PORT-COLOMBO',
    'FED', 'ECB', 'BOE', 'BOJ', 'PBOC', 'RBA', 'SNB', 'BOC', 'RBI', 'CBR', 'BCB',
    'SARB', 'BOK', 'MAS', 'SAMA', 'BANXICO', 'TCMB', 'CBE', 'NORGES', 'RIKSBANK', 'BI',
]


# ── Data migration helpers ─────────────────────────────────────────────────────

def load_rss_sources(apps, schema_editor):
    Source = apps.get_model('core', 'Source')
    fixtures_path = os.path.join(_FIXTURES_DIR, 'initial_rss_sources.json')
    with open(fixtures_path, encoding='utf-8') as f:
        sources = json.load(f)
    existing = set(Source.objects.values_list('code', flat=True))
    created = 0
    for entry in sources:
        fields = entry['fields']
        if fields['code'] not in existing:
            Source.objects.create(**fields)
            created += 1
    print(f'\n[migration] Created {created} RSS source(s), skipped {len(sources) - created} existing.')


def unload_rss_sources(apps, schema_editor):
    Source = apps.get_model('core', 'Source')
    Source.objects.filter(code__in=[
        'reuters-world', 'bbc-world', 'ap-top', 'aljazeera-world',
        'guardian-world', 'france24-world', 'dw-world', 'euronews-world',
        'rfi-world', 'voa-world', 'npr-world', 'sky-world',
        'rt-world', 'tass-world', 'xinhua-world',
        'middle-east-eye', 'dawn-pk', 'scmp-world',
    ]).delete()


def load_additional_sources(apps, schema_editor):
    Source = apps.get_model('core', 'Source')
    with open(os.path.join(_FIXTURES_DIR, 'additional_sources.json'), encoding='utf-8') as f:
        entries = json.load(f)
    existing = set(Source.objects.values_list('code', flat=True))
    created = 0
    for entry in entries:
        fields = entry['fields']
        if fields['code'] not in existing:
            Source.objects.create(**fields)
            created += 1
    print(f'\n[migration] Created {created} source(s), skipped {len(entries) - created} existing.')


def unload_additional_sources(apps, schema_editor):
    Source = apps.get_model('core', 'Source')
    deleted, _ = Source.objects.filter(code__in=_NEW_SOURCE_CODES).delete()
    print(f'\n[migration] Deleted {deleted} source(s).')


def load_static_points(apps, schema_editor):
    StaticPoint = apps.get_model('core', 'StaticPoint')
    with open(os.path.join(_FIXTURES_DIR, 'static_points.json'), encoding='utf-8') as f:
        entries = json.load(f)
    existing = set(StaticPoint.objects.values_list('code', flat=True))
    created = 0
    for entry in entries:
        fields = entry['fields']
        if fields['code'] not in existing:
            StaticPoint.objects.create(**fields)
            created += 1
    print(f'\n[migration] Created {created} static point(s), skipped {len(entries) - created} existing.')


def unload_static_points(apps, schema_editor):
    StaticPoint = apps.get_model('core', 'StaticPoint')
    deleted, _ = StaticPoint.objects.filter(code__in=_NEW_POINT_CODES).delete()
    print(f'\n[migration] Deleted {deleted} static point(s).')


# ── Migration ──────────────────────────────────────────────────────────────────

class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Source',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(help_text='Unique identifier for the source', max_length=64, unique=True)),
                ('type', models.CharField(choices=[('telegram', 'Telegram'), ('website', 'Website'), ('api', 'Api'), ('rss', 'Rss'), ('social', 'Social'), ('email', 'Email'), ('newsletter', 'Newsletter'), ('database', 'Database')], max_length=64)),
                ('name', models.CharField(help_text='Display name of the source', max_length=128)),
                ('description', models.TextField(blank=True)),
                ('url', models.URLField(blank=True, default='', help_text='URL of the source, used in website and RSS feeds', max_length=255)),
                ('author_slug', models.CharField(blank=True, default='', help_text='Author of the source, used in telegram as channel username', max_length=255)),
                ('headers', models.JSONField(blank=True, default=dict)),
                ('is_enabled', models.BooleanField(default=True, help_text='Uncheck to disable fetching from this source')),
                ('updated_on', models.DateTimeField(auto_now=True)),
                ('created_on', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-created_on'],
                'indexes': [models.Index(fields=['created_on'], name='core_source_created_26fa74_idx')],
            },
        ),
        migrations.CreateModel(
            name='Article',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('source_code', models.CharField(max_length=64)),
                ('source_type', models.CharField(choices=[('telegram', 'Telegram'), ('website', 'Website'), ('api', 'Api'), ('rss', 'Rss'), ('social', 'Social'), ('email', 'Email'), ('newsletter', 'Newsletter'), ('database', 'Database')], max_length=64)),
                ('source_url', models.URLField(max_length=512)),
                ('author', models.CharField(max_length=100)),
                ('author_slug', models.CharField(max_length=100)),
                ('title', models.CharField(max_length=200)),
                ('content', models.TextField()),
                ('published_on', models.DateTimeField()),
                ('related', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.article')),
                ('entities', models.JSONField(blank=True, default=list)),
                ('sentiment', models.FloatField(blank=True, null=True)),
                ('location', models.CharField(blank=True, max_length=255, null=True)),
                ('event_intensity', models.FloatField(blank=True, null=True)),
                ('category', models.CharField(blank=True, choices=[('conflict', 'Conflict'), ('protest', 'Protest'), ('disaster', 'Disaster'), ('political', 'Political'), ('economic', 'Economic'), ('crime', 'Crime'), ('general', 'General')], help_text='Rule-based event category', max_length=64, null=True)),
                ('sub_category', models.CharField(blank=True, max_length=64, null=True)),
                ('latitude', models.FloatField(blank=True, null=True)),
                ('longitude', models.FloatField(blank=True, null=True)),
                ('processed_on', models.DateTimeField(blank=True, null=True)),
                ('updated_on', models.DateTimeField(auto_now=True)),
                ('created_on', models.DateTimeField(auto_now_add=True)),
                ('extra_data', models.JSONField(blank=True, default=dict)),
                ('banner_image_url', models.URLField(blank=True, max_length=512, null=True)),
                ('translations', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'ordering': ['-created_on'],
                'indexes': [models.Index(fields=['created_on'], name='core_articl_created_7c1afd_idx'), models.Index(fields=['source_code'], name='core_articl_source__16229e_idx'), models.Index(fields=['author_slug'], name='core_articl_author__a84eca_idx'), models.Index(fields=['category'], name='core_articl_categor_1aa045_idx'), models.Index(fields=['processed_on'], name='core_articl_process_0f6d5b_idx'), models.Index(fields=['location'], name='core_articl_locatio_205cda_idx')],
            },
        ),
        migrations.CreateModel(
            name='Event',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=512)),
                ('content', models.TextField()),
                ('category', models.CharField(choices=[('conflict', 'Conflict'), ('protest', 'Protest'), ('disaster', 'Disaster'), ('political', 'Political'), ('economic', 'Economic'), ('crime', 'Crime'), ('general', 'General')], default='general', max_length=64)),
                ('location_name', models.CharField(max_length=255)),
                ('latitude', models.FloatField(blank=True, null=True)),
                ('longitude', models.FloatField(blank=True, null=True)),
                ('started_at', models.DateTimeField(help_text='Timestamp of the earliest article')),
                ('article_count', models.IntegerField(default=1)),
                ('avg_sentiment', models.FloatField(blank=True, null=True)),
                ('avg_intensity', models.FloatField(blank=True, null=True)),
                ('article_ids', models.JSONField(default=list)),
                ('source_codes', models.JSONField(default=list)),
                ('sub_categories', models.JSONField(blank=True, default=list)),
                ('translations', models.JSONField(blank=True, default=dict)),
                ('topics', models.JSONField(blank=True, default=list)),
                ('topic_slugs', models.JSONField(blank=True, default=list)),
                ('updated_on', models.DateTimeField(auto_now=True)),
                ('created_on', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-started_at'],
                'indexes': [models.Index(fields=['started_at'], name='core_event_started_9ab2e7_idx'), models.Index(fields=['category'], name='core_event_categor_0ee9fa_idx'), models.Index(fields=['location_name'], name='core_event_locatio_09eaed_idx')],
            },
        ),
        migrations.RunPython(
            code=load_rss_sources,
            reverse_code=unload_rss_sources,
        ),
        migrations.CreateModel(
            name='PriceTick',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('symbol', models.CharField(max_length=32)),
                ('stream_key', models.CharField(max_length=32)),
                ('name', models.CharField(max_length=64)),
                ('value', models.FloatField()),
                ('change_pct', models.FloatField(blank=True, null=True)),
                ('volume', models.FloatField(blank=True, null=True)),
                ('occurred_at', models.DateTimeField(db_index=True)),
            ],
            options={
                'ordering': ['-occurred_at'],
                'indexes': [models.Index(fields=['symbol', 'occurred_at'], name='core_pricet_symbol_983d49_idx'), models.Index(fields=['stream_key'], name='core_pricet_stream__2bdb1f_idx')],
            },
        ),
        migrations.CreateModel(
            name='NotamRecord',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('notam_id', models.CharField(max_length=128, unique=True)),
                ('source_region', models.CharField(max_length=32)),
                ('notam_type', models.CharField(max_length=32)),
                ('status', models.CharField(max_length=16)),
                ('effective_from', models.DateTimeField()),
                ('effective_to', models.DateTimeField(blank=True, null=True)),
                ('geometry', models.JSONField(default=dict)),
                ('altitude_min_ft', models.IntegerField(blank=True, null=True)),
                ('altitude_max_ft', models.IntegerField(blank=True, null=True)),
                ('location_name', models.CharField(blank=True, max_length=255)),
                ('country_code', models.CharField(blank=True, max_length=4)),
                ('raw_text', models.TextField(blank=True)),
                ('fetched_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-effective_from'],
                'indexes': [models.Index(fields=['effective_from', 'effective_to'], name='core_notamr_effecti_9c37ae_idx'), models.Index(fields=['status'], name='core_notamr_status_2456bd_idx'), models.Index(fields=['country_code'], name='core_notamr_country_e7fe20_idx')],
            },
        ),
        migrations.CreateModel(
            name='NotamZone',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('notam_id', models.CharField(max_length=128, unique=True)),
                ('notam_type', models.CharField(max_length=32)),
                ('geometry', models.JSONField(default=dict)),
                ('effective_from', models.DateTimeField()),
                ('effective_to', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('location_name', models.CharField(blank=True, max_length=255)),
                ('country_code', models.CharField(blank=True, max_length=4)),
                ('altitude_min_ft', models.IntegerField(blank=True, null=True)),
                ('altitude_max_ft', models.IntegerField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-effective_from'],
                'indexes': [models.Index(fields=['is_active'], name='core_notamz_is_acti_b7fb52_idx'), models.Index(fields=['effective_to'], name='core_notamz_effecti_b8d28e_idx')],
            },
        ),
        migrations.CreateModel(
            name='EarthquakeRecord',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('usgs_id', models.CharField(max_length=32, unique=True)),
                ('magnitude', models.FloatField()),
                ('magnitude_type', models.CharField(blank=True, max_length=8)),
                ('depth_km', models.FloatField(blank=True, null=True)),
                ('location_name', models.CharField(max_length=255)),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('occurred_at', models.DateTimeField()),
                ('tsunami_alert', models.BooleanField(default=False)),
                ('alert_level', models.CharField(blank=True, max_length=16)),
                ('fetched_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-occurred_at'],
                'indexes': [models.Index(fields=['occurred_at'], name='core_earthq_occurre_0b8b8c_idx'), models.Index(fields=['magnitude'], name='core_earthq_magnitu_41d2bf_idx')],
            },
        ),
        migrations.CreateModel(
            name='StaticPoint',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=32, unique=True)),
                ('point_type', models.CharField(choices=[('exchange', 'Stock Exchange'), ('commodity_exchange', 'Commodity Exchange'), ('port', 'Major Port'), ('central_bank', 'Central Bank')], max_length=32)),
                ('name', models.CharField(max_length=128)),
                ('country', models.CharField(max_length=64)),
                ('country_code', models.CharField(max_length=4)),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('metadata', models.JSONField(default=dict)),
                ('is_active', models.BooleanField(default=True)),
            ],
            options={
                'ordering': ['point_type', 'name'],
                'indexes': [models.Index(fields=['point_type'], name='core_static_point_t_bb5f0e_idx'), models.Index(fields=['country_code'], name='core_static_country_5c3f60_idx')],
            },
        ),
        migrations.RunPython(
            code=load_additional_sources,
            reverse_code=unload_additional_sources,
        ),
        migrations.RunPython(
            code=load_static_points,
            reverse_code=unload_static_points,
        ),
        migrations.CreateModel(
            name='Topic',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slug', models.CharField(max_length=128, unique=True)),
                ('name', models.CharField(max_length=255)),
                ('keywords', models.JSONField(blank=True, default=list)),
                ('description', models.TextField(blank=True)),
                ('category', models.CharField(blank=True, choices=[('conflict', 'Conflict'), ('protest', 'Protest'), ('disaster', 'Disaster'), ('political', 'Political'), ('economic', 'Economic'), ('crime', 'Crime'), ('general', 'General')], max_length=64)),
                ('source_url', models.URLField(blank=True, max_length=512)),
                ('source_ids', models.JSONField(blank=True, default=list)),
                ('is_active', models.BooleanField(default=True)),
                ('is_current', models.BooleanField(default=True)),
                ('is_pinned', models.BooleanField(default=False)),
                ('is_top_level', models.BooleanField(default=False)),
                ('parent_slug', models.CharField(blank=True, max_length=128, null=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('fetched_at', models.DateTimeField(auto_now=True)),
                ('historical_month', models.IntegerField(blank=True, null=True)),
                ('historical_day', models.IntegerField(blank=True, null=True)),
                ('historical_year', models.IntegerField(blank=True, null=True)),
                ('event_count', models.IntegerField(default=0)),
                ('topic_score', models.FloatField(default=0.0)),
            ],
            options={
                'ordering': ['name'],
                'indexes': [models.Index(fields=['is_active'], name='core_currenttopic_active_idx'), models.Index(fields=['category'], name='core_currenttopic_cat_idx'), models.Index(fields=['started_at'], name='core_currenttopic_started_idx'), models.Index(fields=['ended_at'], name='core_currenttopic_ended_idx'), models.Index(fields=['parent_slug'], name='core_currenttopic_parent_idx'), models.Index(fields=['is_current'], name='core_topic_is_current_idx'), models.Index(fields=['historical_month', 'historical_day'], name='core_topic_hist_month_day_idx')],
            },
        ),
        migrations.CreateModel(
            name='Forecast',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('symbol', models.CharField(max_length=32)),
                ('stream_key', models.CharField(max_length=32)),
                ('generated_at', models.DateTimeField()),
                ('horizon_hours', models.IntegerField(default=4)),
                ('direction', models.CharField(choices=[('up', 'Up'), ('down', 'Down'), ('neutral', 'Neutral')], max_length=16)),
                ('confidence', models.FloatField()),
                ('predicted_value', models.FloatField(blank=True, null=True)),
                ('actual_value', models.FloatField(blank=True, null=True)),
                ('model_name', models.CharField(max_length=128)),
                ('reasoning', models.TextField(blank=True)),
                ('event_ids', models.JSONField(default=list)),
                ('feature_vector', models.JSONField(default=dict)),
            ],
            options={
                'ordering': ['-generated_at'],
                'indexes': [models.Index(fields=['symbol', 'generated_at'], name='core_forecast_symbol_gen_idx'), models.Index(fields=['stream_key'], name='core_forecast_stream_key_idx'), models.Index(fields=['generated_at'], name='core_forecast_generated_at_idx')],
            },
        ),
        migrations.RenameIndex(
            model_name='forecast',
            new_name='core_foreca_symbol_10ab5f_idx',
            old_name='core_forecast_symbol_gen_idx',
        ),
        migrations.RenameIndex(
            model_name='forecast',
            new_name='core_foreca_stream__1c3b6f_idx',
            old_name='core_forecast_stream_key_idx',
        ),
        migrations.RenameIndex(
            model_name='forecast',
            new_name='core_foreca_generat_1afbef_idx',
            old_name='core_forecast_generated_at_idx',
        ),
        migrations.RenameIndex(
            model_name='topic',
            new_name='core_topic_is_curr_ff956c_idx',
            old_name='core_topic_is_current_idx',
        ),
        migrations.RenameIndex(
            model_name='topic',
            new_name='core_topic_is_acti_37701b_idx',
            old_name='core_currenttopic_active_idx',
        ),
        migrations.RenameIndex(
            model_name='topic',
            new_name='core_topic_categor_6a12b8_idx',
            old_name='core_currenttopic_cat_idx',
        ),
        migrations.RenameIndex(
            model_name='topic',
            new_name='core_topic_started_1764c0_idx',
            old_name='core_currenttopic_started_idx',
        ),
        migrations.RenameIndex(
            model_name='topic',
            new_name='core_topic_ended_a_d6c923_idx',
            old_name='core_currenttopic_ended_idx',
        ),
        migrations.RenameIndex(
            model_name='topic',
            new_name='core_topic_parent__0f1fb8_idx',
            old_name='core_currenttopic_parent_idx',
        ),
        migrations.RenameIndex(
            model_name='topic',
            new_name='core_topic_histori_bcdeea_idx',
            old_name='core_topic_hist_month_day_idx',
        ),
        migrations.AlterField(
            model_name='event',
            name='topics',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='forecast',
            name='id',
            field=django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'),
        ),
        migrations.AlterField(
            model_name='topic',
            name='ended_at',
            field=models.DateTimeField(blank=True, help_text='When this topic resolved or faded. Null means still ongoing.', null=True),
        ),
        migrations.AlterField(
            model_name='topic',
            name='is_pinned',
            field=models.BooleanField(default=False, help_text='Admin override: always shown in header, never auto-demoted.'),
        ),
        migrations.AlterField(
            model_name='topic',
            name='is_top_level',
            field=models.BooleanField(default=False, help_text='Auto-set: shown in UI header when score passes threshold.'),
        ),
        migrations.AlterField(
            model_name='topic',
            name='started_at',
            field=models.DateTimeField(blank=True, help_text='When this topic first appeared in the news', null=True),
        ),
        migrations.AlterField(
            model_name='topic',
            name='topic_score',
            field=models.FloatField(default=0.0, help_text='Composite ranking score, updated by tag_topics_task.'),
        ),
        migrations.AddIndex(
            model_name='topic',
            index=models.Index(fields=['is_top_level'], name='core_topic_is_top__f4d612_idx'),
        ),
        migrations.AlterField(
            model_name='source',
            name='type',
            field=models.CharField(choices=[('website', 'Website'), ('api', 'Api'), ('rss', 'Rss'), ('social', 'Social'), ('email', 'Email'), ('newsletter', 'Newsletter'), ('database', 'Database')], max_length=64),
        ),
        migrations.AlterField(
            model_name='source',
            name='author_slug',
            field=models.CharField(blank=True, default='', help_text='Author/slug of the source', max_length=255),
        ),
        migrations.AddField(
            model_name='article',
            name='finbert_sentiment',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='avg_finbert_sentiment',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='latest_article_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['latest_article_at'], name='core_event_latest__idx'),
        ),
        migrations.AddField(
            model_name='event',
            name='affected_indicators',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name='forecast',
            name='horizon_hours',
            field=models.IntegerField(default=24),
        ),
        migrations.AddField(
            model_name='forecast',
            name='magnitude_bucket',
            field=models.CharField(blank=True, choices=[('strong_down', 'Strong down'), ('down', 'Down'), ('flat', 'Flat'), ('up', 'Up'), ('strong_up', 'Strong up')], max_length=16),
        ),
        migrations.AddField(
            model_name='forecast',
            name='actual_bucket',
            field=models.CharField(blank=True, choices=[('strong_down', 'Strong down'), ('down', 'Down'), ('flat', 'Flat'), ('up', 'Up'), ('strong_up', 'Strong up')], max_length=16),
        ),
        migrations.AddField(
            model_name='forecast',
            name='volatility_bucket',
            field=models.CharField(blank=True, choices=[('calm', 'Calm'), ('normal', 'Normal'), ('elevated', 'Elevated')], max_length=16),
        ),
        migrations.AddField(
            model_name='forecast',
            name='actual_volatility_bucket',
            field=models.CharField(blank=True, choices=[('calm', 'Calm'), ('normal', 'Normal'), ('elevated', 'Elevated')], max_length=16),
        ),
        migrations.AddField(
            model_name='forecast',
            name='reliability',
            field=models.CharField(blank=True, choices=[('high', 'High'), ('med', 'Medium'), ('low', 'Low')], max_length=8),
        ),
        migrations.AddField(
            model_name='forecast',
            name='abstained',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='forecast',
            index=models.Index(fields=['symbol', 'horizon_hours', 'generated_at'], name='core_foreca_symbol_hzn_idx'),
        ),
    ]
