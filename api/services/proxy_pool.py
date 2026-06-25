"""
Open-source HTTP proxy pool for LLM calls.

Fetches proxy lists from GitHub-hosted open-source collections and ProxyScrape,
validates each candidate against the target host in parallel, and rotates through
working proxies round-robin. A background thread refreshes the pool periodically.

Usage:
    pool = get_proxy_pool()   # singleton, starts background refresh on first call
    url = pool.next_proxy()   # str like "http://1.2.3.4:8080", or None if empty
"""

import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

# Open-source / freely available proxy list sources.
# All are maintained on GitHub or provide free public APIs.
_DEFAULT_SOURCES: list[str] = [
    # TheSpeedX — the largest open-source HTTP proxy list (~6–8k entries)
    'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt',
    # ShiftyTR — actively maintained HTTPS list
    'https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt',
    # clarketm — curated, low-noise list
    'https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt',
    # ProxyScrape free API — elite anonymity, HTTPS-capable
    (
        'https://api.proxyscrape.com/v3/free-proxy-list/get'
        '?request=displayproxies&protocol=http&timeout=5000'
        '&country=all&anonymity=elite&ssl=yes'
    ),
]

# Lightweight HTTPS endpoint used to verify each candidate proxy.
# A HEAD request succeeds quickly; we only care that the TCP tunnel works.
_TEST_URL = 'https://openrouter.ai'

_VALIDATE_WORKERS = 50   # parallel validation threads
_VALIDATE_SAMPLE  = 300  # check at most this many candidates per refresh cycle


class ProxyPool:
    """
    Thread-safe rotating pool of validated open-source HTTP proxies.

    start() launches a background daemon thread that:
      1. Fetches all source lists immediately.
      2. Validates up to _VALIDATE_SAMPLE candidates in parallel (HEAD to _TEST_URL).
      3. Loads working proxies into a round-robin cycle.
      4. Repeats every refresh_hours.

    next_proxy() returns None while the pool is empty (first refresh in progress),
    so callers must handle None gracefully (fall back to direct connection).
    """

    def __init__(
        self,
        sources: list[str] | None = None,
        refresh_hours: float = 6,
        validate_timeout: int = 5,
        max_pool_size: int = 100,
    ) -> None:
        self._sources = sources or _DEFAULT_SOURCES
        self._refresh_interval = refresh_hours * 3600
        self._validate_timeout = validate_timeout
        self._max_pool_size = max_pool_size

        self._proxies: list[str] = []
        self._cycle: itertools.cycle | None = None
        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch background refresh (non-blocking; first load happens in the thread)."""
        t = threading.Thread(target=self._loop, daemon=True, name='proxy-pool-refresh')
        t.start()

    def next_proxy(self) -> str | None:
        """Return the next proxy URL in rotation, or None if the pool is not yet ready."""
        with self._lock:
            if self._cycle is None:
                return None
            return next(self._cycle)

    @property
    def size(self) -> int:
        return len(self._proxies)

    # ── internals ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            self._refresh()
            time.sleep(self._refresh_interval)

    def _refresh(self) -> None:
        candidates = self._fetch_all()
        working = self._validate(candidates)
        with self._lock:
            self._proxies = working
            self._cycle = itertools.cycle(working) if working else None
        logger.info(
            'ProxyPool: loaded %d working proxies (validated %d / %d candidates)',
            len(working), min(len(candidates), _VALIDATE_SAMPLE), len(candidates),
        )

    def _fetch_all(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for source in self._sources:
            for p in self._fetch_one(source):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def _fetch_one(self, source_url: str) -> list[str]:
        try:
            r = requests.get(source_url, timeout=15)
            r.raise_for_status()
            proxies = []
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if not line.startswith('http'):
                    line = f'http://{line}'
                proxies.append(line)
            logger.debug('ProxyPool: fetched %d entries from %s', len(proxies), source_url)
            return proxies
        except Exception as exc:
            logger.warning('ProxyPool: could not fetch %s — %s', source_url, exc)
            return []

    def _validate(self, candidates: list[str]) -> list[str]:
        sample = candidates[:_VALIDATE_SAMPLE]
        working: list[str] = []

        def check(proxy_url: str) -> bool:
            try:
                r = requests.head(
                    _TEST_URL,
                    proxies={'http': proxy_url, 'https': proxy_url},
                    timeout=self._validate_timeout,
                    allow_redirects=False,
                )
                return r.status_code < 500
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=_VALIDATE_WORKERS) as pool:
            futures = {pool.submit(check, p): p for p in sample}
            for future in as_completed(futures):
                if future.result():
                    working.append(futures[future])
                if len(working) >= self._max_pool_size:
                    break

        return working


# ── module-level singleton ────────────────────────────────────────────────────

_pool_instance: ProxyPool | None = None
_pool_lock = threading.Lock()


def get_proxy_pool(
    sources: list[str] | None = None,
    refresh_hours: float = 6,
    validate_timeout: int = 5,
    max_pool_size: int = 100,
) -> ProxyPool:
    """
    Return the shared ProxyPool singleton, creating and starting it on first call.
    All arguments are ignored after the first call (singleton is reused as-is).
    """
    global _pool_instance
    if _pool_instance is not None:
        return _pool_instance
    with _pool_lock:
        if _pool_instance is None:
            _pool_instance = ProxyPool(
                sources=sources,
                refresh_hours=refresh_hours,
                validate_timeout=validate_timeout,
                max_pool_size=max_pool_size,
            )
            _pool_instance.start()
    return _pool_instance
