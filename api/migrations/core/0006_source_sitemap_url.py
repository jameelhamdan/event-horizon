from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_article_title_embedding'),
    ]

    operations = [
        migrations.AddField(
            model_name='source',
            name='sitemap_url',
            field=models.URLField(
                blank=True, default='', max_length=255,
                help_text=(
                    'Explicit sitemap URL for historical backfill, when it lives somewhere '
                    'other than the standard paths (robots.txt directive, /sitemap.xml, '
                    '/sitemap_index.xml, /news-sitemap.xml) or on a different domain than '
                    'the feed URL above. Leave blank to use the standard discovery order.'
                ),
            ),
        ),
    ]
