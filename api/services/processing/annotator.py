"""The annotate stage's service — full on-prem NLP annotation, no LLM anywhere.

NLPAnnotator turns raw ArticleDocuments into ArticleFeatures, per article:

  category/sub_category  nearest taxonomy prototype by embedding cosine
                         (shared clustering model); the cosine confidence is
                         reported on ArticleFeatures.confidence for the
                         caller to route low-confidence articles to the
                         refine judge (services.processing.refiner)
  country/city           pretrained multilingual NER → gazetteer resolution
                         (services.processing.geocode), country-of-city
                         backfill, regex country-scan fallback
  intensity              taxonomy prior + lexical severity cues
  sentiment              VADER (general) + FinBERT (financial)
  translations.en        title as-is + extractive summary (leading sentences)
  translations.ar        MarianMT, non-lite documents only

Every model is a pretrained download (MiniLM, wikineural, FinBERT, MarianMT) —
no training, no network calls at inference, works on an empty database. This
module is a stateless service (no Django model imports, ArticleFeatures aside
— it's a plain DTO) and must not import refiner.py: deciding a pipeline stage
from a confidence score, and picking an LLM/judge tier, are both orchestration
concerns that belong to services.workflow.articles, not to annotation itself.
"""

import functools
import logging
import re

from services.processing._lazy import lazy_loader
from services.processing.geocode import (
    canonical_country, city_country_conflict, country_of_city, find_demonym, find_place, geocode,
    is_city, resolve_state_country_collision,
)
from services.processing.taxonomy import PRIORS, PROTOTYPES
from settings.model_names import NER_MODEL_NAME

logger = logging.getLogger(__name__)

# Below this cosine-to-nearest-prototype confidence, ArticleFeatures.confidence
# tells the caller (services.workflow.articles.annotate_articles) to route the
# article to the refine stage instead of leaving it terminal — the
# classification itself still stands either way, this only decides who gets a
# second opinion.
ESCALATE_BELOW = 0.45
# general/other carries many broad prototypes (sports/tech/culture/crime/…) that
# vacuum up borderline events and narrowly outscore a specific category — a real
# conflict/political/economic headline lost to general by a hair strands the
# event (no routing, floor intensity). When general wins the argmax but a
# specific category's best prototype is within this cosine margin, prefer the
# specific one: a false 'general' is more costly than a false specific.
GENERAL_TIEBREAK_MARGIN = 0.04
# …but only when the specific candidate is a meaningful match, not noise: below
# this cosine, no prototype really fits (the whole row escalates to refine
# anyway) and flipping general→specific just swaps one bad guess for another
# (observed: a 0.09-cosine "car attack" row flipping general→disaster/storm).
GENERAL_TIEBREAK_MIN_SPECIFIC = 0.30
# When picking a sub-category *within* a fixed category (refiner verdicts), a
# prototype match weaker than this doesn't justify a specific sub.
_SUB_FLOOR = 0.25

# How much text feeds each model — headline-plus-lead is where the signal is.
_CLASSIFY_MAX_CHARS = 350
_NER_MAX_CHARS = 400
_SUMMARY_MAX_CHARS = 350
_SUMMARY_MAX_SENTENCES = 3

_ner_pipeline = lazy_loader('ner', 'NER_ENABLED', lambda: _build_ner())


def _build_ner():
    from transformers import pipeline
    return pipeline('token-classification', model=NER_MODEL_NAME, aggregation_strategy='simple')


@functools.lru_cache(maxsize=1)
def _prototypes():
    """(pairs, embedding matrix) over every prototype sentence, flattened —
    ``pairs[i]`` is the (category, sub) the i-th sentence belongs to, so a pair
    with several prototypes simply owns several rows and argmax over rows is
    already max-over-a-pair's-prototypes. Encoded once per process with the
    shared clustering model."""
    from services.processing.clustering import get_clusterer
    pairs, texts = [], []
    for pair, sentences in PROTOTYPES.items():
        for sentence in sentences:
            pairs.append(pair)
            texts.append(sentence)
    return pairs, get_clusterer().encode(texts)


# ── Rule-based intensity ──────────────────────────────────────────────────────

