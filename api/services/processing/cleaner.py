from typing import Sequence

from core.models import ArticleDocument, ArticleFeatures
from services.processing.analyzer import ArticleAnalyzer


class CleaningError(Exception):
    """Base exception for the cleaning service."""
    pass


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

        location = ', '.join(filter(None, [analysis.city, analysis.country])) or None

        return ArticleFeatures(
            id=document.id,
            entities=entities,
            sentiment=sentiment,
            finbert_sentiment=finbert_sentiment,
            location=location,
            latitude=analysis.latitude,
            longitude=analysis.longitude,
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
        results = []
        for doc, raw_entities, finbert_sentiment in zip(documents, ner_batch, finbert_batch):
            entities = [{'text': e['word'], 'label': e['entity_group']} for e in raw_entities]
            analysis = self._analyzer.analyze(doc.full_text)
            sentiment = round(self._vader.polarity_scores(doc.full_text)['compound'], 4)
            entity_density = min(len(entities) / 8.0, 1.0)
            event_intensity = round(entity_density * 0.65 + abs(sentiment) * 0.35, 4)
            location = ', '.join(filter(None, [analysis.city, analysis.country])) or None
            results.append(ArticleFeatures(
                id=doc.id,
                entities=entities,
                sentiment=sentiment,
                finbert_sentiment=finbert_sentiment,
                location=location,
                latitude=analysis.latitude,
                longitude=analysis.longitude,
                event_intensity=event_intensity,
                category=analysis.category,
                sub_category=analysis.sub_category,
                llm_data=analysis.llm_data,
                translations=analysis.translations,
            ))
        return results
