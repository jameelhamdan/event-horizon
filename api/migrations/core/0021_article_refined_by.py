"""Add Article.refined_by — the provider that produced the article's current
refine verdict ('zeroshot' | 'ollama' | 'cloud').

Previously this lived only inside extra_data.llm.refined_by (JSON, not
queryable/filterable). A first-class field lets an operator see and filter on
it in the admin, and lets refine_articles overwrite it cleanly on a manual
re-refine (services/processing/refiner.py::LLMRefiner.apply,
core/admin.py::ArticleAdmin.rerefine_selected). No backfill: pre-migration
refined articles keep refined_by=NULL until they're next (re-)refined; their
provider is still recoverable from extra_data.llm.refined_by if needed.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0020_article_stage_refined_on'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='refined_by',
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
    ]
