"""Task functions for newsletter generation and sending.

Business logic lives in :mod:`services.newsletter`; this module provides thin
wrappers enqueued via django-rq (services.queue.enqueue).
"""

import logging

from django.conf import settings

from services.newsletter import generate_newsletter, send_newsletter

logger = logging.getLogger(__name__)


def generate_newsletter_task(date_str: str | None = None) -> str:
    if not settings.NEWSLETTER_ENABLED:
        logger.info('[newsletter] NEWSLETTER_ENABLED is off — skipping generate')
        return 'disabled'
    return generate_newsletter(date_str=date_str)


def send_newsletter_task(date_str: str | None = None) -> str:
    if not settings.NEWSLETTER_ENABLED:
        logger.info('[newsletter] NEWSLETTER_ENABLED is off — skipping send')
        return 'disabled'
    return send_newsletter(date_str=date_str)
