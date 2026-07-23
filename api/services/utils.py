import re
from datetime import datetime, timezone
from pathlib import Path


STOP_WORDS: frozenset[str] = frozenset({
    'the', 'a', 'an', 'in', 'on', 'at', 'to', 'of', 'for', 'and', 'or',
    'but', 'is', 'are', 'was', 'were', 'be', 'been', 'has', 'have', 'had',
    'its', 'that', 'this', 'with', 'by', 'from', 'as', 'after', 'amid',
    'over', 'into', 'also', 'not', 'new', 'says', 'said', 'two', 'three',
})

_SPLIT_RE = re.compile(r'[^a-zA-Z0-9]+')


def tokenize(text: str) -> frozenset[str]:
    """Lowercase word tokens, dropping stop words and tokens ≤ 2 characters."""
    return frozenset(t.lower() for t in _SPLIT_RE.split(text or '') if len(t) > 2 and t.lower() not in STOP_WORDS)


def jaccard(a: frozenset, b: frozenset) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def mark_stage(record, stage: str, ok: bool, error: str | None = None) -> dict:
    """Record ``stage``'s outcome on ``record.stage_status`` (in place). Returns the dict.

    Shape: {"<stage>": {"ok": bool, "at": "ISO-8601", "error": str | None}, ...}
    Caller is responsible for including 'stage_status' in save(update_fields=...).
    """
    status = dict(getattr(record, 'stage_status', None) or {})
    status[stage] = {
        'ok': bool(ok),
        'at': datetime.now(timezone.utc).isoformat(),
        'error': (error or None) if not ok else None,
    }
    record.stage_status = status
    return status


def map_concurrent(items, fn, *, max_workers: int = 8, default=None) -> list:
    """Apply ``fn`` to each item on a bounded thread pool, returning a list of
    results aligned 1:1 with ``items`` (input order preserved).

    Any per-item exception is swallowed and yields ``default`` in that slot — for
    best-effort I/O fan-out (e.g. HTTP fetches) where one failure must not sink
    the whole batch. Returns ``[]`` for empty input without spawning a pool.

    ``fn`` runs on worker threads, so it must be thread-safe and must NOT touch
    the Django ORM — do DB work in the calling thread with the returned results.
    Pass an immutable ``default`` (None, a tuple): the same object fills every
    failed slot.
    """
    from concurrent.futures import ThreadPoolExecutor

    items = list(items)
    if not items:
        return []
    results = [default] * len(items)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for future in futures:
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception:
                results[i] = default
    return results


def results_dir(name: str) -> Path:
    """Directory for a test/eval command's report files —
    ``<repo>/results/<name>/`` (git-ignored), created on first use. Every
    report-writing command (eval_analyzer, evaluate_*, e2e_*) writes here so
    generated artifacts never land in the working tree.
    """
    from django.conf import settings
    path = Path(settings.BASE_DIR) / 'results' / name
    path.mkdir(parents=True, exist_ok=True)
    return path
