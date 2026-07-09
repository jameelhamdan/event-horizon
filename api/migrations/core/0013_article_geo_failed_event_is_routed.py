"""Promote two extra_data/JSONField-nested pipeline flags to real indexed fields.

Article.geo_failed (was extra_data['geo_failed']) and Event.is_routed (was
inferred from affected_indicators == []) both backed pending-work queries in
services/stages.py that had to filter in Python because MongoDB can't serve
an indexed equality/emptiness check on a nested JSONField key or a list.

Includes a data backfill: without it, every pre-existing row that was already
geo_failed/routed under the old convention would default to False on the new
field and look pending again — prune_stale_articles_task would stop deleting
already-confirmed-unlocatable articles (its filter now requires
geo_failed=True) and the route stage / admin "unrouted" filter would
re-flag already-routed events, burning a repair pass on each of them.
"""
from django.db import migrations, models


def _backfill_forward(apps, schema_editor):
    # Filtered in Python, not via extra_data__geo_failed=... / exclude(affected_indicators=[])
    # queries — list/nested-key equality filters are unreliable on this MongoDB backend (see
    # services/stages.py's _count() comment: "e.g. list-equality filter unsupported").
    Article = apps.get_model('core', 'Article')
    Event = apps.get_model('core', 'Event')

    geo_failed_ids = [
        a.pk for a in Article.objects.only('pk', 'extra_data').iterator()
        if isinstance(a.extra_data, dict) and a.extra_data.get('geo_failed')
    ]
    if geo_failed_ids:
        Article.objects.filter(pk__in=geo_failed_ids).update(geo_failed=True)

    routed_ids = [
        e.pk for e in Event.objects.only('pk', 'affected_indicators').iterator()
        if e.affected_indicators
    ]
    if routed_ids:
        Event.objects.filter(pk__in=routed_ids).update(is_routed=True)


def _backfill_reverse(apps, schema_editor):
    # Fields are dropped by the reverse migration anyway; nothing to undo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_article_published_on_idx_event_intensity_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='geo_failed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='event',
            name='is_routed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='article',
            index=models.Index(fields=['geo_failed'], name='core_article_geo_failed_idx'),
        ),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['is_routed'], name='core_event_is_routed_idx'),
        ),
        migrations.RunPython(_backfill_forward, _backfill_reverse),
    ]
