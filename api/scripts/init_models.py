"""ML model lifecycle helper — populates the on-disk HF cache at Docker
**build** time (Dockerfile) so no model is fetched over the network at
runtime. Must not import any app code: at that build layer ``services`` has
not been copied into the image yet.
"""

# settings/model_names.py is copied standalone to the container root (Dockerfile)
# so it's importable both here — at build time, before /app exists — and later
# at runtime, when this same file is loaded as scripts.init_models from inside /app.
try:
    from settings.model_names import (
        CLUSTER_MODEL_NAME, FINBERT_MODEL_NAME, TRANSLATION_MODEL_NAME,
        NER_MODEL_NAME, ZEROSHOT_MODEL_NAMES,
    )
except ImportError:
    from model_names import (
        CLUSTER_MODEL_NAME, FINBERT_MODEL_NAME, TRANSLATION_MODEL_NAME,
        NER_MODEL_NAME, ZEROSHOT_MODEL_NAMES,
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
    pipeline("token-classification", model=NER_MODEL_NAME, aggregation_strategy="simple")

    for zs_name in ZEROSHOT_MODEL_NAMES:  # ensemble members (refine judge)
        print(f"Downloading {zs_name}...")
        pipeline("zero-shot-classification", model=zs_name)

    # VADER (services.processing.vader) is rule-based — ships with the
    # vaderSentiment package, no model weights to pre-fetch.

    print("All models downloaded.")


if __name__ == "__main__":
    download_models()
