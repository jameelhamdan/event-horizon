"""The refine stage's service — second-opinion judging of low-confidence articles.

LLMRefiner re-judges articles the annotate stage flagged (Article.stage =
'refine': prototype-classification confidence below annotator.ESCALATE_BELOW).
It only touches the judgment fields — category, sub_category, country/city,
event_intensity (and, for the cloud provider, the EN summary) — everything
else (sentiment, translations, importance) stays as annotated.

One provider per deployment, selected by settings.REFINE_PROVIDER, each with a
batch size matched to its capacity:

  'zeroshot' (default) — mDeBERTa zero-shot NLI, on-prem, batches of 16.
                         Category-only verdict + prototype sub-pick; violent-
                         metaphor verdicts are gated by _CONFLICT_EVIDENCE.
  'ollama'             — local LLM, one article per call (the Ollama box serves
                         one generation at a time), JSON-schema constrained so
                         the output cannot fail to parse.
  'cloud'              — the existing LLM prompt client (analyzer.py) via
                         LLM_ROUTES['analyzer_lite'], batches of 8; also
                         refreshes the abstractive EN summary.
  'off'                — the refine stage never dispatches (stages.py).

A verdict of None for an article means "judge unavailable/failed" — the caller
leaves it at stage='refine' so the stage retries later.
"""

import json
import logging
import re

from services.processing._lazy import lazy_loader
from services.processing.taxonomy import CATEGORIES, SUB_CATEGORIES
from settings.model_names import ZEROSHOT_MODEL_NAMES

logger = logging.getLogger(__name__)

#: dict per article: {'category', 'sub_category', 'country', 'city',
#: 'intensity' (optional), 'summary' (optional), 'provider'} — or None.
Verdict = dict | None

# ── zero-shot provider ────────────────────────────────────────────────────────

# Candidate labels (NLI hypothesis phrasing) → category slug — the primary
# category classifier for annotate and the refine second opinion (classify_zeroshot).
# Several labels map to one slug on purpose: NLI is a set of independent binary
# premise/hypothesis checks with poor cross-class calibration, so a single broad
# label per category collides badly (measured live: sports & IPO stories pulled to
# 'economic', tech-company news pulled to 'political'). The fix is vocabulary
# augmentation — more, narrower hypotheses that each name concrete story shapes,
# not fewer. Each category gets discriminative wording; 'general' carries dedicated
# sports / tech-company / culture / human-interest labels so those beat the economic
# & political hypotheses they used to lose to; 'economic' carries both a markets
# label and a fiscal/budget label so government-finance stories don't read as
# 'political'. Phrase every label as *what the story is about*. Tuned on live evals.
_ZEROSHOT_LABELS: dict[str, str] = {
    'a war, armed conflict, military strike, airstrike, or attack by armed forces or militants': 'conflict',
    'a natural disaster, earthquake, flood, storm, wildfire, or a deadly accident': 'disaster',
    'financial markets, stocks, an IPO, inflation, trade, tariffs, or central bank monetary policy': 'economic',
    'the economy, a government budget, taxes, public spending, borrowing, debt, or fiscal policy': 'economic',
    'a government, an election, legislation, a protest, a diplomatic summit, or relations between countries': 'political',
    'a disease, medicine, public health, pregnancy, a hospital, or the healthcare system': 'health',
    'a sports match, tournament, championship, race, or an athlete': 'general',
    'consumer technology, a gadget, an app, a tech company, or an AI product': 'general',
    'culture, entertainment, film, music, a celebrity, the arts, or lifestyle': 'general',
    'an ordinary crime, a court trial, a human-interest story, or a personal profile': 'general',
}
_ZEROSHOT_TEMPLATE = 'This news article is about {}.'
_ZEROSHOT_BATCH = 16

