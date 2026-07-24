"""Add Article.annotator_version — which settings.ANNOTATOR_VERSION (the
deploy version, api/settings/base.py) produced this article's current
category/sub_category/geo/intensity.

Stamped by annotate_articles and refine_articles on success, alongside
processed_on/stage/refined_on. Lets annotate_deferred_batch_task
(services/tasks.py) skip articles that are already terminal
(annotated/refined) AND already stamped current, so overlapping/repeated
reprocess_corpus_task dispatches become cheap no-ops instead of redundant
NLP passes. No backfill: pre-migration rows keep annotator_version=NULL
until next (re-)annotated or (re-)refined.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_alter_article_options_alter_article_managers_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='annotator_version',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
    ]
