# Hand-written: reintroduces the prediction layer (event-fused symbol forecasting).
# Adds PriceBar (daily OHLC training/charting substrate), the reborn Forecast model
# (dual-horizon, classifier + regressor outputs), and Event.router_source (records
# whether affected_indicators came from the LLM router or the deterministic rules,
# for the backtest ablation). New data only — no retro-migration.

import django_mongodb_backend.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_delete_forecast'),
    ]

    operations = [
        migrations.CreateModel(
            name='PriceBar',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('symbol', models.CharField(max_length=32)),
                ('stream_key', models.CharField(max_length=32)),
                ('name', models.CharField(blank=True, max_length=64)),
                ('interval', models.CharField(default='1d', max_length=8)),
                ('open', models.FloatField(blank=True, null=True)),
                ('high', models.FloatField(blank=True, null=True)),
                ('low', models.FloatField(blank=True, null=True)),
                ('close', models.FloatField()),
                ('volume', models.FloatField(blank=True, null=True)),
                ('date', models.DateTimeField()),
                ('created_on', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-date'],
                'indexes': [
                    models.Index(fields=['symbol', 'interval', 'date'], name='core_priceb_symbol_int_idx'),
                    models.Index(fields=['date'], name='core_priceb_date_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='Forecast',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('symbol', models.CharField(max_length=32)),
                ('stream_key', models.CharField(blank=True, max_length=32)),
                ('generated_at', models.DateTimeField()),
                ('as_of_date', models.DateTimeField()),
                ('horizon_days', models.IntegerField(default=1)),
                ('direction', models.CharField(default='neutral', max_length=8)),
                ('proba_up', models.FloatField(default=0.5)),
                ('predicted_change_pct', models.FloatField(default=0.0)),
                ('predicted_price', models.FloatField(blank=True, null=True)),
                ('band_low', models.FloatField(blank=True, null=True)),
                ('band_high', models.FloatField(blank=True, null=True)),
                ('confidence', models.FloatField(default=0.0)),
                ('current_value', models.FloatField(blank=True, null=True)),
                ('router_source', models.CharField(default='rules', max_length=8)),
                ('model_version', models.CharField(blank=True, max_length=64)),
                ('realized_direction', models.CharField(blank=True, max_length=8, null=True)),
                ('realized_change_pct', models.FloatField(blank=True, null=True)),
                ('is_correct', models.BooleanField(blank=True, null=True)),
                ('scored_at', models.DateTimeField(blank=True, null=True)),
                ('created_on', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-generated_at'],
                'indexes': [
                    models.Index(fields=['symbol', 'horizon_days', 'generated_at'], name='core_foreca_sym_hor_gen_idx'),
                    models.Index(fields=['as_of_date'], name='core_foreca_as_of_idx'),
                    models.Index(fields=['generated_at'], name='core_foreca_gen_idx'),
                ],
            },
        ),
        migrations.AddField(
            model_name='event',
            name='router_source',
            field=models.CharField(blank=True, default='rules', max_length=8),
        ),
    ]