# Sanity gate for zero-shot 'conflict' verdicts: violent *metaphors* in soft
# news ("EVs killed off", "silencing a generation") read as conflict to the NLI
# model. A real conflict story virtually always carries at least one concrete
# military/violence term; a conflict verdict with none downgrades to general.
_CONFLICT_EVIDENCE = re.compile(
    r'\b(?:airstrike|air strike|attack\w*|missile|drone|troops|soldier\w*|army|military|militant\w*|'
    r'rebel\w*|insurgent\w*|gunmen|gunman|shelling|invasion|ceasefire|warplane\w*|'
    r'bomb\w*|explosion\w*|terror\w*|hostage\w*|casualt\w*|wounded|war\b|combat\b|'
    # Unambiguous mass-violence markers — never present in a non-conflict story,
    # so they only ever PREVENT the downgrade gate from destroying a real conflict
    # verdict (measured: "Israel kills more than 80 in Gaza" and "committing
    # genocide in Gaza" were flipped to general because the old count pattern only
    # matched "kills 80", not "kills more than 80").
    r'genocide|massacre\w*|ethnic cleansing|war crime\w*|atrocit\w*|death toll|'
    r'shot dead|killed in|kill(?:s|ed|ing)?\b[\s\w]{0,12}\d)',
    re.I,
)

# Same idea for best_sub's 'protest-policy' pick: its prototype text ("police
# crackdown on protesters") embeds close to ordinary-crime phrasing like
# "police shoot suspect" — observed live: a Reno casino shooting picked
# protest-policy purely from "police shoot suspect" wording, with the site's
# own nav chrome ("Election Voter Guide... National Politics...") bleeding
# into content and dragging the category to 'political' too. Require actual
# protest/demonstration vocabulary before trusting the sub-pick; otherwise
# the story is ordinary crime, not policy unrest — drop to general/other.
_PROTEST_EVIDENCE = re.compile(
    r'\b(?:protest\w*|demonstrat\w*|riot\w*|rally|rallies|march(?:es)?|strike\w*|'
    r'unrest|crackdown|activist\w*)\b',
    re.I,
)

# Disaster⇄conflict disambiguation. A natural-disaster story often mentions the
# political fallout (aid, junta, government response) and gets pulled to
# 'conflict'; conversely a military strike ON infrastructure reads as an
# industrial accident. These two gates use the concrete physical cause to
# correct the verdict (measured misses: Myanmar earthquake -> conflict; Ukraine
# drone strike on a refinery -> disaster/industrial-accident).
_NATURAL_DISASTER_EVIDENCE = re.compile(
    r'\b(?:earthquake|quake|aftershock\w*|magnitude|richter|tremor\w*|'
    r'flood\w*|flash flood|wildfire\w*|bushfire\w*|hurricane|typhoon|cyclone|'
    r'tornado|tsunami|landslide|mudslide|volcan\w*|eruption|drought|heatwave)\b',
    re.I,
)
# Deliberate armed action — distinguishes a military strike from an accident.
_MILITARY_ACTION = re.compile(
    r'\b(?:airstrike|air strike|missile\w*|drone\w*|shell(?:ing|ed)?|bombard\w*|'
    r'troops|soldier\w*|militant\w*|warplane\w*|struck|strikes?\b)\b',
    re.I,
)
# A 'disaster' verdict must have a physical cause — a natural event OR an
# accident. Without one, a "N killed/bodies found" story is a crime, not a
# disaster (measured: cartel killings and a cult-leader killing were pulled to
# disaster by casualty wording; a coral-bleaching feature too). Fails this gate
# -> drop to general.
_ACCIDENT_EVIDENCE = re.compile(
    # Accident EVENT words only — not bare vehicle nouns (a "dump truck" in a
    # cartel-killing story is not an accident; a real crash carries crash/
    # derail/capsize/sank regardless of the vehicle).
    r'\b(?:crash\w*|collision|collaps\w*|fire|blaze|explos\w*|blast|capsiz\w*|'
    r'sank|sunk|shipwreck|derail\w*|accident\w*|spill|leak|turbulence|'
    r'stampede|wreck\w*)\b',
    re.I,
)
# A deliberate human actor/attack framing — guards the conflict->disaster
# lateral swap below from misreading a real (if lightly-worded) attack as an
# accident just because it lacks hardware-specific _MILITARY_ACTION terms
# (e.g. a market bombing blamed on "militants"/"terrorists" with no mention of
# "attack"-adjacent hardware words).
_DELIBERATE_ACTOR = re.compile(
    r'\b(?:attack\w*|terror\w*|militant\w*|rebel\w*|insurgent\w*|gunmen|gunman|'
    r'hostage\w*|genocide|massacre\w*|war crime\w*|atrocit\w*)\b',
    re.I,
)

