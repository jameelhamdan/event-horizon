"""Indexes for the dashboard activity chart (core/admin_dashboard.py).

Article had no standalone index on published_on — the existing compound
(processed_on, published_on) index doesn't serve a published_on-only range
filter, so the chart's original per-month Article.count() loop was a full
collection scan per bucket. Both new indexes also cover the "most important
first" fetch: filter by month range, then sort+limit by the importance field
without an in-memory sort blowing past Mongo's per-op memory limit.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_repair_broken_rss_sources'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='article',
            index=models.Index(fields=['published_on', 'importance_score'], name='core_article_pub_imp_idx'),
        ),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['started_at', 'avg_intensity'], name='core_event_start_int_idx'),
        ),
    ]
