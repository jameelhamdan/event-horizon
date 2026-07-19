"""Add Article.stage + Article.refined_on — the two-stage analyzer state model.

``stage`` is the single stored pipeline-position field the annotate/refine
stage predicates filter on (fetched → annotated | refine → refined), replacing
the previous derived pipeline_state property. ``refined_on`` records when the
refine stage re-judged a low-confidence article.

Backfill: every already-processed article (processed_on set) becomes
'annotated' — pre-migration rows were fully analysed by the old process stage,
so none are queued for the judge retroactively. Unprocessed rows keep the
'fetched' default and flow into the new annotate stage naturally.
"""
from django.db import migrations, models


def _backfill_stage(apps, schema_editor):
    Article = apps.get_model('core', 'Article')
    Article.objects.filter(processed_on__isnull=False).update(stage='annotated')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_remove_article_geo_failed'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='stage',
            field=models.CharField(db_index=True, default='fetched', max_length=16),
        ),
        migrations.AddField(
            model_name='article',
            name='refined_on',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(_backfill_stage, migrations.RunPython.noop),
    ]
