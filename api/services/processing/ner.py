"""Local named-entity recognition (dslim/bert-base-NER).

A dedicated 110M-param NER model is both cheaper and a better fit for this
narrow task than asking a general LLM to extract entities. Loaded lazily and
cached (mirrors services.processing.finbert).
"""

import logging

from services.processing._lazy import lazy_loader

logger = logging.getLogger(__name__)

_MODEL_NAME = 'dslim/bert-base-NER'
# BERT truncates at 512 tokens; cap input chars to keep batching cheap.
_MAX_CHARS = 1500
# HF entity_group labels this model emits (aggregation_strategy='simple' merges
# B-/I- sub-tokens into single spans already labelled PER/ORG/LOC/MISC).
_VALID_LABELS = {'PER', 'ORG', 'LOC', 'MISC'}
# Below this confidence an entity span is dropped as noise.
_MIN_SCORE = 0.5


def _build_pipeline():
    from transformers import pipeline
    # transformers>=5.3's TokenClassificationPipeline doesn't accept truncation/
    # max_length kwargs at all — it truncates automatically using the model's
    # own tokenizer.model_max_length (512 for BERT). _MAX_CHARS above is a
    # cheap pre-clip so we don't tokenize huge inputs just to discard them.
    return pipeline('ner', model=_MODEL_NAME, aggregation_strategy='simple')


# Opt-out via NER_ENABLED (default on).
_pipeline = lazy_loader('ner', 'NER_ENABLED', _build_pipeline)


def _clean(raw: list[dict]) -> list[dict]:
    out = []
    seen: set[tuple[str, str]] = set()
    for e in raw:
        label = str(e.get('entity_group') or '').upper()
        if label not in _VALID_LABELS:
            continue
        if float(e.get('score') or 0.0) < _MIN_SCORE:
            continue
        text = str(e.get('word') or '').strip()
        if not text:
            continue
        key = (text.lower(), label)
        if key in seen:
            continue
        seen.add(key)
        out.append({'text': text, 'label': label})
    return out


def extract_batch(texts: list[str]) -> list[list[dict]]:
    """Batch-extract entities. Returns a list aligned with the input
    ([{'text','label'}] per item; [] on failure or for blank input)."""
    if not texts:
        return []
    pipe = _pipeline()
    if pipe is None:
        return [[] for _ in texts]
    clipped = [(t or '')[:_MAX_CHARS] for t in texts]
    try:
        raw = pipe(clipped, batch_size=16)
    except Exception:
        logger.exception('[ner] batch extraction failed')
        return [[] for _ in texts]
    # pipeline() over a list of strings yields a list[list[dict]], one per input.
    return [_clean(item) for item in raw]
