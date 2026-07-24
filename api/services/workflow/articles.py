"""Article-flow orchestration glue for the fetch/analyze/annotate/refine stages.

Thin, id-driven executors over the services that do the actual work:
``fetch_source`` (RSS via services.data), ``analyze_live_articles``
(services.processing.analyzer — full cloud-LLM analysis, live traffic only),
``annotate_articles`` (services.processing.annotator + services.scoring — full
on-prem NLP, historical/backfill volume plus any live article the LLM pass
didn't reach in time), and ``refine_articles`` (services.processing.refiner —
second-opinion judge for annotate's low-confidence output). Selection
predicates live in services/stages.py — these functions never decide *what*
to run on, only *how* to persist the results.
"""

import logging
import re
import requests
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _article_uuids(ids: list) -> list:
    """Normalize a mixed list of article ids (str UUIDs or UUID objects) to
    UUIDs for an ``id__in`` filter — the shared preamble of every id-driven
    workflow function (analyze/annotate/refine/rehydrate)."""
    import uuid
    return [i if isinstance(i, uuid.UUID) else uuid.UUID(str(i)) for i in ids]


# Cursor floor: never fetch further back than this on the live path — feeds
# rarely retain more than a day, and anything older is historical-backfill
# territory (services/data/historical.py).
FETCH_CURSOR_MAX_LOOKBACK_HOURS = 24


def fetch_source(source_code: str, start: datetime | None = None) -> int:
    """Fetch one source from its ``last_fetched_at`` cursor and advance it.

    The cursor (start of the last *successful* fetch) replaces the old fixed
    ``now − 20min`` window, so downtime longer than the fetch interval no
    longer drops articles published during the gap — the next successful run
    picks up exactly where the last one left off (clamped to
    FETCH_CURSOR_MAX_LOOKBACK_HOURS; URL-level get_or_create makes any overlap
    free). The cursor only advances on success: a fetch that raises leaves it
    untouched, so the window keeps widening until the source recovers.

    start: explicit window start (CLI/e2e override) — still clamped to the
    lookback floor; anything older is historical-backfill territory.
    """
    from services.data import DataService
    from core.models import Source

    source = Source.objects.filter(code=source_code).first()
    if source is None or not source.is_enabled:
        return 0

    now = datetime.now(dt_timezone.utc)
    floor = now - timedelta(hours=FETCH_CURSOR_MAX_LOOKBACK_HOURS)
    if start is None:
        start = source.last_fetched_at or floor
    if start < floor:
        start = floor

    count = DataService(source).refresh_until(start)
    # Stamp with the time *before* the fetch began, so articles published while
    # the fetch was running fall inside the next run's window.
    source.last_fetched_at = now
    source.save(update_fields=['last_fetched_at'])
    logger.info('[fetch] %s: %d new article(s) (since %s)', source.code, count, start)
    return count


def fetch_sources(source_code: str | None = None, start: datetime | None = None) -> int:
    """Fetch one or all enabled sources via the cursor-correct ``fetch_source``
    path (the same one the fetch stage uses). CLI/e2e convenience — per-source
    failures are logged and skipped so one broken feed doesn't stop the sweep.
    Returns the number of newly created articles.
    """
    from core.models import Source

    codes = (
        [source_code]
        if source_code
        else list(Source.objects.filter(is_enabled=True).values_list('code', flat=True))
    )
    total = 0
    for code in codes:
        try:
            total += fetch_source(code, start=start)
        except Exception:
            logger.exception('[fetch] %s failed', code)
    logger.info('[fetch] done — %d new article(s) across %d source(s)', total, len(codes))
    return total


def _fetch_og_image(url: str) -> str | None:
    """Best-effort: fetch og:image meta tag from a URL. Returns None on any failure."""
    try:
        r = requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'}, stream=True)
        chunk = next(r.iter_content(65536), b'')
        r.close()
        text = chunk.decode('utf-8', errors='ignore')
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            text,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            text,
        )
        return m.group(1).strip() if m else None
    except Exception:
        return None


