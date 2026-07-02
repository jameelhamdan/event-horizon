from typing import Sequence

from core.models import ArticleDocument, ArticleFeatures
from services.processing.analyzer import ArticleAnalyzer


class ArticleCleaner:
    """
    Step 2 — Clean: enriches raw ArticleDocuments with NLP features.

    - ArticleAnalyzer (LLM): category, sub-category, country, city, coordinates,
      and event_intensity — the fields that need real judgment.
    - NER (local, dslim/bert-base-NER): named entities.
    - VADER (local, rule-based): general sentiment polarity.
    - FinBERT (local): financial sentiment — a separate, domain-specific signal.

    Input:  sequence of ArticleDocument
    Output: list of ArticleFeatures
    """

    def __init__(self) -> None:
        self._analyzer = ArticleAnalyzer()

    def clean(self, document: ArticleDocument, lite: bool = False) -> ArticleFeatures:
        """Enrich a single document (thin wrapper over clean_batch)."""
        return self.clean_batch([document], lite_flags=lite)[0]

    def clean_batch(
        self,
        documents: Sequence[ArticleDocument],
        lite_flags: bool | Sequence[bool] = False,
    ) -> list[ArticleFeatures]:
        """Enrich many documents.

        ``lite_flags`` selects whether the local Arabic translation step runs per
        document (or for all, if a single bool); the LLM call itself is always
        English-only. category/sub_category/geo/event_intensity come from one LLM
        call over the whole chunk (so a mixed lite/full chunk still maps to exactly
        one batched call — see ArticleAnalyzer.ANALYZE_BATCH_SIZE); entities (NER),
        sentiment (VADER), and finbert_sentiment all run locally on every document,
        independent of the LLM call.
        """
        if not documents:
            return []
        texts = [doc.full_text for doc in documents]
        from services.processing import finbert, ner, vader
        finbert_batch = finbert.score_batch(texts)
        entities_batch = ner.extract_batch(texts)
        sentiment_batch = vader.score_batch(texts)

        if isinstance(lite_flags, bool):
            lite_flags = [lite_flags] * len(documents)

        # Single LLM pass for the whole chunk (translate=False: the LLM output is
        # EN-only either way). Local Arabic translation is then added only for the
        # non-lite subset, mutating those ArticleAnalysis objects in place — this
        # keeps chunk-to-LLM-call mapping 1:1 even when lite_flags are mixed.
        analyses = self._analyzer.analyze_batch(texts, translate=False)
        full_idxs = [i for i, lf in enumerate(lite_flags) if not lf]
        if full_idxs:
            self._analyzer._add_arabic_translations([analyses[i] for i in full_idxs])

        results = []
        for doc, finbert_sentiment, entities, sentiment, analysis in zip(
            documents, finbert_batch, entities_batch, sentiment_batch, analyses,
        ):
            location = ', '.join(filter(None, [analysis.city, analysis.country])) or None
            results.append(ArticleFeatures(
                id=doc.id,
                entities=entities,
                sentiment=sentiment,
                finbert_sentiment=finbert_sentiment,
                location=location,
                latitude=analysis.latitude,
                longitude=analysis.longitude,
                event_intensity=analysis.intensity,
                category=analysis.category,
                sub_category=analysis.sub_category,
                llm_data=analysis.llm_data,
                translations=analysis.translations,
                llm_usage=analysis.llm_usage,
                llm_error=analysis.error,
            ))
        return results
