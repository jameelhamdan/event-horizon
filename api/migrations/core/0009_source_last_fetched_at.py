from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_taskrun_queued_status_result_tracking'),
    ]

    operations = [
        migrations.AddField(
            model_name='source',
            name='last_fetched_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
