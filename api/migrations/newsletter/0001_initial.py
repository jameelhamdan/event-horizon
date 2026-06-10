import uuid

import django_mongodb_backend.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    replaces = [
        ('newsletter', '0002_body_field'),
    ]

    initial = True

    dependencies = []

    operations = [

        # ── Subscriber ────────────────────────────────────────────────────────
        migrations.CreateModel(
            name='Subscriber',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.CharField(max_length=254, unique=True)),
                ('token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('subscribed_at', models.DateTimeField(auto_now_add=True)),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=False)),
                ('unsubscribed_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['-subscribed_at'],
            },
        ),

        # ── DailyNewsletter ───────────────────────────────────────────────────
        migrations.CreateModel(
            name='DailyNewsletter',
            fields=[
                ('id', django_mongodb_backend.fields.ObjectIdAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(unique=True)),
                ('subject', models.CharField(max_length=255)),
                ('body', models.TextField(help_text='Newsletter content in Markdown format')),
                ('articles', models.JSONField(
                    blank=True, default=list,
                    help_text='Snapshot of articles referenced in this newsletter',
                )),
                ('cover_image_url', models.URLField(blank=True, max_length=512, null=True)),
                ('cover_image_credit', models.CharField(blank=True, max_length=255, null=True)),
                ('generated_at', models.DateTimeField(auto_now_add=True)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('sent_count', models.IntegerField(default=0)),
                ('status', models.CharField(
                    choices=[
                        ('draft', 'Draft'),
                        ('sending', 'Sending'),
                        ('sent', 'Sent'),
                        ('error', 'Error'),
                    ],
                    default='draft',
                    max_length=16,
                )),
                ('event_count', models.IntegerField(default=0)),
            ],
            options={
                'ordering': ['-date'],
            },
        ),
    ]
