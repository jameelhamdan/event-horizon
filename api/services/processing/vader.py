"""Local general-purpose sentiment (VADER).

VADER is rule-based (no model download, sub-ms per article) and English-tuned —
a reasonable general-polarity signal alongside the domain-specific FinBERT
score computed in cleaner.py.
"""

import logging

from services.processing._lazy import lazy_loader

logger = logging.getLogger(__name__)

_MAX_CHARS = 3000


def _build_analyzer():
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    return SentimentIntensityAnalyzer()


# Opt-out via VADER_ENABLED (default on).
_analyzer = lazy_loader('vader', 'VADER_ENABLED', _build_analyzer)


def score_batch(texts: list[str]) -> list[float]:
    """Batch-score texts. Returns compound scores in [-1, 1] aligned with the
    input (0.0 on failure/disabled/blank input — neutral, never None, since
    this is a required field on Article)."""
    analyzer = _analyzer()
    if analyzer is None or not texts:
        return [0.0] * len(texts)
    out = []
    for text in texts:
        try:
            out.append(round(analyzer.polarity_scores((text or '')[:_MAX_CHARS])['compound'], 4))
        except Exception:
            out.append(0.0)
    return out
