# Hand-written: drops the Forecast model. The market-forecasting prediction layer
# was removed and will be reimplemented from scratch. The deterministic event→symbol
# router (services.forecasting.routing) and Event.affected_indicators are retained.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_align_category_choices'),
    ]

    operations = [
        migrations.DeleteModel(
            name='Forecast',
        ),
    ]