def _scrape_banners(articles: list, eligible: list[bool] | None = None) -> dict[int, str]:
    """og:image for articles that don't already have one, keyed by index into
    *articles* — independent HTTP fetches, run concurrently instead of
    serially (was up to chunk_size * 5s of blocking HTTP in the caller's
    loop; one slow/unresponsive source could stall the entire chunk).
    Shared by analyze_live_articles and annotate_articles.

    ``eligible`` (same length as *articles*, default all True) lets a caller
    exclude some articles up front — annotate_articles skips lite/backfill
    rows, which get no banner scrape at all.
    """
    if eligible is None:
        eligible = [True] * len(articles)
    targets = [
        (i, article.source_url)
        for i, (article, ok) in enumerate(zip(articles, eligible))
        if ok and not article.banner_image_url and article.source_url.startswith('https://')
    ]
    if not targets:
        return {}
    from services.utils import map_concurrent
    ogs = map_concurrent(targets, lambda t: _fetch_og_image(t[1]), max_workers=8)
    return {targets[k][0]: og for k, og in enumerate(ogs) if og}


def analyze_live_articles(ids: list) -> int:
    """Full cloud-LLM analysis for exactly these article ids — the 'analyze'
    stage's executor. Uses services.processing.analyzer.ArticleAnalyzer
    (LLM_ROUTES['analyzer_lite']) for category/sub-category/geo/intensity/EN
    summary — the same fields annotate_articles produces on-prem, so nothing
    downstream (aggregate/tag/route, the API, the UI) needs to know which
    analyzer produced a given article. Sentiment (VADER + FinBERT), Arabic
    translation and importance scoring are unchanged: they're local/rule-based
    regardless of backend, so they run exactly as they do in annotate_articles.

    Only articles services.stages._analyze_ids actually selects (fetched
    within LIVE_ANALYZE_FRESHNESS_HOURS, not backfill-tagged) should reach
    this function in production; it doesn't re-check freshness itself; e.g.
    the analyzer-eval skill deliberately calls it on chosen samples regardless
    of age.

    A *failed* analysis (``error`` set) does NOT stamp ``processed_on`` or
    advance ``stage`` — the article stays at 'fetched', so either this stage
    retries it on its next 3h tick, or (once it ages past the freshness
    window) the on-prem 'annotate' stage picks it up for free instead. A live
    article is therefore never stranded by an LLM outage or a disabled
    LIVE_LLM_ENABLED switch — coverage only ever gets worse in analysis
    *quality*, never in whether an article gets analyzed at all. A
    *successful* analysis stamps ``annotator_version`` with
    ``settings.ANNOTATOR_VERSION``, same as annotate_articles — NOT
    ``refined_by``, which is reserved for the refine stage's judge and stays
    NULL here since this article was never refined.
    """
    from core.models import Article
    from services.processing.analyzer import ArticleAnalyzer
    from services.processing.annotator import add_arabic_translations
    from services.processing import finbert, vader
    from services.scoring import ImportanceScorer
    from services.utils import mark_stage

    articles = list(Article.objects.filter(id__in=_article_uuids(ids)))
    if not articles:
        return 0

    texts = [f'{a.title} {a.content}' for a in articles]
    analyses = ArticleAnalyzer().analyze_batch(texts)

    full_blocks = [a.translations for a in analyses if a.error is None]
    if full_blocks:
        add_arabic_translations(full_blocks)

    finbert_batch = finbert.score_batch(texts)
    sentiment_batch = vader.score_batch(texts)

    importance = ImportanceScorer().score_from_intensity(articles, {
        str(article.id): analysis.intensity
        for article, analysis in zip(articles, analyses) if analysis.error is None
    })

    banner_results = _scrape_banners(articles)

    update_fields = [
        'sentiment', 'finbert_sentiment', 'location', 'latitude', 'longitude',
        'event_intensity', 'category', 'sub_category', 'processed_on', 'stage',
        'importance_score', 'importance_source', 'annotator_version',
        'extra_data', 'translations', 'llm_usage', 'stage_status',
    ]
    to_save = []
    for i, (article, analysis, sent, fin) in enumerate(zip(articles, analyses, sentiment_batch, finbert_batch)):
        article.sentiment = sent
        article.finbert_sentiment = fin
        article.location = ', '.join(filter(None, [analysis.city, analysis.country])) or None
        article.latitude = analysis.latitude
        article.longitude = analysis.longitude
        article.event_intensity = analysis.intensity
        article.category = analysis.category
        article.sub_category = analysis.sub_category
        # Only a successful analysis marks the article processed and advances
        # its stage. A failure leaves it at 'fetched' for the fallback path
        # above (see the function docstring) instead of stamping a degraded
        # result as done.
        if analysis.error is None:
            article.processed_on = timezone.now()
            article.stage = Article.STAGE_ANNOTATED
            article.annotator_version = settings.ANNOTATOR_VERSION
            score = importance.get(str(article.id))
            if score is not None:
                article.importance_score = score
                article.importance_source = 'rules'
        article.extra_data = {**(article.extra_data or {}), 'llm': analysis.llm_data}
        article.translations = analysis.translations
        article.llm_usage = analysis.llm_usage

        # Surface the real outcome so a failed analysis shows up in
        # pipeline_coverage()'s error_sample instead of hiding degraded data.
        mark_stage(article, 'analyze', ok=analysis.error is None, error=analysis.error)

        og = banner_results.get(i)
        if og:
            article.banner_image_url = og
            if 'banner_image_url' not in update_fields:
                update_fields.append('banner_image_url')

        to_save.append(article)
        location = article.location or '?'
        category = '/'.join(filter(None, [article.category, article.sub_category]))
        logger.info(f'[analyze] {article.title[:70]} → {category} @ {location} [{article.stage}]')

    if to_save:
        Article.objects.bulk_update(to_save, update_fields, batch_size=500)

    return len(to_save)


