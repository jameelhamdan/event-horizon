"""Newsletter generation business logic."""

import json
import logging
import uuid as _uuid
from collections import defaultdict
from datetime import datetime, timezone as dt_timezone

logger = logging.getLogger(__name__)

# Human-readable headings for each event category
_CATEGORY_HEADINGS = {
    'conflict':  'Armed Conflict & Military Operations',
    'protest':   'Civil Unrest & Protests',
    'disaster':  'Natural Disasters & Emergencies',
    'political': 'Political Developments',
    'economic':  'Economic & Financial News',
    'crime':     'Crime & Security',
    'general':   'Other Notable Events',
}

# Preferred display order
_CATEGORY_ORDER = ['conflict', 'disaster', 'political', 'protest', 'economic', 'crime', 'general']


def day_bounds(date) -> tuple[datetime, datetime]:
    start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=dt_timezone.utc)
    end = datetime(date.year, date.month, date.day, 23, 59, 59, 999999, tzinfo=dt_timezone.utc)
    return start, end


def build_markdown_body(sections: list[dict]) -> str:
    """Build Markdown body from a list of {heading, summary} dicts."""
    parts = []
    for s in sections:
        heading = s.get('heading', '').strip()
        summary = s.get('summary', '').strip()
        if heading and summary:
            parts.append(f'## {heading}\n\n{summary}')
    return '\n\n'.join(parts)


def build_fallback_body(events_by_category: dict) -> list[dict]:
    """Build per-category bullet sections without LLM."""
    sections = []
    for cat in _CATEGORY_ORDER:
        evs = events_by_category.get(cat, [])
        if not evs:
            continue
        heading = _CATEGORY_HEADINGS.get(cat, cat.title())
        bullets = '\n'.join(
            f'- **{ev.title}** — {ev.location_name} ({ev.article_count} source{"s" if ev.article_count != 1 else ""})'
            for ev in evs
        )
        sections.append({'category': cat, 'heading': heading, 'summary': bullets})
    return sections


def generate_newsletter(date_str: str | None = None) -> str:
    """
    Generate a DailyNewsletter for the given date (YYYY-MM-DD) or today.
    Idempotent — skips if a newsletter already exists for that date.
    Returns a status message string.
    """
    from django.utils import timezone
    from core import models as core_models
    from newsletter.models import DailyNewsletter
    from services.llm import get_llm_service, LLMError

    if date_str:
        from datetime import date
        target_date = date.fromisoformat(date_str)
    else:
        target_date = timezone.now().date()

    if DailyNewsletter.objects.filter(date=target_date).exists():
        return f'Newsletter for {target_date} already exists — skipped.'

    start, end = day_bounds(target_date)
    events = list(
        core_models.Event.objects.filter(
            started_at__gte=start,
            started_at__lt=end,
        ).order_by('-article_count')
    )

    if not events:
        return f'No events found for {target_date} — newsletter not generated.'

    # --- Collect articles for all events ---
    all_article_ids = [
        _uuid.UUID(a)
        for ev in events
        for a in (ev.article_ids or [])
    ]
    articles = (
        list(core_models.Article.objects.filter(id__in=all_article_ids))
        if all_article_ids else []
    )
    article_dicts = [
        {
            'id': str(a.id),
            'title': a.title,
            'source_url': a.source_url,
            'source_code': a.source_code,
            'category': a.category,
            'published_on': a.published_on.isoformat(),
            'banner_image_url': a.banner_image_url or None,
            'event_intensity': a.event_intensity,
        }
        for a in articles
    ]

    # --- Pick cover image: highest-intensity article with a banner ---
    cover_article = max(
        (a for a in articles if a.banner_image_url),
        key=lambda a: a.event_intensity or 0,
        default=None,
    )
    cover_image_url = cover_article.banner_image_url if cover_article else None
    cover_image_credit = (
        f'{cover_article.source_code}: {cover_article.title[:80]}'
        if cover_article else None
    )

    # --- Group events by category ---
    events_by_category: dict[str, list] = defaultdict(list)
    for ev in events:
        events_by_category[ev.category or 'general'].append(ev)

    present_cats = [c for c in _CATEGORY_ORDER if events_by_category.get(c)]

    # --- Build LLM prompt with per-category sections ---
    category_blocks = []
    for cat in present_cats:
        heading = _CATEGORY_HEADINGS.get(cat, cat.title())
        evs = events_by_category[cat]
        lines = '\n'.join(
            f'  - {ev.title} — {ev.location_name} ({ev.article_count} source{"s" if ev.article_count != 1 else ""})'
            for ev in evs
        )
        category_blocks.append(f'[{heading}]\n{lines}')

    events_text = '\n\n'.join(category_blocks)

    sections_schema = ', '.join(
        f'{{"category": "{c}", "heading": "{_CATEGORY_HEADINGS[c]}", "summary": "<paragraph>"}}'
        for c in present_cats
    )

    prompt_user = (
        f"Today is {target_date.strftime('%B %d, %Y')}. "
        f"Here are today's {len(events)} global news events grouped by category:\n\n"
        f"{events_text}\n\n"
        "Write a daily geopolitical intelligence newsletter. "
        "For each category above, write one concise factual paragraph (3-5 sentences). "
        "Skip any category that has no meaningful developments. "
        "Respond with ONLY a JSON object (no markdown, no explanation):\n"
        '{"subject": "<compelling headline, max 80 chars>", '
        f'"sections": [{sections_schema}]}}'
    )

    try:
        llm = get_llm_service('newsletter')
        raw = llm.chat([
            {'role': 'system', 'content': (
                'You are a senior editor at a geopolitical intelligence newsletter. '
                'Write clear, factual, professional summaries. '
                'Always respond with valid JSON only — no markdown fences, no extra text.'
            )},
            {'role': 'user', 'content': prompt_user},
        ])
        raw = raw.strip().lstrip('`').rstrip('`')
        if raw.startswith('json'):
            raw = raw[4:].strip()
        data = json.loads(raw)
        subject = str(data.get('subject', f'Daily Briefing — {target_date}')).strip()
        sections = [
            {
                'category': str(s.get('category', '')),
                'heading': str(s.get('heading', '')).strip(),
                'summary': str(s.get('summary', '')).strip(),
            }
            for s in data.get('sections', [])
            if str(s.get('summary', '')).strip()
        ]
        if not sections:
            sections = build_fallback_body(events_by_category)
    except (LLMError, json.JSONDecodeError, KeyError) as exc:
        logger.warning('Newsletter LLM generation failed (%s) — using fallback summary.', exc)
        subject = f'Daily Briefing — {target_date.strftime("%B %d, %Y")}'
        sections = build_fallback_body(events_by_category)

    body = build_markdown_body(sections)

    newsletter = DailyNewsletter.objects.create(
        date=target_date,
        subject=subject,
        body=body,
        articles=article_dicts,
        cover_image_url=cover_image_url,
        cover_image_credit=cover_image_credit,
        status=DailyNewsletter.STATUS_DRAFT,
        event_count=len(events),
    )
    logger.info('Generated newsletter %s: "%s" (%d events)', newsletter.date, subject, len(events))
    return f'Generated newsletter for {target_date}: "{subject}"'
