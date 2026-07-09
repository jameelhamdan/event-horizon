"""Flower reverse proxy — the Flower container publishes no port; staff reach
it at /flower/ through Django, reusing admin session auth (staff_member_required
bounces anonymous/non-staff users to the admin login). Flower runs with
--url_prefix=flower so its asset/AJAX URLs all resolve under this same path.
"""
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from proxy.views import proxy_view


@staff_member_required
def flower(request, path=''):
    return proxy_view(request, f'{settings.FLOWER_INTERNAL_URL}/flower/{path}')