# Political<->conflict disambiguation. A diplomatic meeting ABOUT an ongoing
# war ("Pope meets Russian Orthodox cleric to discuss Ukraine war") reads as
# 'conflict' to the NLI model purely off the war mention, with no actual
# hostility in the story — a real conflict verdict needs either military
# action or a named perpetrator, not just proximity to a war topic (measured:
# the ensemble scored 'conflict' at 0.65 confidence — not a low-confidence
# fluke a threshold would catch — for a story that is entirely about a
# diplomatic meeting).
_DIPLOMATIC_MEETING_EVIDENCE = re.compile(
    r'\b(?:meets?|met with|holds? talks|held talks|summit|discuss(?:es|ed|ion)?|'
    r'envoy|ambassador|diplomat\w*|foreign minister|peace (?:talks|deal|plan|process)|'
    r'\bpope\b|vatican|patriarch|archbishop|\bcleric\b)\b',
    re.I,
)

def _build_zeroshot(names):
    """A zero-shot pipeline per model name."""
    from transformers import pipeline
    return [pipeline('zero-shot-classification', model=name) for name in names]


# Two loaders so the high-volume annotate pass (single=True) loads ONLY the one
# fast 92% model — never the heavier ensemble member — keeping its memory
# footprint within the heavy-queue cap. The full ensemble loads lazily only when
# the refine second opinion actually runs (classify_zeroshot averages members).
# A 'health' verdict needs real disease / care-delivery context. Zero-shot
# entailment fires 'health' on incidental health words — a company *named*
# "…Health Care", "public health threat" as an aside in an energy story — so a
# health verdict without this evidence takes the best non-health category
# instead (measured over-triggers: a solar-farm backlash and a hospital-chain
# bankruptcy both pulled to health).
_HEALTH_EVIDENCE = re.compile(
    r'\b(?:disease\w*|virus\w*|viral|outbreak\w*|infect\w*|pandemic\w*|epidemic\w*|'
    r'patient\w*|hospital\w*|clinic\w*|vaccin\w*|physician\w*|doctor\w*|nurse\w*|'
    r'medicine|symptom\w*|illness\w*|cancer|diabet\w*|mental health|pregnan\w*|'
    r'maternal|surger\w*|therap\w*|coronavirus|covid|\bWHO\b|health ministry|'
    r'health\s+system|healthcare system|public health (?:emergency|crisis|official|agency))\b',
    re.I,
)

_zeroshot_pipeline = lazy_loader('zeroshot', 'ZEROSHOT_ENABLED', lambda: _build_zeroshot(ZEROSHOT_MODEL_NAMES))
_zeroshot_primary_pipeline = lazy_loader(
    'zeroshot_primary', 'ZEROSHOT_ENABLED', lambda: _build_zeroshot(ZEROSHOT_MODEL_NAMES[:1]))


