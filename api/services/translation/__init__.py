"""
Local English→Arabic translation (MarianMT), replacing LLM-generated Arabic
translations in ArticleAnalyzer.

Helsinki-NLP/opus-mt-en-ar is a small dedicated seq2seq translation model — a much
better fit for this narrow task than asking a general LLM to translate, and it runs
comfortably CPU-only. Loaded lazily and cached (mirrors services.processing.finbert).
"""

import logging

from services.processing._lazy import lazy_loader

logger = logging.getLogger(__name__)

_MODEL_NAME = 'Helsinki-NLP/opus-mt-en-ar'
# MarianMT truncates around 512 tokens; cap input chars to keep batching cheap
# and translations focused on the title/summary (not full article bodies).
_MAX_CHARS = 1000


def _build_pipeline():
    from transformers import pipeline
    return pipeline('translation', model=_MODEL_NAME, truncation=True, max_length=512)


# Opt-out via TRANSLATION_ENABLED (default on).
_pipeline = lazy_loader('translation', 'TRANSLATION_ENABLED', _build_pipeline)


def translate_en_ar_batch(texts: list[str]) -> list[str | None]:
    """Batch-translate English texts to Arabic. Returns a list aligned with the
    input (None per item on failure or for empty/blank input)."""
    pipe = _pipeline()
    if pipe is None or not texts:
        return [None] * len(texts)

    # Preserve position; only feed non-blank strings to the model.
    idxs = [i for i, t in enumerate(texts) if t and t.strip()]
    if not idxs:
        return [None] * len(texts)
    clipped = [texts[i][:_MAX_CHARS] for i in idxs]

    out: list[str | None] = [None] * len(texts)
    try:
        results = pipe(clipped, batch_size=16)
    except Exception:
        logger.exception('[translation] batch translation failed')
        return out

    for i, result in zip(idxs, results):
        try:
            out[i] = result['translation_text'].strip()
        except (KeyError, TypeError, AttributeError):
            out[i] = None
    return out


def translate_en_ar(text: str) -> str | None:
    """Translate a single English string to Arabic (thin wrapper over the batch API)."""
    return translate_en_ar_batch([text])[0]