def rehydrate_articles(ids: list, use_wayback: bool = False, max_workers: int = 8) -> int:
    """Rehydrate each article: re-fetch its ``source_url`` through the current
    services/data/bodies.py extractor (trafilatura) and overwrite ``content``
    when a non-empty body comes back — for repairing articles whose stored
    content was extracted by an older/worse extractor (nav/boilerplate bleed,
    thin or empty bodies). Id-driven, like annotate/refine; selection lives
    with the caller.

    Direct HTTP only by default — a plain GET per article link, no crawling —
    which rehydrates ~84% of links at ~0.5s each and is the cheap bulk path.
    ``use_wayback=True`` adds the archive fallback for the dead/JS-only/paywalled
    remainder, but that path is globally throttled (see services/data/wayback.py)
    and turns a hours-long job into a day-long one, so it's opt-in.

    Two rows are settled before any HTTP happens (cheap, no wasted fetch):
      * an article whose stored body is **already good quality**
        (``is_good_quality_body``) is left untouched — no re-fetch;
      * an article from a source **known to always fail** hydration
        (``ALWAYS_FAIL_HYDRATION_SOURCES`` — hard server-side paywalls) whose
        body is still not good is **soft-deleted** (``is_deleted=True`` via the
        same mechanism annotate uses for junk — hidden everywhere, kept as
        training data, no migration), since its body is unreachable and the
        thin stub isn't worth carrying.

    Only ``content`` is touched on the rows that ARE fetched; a link that
    returns nothing is left as-is. Does not annotate or change stage — pair it
    with annotate_articles to re-classify on the improved text. Returns the
    number of articles whose content was replaced.
    """
    from core.models import Article
    from services.data.bodies import (
        ALWAYS_FAIL_HYDRATION_SOURCES, fetch_article_page, fetch_wayback_page,
        is_good_quality_body, is_junk_page_title,
    )
    from services.utils import map_concurrent

    articles = [a for a in Article.objects.filter(id__in=_article_uuids(ids)) if a.source_url]
    if not articles:
        return 0

    # Settle before any HTTP: keep already-good rows as-is; soft-delete rows
    # from always-fail sources whose body can never be improved.
    to_fetch, to_delete = [], []
    for a in articles:
        if is_good_quality_body(a.content):
            continue  # already good — no re-fetch
        if a.source_code in ALWAYS_FAIL_HYDRATION_SOURCES:
            to_delete.append(a)
        else:
            to_fetch.append(a)

    if to_delete:
        now = timezone.now()
        for a in to_delete:
            a.is_deleted = True
            a.processed_on = now
        Article.all_objects.bulk_update(to_delete, ['is_deleted', 'processed_on'], batch_size=500)
        logger.info('[rehydrate] soft-deleted %d always-fail-source row(s) before any fetch', len(to_delete))

    if not to_fetch:
        return 0

    def _refetch(article):
        title, body = fetch_article_page(article.source_url, article.source_code)
        if use_wayback and (not body or is_junk_page_title(title)):
            _wb_title, wb_body = fetch_wayback_page(article.source_url, around=article.published_on)
            if wb_body:
                body = wb_body
        return article, body

    results = map_concurrent(to_fetch, _refetch, max_workers=max_workers, default=(None, None))
    to_save = []
    for article, body in results:
        if article is not None and body:
            article.content = body
            to_save.append(article)
    if to_save:
        Article.objects.bulk_update(to_save, ['content'], batch_size=500)
    logger.info('[rehydrate] refetched %d/%d fetched article(s) (use_wayback=%s)', len(to_save), len(to_fetch), use_wayback)
    return len(to_save)


