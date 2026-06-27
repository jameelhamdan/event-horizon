from typing import Sequence

from core.models import ArticleDocument, ArticleFeatures
from services.processing.analyzer import ArticleAnalyzer, geocode_from_entities


class CleaningError(Exception):
    """Base exception for the cleaning service."""
    pass


def _resolve_location(analysis, entities):
    """Return (location, lat, lon), falling back to NER-derived geography when the LLM gave none.

    Mutates analysis.llm_data with the NER-derived city/country so event bucketing stays correct.
    """
    city, country = analysis.city, analysis.country
    lat, lon = analysis.latitude, analysis.longitude
    if not city and not country:
        city, country, lat, lon = geocode_from_entities(entities)
        if city or country:
            analysis.llm_data['city'] = city
            analysis.llm_data['country'] = country
    location = ', '.join(filter(None, [city, country])) or None
    return location, lat, lon


class ArticleCleaner:
    """
    Step 2 — Clean: enriches raw ArticleDocuments with NLP features.

    - HuggingFace NER (dslim/bert-base-NER): named entity extraction
    - VADER: sentiment score
    - ArticleAnalyzer (LLM): category, country, city, coordinates

    Input:  sequence of ArticleDocument
    Output: list of ArticleFeatures
    Raises CleaningError if a required dependency is unavailable.
    """

    def __init__(self) -> None:
        try:
            from transformers import pipeline
            self._ner = pipeline(
                'token-classification',
                model='dslim/bert-base-NER',
                aggregation_strategy='simple',
            )
        except ImportError as e:
            raise CleaningError('transformers not installed. Run: pip install transformers') from e

        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._vader = SentimentIntensityAnalyzer()
        except ImportError as e:
            raise CleaningError('vaderSentiment not installed. Run: pip install vaderSentiment') from e

        self._analyzer = ArticleAnalyzer()

    def clean(self, document: ArticleDocument, lite: bool = False) -> ArticleFeatures:
        """
        Enrich a single document.

        lite=True runs the English-only analyzer (skips Arabic) — used for
        backfilled historical articles.

        event_intensity = entity_density * 0.65 + abs(sentiment) * 0.35
        where entity_density = min(entity_count / 8, 1.0)

        Entity density is the primary signal (factual richness / coverage depth);
        sentiment polarity is secondary (emotional charge).
        Saturation at 8 entities makes the density component more sensitive.
        """
        raw_entities = self._ner(document.full_text)
        entities = [{'text': e['word'], 'label': e['entity_group']} for e in raw_entities]

        analysis = self._analyzer.analyze(document.full_text, translate=not lite)

        sentiment = round(self._vader.polarity_scores(document.full_text)['compound'], 4)
        from services.processing import finbert
        finbert_sentiment = finbert.score(document.full_text)
        entity_density = min(len(entities) / 8.0, 1.0)
        event_intensity = round(entity_density * 0.65 + abs(sentiment) * 0.35, 4)

        location, lat, lon = _resolve_location(analysis, entities)

        return ArticleFeatures(
            id=document.id,
            entities=entities,
            sentiment=sentiment,
            finbert_sentiment=finbert_sentiment,
            location=location,
            latitude=lat,
            longitude=lon,
            event_intensity=event_intensity,
            category=analysis.category,
            sub_category=analysis.sub_category,
            llm_data=analysis.llm_data,
            translations=analysis.translations,
        )

    def clean_batch(
        self,
        documents: Sequence[ArticleDocument],
        lite_flags: bool | Sequence[bool] = False,
    ) -> list[ArticleFeatures]:
        """Enrich many documents.

        ``lite_flags`` selects the English-only analyzer per document (or for all,
        if a single bool). Local NLP (NER, FinBERT, VADER) runs on every document
        regardless; only the LLM analysis differs. Documents are grouped by mode so
        each LLM call stays homogeneous (full vs lite use different schemas/batch
        sizes) while still batching maximally within each group.
        """
        if not documents:
            return []
        texts = [doc.full_text for doc in documents]
        ner_batch = self._ner(texts, batch_size=16)
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
        for doc, raw_entities, finbert_sentiment, analysis in zip(
            documents, ner_batch, finbert_batch, analyses,
        ):
            entities = [{'text': e['word'], 'label': e['entity_group']} for e in raw_entities]
            sentiment = round(self._vader.polarity_scores(doc.full_text)['compound'], 4)
            entity_density = min(len(entities) / 8.0, 1.0)
            event_intensity = round(entity_density * 0.65 + abs(sentiment) * 0.35, 4)
            location, lat, lon = _resolve_location(analysis, entities)
            results.append(ArticleFeatures(
                id=doc.id,
                entities=entities,
                sentiment=sentiment,
                finbert_sentiment=finbert_sentiment,
                location=location,
                latitude=lat,
                longitude=lon,
                event_intensity=event_intensity,
                category=analysis.category,
                sub_category=analysis.sub_category,
                llm_data=analysis.llm_data,
                translations=analysis.translations,
            ))
        return results
