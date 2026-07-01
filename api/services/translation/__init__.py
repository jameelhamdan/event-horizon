"""
Local English→Arabic translation (MarianMT), replacing LLM-generated Arabic
translations in ArticleAnalyzer.

Helsinki-NLP/opus-mt-en-ar is a small dedicated seq2seq translation model — a much
better fit for this narrow task than asking a general LLM to translate, and it runs
comfortably CPU-only. Loaded lazily and cached (mirrors services.processing.finbert).

Uses the tokenizer/model pair directly (tokenize → generate → decode) rather than
``transformers.pipeline()`` — transformers 5.x dropped the generic "translation"/
"text2text-generation" pipeline task, so there's no ``pipeline("translation", ...)``
shortcut for a seq2seq model anymore.
"""

import logging

from services.processing._lazy import lazy_loader

logger = logging.getLogger(__name__)

_MODEL_NAME = 'Helsinki-NLP/opus-mt-en-ar'
# MarianMT truncates around 512 tokens; cap input chars to keep batching cheap
# and translations focused on the title/summary (not full article bodies).
_MAX_CHARS = 1000
_MAX_NEW_TOKENS = 512


def _build_model():
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(_MODEL_NAME)
    model = AutoModelForSeq2SeqLM.from_pretrained(_MODEL_NAME)
    return tokenizer, model


# Opt-out via TRANSLATION_ENABLED (default on).
_get_model = lazy_loader('translation', 'TRANSLATION_ENABLED', _build_model)


def translate_en_ar_batch(texts: list[str]) -> list[str | None]:
    """Batch-translate English texts to Arabic. Returns a list aligned with the
    input (None per item on failure or for empty/blank input)."""
    loaded = _get_model()
    if loaded is None or not texts:
        return [None] * len(texts)
    tokenizer, model = loaded

    # Preserve position; only feed non-blank strings to the model.
    idxs = [i for i, t in enumerate(texts) if t and t.strip()]
    if not idxs:
        return [None] * len(texts)
    clipped = [texts[i][:_MAX_CHARS] for i in idxs]

    out: list[str | None] = [None] * len(texts)
    try:
        inputs = tokenizer(clipped, return_tensors='pt', padding=True, truncation=True, max_length=512)
        outputs = model.generate(**inputs, max_new_tokens=_MAX_NEW_TOKENS)
        decoded = tokenizer.batch_decode(outputs, skip_special_tokens=True)
    except Exception:
        logger.exception('[translation] batch translation failed')
        return out

    for i, text in zip(idxs, decoded):
        out[i] = text.strip() or None
    return out


def translate_en_ar(text: str) -> str | None:
    """Translate a single English string to Arabic (thin wrapper over the batch API)."""
    return translate_en_ar_batch([text])[0]
