from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_article_process_queued_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='title_embedding',
            field=models.JSONField(default=list, blank=True),
        ),
        migrations.AddField(
            model_name='article',
            name='title_embedding_model',
            field=models.CharField(max_length=128, null=True, blank=True),
        ),
        migrations.AddIndex(
            model_name='article',
            index=models.Index(fields=['processed_on', 'published_on'], name='core_article_proc_pub_idx'),
        ),
    ]
