from typing import Sequence

from core.models import ArticleDocument, ArticleFeatures
from services.processing.analyzer import ArticleAnalyzer


class ArticleCleaner:
    """
    Step 2 — Clean: enriches raw ArticleDocuments with NLP features.

    - ArticleAnalyzer (LLM): category, country, city, coordinates, named
      entities, and sentiment.
    - FinBERT (local): financial sentiment — retained as a calibrated numeric
      forecasting feature.

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

        ``lite_flags`` selects the English-only analyzer per document (or for all,
        if a single bool). Entities, sentiment, and event_intensity all come from
        the LLM analysis; FinBERT (local) runs on every document. Documents are
        grouped by mode so each LLM call stays homogeneous (full vs lite use
        different schemas/batch sizes) while still batching maximally within each
        group.
        """
        if not documents:
            return []
        texts = [doc.full_text for doc in documents]
        from services.processing import finbert
        finbert_batch = finbert.score_batch(texts)

        if isinstance(lite_flags, bool):
            lite_flags = [lite_flags] * len(documents)

        # One batched LLM call per mode; scatter results back into input order.
        analyses: list = [None] * len(documents)
        for lite in (False, True):
            idxs = [i for i, lf in enumerate(lite_flags) if bool(lf) is lite]
            if not idxs:
                continue
            sub = self._analyzer.analyze_batch([texts[i] for i in idxs], translate=not lite)
            for i, analysis in zip(idxs, sub):
                analyses[i] = analysis

        # Belt-and-suspenders: fill any None slot (shouldn't happen — analyze_batch
        # guarantees equal-length output, but protects if a future change breaks that).
        analyses = [a if a is not None else self._analyzer._empty() for a in analyses]

        results = []
        for doc, finbert_sentiment, analysis in zip(
            documents, finbert_batch, analyses,
        ):
            # Entities, sentiment, intensity, city/country (and coords) all come
            # straight from the single LLM analysis — no local NLP heuristics.
            location = ', '.join(filter(None, [analysis.city, analysis.country])) or None
            results.append(ArticleFeatures(
                id=doc.id,
                entities=analysis.entities,
                sentiment=analysis.sentiment,
                finbert_sentiment=finbert_sentiment,
                location=location,
                latitude=analysis.latitude,
                longitude=analysis.longitude,
                event_intensity=analysis.intensity,
                category=analysis.category,
                sub_category=analysis.sub_category,
                llm_data=analysis.llm_data,
                translations=analysis.translations,
            ))
        return results