def annotate_articles(ids: list) -> int:
    """Run the full on-prem NLP annotation on exactly these article ids: the
    NLPAnnotator extracts category/sub-category, a place name (geocoded inline
    to lat/lon via a local geonamescache lookup — there is no separate geocode
    step), intensity, sentiment (VADER + FinBERT) and translations; importance
    is then computed from intensity + source weight + corroboration. Returns
    the number of articles annotated.

    Selection lives with the caller — the ``_annotate_pending`` predicate in
    services/stages.py is the single source of truth for what needs annotating;
    this function is the id-driven executor.

    A *failed* annotation (``llm_error`` set) does NOT stamp ``processed_on`` or
    advance ``stage``: the article stays at 'fetched' so the annotate stage
    retries it, rather than a degraded 'general'/no-location result
    masquerading as done. A *successful* annotation advances stage to
    'annotated' (confident) or 'refine' (queued for the judge — see
    services/processing/refiner.py) and stamps ``annotator_version`` with
    ``settings.ANNOTATOR_VERSION`` (see annotate_deferred_batch_task's
    self-healing skip); either is processed, and an article that resolves no
    location is simply terminal (never aggregates into an event).
    """
    from core.models import Article, ArticleDocument
    from services.data.bodies import is_junk_article
    from services.processing.annotator import ESCALATE_BELOW, NLPAnnotator
    from services.scoring import ImportanceScorer
    from services.utils import mark_stage

    articles = list(Article.objects.filter(id__in=_article_uuids(ids)))

    if not articles:
        return 0

    # Quarantine structural junk (non-article pages, raw-URL/paywall stubs)
    # before spending NLP on it: soft-delete so the default manager hides it
    # everywhere, and stamp processed_on so it drops out of the pending queue.
    # The row is kept (training data), just excluded — nothing hard-deletes.
    junk = [a for a in articles if is_junk_article(a.title, a.source_url)]
    if junk:
        for a in junk:
            a.is_deleted = True
            a.processed_on = timezone.now()
        Article.all_objects.bulk_update(junk, ['is_deleted', 'processed_on'], batch_size=500)
        logger.info('[annotate] soft-deleted %d junk (non-article) row(s)', len(junk))
    articles = [a for a in articles if not a.is_deleted]
    if not articles:
        return 0

    docs = [
        ArticleDocument(
            id=str(article.id),
            title=article.title,
            content=article.content,
            source_code=article.source_code,
            published_on=article.published_on.isoformat(),
        )
        for article in articles
    ]
    # Backfilled historical articles get the lean path: English-only analysis
    # (no Arabic) and no banner scrape. They still geocode + categorize.
    lite_flags = [bool((a.extra_data or {}).get('backfill_day')) for a in articles]

    feature_list = NLPAnnotator().annotate_batch(docs, lite_flags=lite_flags)

    importance = ImportanceScorer().score_from_intensity(articles, {
        str(a.id): f.event_intensity
        for a, f in zip(articles, feature_list) if f.llm_error is None
    })

    # Lite/backfill rows get no banner scrape at all.
    banner_results = _scrape_banners(articles, eligible=[not lite for lite in lite_flags])

    # Base field set written for every article; banner_image_url is added to the
    # bulk_update field list only when at least one article in the chunk got a
    # fresh og:image (articles without one are written unchanged — harmless).
    update_fields = [
        'entities', 'sentiment', 'finbert_sentiment', 'location', 'latitude', 'longitude',
        'event_intensity', 'category', 'sub_category', 'processed_on', 'stage',
        'importance_score', 'importance_source', 'annotator_version',
        'extra_data', 'translations', 'llm_usage', 'stage_status',
    ]
    to_save = []
    for i, (article, features, lite) in enumerate(zip(articles, feature_list, lite_flags)):
        if features.sentiment is not None:
            article.sentiment = features.sentiment
        if features.finbert_sentiment is not None:
            article.finbert_sentiment = features.finbert_sentiment
        article.location = features.location
        article.latitude = features.latitude
        article.longitude = features.longitude
        article.event_intensity = features.event_intensity
        article.category = features.category
        article.sub_category = features.sub_category
        # Only a successful annotation marks the article processed and advances
        # its stage ('annotated' or 'refine'). A failure leaves it at 'fetched'
        # so the annotate stage retries it instead of stamping a degraded
        # result as done (see the function docstring).
        if features.llm_error is None:
            article.processed_on = timezone.now()
            article.stage = Article.STAGE_ANNOTATED if features.confidence >= ESCALATE_BELOW else Article.STAGE_REFINE
            article.annotator_version = settings.ANNOTATOR_VERSION
            score = importance.get(str(article.id))
            if score is not None:
                article.importance_score = score
                article.importance_source = 'rules'
        article.extra_data = {**(article.extra_data or {}), 'llm': features.llm_data}
        article.translations = features.translations
        article.llm_usage = features.llm_usage

        # Surface the real outcome so a failed annotation shows up in
        # pipeline_coverage()'s error_sample instead of hiding degraded data.
        mark_stage(article, 'annotate', ok=features.llm_error is None, error=features.llm_error)

        og = banner_results.get(i)
        if og:
            article.banner_image_url = og
            if 'banner_image_url' not in update_fields:
                update_fields.append('banner_image_url')

        to_save.append(article)
        location = features.location or '?'
        category = '/'.join(filter(None, [features.category, features.sub_category]))
        logger.info(f'[annotate] {article.title[:70]} → {category} @ {location} [{article.stage}]')

    if to_save:
        Article.objects.bulk_update(to_save, update_fields, batch_size=500)

    return len(to_save)


