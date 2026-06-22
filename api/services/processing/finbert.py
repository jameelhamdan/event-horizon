"""FinBERT news-domain sentiment scorer.

FinBERT (``ProsusAI/finbert``) is domain-matched for financial/news text (~72% vs
VADER ~50% on financial news). It is computed **once at process time** on the heavy
queue and cached on the Article (plan §"Performance notes"); never re-run at feature
time. Sentiment is a *feature*, never the predictor.

The signed score in [-1, 1] is derived from the 3-class softmax:
    score = p(positive) - p(negative)
(neutral mass is implicitly the residual). Loaded lazily; CPU-only.
"""

from __future__ import annotations

import functools
import logging
import os

logger = logging.getLogger(__name__)

# FinBERT truncates at 512 tokens; cap input chars to keep batching cheap.
_MAX_CHARS = 1500


def _enabled() -> bool:
    """FinBERT is opt-out via FINBERT_ENABLED (default on).

    Lets a lean deployment skip the ~500 MB model download + its memory/CPU cost
    without removing transformers (still required by core sentence-transformers
    clustering). When off, scores fall back to None and the pipeline degrades
    gracefully — VADER remains the available sentiment signal.
    """
    return os.getenv('FINBERT_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no')


@functools.lru_cache(maxsize=1)
def _pipeline():
    """Lazily build the FinBERT pipeline. Returns None if disabled or unavailable."""
    if not _enabled():
        logger.info('[finbert] FINBERT_ENABLED is off — FinBERT disabled')
        return None
    try:
        from transformers import pipeline
    except ImportError:
        logger.warning('[finbert] transformers not installed — FinBERT disabled')
        return None
    try:
        return pipeline(
            'text-classification',
            model='ProsusAI/finbert',
            top_k=None,            # return all class scores
            truncation=True,
            max_length=512,
        )
    except Exception:
        logger.exception('[finbert] failed to load model — FinBERT disabled')
        return None


def _to_signed(scores: list[dict]) -> float:
    """Convert a list of {label, score} into a signed compound in [-1, 1]."""
    by_label = {str(s['label']).lower(): float(s['score']) for s in scores}
    return round(by_label.get('positive', 0.0) - by_label.get('negative', 0.0), 4)


def score(text: str) -> float | None:
    """Score a single text. Returns None if FinBERT is unavailable."""
    result = score_batch([text])
    return result[0] if result else None


def score_batch(texts: list[str]) -> list[float | None]:
    """Batch-score texts. Returns a list aligned with the input (None on failure)."""
    pipe = _pipeline()
    if pipe is None or not texts:
        return [None] * len(texts)
    clipped = [(t or '')[:_MAX_CHARS] for t in texts]
    try:
        raw = pipe(clipped, batch_size=16)
    except Exception:
        logger.exception('[finbert] batch scoring failed')
        return [None] * len(texts)
    out: list[float | None] = []
    for item in raw:
        # pipeline(top_k=None) yields a list[dict] per input
        if isinstance(item, dict):
            item = [item]
        try:
            out.append(_to_signed(item))
        except Exception:
            out.append(None)
    return out
