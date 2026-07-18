"""Drop Article.geo_failed — the geocode-repair stage it backed is gone.

``geo_failed`` existed only to let the (now-removed) 12h geocode-repair stage
skip articles it had given up locating. With geocoding folded into the process
stage (a local geonamescache lookup inside analyzer._geocode) there is no repair
loop and no give-up flag: a processed article either has a location or doesn't,
and a location-less one is simply terminal (kept, never re-queued by a geo
stage). 0018 already drained the old bucket into honest states, so nothing reads
this field anymore.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_cleanup_article_pipeline_state'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='article',
            name='core_article_geo_failed_idx',
        ),
        migrations.RemoveField(
            model_name='article',
            name='geo_failed',
        ),
    ]