def refine_articles(ids: list) -> int:
    """Second-opinion pass over exactly these article ids: the configured judge
    (services.processing.refiner.LLMRefiner) re-decides category/sub-category —
    and, for LLM providers, geo/intensity/summary — then the article advances
    to stage='refined' with ``refined_on``/``refined_by`` re-stamped (the
    latter to the judging provider's name: 'zeroshot' | 'ollama' | 'cloud').

    Id-driven, like annotate_articles: it does NOT filter by current stage, so
    it can re-refine an already-refined (or even already-annotated) article on
    request — e.g. to re-judge with a different REFINE_PROVIDER, or to redo a
    judgment now considered wrong. The refine *stage*'s own automatic dispatch
    only ever passes ids selected by stages._refine_pending() (stage='refine'
    only); this function is the shared executor for both that scheduled path
    and any manual/admin re-refine.

    A None verdict (judge unavailable/failed) leaves the article at its
    current stage unchanged so a later retry can pick it up again. Returns the
    number of articles refined.
    """
    from core.models import Article
    from services.processing.refiner import LLMRefiner
    from services.utils import mark_stage

    articles = list(Article.objects.filter(id__in=_article_uuids(ids)))
    if not articles:
        return 0

    refiner = LLMRefiner()
    verdicts = refiner.judge([(a.title, a.content or '') for a in articles])

    update_fields = [
        'category', 'sub_category', 'location', 'latitude', 'longitude',
        'event_intensity', 'translations', 'extra_data', 'llm_usage',
        'stage', 'refined_on', 'refined_by', 'annotator_version', 'stage_status',
    ]
    to_save, refined = [], 0
    for article, verdict in zip(articles, verdicts):
        if verdict is None:
            # Leave the article's stage untouched (unset on failure) — a
            # scheduled retry only re-selects it if it was already 'refine';
            # a manual re-refine call is free to try again immediately.
            # Still record the outcome so it's visible in stage_status.
            mark_stage(article, 'refine', ok=False, error=f'judge unavailable ({refiner.provider})')
            to_save.append(article)
            continue

        refiner.apply(article, verdict)
        article.stage = Article.STAGE_REFINED
        article.refined_on = timezone.now()
        article.annotator_version = settings.ANNOTATOR_VERSION
        mark_stage(article, 'refine', ok=True)
        to_save.append(article)
        refined += 1
        logger.info(
            f'[refine/{verdict["provider"]}] {article.title[:70]} → '
            f'{article.category}/{article.sub_category or "-"}'
        )

    if to_save:
        Article.objects.bulk_update(to_save, update_fields, batch_size=500)
    return refined
