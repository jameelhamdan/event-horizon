from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='forecast',
            index=models.Index(fields=['realized_direction'], name='core_foreca_realize_idx'),
        ),
    ]
