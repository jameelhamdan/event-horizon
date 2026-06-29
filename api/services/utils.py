import re
from datetime import datetime, timezone


STOP_WORDS: frozenset[str] = frozenset({
    'the', 'a', 'an', 'in', 'on', 'at', 'to', 'of', 'for', 'and', 'or',
    'but', 'is', 'are', 'was', 'were', 'be', 'been', 'has', 'have', 'had',
    'its', 'that', 'this', 'with', 'by', 'from', 'as', 'after', 'amid',
    'over', 'into', 'also', 'not', 'new', 'says', 'said', 'two', 'three',
})

_SPLIT_RE = re.compile(r'[^a-zA-Z0-9]+')


def tokenize(text: str) -> frozenset[str]:
    """Lowercase word tokens, dropping stop words and tokens ≤ 2 characters."""
    return frozenset(
        t.lower() for t in _SPLIT_RE.split(text or '')
        if len(t) > 2 and t.lower() not in STOP_WORDS
    )


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
