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

    def clean(self, document: ArticleDocument) -> ArticleFeatures:
        """
        Enrich a single document.

        event_intensity = entity_density * 0.65 + abs(sentiment) * 0.35
        where entity_density = min(entity_count / 8, 1.0)

        Entity density is the primary signal (factual richness / coverage depth);
        sentiment polarity is secondary (emotional charge).
        Saturation at 8 entities makes the density component more sensitive.
        """
        raw_entities = self._ner(document.full_text)
        entities = [{'text': e['word'], 'label': e['entity_group']} for e in raw_entities]

        analysis = self._analyzer.analyze(document.full_text)

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

    def clean_batch(self, documents: Sequence[ArticleDocument]) -> list[ArticleFeatures]:
        if not documents:
            return []
        texts = [doc.full_text for doc in documents]
        ner_batch = self._ner(texts, batch_size=16)
        from services.processing import finbert
        finbert_batch = finbert.score_batch(texts)
        # Single multi-article LLM call per chunk instead of one call per document.
        analyses = self._analyzer.analyze_batch(texts)
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
