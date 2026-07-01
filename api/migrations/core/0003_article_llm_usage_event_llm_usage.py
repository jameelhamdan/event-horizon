from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_forecast_realized_direction_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='llm_usage',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='event',
            name='llm_usage',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
