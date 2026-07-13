"""Add Article.annotation_deferred — the fetch-only backfill marker.

A backfill run with BACKFILL_LLM_ENABLED=False saves articles but skips the
LLM-dependent score/process (annotation) steps. Those articles are flagged
annotation_deferred=True so the live score/process pipeline stages exclude them
(.exclude(annotation_deferred=True)) instead of annotating them on the next
tick — annotate_deferred_articles_task processes them later and clears the flag.

No data backfill: pre-existing articles were saved *with* annotation, so the
default (False) is correct for every existing row. The stage predicates use
.exclude(annotation_deferred=True), which matches rows where the field is False
or (on this MongoDB backend) not yet materialized on old documents.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_repair_broken_rss_sources_jul2026'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='annotation_deferred',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='article',
            index=models.Index(fields=['annotation_deferred'], name='core_article_annot_defer_idx'),
        ),
    ]
