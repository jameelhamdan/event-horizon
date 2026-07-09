"""Repair the RSS sources found broken in production (July 2026).

Fixtures only seed fresh installs (existing codes are skipped by 0001's
loader), so URL moves and retirements must also be applied to existing rows.

Moved — same publisher, new feed location:
  guardian-crime   theguardian.com/uk/crime/rss → /uk/ukcrime/rss
  occrp            occrp.org/en/rss/ → /en/feed (2024 site relaunch)
  daily-sabah      dailysabah.com/rss (HTML index page) → /rss/world
  allafrica        headlines/rdf/world/ (removed) → /rdf/africa/
  brookings        /feed/ (now redirects to homepage) → /feed/?post_type=article

Retired — publisher no longer offers a fetchable feed, disabled:
  ap-top           feeds.apnews.com DNS no longer resolves; AP has no public RSS
  voa-world        VOA removed its World zone; only regional feeds remain
  oecd-news        RSS dropped in OECD's 2024 site redesign
  interpol-news    bot protection (403/503) even with a browser User-Agent
  world-bank-blog  feeds removed in the blogs platform migration

tass-world and arab-news keep their URLs — their production failures were
User-Agent blocking, fixed in services/data/rss.py.
"""
from django.db import migrations

URL_UPDATES = {
    'guardian-crime': 'https://www.theguardian.com/uk/ukcrime/rss',
    'occrp': 'https://www.occrp.org/en/feed',
    'daily-sabah': 'https://www.dailysabah.com/rss/world',
    'allafrica': 'https://allafrica.com/tools/headlines/rdf/africa/headlines.rdf',
    'brookings': 'https://www.brookings.edu/feed/?post_type=article',
}

DISABLE = ('ap-top', 'voa-world', 'oecd-news', 'interpol-news', 'world-bank-blog')


def repair_sources(apps, schema_editor):
    Source = apps.get_model('core', 'Source')
    for code, url in URL_UPDATES.items():
        Source.objects.filter(code=code).update(url=url)
    Source.objects.filter(code__in=DISABLE).update(is_enabled=False)


def unrepair_sources(apps, schema_editor):
    # Old URLs are dead — nothing sensible to restore; just re-enable nothing.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0010_event_affected_indicators_symbol_index'),
    ]

    operations = [
        migrations.RunPython(repair_sources, reverse_code=unrepair_sources),
    ]
