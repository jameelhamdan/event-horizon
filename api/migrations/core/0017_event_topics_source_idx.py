"""Index Event.topics_source.

The 'tag' stage (services/stages.py) selects and counts events still needing
topic tagging by filtering the started_at window and excluding already
embed-tagged rows (exclude(topics_source='embed')). This index backs that
equality check so the coverage count and dispatch selection no longer
materialize+filter every event in the 168h window in Python.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_runtimeconfig'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['topics_source'], name='core_event_topics_src_idx'),
        ),
    ]
