# Hand-written: pipeline redesign — FinBERT sentiment, affected indicators,
# multi-horizon two-head Forecast buckets (plan §"Data-model changes").
# New data only; no migration of pre-existing Events/Forecasts.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_remove_telegram_source_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='finbert_sentiment',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='avg_finbert_sentiment',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='latest_article_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['latest_article_at'], name='core_event_latest__idx'),
        ),
        migrations.AddField(
            model_name='event',
            name='affected_indicators',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name='forecast',
            name='horizon_hours',
            field=models.IntegerField(default=24),
        ),
        migrations.AddField(
            model_name='forecast',
            name='magnitude_bucket',
            field=models.CharField(blank=True, max_length=16, choices=[
                ('strong_down', 'Strong down'), ('down', 'Down'), ('flat', 'Flat'),
                ('up', 'Up'), ('strong_up', 'Strong up'),
            ]),
        ),
        migrations.AddField(
            model_name='forecast',
            name='actual_bucket',
            field=models.CharField(blank=True, max_length=16, choices=[
                ('strong_down', 'Strong down'), ('down', 'Down'), ('flat', 'Flat'),
                ('up', 'Up'), ('strong_up', 'Strong up'),
            ]),
        ),
        migrations.AddField(
            model_name='forecast',
            name='volatility_bucket',
            field=models.CharField(blank=True, max_length=16, choices=[
                ('calm', 'Calm'), ('normal', 'Normal'), ('elevated', 'Elevated'),
            ]),
        ),
        migrations.AddField(
            model_name='forecast',
            name='actual_volatility_bucket',
            field=models.CharField(blank=True, max_length=16, choices=[
                ('calm', 'Calm'), ('normal', 'Normal'), ('elevated', 'Elevated'),
            ]),
        ),
        migrations.AddField(
            model_name='forecast',
            name='reliability',
            field=models.CharField(blank=True, max_length=8, choices=[
                ('high', 'High'), ('med', 'Medium'), ('low', 'Low'),
            ]),
        ),
        migrations.AddField(
            model_name='forecast',
            name='abstained',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='forecast',
            index=models.Index(
                fields=['symbol', 'horizon_hours', 'generated_at'],
                name='core_foreca_symbol_hzn_idx',
            ),
        ),
    ]
