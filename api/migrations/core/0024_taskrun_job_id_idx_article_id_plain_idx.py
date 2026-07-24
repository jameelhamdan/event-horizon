"""Prod slowness fix (2026-07-24 investigation): two missing indexes causing
COLLSCANs under load.

- core_taskrun had no index on job_id, so resume_deferred_backfill_task's
  in-flight dedupe check and admin job-status lookups scanned the whole
  collection (confirmed live: ~180k docs, 100-180ms per lookup).

- core_article's only index on id is the auto-generated unique constraint,
  which django-mongodb-backend builds as a *partial* index
  (partialFilterExpression id:$type string). Verified locally that Mongo's
  planner cannot produce tight bounds for an id__in(...) query against a
  partial index on that same field — even .hint()-forcing it degrades to an
  unbounded [MinKey, MaxKey] scan, same cost as COLLSCAN. annotate_deferred_
  batch_task's per-batch id__in lookups (services/tasks.py) were paying this
  full-collection cost every ~50-id batch. This adds a plain, non-partial
  index that a $in query can actually seek against, without touching the
  existing unique constraint.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_article_annotator_version'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='taskrun',
            index=models.Index(fields=['job_id'], name='core_taskrun_job_id_idx'),
        ),
        migrations.AddIndex(
            model_name='article',
            index=models.Index(fields=['id'], name='core_article_id_plain_idx'),
        ),
    ]
