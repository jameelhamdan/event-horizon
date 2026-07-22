"""Egress proxy pools for historical backfill fetching.

At scale the backfill bottleneck is per-IP rate limiting / IP-reputation blocks
from news sites and the Wayback Machine (observed live: scmp/who timeout-blocks,
allafrica 403, archive.org throttling) — not CPU. Rotating requests across a
pool of egress IPs spreads load so no single IP trips a per-IP limit, and lets a
blocked request retry from a fresh IP.

Two configured pools (module singletons below), both empty (disabled) by default:
  * ``EGRESS_PROXIES``  — the live article-page fetch (services/data/bodies.py)
  * ``WAYBACK_PROXIES`` — Wayback Machine requests (services/data/wayback.py),
    falling back to the legacy single ``WAYBACK_PROXY_URL`` when its pool is unset.

Set the backing env var to a comma-separated list of proxy URLs
(``http://user:pass@host:port``). With no proxies configured, every request is a
single direct connection — unchanged behaviour.
"""
import random

import requests
from django.conf import settings


def _parse_pool(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(',') if p.strip()]
    return [str(p).strip() for p in raw if str(p).strip()]


def as_proxies(proxy: str | None) -> dict | None:
    """A single proxy URL as a requests ``proxies=`` dict, or None for direct."""
    return {'http': proxy, 'https': proxy} if proxy else None


class ProxyPool:
    """A named pool of egress proxy URLs with direct-first, rotate-on-block
    fetching. The pool is read from settings on each use (config can change
    without a restart) and is empty by default, so an unconfigured pool is just
    a single direct request.
    """

    # Statuses that mean "this IP is being blocked/throttled" — worth retrying
    # from a different egress IP. A 404/500 is about the URL, not the IP, so it
    # is not retried across proxies.
    BLOCK_STATUSES = frozenset({403, 429, 503})

    def __init__(self, setting_name: str, fallback_setting: str | None = None):
        self._setting = setting_name
        self._fallback = fallback_setting

    def urls(self) -> list[str]:
        """Current proxy URLs — the pool setting, or the legacy single-proxy
        fallback setting when the pool is unset (empty = direct only)."""
        pool = _parse_pool(getattr(settings, self._setting, ''))
        if not pool and self._fallback:
            legacy = getattr(settings, self._fallback, '')
            if legacy:
                pool = [legacy]
        return pool

    def attempt_order(self, direct_first: bool = True) -> list[str | None]:
        """The sequence of egress hops to try: an optional direct (None) hop
        first, then the pool in a randomly rotated order so concurrent callers
        spread across IPs rather than all hammering the first proxy. Always at
        least [None] so an empty pool still makes one direct attempt."""
        order: list[str | None] = self.urls()
        random.shuffle(order)
        if direct_first:
            order = [None, *order]
        return order or [None]

    def get(self, url: str, *, headers=None, timeout: int = 15,
            direct_first: bool = True, **kwargs) -> requests.Response:
        """``requests.get`` that rotates egress proxies on a block/error.

        Tries a direct connection first (cheapest), then each proxy until a
        non-blocked response, the attempts are exhausted, or all raise. Returns
        the last Response (even a blocked one, so the caller sees the real
        status), or re-raises the last RequestException if every attempt raised
        — preserving the exception type (e.g. Timeout) callers branch on."""
        last_exc: Exception | None = None
        last_resp: requests.Response | None = None
        for proxy in self.attempt_order(direct_first):
            try:
                resp = requests.get(url, headers=headers, timeout=timeout, proxies=as_proxies(proxy), **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                continue
            last_resp = resp
            if resp.status_code not in self.BLOCK_STATUSES:
                return resp
        if last_resp is not None:
            return last_resp
        raise last_exc


EGRESS_PROXIES = ProxyPool('EGRESS_PROXY_POOL')
WAYBACK_PROXIES = ProxyPool('WAYBACK_PROXY_POOL', fallback_setting='WAYBACK_PROXY_URL')
