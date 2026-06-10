import accounts.models
import django_mongodb_backend.fields
import logging
from django.db import migrations, models

logger = logging.getLogger(__name__)


def initialize_data(apps, schema_editor):
    from accounts.models import User
    system_user, created = User.objects.get_or_create(is_superuser=True, defaults=dict(
        email='admin@example.com',
    ))
    if created:
        system_user.set_password('1234')
        logger.warning('ADMIN PASSWORD SET, CHANGE AS SOON AS POSSIBLE!!')
        system_user.save()


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(
                    default=False,
                    help_text='Designates that this user has all permissions without explicitly assigning them.',
                    verbose_name='superuser status',
                )),
                ('email', models.EmailField(db_index=True, max_length=254, unique=True, verbose_name='email address')),
                ('is_active', models.BooleanField(
                    default=True,
                    help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.',
                    verbose_name='active',
                )),
                ('display_name', models.CharField(max_length=256, verbose_name='display name')),
                ('created_on', models.DateTimeField(auto_now_add=True, verbose_name='date joined')),
                ('updated_on', models.DateTimeField(auto_now=True)),
                ('groups', models.ManyToManyField(
                    blank=True,
                    help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.',
                    related_name='user_set',
                    related_query_name='user',
                    to='auth.group',
                    verbose_name='groups',
                )),
                ('user_permissions', models.ManyToManyField(
                    blank=True,
                    help_text='Specific permissions for this user.',
                    related_name='user_set',
                    related_query_name='user',
                    to='auth.permission',
                    verbose_name='user permissions',
                )),
            ],
            options={
                'verbose_name': 'user',
                'verbose_name_plural': 'users',
                'ordering': ['-id'],
                'default_manager_name': 'objects',
            },
            managers=[
                ('objects', accounts.models.UserManager()),
            ],
        ),
        migrations.RunPython(initialize_data, migrations.RunPython.noop),
    ]
