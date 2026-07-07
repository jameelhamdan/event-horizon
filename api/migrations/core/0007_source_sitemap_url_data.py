"""Backfill Source.sitemap_url for sources whose historical sitemap was
confirmed live during development — either at a non-default path (found via
robots.txt "Sitemap:" directives) or at the standard /sitemap.xml path but
worth locking in explicitly so future robots.txt changes or fallback-path
guessing can't regress a source that's known to work (see
services/data/historical.py for the discovery order this overrides).

Fixture files (core/fixtures/*.json) already carry sitemap_url for fresh
installs; this migration updates rows created before 0006 added the field, so
existing deployments don't need a re-seed.
"""
from django.db import migrations

_SITEMAP_URLS = {
    # Non-default path (found via robots.txt Sitemap: directive)
    'dawn-pk': 'https://www.dawn.com/feeds/sitemap',
    'scmp-world': 'https://www.scmp.com/sitemap/archives-0.xml',
    'africa-news': 'https://www.africanews.com/sitemaps/en/sitemap.xml',
    'allafrica': 'https://allafrica.com/misc/sitemap/aans-urls-en.xml',
    'elpais-english': 'https://english.elpais.com/sitemap.xml',
    # Standard /sitemap.xml path, but confirmed live — locked in explicitly
    'aljazeera-world': 'https://www.aljazeera.com/sitemap.xml',
    'arab-news': 'https://www.arabnews.com/sitemap.xml',
    'brookings': 'https://www.brookings.edu/sitemap.xml',
    'techcrunch': 'https://techcrunch.com/sitemap.xml',
}


def set_sitemap_urls(apps, schema_editor):
    Source = apps.get_model('core', 'Source')
    for code, sitemap_url in _SITEMAP_URLS.items():
        Source.objects.filter(code=code, sitemap_url='').update(sitemap_url=sitemap_url)


def unset_sitemap_urls(apps, schema_editor):
    Source = apps.get_model('core', 'Source')
    Source.objects.filter(code__in=_SITEMAP_URLS.keys()).update(sitemap_url='')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_source_sitemap_url'),
    ]

    operations = [
        migrations.RunPython(set_sitemap_urls, reverse_code=unset_sitemap_urls),
    ]
