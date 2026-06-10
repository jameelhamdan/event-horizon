import django.contrib.contenttypes.models
import django_mongodb_backend.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    replaces = [
        ('contenttypes', '0002_alter_contenttype_options'),
    ]

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='ContentType',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('app_label', models.CharField(max_length=100)),
                ('model', models.CharField(max_length=100, verbose_name='python model class name')),
            ],
            options={
                'verbose_name': 'content type',
                'verbose_name_plural': 'content types',
                'unique_together': {('app_label', 'model')},
            },
            managers=[
                ('objects', django.contrib.contenttypes.models.ContentTypeManager()),
            ],
        ),
    ]
