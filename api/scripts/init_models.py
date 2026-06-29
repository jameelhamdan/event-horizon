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


def download_models() -> None:
    """Build-time: fetch model weights into the HF cache. No app imports."""
    from transformers import pipeline
    from sentence_transformers import SentenceTransformer

    print("Downloading paraphrase-multilingual-MiniLM-L12-v2...")
    SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

    print("Downloading ProsusAI/finbert...")
    pipeline("text-classification", model="ProsusAI/finbert", top_k=None, truncation=True, max_length=512)

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


if __name__ == "__main__":
    download_models()
