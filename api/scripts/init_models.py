"""ML model lifecycle helpers — two distinct, non-overlapping jobs.

``download_models()`` runs at Docker **build** time (Dockerfile) to populate the
HF cache on disk. It must not import any app code: at that build layer ``services``
has not been copied into the image yet.

``preload_into_memory()`` runs at **worker start**, inside the long-lived
worker-pool parent process *before* RQ forks its per-job work-horses. RQ executes
every job in a freshly forked horse that exits when the job finishes, so a model
loaded lazily inside a job dies with that horse and the next job reloads it from
disk ("Loading weights ..." on every task). By warming the same cached accessors
the tasks use, here in the parent, the forked horses inherit the already-loaded
weights via copy-on-write — the model loads once and is shared across all jobs.
"""

import logging

logger = logging.getLogger(__name__)

# settings/model_names.py is copied standalone to the container root (Dockerfile)
# so it's importable both here — at build time, before /app exists — and later
# at runtime, when this same file is loaded as scripts.init_models from inside /app.
try:
    from settings.model_names import (
        CLUSTER_MODEL_NAME, FINBERT_MODEL_NAME, TRANSLATION_MODEL_NAME, NER_MODEL_NAME,
    )
except ImportError:
    from model_names import (
        CLUSTER_MODEL_NAME, FINBERT_MODEL_NAME, TRANSLATION_MODEL_NAME, NER_MODEL_NAME,
    )


def download_models() -> None:
    """Build-time: fetch model weights into the HF cache. No app imports."""
    from transformers import pipeline
    from sentence_transformers import SentenceTransformer

    print(f"Downloading {CLUSTER_MODEL_NAME}...")
    SentenceTransformer(CLUSTER_MODEL_NAME)

    print(f"Downloading {FINBERT_MODEL_NAME}...")
    pipeline("text-classification", model=FINBERT_MODEL_NAME, top_k=None, truncation=True, max_length=512)

    print(f"Downloading {TRANSLATION_MODEL_NAME}...")
    # No generic "translation"/"text2text-generation" pipeline task in transformers
    # 5.x — fetch the tokenizer/model pair directly (matches services.translation).
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    AutoTokenizer.from_pretrained(TRANSLATION_MODEL_NAME)
    AutoModelForSeq2SeqLM.from_pretrained(TRANSLATION_MODEL_NAME)

    print(f"Downloading {NER_MODEL_NAME}...")
    # transformers>=5.3's TokenClassificationPipeline doesn't accept truncation/
    # max_length kwargs at all — it truncates automatically using the model's
    # own tokenizer.model_max_length.
    pipeline("ner", model=NER_MODEL_NAME, aggregation_strategy="simple")

    # VADER (services.processing.vader) is rule-based — ships with the
    # vaderSentiment package, no model weights to pre-fetch.

    print("All models downloaded.")


def preload_into_memory() -> None:
    """Worker-start: warm the in-process model caches the pipeline actually uses.

    Call once in the worker-pool parent before it forks job horses. Uses the
    same cached accessors as the tasks (``finbert._pipeline`` lru_cache and the
    clusterer ``_model`` cached_property) so the cache populated here is the cache
    the horses inherit. Best-effort: a failure here must not stop the worker —
    the model will simply load lazily per job as before.
    """
    try:
        from services.processing import finbert
        if finbert._pipeline() is not None:
            logger.info("[preload] FinBERT warmed into worker memory")
    except Exception:
        logger.exception("[preload] FinBERT preload failed — will load lazily per job")

    try:
        from services.processing.clustering import get_clusterer
        # Touch the cached_property to force the sentence-transformer load now.
        get_clusterer()._model
        logger.info("[preload] sentence-transformer clusterer warmed into worker memory")
    except Exception:
        logger.exception("[preload] clusterer preload failed — will load lazily per job")

    try:
        from services.translation import _get_model as _get_translation_model
        if _get_translation_model() is not None:
            logger.info("[preload] translation (MarianMT) warmed into worker memory")
    except Exception:
        logger.exception("[preload] translation preload failed — will load lazily per job")

    try:
        from services.processing import ner
        if ner._pipeline() is not None:
            logger.info("[preload] NER (%s) warmed into worker memory", NER_MODEL_NAME)
    except Exception:
        logger.exception("[preload] NER preload failed — will load lazily per job")

    try:
        from services.processing import vader
        if vader._analyzer() is not None:
            logger.info("[preload] VADER warmed into worker memory")
    except Exception:
        logger.exception("[preload] VADER preload failed — will load lazily per job")


if __name__ == "__main__":
    download_models()
