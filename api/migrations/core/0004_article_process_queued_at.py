from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_article_llm_usage_event_llm_usage'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='process_queued_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
