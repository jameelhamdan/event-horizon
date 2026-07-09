"""Index Event.affected_indicators.symbol for the /api/events/?symbol= filter.

The filter (api/api/views/events.py::EventListView) compiles to the Mongo
dot-path match {'affected_indicators.symbol': X}, which matches inside the
embedded [{symbol, weight}] array. Django's models.Index can't express a
nested-array key on a JSONField, so the index is created directly through the
backend's pymongo handle.
"""
from django.db import migrations

_INDEX_NAME = 'core_event_affind_symbol_idx'
_COLLECTION = 'core_event'


def create_index(apps, schema_editor):
    db = schema_editor.connection.database
    db[_COLLECTION].create_index('affected_indicators.symbol', name=_INDEX_NAME)


def drop_index(apps, schema_editor):
    db = schema_editor.connection.database
    try:
        db[_COLLECTION].drop_index(_INDEX_NAME)
    except Exception:  # noqa: BLE001 — index may already be gone
        pass


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0009_source_last_fetched_at'),
    ]

    operations = [
        migrations.RunPython(create_index, drop_index),
    ]
