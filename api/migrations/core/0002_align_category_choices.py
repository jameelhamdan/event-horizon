# Hand-written: aligns choice sets with the current models after the 0001 squash.
# Adds 'health' to the event/article/topic category choices and drops the retired
# 'telegram' source type. All changes are Python-level (choices are not enforced at
# the MongoDB level), so this migration is a no-op against existing data.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='article',
            name='category',
            field=models.CharField(blank=True, choices=[('conflict', 'Conflict'), ('disaster', 'Disaster'), ('economic', 'Economic'), ('political', 'Political'), ('health', 'Health'), ('general', 'General'), ('protest', 'Protest'), ('crime', 'Crime')], help_text='Rule-based event category', max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name='article',
            name='source_type',
            field=models.CharField(choices=[('website', 'Website'), ('api', 'Api'), ('rss', 'Rss'), ('social', 'Social'), ('email', 'Email'), ('newsletter', 'Newsletter'), ('database', 'Database')], max_length=64),
        ),
        migrations.AlterField(
            model_name='event',
            name='category',
            field=models.CharField(choices=[('conflict', 'Conflict'), ('disaster', 'Disaster'), ('economic', 'Economic'), ('political', 'Political'), ('health', 'Health'), ('general', 'General'), ('protest', 'Protest'), ('crime', 'Crime')], default='general', max_length=64),
        ),
        migrations.AlterField(
            model_name='topic',
            name='category',
            field=models.CharField(blank=True, choices=[('conflict', 'Conflict'), ('disaster', 'Disaster'), ('economic', 'Economic'), ('political', 'Political'), ('health', 'Health'), ('general', 'General'), ('protest', 'Protest'), ('crime', 'Crime')], max_length=64),
        ),
    ]
