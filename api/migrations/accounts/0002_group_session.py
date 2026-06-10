import django.contrib.auth.models
import qsessions.models
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('auth', '0001_initial'),
        ('qsessions', '0002_session_created_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='Group',
            fields=[],
            options={
                'verbose_name': 'group',
                'verbose_name_plural': 'groups',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('auth.group',),
            managers=[
                ('objects', django.contrib.auth.models.GroupManager()),
            ],
        ),
        migrations.CreateModel(
            name='Session',
            fields=[],
            options={
                'verbose_name': 'session',
                'verbose_name_plural': 'sessions',
                'abstract': False,
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('qsessions.session',),
            managers=[
                ('objects', qsessions.models.SessionManager()),
            ],
        ),
    ]
