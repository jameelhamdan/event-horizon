"""Task functions for newsletter generation and sending.

Business logic lives in :mod:`services.newsletter`; this module provides thin
Celery (@shared_task) wrappers enqueued via services.queue.enqueue.
"""

import logging

from celery import shared_task
from django.conf import settings

from services.newsletter import generate_newsletter, send_newsletter
from services.tasks import _log_task

logger = logging.getLogger(__name__)


@shared_task
@_log_task
def generate_newsletter_task(date_str: str | None = None) -> str:
    if not settings.NEWSLETTER_ENABLED:
        logger.info('[newsletter] NEWSLETTER_ENABLED is off — skipping generate')
        return 'disabled'
    return generate_newsletter(date_str=date_str)


@shared_task
@_log_task
def send_newsletter_task(date_str: str | None = None) -> str:
    if not settings.NEWSLETTER_ENABLED:
        logger.info('[newsletter] NEWSLETTER_ENABLED is off — skipping send')
        return 'disabled'
    return send_newsletter(date_str=date_str)
