"""Add core.RuntimeConfig — the singleton, dashboard-editable runtime config.

Holds the live LLM master switches (live_llm_enabled / backfill_llm_enabled) so
operators can pause/resume the pipeline's or backfill's LLM work from the admin
dashboard without a redeploy. See services/runtime_config.py. The row is created
lazily on first read (RuntimeConfig.load()), so no data migration is needed.
"""
import django_mongodb_backend.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_article_annotation_deferred'),
    ]

    operations = [
        migrations.CreateModel(
            name='RuntimeConfig',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('live_llm_enabled', models.BooleanField(default=True)),
                ('backfill_llm_enabled', models.BooleanField(default=True)),
                ('created_on', models.DateTimeField(auto_now_add=True)),
                ('updated_on', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