def _apply_category_gates(category: str, text: str, downgrade_to_general: bool = True) -> str:
    """Post-hoc evidence gates over a zero-shot category verdict.

    Two kinds of correction:
      * *lateral* swaps by concrete cause — disaster⇄conflict (a natural
        disaster with political fallout misread as conflict; a military
        strike misread as an accident) and conflict→political (a diplomatic
        meeting that references an ongoing war, misread as the war itself) —
        always applied, they only ever correct one specific category to
        another.
      * *downgrade-to-general* (conflict/disaster with no concrete evidence) —
        a precision patch for the low-recall refine second opinion. It is
        DESTRUCTIVE: it flips real conflict/disaster stories that simply lack the
        exact evidence vocabulary ("killed by forces", "blackout") to general.
        Measured: it wrongly downgraded Gaza-casualty→general and blackout→
        general. So the high-recall primary pass (annotate) sets
        ``downgrade_to_general=False`` and trusts the 92% model's category;
        only refine keeps the downgrades.
    """
    if (category == 'conflict' and not _MILITARY_ACTION.search(text) and not _DELIBERATE_ACTOR.search(text) and (_NATURAL_DISASTER_EVIDENCE.search(text) or _ACCIDENT_EVIDENCE.search(text))):
        # No deliberate-actor language and no military hardware — a
        # "conflict" verdict driven by explosion/blast/crash wording alone is
        # almost always an accidental industrial/transport disaster, not an
        # armed strike (measured: Beirut port ammonium-nitrate explosion ->
        # conflict/airstrike; a sailor's account of "mishandled cargo" has no
        # attacker, just an accident).
        category = 'disaster'
    elif category == 'disaster' and _MILITARY_ACTION.search(text) and _CONFLICT_EVIDENCE.search(text):
        category = 'conflict'
    elif (category == 'conflict' and not _MILITARY_ACTION.search(text) and not _DELIBERATE_ACTOR.search(text)
            and _DIPLOMATIC_MEETING_EVIDENCE.search(text)):
        # Same idea, third pairing: no hostility evidence at all, just a
        # diplomatic/religious meeting that happens to reference an ongoing
        # war — the story is about the meeting, not a hostile act (measured:
        # "Pope Leo meets Russian Orthodox cleric to discuss Ukraine war" ->
        # conflict/other at 0.65 confidence, purely off the war mention).
        category = 'political'
    if downgrade_to_general:
        if category == 'conflict' and not _CONFLICT_EVIDENCE.search(text):
            category = 'general'
        if category == 'disaster' and not (_NATURAL_DISASTER_EVIDENCE.search(text) or _ACCIDENT_EVIDENCE.search(text)):
            category = 'general'
    return category


def classify_zeroshot(
    texts: list[str], single: bool = False, downgrade_to_general: bool = True,
) -> list[tuple[str | None, str | None, float]]:
    """Zero-shot category + hierarchical sub-category + confidence for each text.

    The primary classifier for the annotate stage (``single=True`` → the one
    fast 92% model) and the category verdict for the refine judge
    (``single=False`` → the full ensemble, per-label scores summed). Category is
    decided by NLI *entailment* (what the story is *about*), not embedding
    cosine (topical word-overlap) — this is what stops company names like
    "Steward Health Care" or an incidental "covid" mention from pulling a story
    into ``health``. Sub-category is then a cosine pick *within* the chosen
    category (``best_sub``), so a sub prototype can never drag in the wrong
    parent. Confidence is the winning label's (summed) score; a low value routes
    the row to the refine ensemble for a second opinion.

    Returns (category, sub_category, confidence) per text, or (None, None, 0.0)
    where the model is unavailable or a chunk failed (caller falls back)."""
    from services.processing.annotator import best_sub

    pipes = _zeroshot_primary_pipeline() if single else _zeroshot_pipeline()
    if not pipes:
        return [(None, None, 0.0)] * len(texts)
    labels = list(_ZEROSHOT_LABELS)
    clipped = [t[:350] for t in texts]
    results: list[tuple[str | None, str | None, float]] = [(None, None, 0.0)] * len(texts)
    for i in range(0, len(clipped), _ZEROSHOT_BATCH):
        chunk = clipped[i : i + _ZEROSHOT_BATCH]
        try:
            per_model = []
            for pipe in pipes:
                raw = pipe(chunk, candidate_labels=labels, hypothesis_template=_ZEROSHOT_TEMPLATE, batch_size=8)
                per_model.append([raw] if isinstance(raw, dict) else raw)
        except Exception:
            logger.exception('[zeroshot] classify failed (%d text(s))', len(chunk))
            continue
        for j, text in enumerate(chunk):
            agg: dict[str, float] = {}
            for raw in per_model:
                for label, score in zip(raw[j]['labels'], raw[j]['scores']):
                    agg[label] = agg.get(label, 0.0) + score
            if not agg:
                continue
            top_label = max(agg, key=agg.get)
            confidence = agg[top_label] / max(len(per_model), 1)  # back to a 0-1 scale
            category = _ZEROSHOT_LABELS[top_label]
            # Incidental-health guard: a health verdict with no real disease/
            # care-delivery evidence takes the best non-health category instead.
            if category == 'health' and not _HEALTH_EVIDENCE.search(text):
                best = max((lab for lab in agg if _ZEROSHOT_LABELS[lab] != 'health'), key=agg.get, default=None)
                if best:
                    category = _ZEROSHOT_LABELS[best]
            category = _apply_category_gates(category, text, downgrade_to_general)
            sub = best_sub(category, [text])[0]
            # 'protest-policy' embeds close to ordinary-crime phrasing; without
            # real protest vocabulary it's the wrong sub. Same destructive-downgrade
            # trap as the conflict/disaster gates above (see _apply_category_gates
            # docstring): dropping to 'general' here nukes real legislation/
            # diplomacy/policy stories that simply don't use protest vocabulary
            # (measured live: an Australia youth social-media-ban law, Israel
            # opening Gaza aid routes, France recognizing Palestine, a South
            # Africa poverty "national dialogue" were all wrongly flipped to
            # general by this branch). Always just correct the sub — never the
            # category — regardless of refine vs. primary pass.
            if category == 'political' and sub == 'protest-policy' and not _PROTEST_EVIDENCE.search(text):
                sub = 'other'
            results[i + j] = (category, sub, float(confidence))
    return results


