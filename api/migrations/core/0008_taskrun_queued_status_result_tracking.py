from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_source_sitemap_url_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskrun',
            name='picked_up_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='taskrun',
            name='result',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='taskrun',
            name='retries',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='taskrun',
            name='traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name='taskrun',
            name='status',
            field=models.CharField(
                choices=[
                    ('queued', 'Queued'), ('running', 'Running'), ('success', 'Success'),
                    ('failed', 'Failed'), ('cancelled', 'Cancelled'),
                ],
                default='queued', max_length=16,
            ),
        ),
    ]
