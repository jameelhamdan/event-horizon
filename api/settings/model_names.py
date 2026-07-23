"""Canonical HF model identifiers — the single source of truth.

Every place that needs one of these names (the local model wrappers in
``services/``, the build-time downloader, and the worker-start preloader in
``scripts/init_models.py``) imports from here instead of repeating the string,
so a model swap is a one-line change.

Keep this file dependency-free (stdlib only): it's also copied standalone into
the Docker build layer, before the rest of the app exists in the image (see
``scripts/init_models.py``'s module docstring and the Dockerfile).
"""

CLUSTER_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
FINBERT_MODEL_NAME = "ProsusAI/finbert"
TRANSLATION_MODEL_NAME = "Helsinki-NLP/opus-mt-en-ar"
# Two-stage analyzer (services/processing/annotator.py + refiner.py) — pretrained, no finetuning.
NER_MODEL_NAME = "Babelscape/wikineural-multilingual-ner"
# Zero-shot judge ENSEMBLE — the refine stage averages per-label scores across
# these purpose-built zeroshot classifiers (trained on a large NLI +
# classification mix specifically for zeroshot, not generic NLI bases). Measured
# on a labeled category benchmark: the old single mDeBERTa-v3-base-mnli-xnli
# scored 38%; deberta-v3-base-zeroshot-v2.0 alone 92%; bge-m3-zeroshot-v2.0
# (multilingual) alone 92%; the two ENSEMBLED 100%. bge-m3 also covers the
# non-English articles the English deberta would miss. Both run on CPU
# (~0.3s + ~0.7s per item). Trim to one entry if latency matters more than the
# last few points of accuracy.
ZEROSHOT_MODEL_NAMES = ["MoritzLaurer/deberta-v3-base-zeroshot-v2.0", "MoritzLaurer/bge-m3-zeroshot-v2.0"]
# Back-compat single-name alias (first ensemble member) for any caller/preloader
# that still expects one string.
ZEROSHOT_MODEL_NAME = ZEROSHOT_MODEL_NAMES[0]
