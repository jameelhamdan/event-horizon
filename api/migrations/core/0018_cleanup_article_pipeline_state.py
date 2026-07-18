"""Drain and correct the misnamed "geocode repair" backlog into honest states.

The geocode-repair bucket (processed_on set · geo_failed=False · no location)
had grown to ~59k because process_articles stamps ``processed_on`` even when the
LLM analysis *failed* — so a failed article looks "processed but un-located"
when it is really *unprocessed*. This one-shot data migration re-classifies every
article in that bucket into a state that reflects what actually happened:

  * analysed, has a country/city that now resolves via the alias-aware
    geocoder → write ``location``/``lat``/``lon`` (recovers the genuinely-located
    ones for free — no LLM);
  * analysed, named a place that still can't be mapped → left as processed with
    no location (terminal, but kept — never deleted);
  * empty/failed analysis (no stored country/city) → *effectively unprocessed*:
    reset ``processed_on=NULL`` and set ``annotation_deferred=True`` so it leaves
    the live pipeline and waits to be reprocessed by the (local) deferred
    annotator instead of masquerading as done.

No article is ever deleted. Schema is unchanged here — the ``geo_failed`` field
drop lives in the next migration. Data-only and not cleanly reversible (parking
discards the fake processed_on), so the reverse is a no-op.
"""
from django.db import migrations


_UPDATE_CHUNK = 1000


def _chunked_update(Article, ids, **fields):
    for i in range(0, len(ids), _UPDATE_CHUNK):
        Article.objects.filter(pk__in=ids[i:i + _UPDATE_CHUNK]).update(**fields)


def _cleanup_forward(apps, schema_editor):
    # Alias-aware local geocoder — resolves USA/UK/Türkiye/Palestine/… that the
    # bare geonamescache lookup silently missed (the main cause of the backlog).
    from services.processing.analyzer import _geocode
    Article = apps.get_model('core', 'Article')

    qs = Article.objects.filter(
        processed_on__isnull=False,
    ).only('pk', 'extra_data', 'location', 'latitude', 'longitude')

    relocated = []      # model instances with a freshly-resolved location
    parked_ids = []     # empty/failed analysis → deferred, to be reprocessed

    for a in qs.iterator(chunk_size=_UPDATE_CHUNK):
        if (a.location or '').strip():
            continue  # already located — nothing to do
        extra = a.extra_data if isinstance(a.extra_data, dict) else {}
        llm = extra.get('llm')
        llm = llm if isinstance(llm, dict) else {}
        country = llm.get('country') or None
        city = llm.get('city') or None

        if country or city:
            # Analysis succeeded and named a place: try to resolve it locally.
            lat, lon = _geocode(city, country)
            if lat is not None:
                a.location = ', '.join(x for x in (city, country) if x)
                a.latitude, a.longitude = lat, lon
                relocated.append(a)
            # else: analysed but the place can't be mapped → leave as processed
            # with no location (terminal, but kept — never deleted).
        else:
            # Empty analysis (LLM failed) → not really processed. Park as deferred
            # so it re-enters (local) annotation instead of masquerading as done.
            parked_ids.append(a.pk)

    for i in range(0, len(relocated), _UPDATE_CHUNK):
        Article.objects.bulk_update(
            relocated[i:i + _UPDATE_CHUNK],
            ['location', 'latitude', 'longitude'],
        )
    _chunked_update(Article, parked_ids, processed_on=None, annotation_deferred=True, process_queued_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_event_topics_source_idx'),
    ]

    operations = [
        migrations.RunPython(_cleanup_forward, migrations.RunPython.noop),
    ]
