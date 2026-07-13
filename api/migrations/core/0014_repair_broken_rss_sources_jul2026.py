"""Repair the RSS sources found broken in the July 2026 production investigation.

Same pattern as 0011: fixtures only seed fresh installs (0001's loader skips
existing codes), so URL moves and retirements must be applied to existing rows
via a data migration. All four failed on *every* fetch cycle with a hard HTTP
status (not a transient network error).

Moved — same publisher, new feed location:
  kyiv-independent  kyivindependent.com/rss (404) → /news-archive/rss/
                    (site relaunch; the feed link is now advertised there)
  defense-news      defensenews.com/rss/ (404) → the Arc outbound feed at
                    /arc/outboundfeeds/rss/?outputType=xml

Retired — no fetchable feed reachable from prod, disabled:
  imf-news          imf.org/en/News/rss returns 403 (Cloudflare) on every
                    known feed path, even with a browser User-Agent — the feed
                    is gone/blocked outright, not moved.
  arab-news         arabnews.com/rss.xml is a *valid* feed and 200s from most
                    IPs, but Cloudflare 403s the prod egress IP on every cycle.
                    No URL change fixes an IP block, so disable it rather than
                    let it fail every 10 minutes. Re-enable if the egress IP
                    stops being blocked (or once a Wayback/proxy path exists).
"""
from django.db import migrations

URL_UPDATES = {
    'kyiv-independent': 'https://kyivindependent.com/news-archive/rss/',
    'defense-news': 'https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml',
}

DISABLE = ('imf-news', 'arab-news')


def repair_sources(apps, schema_editor):
    Source = apps.get_model('core', 'Source')
    for code, url in URL_UPDATES.items():
        Source.objects.filter(code=code).update(url=url)
    Source.objects.filter(code__in=DISABLE).update(is_enabled=False)


def unrepair_sources(apps, schema_editor):
    # Old URLs are dead — nothing sensible to restore.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0013_article_geo_failed_event_is_routed'),
    ]

    operations = [
        migrations.RunPython(repair_sources, reverse_code=unrepair_sources),
    ]