_CASUALTIES = re.compile(r'\b(\d[\d,]*)\s+(?:people\s+|persons\s+)?(?:dead|killed|deaths|died|injured|wounded|missing|feared dead)\b', re.I)
_MAGNITUDE = re.compile(r'\bmagnitude[- ](\d(?:\.\d)?)\b', re.I)
_ESCALATION = re.compile(r'\b(?:invasion|state of emergency|mass casualt\w*|catastroph\w*|unprecedented|historic|nuclear|declaration of war|martial law)\b', re.I)
_ROUTINE = re.compile(r'\b(?:opinion|analysis|review|interview|preview|explainer|op-ed)\b:?', re.I)


def rate_intensity(category: str, sub_category: str | None, text: str) -> float:
    """Taxonomy prior adjusted by lexical severity cues, clamped to [0, 1].

    Mirrors the classification rubric: casualty counts and escalation
    vocabulary push up; opinion/analysis framing pulls down.
    """
    score = PRIORS.get((category, sub_category or 'other'), 0.2)

    counts = [int(m.replace(',', '')) for m in _CASUALTIES.findall(text)]
    if counts:
        worst = max(counts)
        score += 0.25 if worst >= 100 else 0.15 if worst >= 10 else 0.05
    quakes = [float(m) for m in _MAGNITUDE.findall(text)]
    if quakes:
        strongest = max(quakes)
        score += 0.25 if strongest >= 7 else 0.15 if strongest >= 6 else 0.0
    if _ESCALATION.search(text):
        score += 0.1
    if _ROUTINE.search(text[:80]):  # framing labels live at the start of a title
        score -= 0.15

    return round(max(0.0, min(1.0, score)), 4)


def best_sub(category: str, texts: list[str]) -> list[str | None]:
    """Best sub-category slug within a fixed *category* for each text — used by
    the refiner to complete a judge's category-only verdict. Falls back to
    'other' below the sub-confidence floor."""
    from sentence_transformers import util
    from services.processing.clustering import get_clusterer

    if not texts:
        return []
    pairs, proto_emb = _prototypes()
    sim = util.cos_sim(get_clusterer().encode(texts), proto_emb)
    out: list[str | None] = []
    for i in range(len(texts)):
        in_cat = [(float(sim[i][j]), pair[1]) for j, pair in enumerate(pairs) if pair[0] == category]
        score, sub = max(in_cat)
        out.append(sub if score >= _SUB_FLOOR else 'other')
    return out


def _extract_summary(content: str) -> str:
    """Leading sentences of the article body, bounded in count and length."""
    text = ' '.join((content or '').split())
    if not text:
        return ''
    sentences = re.split(r'(?<=[.!?])\s+', text)
    out = ''
    for sentence in sentences[:_SUMMARY_MAX_SENTENCES]:
        if out and len(out) + len(sentence) + 1 > _SUMMARY_MAX_CHARS:
            break
        out = f'{out} {sentence}'.strip()
    return out[:_SUMMARY_MAX_CHARS]


def add_arabic_translations(translation_blocks: list[dict]) -> None:
    """Add an 'ar' block to each i18n subdocument (mutating in place), derived
    from its 'en' block via the local MarianMT model. Batches every field
    across every document into a single translation-model call."""
    from services.translation import translate_en_ar_batch

    fields = ('title', 'summary', 'country', 'city')
    flat_texts: list[str] = []
    slots: list[tuple[int, str]] = []  # (block_index, field)
    for i, block in enumerate(translation_blocks):
        en = block.get('en') if isinstance(block, dict) else None
        if not isinstance(en, dict):
            continue
        for field in fields:
            val = en.get(field)
            if isinstance(val, str) and val.strip():
                slots.append((i, field))
                flat_texts.append(val)
    if not flat_texts:
        return

    translated = translate_en_ar_batch(flat_texts)
    for (i, field), tr in zip(slots, translated):
        if tr:
            translation_blocks[i].setdefault('ar', {})[field] = tr


