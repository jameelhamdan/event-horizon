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
NER_MODEL_NAME = "dslim/bert-base-NER"
