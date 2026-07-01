"""FinBERT news-domain sentiment scorer.

FinBERT (``ProsusAI/finbert``) is domain-matched for financial/news text (~72%
accuracy on financial news). It is computed **once at process time** on the heavy
queue and cached on the Article (plan §"Performance notes"); never re-run at feature
time. Sentiment is a *feature*, never the predictor.

The signed score in [-1, 1] is derived from the 3-class softmax:
    score = p(positive) - p(negative)
(neutral mass is implicitly the residual). Loaded lazily; CPU-only.
"""


import logging

from settings.model_names import FINBERT_MODEL_NAME
from services.processing._lazy import lazy_loader

logger = logging.getLogger(__name__)

# FinBERT truncates at 512 tokens; cap input chars to keep batching cheap.
_MAX_CHARS = 1500


def _build_pipeline():
    from transformers import pipeline
    return pipeline(
        'text-classification',
        model=FINBERT_MODEL_NAME,
        top_k=None,            # return all class scores
        truncation=True,
        max_length=512,
    )


# Opt-out via FINBERT_ENABLED (default on) — lets a lean deployment skip the ~500 MB
# model download + its memory/CPU cost without removing transformers (still required
# by core sentence-transformers clustering). When off, scores fall back to None and
# the pipeline degrades gracefully — the VADER general-sentiment score remains available.
_pipeline = lazy_loader('finbert', 'FINBERT_ENABLED', _build_pipeline)


def _to_signed(scores: list[dict]) -> float:
    """Convert a list of {label, score} into a signed compound in [-1, 1]."""
    by_label = {str(s['label']).lower(): float(s['score']) for s in scores}
    return round(by_label.get('positive', 0.0) - by_label.get('negative', 0.0), 4)


def score_batch(texts: list[str]) -> list[float | None]:
    """Batch-score texts. Returns a list aligned with the input (None on failure)."""
    if not texts:
        return []
    pipe = _pipeline()
    if pipe is None:
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
