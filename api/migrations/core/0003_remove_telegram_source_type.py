# Hand-written: drops the Telegram SourceType choice and updates author_slug
# help text. Both changes are Python-level (choices/help_text are not enforced
# at the MongoDB level), so this migration is a no-op against existing data.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_rename_core_forecast_symbol_gen_idx_core_foreca_symbol_10ab5f_idx_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='source',
            name='type',
            field=models.CharField(max_length=64, choices=[
                ('website', 'Website'), ('api', 'Api'), ('rss', 'Rss'),
                ('social', 'Social'), ('email', 'Email'),
                ('newsletter', 'Newsletter'), ('database', 'Database'),
            ]),
        ),
        migrations.AlterField(
            model_name='source',
            name='author_slug',
            field=models.CharField(
                blank=True, default='', max_length=255,
                help_text='Author/slug of the source'),
        ),
    ]