class NLPAnnotator:
    """Annotate ArticleDocuments with every on-prem feature in one pass.

    Failure semantics match the old LLM contract: if the embedding classifier
    (the one hard dependency) is unavailable, every features object carries
    ``llm_error`` so the caller leaves the article at stage='fetched' for retry
    instead of stamping degraded 'general' annotations as done. NER, FinBERT,
    VADER and translation all degrade gracefully per-field instead.
    """

    def annotate(self, document, lite: bool = False):
        return self.annotate_batch([document], lite_flags=lite)[0]

    def annotate_batch(self, documents, lite_flags=False) -> list:
        from core.models import ArticleFeatures
        from services.processing import finbert, vader

        if not documents:
            return []
        if isinstance(lite_flags, bool):
            lite_flags = [lite_flags] * len(documents)

        texts = [doc.full_text for doc in documents]
        finbert_batch = finbert.score_batch(texts)
        sentiment_batch = vader.score_batch(texts)

        heads = [f'{doc.title}. {doc.content}'[:_CLASSIFY_MAX_CHARS] for doc in documents]
        try:
            classes = self.classify_batch(heads)
        except Exception as exc:
            logger.exception('NLPAnnotator classification failed (%d article(s))', len(documents))
            err = f'annotation failed: {exc}'[:300]
            return [
                self._empty_features(doc, sent, fin, error=err)
                for doc, sent, fin in zip(documents, sentiment_batch, finbert_batch)
            ]

        geo = self._locate([f'{doc.title}. {doc.content}'[:_NER_MAX_CHARS] for doc in documents])

        results = []
        for doc, sent, fin, (category, sub_category, confidence), (city, country) in zip(
            documents, sentiment_batch, finbert_batch, classes, geo,
        ):
            if city and not country:
                country = country_of_city(city)
            lat, lon = geocode(city, country)
            summary = _extract_summary(doc.content) or doc.title
            results.append(ArticleFeatures(
                id=doc.id,
                sentiment=sent,
                finbert_sentiment=fin,
                location=', '.join(filter(None, [city, country])) or None,
                latitude=lat, longitude=lon,
                event_intensity=rate_intensity(category, sub_category, f'{doc.title}. {doc.content[:500]}'),
                category=category, sub_category=sub_category,
                llm_data={
                    'category': category, 'sub_category': sub_category,
                    'country': country, 'city': city,
                    'annotator': 'nlp', 'confidence': round(confidence, 4),
                },
                translations={'en': {'title': doc.title, 'summary': summary, 'country': country, 'city': city}},
                llm_usage={'provider': 'nlp'},
                confidence=confidence,
            ))

        full_blocks = [r.translations for r, lite in zip(results, lite_flags) if not lite]
        if full_blocks:
            add_arabic_translations(full_blocks)
        return results

    @staticmethod
    def _empty_features(doc, sentiment: float, finbert_sentiment: float | None, error: str):
        """A zeroed-out ArticleFeatures for a document whose classification
        failed — mirrors ArticleAnalyzer._empty() in analyzer.py so both
        analyzers degrade the same way. confidence=0.0 so a caller that (for
        some reason) inspects it before checking llm_error still reads
        "not confident" rather than "fully confident"."""
        from core.models import ArticleFeatures
        return ArticleFeatures(
            id=doc.id, sentiment=sentiment, finbert_sentiment=finbert_sentiment,
            location=None, latitude=None, longitude=None,
            event_intensity=0.0, category='general', sub_category=None,
            llm_data={}, translations={}, llm_usage={},
            confidence=0.0, llm_error=error,
        )

    # ── classification ────────────────────────────────────────────────────────

    def classify_batch(self, texts: list[str]) -> list[tuple[str, str | None, float]]:
        """(category, sub_category, confidence) per text.

        Primary decision is zero-shot NLI *entailment* — what the story is about
        — decided at the category level, with sub-category picked hierarchically
        within it (services.processing.refiner.classify_zeroshot, single fast
        model). Entailment replaced the old nearest-prototype cosine argmax,
        which scored topical word-overlap and pulled company names ("Steward
        Health Care") or incidental mentions ("covid") into the wrong category.
        Nearest-prototype cosine remains the fallback for any row the zero-shot
        model can't score (ZEROSHOT_ENABLED off, or a chunk failure). No
        escalation here — the caller compares confidence against ESCALATE_BELOW
        to route low-confidence rows to the refine ensemble."""
        from services.processing.refiner import classify_zeroshot

        results = classify_zeroshot(texts, single=True, downgrade_to_general=False)
        missing = [i for i, (cat, _sub, _conf) in enumerate(results) if cat is None]
        if missing:
            fallback = self._classify_cosine([texts[i] for i in missing])
            for i, res in zip(missing, fallback):
                results[i] = res
        return results

    def _classify_cosine(self, texts: list[str]) -> list[tuple[str, str | None, float]]:
        """Nearest-prototype (category, sub_category, cosine) — the fallback when
        the zero-shot classifier is unavailable. Retains the general tie-break
        (a specific category narrowly beaten by general's broad prototypes wins,
        when the specific match is meaningful)."""
        if not texts:
            return []
        from sentence_transformers import util
        from services.processing.clustering import get_clusterer

        pairs, proto_emb = _prototypes()
        specific_rows = [j for j, p in enumerate(pairs) if p[0] != 'general']
        sim = util.cos_sim(get_clusterer().encode(texts), proto_emb)
        results = []
        for i in range(len(texts)):
            row = sim[i]
            best = int(row.argmax())
            cat, sub = pairs[best]
            score = float(row[best])
            if cat == 'general' and specific_rows:
                spec_best = specific_rows[int(row[specific_rows].argmax())]
                spec_score = float(row[spec_best])
                if spec_score >= GENERAL_TIEBREAK_MIN_SPECIFIC and score - spec_score <= GENERAL_TIEBREAK_MARGIN:
                    cat, sub = pairs[spec_best]
                    score = spec_score
            results.append((cat, sub, score))
        return results

    # ── geography ─────────────────────────────────────────────────────────────

    def _locate(self, texts: list[str]) -> list[tuple[str | None, str | None]]:
        """(city, country) per text: NER location spans resolved against the
        gazetteer in order of appearance (title first), falling back to a regex
        country scan when NER is unavailable or finds nothing."""
        ner = _ner_pipeline()
        entities: list[list[str]] = [[] for _ in texts]
        # Whether NER found at least one raw LOC span before the person-name
        # filter below, per text — distinguishes "NER found nothing" (the
        # regex fallback below is meant for this) from "NER found a place
        # name but we deliberately excluded it as a person-name collision"
        # (falling back to a raw regex scan of the same text would just
        # rediscover the same false positive, since the surname and the
        # place name are spelled identically).
        had_raw_loc = [False] * len(texts)
        if ner is not None:
            try:
                raw = ner(texts, batch_size=8)
                if texts and isinstance(raw[0], dict):
                    raw = [raw]
                for i, ents in enumerate(raw):
                    # A place name that's also a common surname ("Jordan" in
                    # "Jim Jordan") occasionally gets mistagged LOC by the NER
                    # model — confirmed live (House Speaker race geocoded to
                    # the country Jordan). Any PER span in the same text is a
                    # same-document signal the model saw it as part of a
                    # person's name; drop a LOC span whose word is also a
                    # token of a PER span rather than trusting it as a place.
                    person_tokens = {tok for e in ents if e.get('entity_group') == 'PER' for tok in e['word'].strip().lower().split()}
                    raw_locs = [e for e in ents if e.get('entity_group') == 'LOC' and float(e.get('score', 0)) >= 0.5]
                    had_raw_loc[i] = bool(raw_locs)
                    entities[i] = [e['word'] for e in raw_locs if e['word'].strip().lower() not in person_tokens]
            except Exception:
                logger.exception('[ner] entity extraction failed (%d text(s))', len(texts))

        results = []
        for text, ents, had_loc in zip(texts, entities, had_raw_loc):
            city = country = None
            for name in ents:
                canonical = canonical_country(name)
                if canonical:
                    # A country/territory mention is never a city, even when the
                    # gazetteer also lists the name as a town ("Jordan" is both a
                    # country and a Minnesota city — the country reading wins).
                    country = country or canonical
                elif city is None and is_city(name):
                    city = name
                if city and country:
                    break
            if city is None and country is None:
                # NER resolved no place. If it found no LOC span at all, a full
                # country-name + demonym scan is safe. If it *did* find a LOC we
                # excluded (a place-name/surname collision), only the demonym
                # scan is safe — a country-name scan would rediscover the same
                # false positive, but a demonym never collides with a person span.
                country = find_place(text) if not had_loc else find_demonym(text)
            country = resolve_state_country_collision(country, text)
            if city and country and city.strip().lower() == country.strip().lower():
                city = None  # avoid 'Mexico, Mexico'
            elif city_country_conflict(city, country):
                # Self-contradictory pair (e.g. "Kyiv, Russia") — a real city
                # found via the gazetteer beats an unrelated country mention
                # picked up elsewhere in the text; trust the city's own country.
                country = country_of_city(city)
            results.append((city, country))
        return results