# ── ollama provider ───────────────────────────────────────────────────────────

# JSON schema for Ollama constrained decoding — the model literally cannot emit
# an out-of-taxonomy category or unparseable output.
_OLLAMA_SCHEMA = {
    'type': 'object',
    'properties': {
        'category': {'type': 'string', 'enum': sorted(CATEGORIES)},
        'sub_category': {'type': ['string', 'null']},
        'country': {'type': ['string', 'null']},
        'city': {'type': ['string', 'null']},
        'intensity': {'type': 'number'},
    },
    'required': ['category', 'sub_category', 'country', 'city', 'intensity'],
}

_OLLAMA_PROMPT = (
    'Classify this news article. JSON only.\n'
    'category: one of ' + '|'.join(sorted(CATEGORIES)) + '.\n'
    'sub_category: '
    + '; '.join(f'{c}: {"|".join(sorted(s))}' for c, s in sorted(SUB_CATEGORIES.items()))
    + '.\n'
    'country/city: the place the story is about (English names) or null.\n'
    'intensity: severity 0.0-1.0 (routine 0.1, notable 0.4, major 0.7, historic 0.9).'
)


class LLMRefiner:
    """Judge a batch of (title, content) items with the configured provider.

    ``judge(items)`` returns one Verdict (or None) per item, in order. Batching
    is internal per provider; the caller passes whatever chunk the stage
    dispatched.
    """

    def __init__(self, provider: str | None = None):
        from django.conf import settings
        self.provider = provider or getattr(settings, 'REFINE_PROVIDER', 'zeroshot')

    def judge(self, items: list[tuple[str, str]]) -> list[Verdict]:
        if not items:
            return []
        judge = {
            'zeroshot': self._judge_zeroshot,
            'ollama': self._judge_ollama,
            'cloud': self._judge_cloud,
        }.get(self.provider)
        if judge is None:
            logger.warning('[refine] unknown/off provider %r — no verdicts', self.provider)
            return [None] * len(items)
        return judge(items)

    # ── providers ─────────────────────────────────────────────────────────────

    def _judge_zeroshot(self, items: list[tuple[str, str]]) -> list[Verdict]:
        # Full ensemble (single=False) — the refine second opinion; shares the
        # exact category/gate/sub logic the annotate primary pass uses.
        texts = [f'{title}. {content}' for title, content in items]
        cls = classify_zeroshot(texts, single=False)
        return [{'category': c, 'sub_category': s, 'provider': 'zeroshot'} if c else None for c, s, _conf in cls]

    def _judge_ollama(self, items: list[tuple[str, str]]) -> list[Verdict]:
        from services.llm import LLMError, get_provider, strip_code_fences

        service = get_provider('ollama_medium')
        if service is None:
            logger.warning('[refine/ollama] ollama_medium not configured')
            return [None] * len(items)
        verdicts: list[Verdict] = []
        for title, content in items:  # one generation at a time — see module docstring
            user = f'{title}\n\n{content[:550]}'
            try:
                raw = service.chat(
                    [{'role': 'system', 'content': _OLLAMA_PROMPT}, {'role': 'user', 'content': user}],
                    temperature=0, format=_OLLAMA_SCHEMA,
                )
                verdicts.append(self._parse_verdict(json.loads(strip_code_fences(raw)), provider='ollama'))
            except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning('[refine/ollama] %s', exc)
                verdicts.append(None)
        return verdicts

    def _judge_cloud(self, items: list[tuple[str, str]]) -> list[Verdict]:
        from services.processing.analyzer import ArticleAnalyzer

        analyses = ArticleAnalyzer().analyze_batch([f'{title} {content}' for title, content in items])
        verdicts: list[Verdict] = []
        for a in analyses:
            if a.error is not None:
                verdicts.append(None)
                continue
            en = a.translations.get('en') or {}
            verdicts.append({
                'category': a.category, 'sub_category': a.sub_category,
                'country': a.country, 'city': a.city,
                'intensity': a.intensity,
                'summary': en.get('summary'),
                'provider': 'cloud', 'llm_usage': a.llm_usage,
            })
        return verdicts

    # ── verdict application ───────────────────────────────────────────────────

    @staticmethod
    def apply(article, verdict: dict) -> None:
        """Apply a (non-None) verdict onto an Article in place — the single
        place refine-domain field semantics live. Does not save and does not
        touch stage/refined_on bookkeeping (the workflow owns persistence and
        stage transitions).

        Rules: category/sub always taken from the verdict; geo replaced only if
        the verdict's place actually geocodes (otherwise the annotator's geo
        stands); intensity from the verdict when present, else re-rated for the
        new category; summary merged only for non-lite articles when the judge
        produced one (cloud provider); refined_by always overwritten with this
        verdict's provider, so a re-refine (see refine_articles) correctly
        reflects whichever judge most recently decided the article's fields;
        llm_usage overwritten when the verdict carries one (cloud provider
        only — zeroshot/ollama verdicts have no 'llm_usage' key, so a prior
        annotate/analyze pass's usage is left as-is rather than cleared).
        """
        from services.processing.annotator import rate_intensity
        from services.processing.geocode import geocode

        article.category = verdict['category']
        article.sub_category = verdict['sub_category']

        city, country = verdict.get('city'), verdict.get('country')
        if city or country:
            lat, lon = geocode(city, country)
            if lat is not None:
                article.location = ', '.join(filter(None, [city, country]))
                article.latitude, article.longitude = lat, lon

        article.event_intensity = verdict.get('intensity') if verdict.get('intensity') is not None else rate_intensity(
            article.category, article.sub_category, f'{article.title}. {(article.content or "")[:500]}',
        )

        summary = verdict.get('summary')
        if summary and not (article.extra_data or {}).get('backfill_day'):
            translations = dict(article.translations or {})
            translations.setdefault('en', {})['summary'] = summary
            article.translations = translations

        article.refined_by = verdict['provider']
        if verdict.get('llm_usage') is not None:
            article.llm_usage = verdict['llm_usage']

        llm_block = dict((article.extra_data or {}).get('llm') or {})
        llm_block.update({'category': article.category, 'sub_category': article.sub_category})
        article.extra_data = {**(article.extra_data or {}), 'llm': llm_block}

    # ── parsing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_verdict(data: dict, provider: str) -> Verdict:
        category = data.get('category')
        if category not in CATEGORIES:
            return None
        sub = data.get('sub_category') or None
        if sub not in SUB_CATEGORIES[category]:
            sub = None
        verdict: dict = {
            'category': category, 'sub_category': sub,
            'country': data.get('country') or None, 'city': data.get('city') or None,
            'provider': provider,
        }
        try:
            if data.get('intensity') is not None:
                verdict['intensity'] = round(max(0.0, min(1.0, float(data['intensity']))), 4)
        except (TypeError, ValueError):
            pass
        return verdict
