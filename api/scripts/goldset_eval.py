"""Hydrated gold-set accuracy harness — fast, deterministic fix→measure loop.

Hydrates a fixed set of real article URLs ONCE (via services.data.bodies, same
as production) and caches the body to disk; re-runs load the cache and re-run
annotate+refine, so iterating on annotator/refiner/taxonomy is a sub-minute
loop with no re-fetching. Prints category accuracy vs the hand-assigned gold
label + every miss. Run from api/:  .venv/bin/python -m scripts.goldset_eval
"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.base')
import django; django.setup()

CACHE = Path('/private/tmp/claude-501/-Users-jameel-projects-happinga-meter/bb5694eb-ae27-4a6c-b59f-39fb0e839ec7/scratchpad/goldset_cache.json')

# (url, gold_category). Balanced across categories; includes every known miss
# (marked # MISS) plus correct cases as regression guards.
GOLD = [
    # conflict (real — regression guards)
    ('https://foreignpolicy.com/2025/03/31/sudan-civil-war-humanitarian-aid-crisis-trump-congress/', 'conflict'),
    ('https://www.theguardian.com/world/2025/apr/01/china-launches-surprise-military-drills-around-taiwan', 'conflict'),
    ('https://www.straitstimes.com/world/middle-east/us-says-it-killed-top-houthi-missile-expert-but-questions-linger', 'conflict'),
    ('https://www.aljazeera.com/news/2024/11/1/five-children-among-seven-killed-in-attack-on-pakistan-polio-vaccine-drive', 'conflict'),
    ('https://www.timesofisrael.com/israeli-commandos-said-to-nab-top-hezbollah-naval-commander-in-north-lebanon-raid/', 'conflict'),
    ('https://english.alarabiya.net/News/world/2024/07/01/russia-says-captured-two-more-east-ukrainian-villages', 'conflict'),
    ('https://www.npr.org/2025/06/30/1255015983/russias-largest-bombardment-of-ukraine', 'conflict'),
    # conflict MISSes
    ('https://www.theguardian.com/world/2025/apr/01/myanmar-earthquake-junta-accused-blocking-aid', 'disaster'),   # MISS: was conflict
    ('https://foreignpolicy.com/2023/09/30/mustafa-nayyem-ukraine-war-russia-reconstruction-refugees-economy-marshall-plan-corruption/', 'political'),  # MISS: was conflict (metaphor 'Fights')
    ('https://foreignpolicy.com/2025/03/31/china-russia-wagner-security-stability-africa/', 'political'),  # MISS: was conflict/border-clash
    # disaster (real — guards)
    ('https://www.theguardian.com/world/2024/oct/31/why-were-the-floods-in-spain-so-bad-a-visual-guide', 'disaster'),
    ('https://www.bbc.co.uk/news/live/cgk1m7g73ydt', 'disaster'),
    ('https://www.cnn.com/2024/07/01/weather/hurricane-beryl-caribbean-landfall-monday/index.html', 'disaster'),
    ('https://abcnews.go.com/amp/International/wireStory/8-killed-roof-collapse-serbian-railway-station-115393448', 'disaster'),
    ('https://apnews.com/article/south-korea-car-accident-seoul-1357f9065602aa9674b2451fde949e90', 'disaster'),
    ('https://www.theguardian.com/world/2025/apr/01/myanmar-earthquake-death-toll-unmarked-graves', 'disaster'),
    # disaster MISSes
    ('https://www.straitstimes.com/singapore/singapore-corals-showing-signs-of-recovery-amid-global-coral-bleaching-event', 'general'),  # MISS: was disaster
    ('https://apnews.com/article/mexico-cult-santa-muerte-leader-killed-criminals-violence-64cf13cfba32c7321ebe7303413011a3', 'general'),  # MISS: was disaster
    ('https://kyivindependent.com/important-facility-hit-ukraine-attacks-russian-oil-refinery-in-saratov-oblast/', 'conflict'),  # MISS: was disaster/industrial-accident
    # economic (guards)
    ('https://foreignpolicy.com/2025/03/31/trump-tariffs-national-security-russia-canada-mexico/', 'economic'),
    ('https://www.forbes.com/advisor/mortgages/refinance/mortgage-refinance-rates-03-31-25/', 'economic'),
    ('https://www.straitstimes.com/business/economy/indonesias-feb-trade-surplus-smallest-in-9-months', 'economic'),
    ('https://www.cnbc.com/2024/11/02/china-eases-rules-for-foreign-investment-in-listed-companies.html', 'economic'),
    # political (guards)
    ('https://www.occrp.org/en/news/le-pen-sentenced-in-eu-funds-scandal-her-2027-presidential-bid-blocked', 'political'),
    ('https://www.wired.com/story/judge-approves-doge-usip-office-building/', 'political'),
    ('https://foreignpolicy.com/2025/03/31/republicans-department-education-trump-dismantle-reagan/', 'political'),
    ('https://www.reuters.com/world/africa/botswanas-ruling-party-loses-its-majority-this-weeks-election-mmegi-newspaper-2024-11-01/', 'political'),
    ('https://apnews.com/article/kenya-deputy-president-impeachment-f2df308350568161087e419320c2d08c', 'political'),
    ('https://www.cnn.com/politics/live-news/trump-immunity-supreme-court-decision-07-01-24/index.html', 'political'),
    ('https://www.straitstimes.com/world/europe/greenland-strengthens-danish-ties-as-it-eyes-independence', 'political'),
    # political MISSes
    ('https://www.rt.com/news/615116-doj-luigi-death-penalty/', 'general'),  # MISS: was political (crime)
    ('https://www.engadget.com/hitting-the-books-democracy-in-a-hotter-time-david-orr-mit-press-143034391.html', 'general'),  # MISS: was political (book)
    ('https://www.theguardian.com/world/2024/nov/01/shootout-several-hundred-people--france-poitiers-retailleau', 'general'),  # MISS: was conflict (brawl)
    # health (guards)
    ('https://www.propublica.org/article/nevaeh-crain-death-texas-abortion-ban-emtala', 'health'),
    ('https://www.propublica.org/article/miscarriage-blood-transfusion-texas-abortion-ban', 'health'),
    ('https://www.theguardian.com/world/article/2024/jul/01/life-at-the-heart-of-japans-solitary-deaths-epidemic-i-would-be-lying-if-i-said-i-wasnt-worried', 'health'),
    # health MISS
    ('https://www.forbes.com/advisor/business/what-is-human-resources/', 'general'),  # MISS: was health
    # general (guards — sports/tech/culture/crime)
    ('https://www.straitstimes.com/sport/tennis/carlos-alcaraz-stunned-by-18th-ranked-ugo-humbert-at-paris-masters', 'general'),
    ('https://www.engadget.com/social-media/arkansas-social-media-age-verification-law-blocked-by-federal-judge-194614568.html', 'general'),
    ('https://www.wired.com/story/nomad-april-sale-2025/', 'general'),
    ('https://apnews.com/article/mexico-chiapas-20-killed-drug-cartels-0ca11d7b79b266a6520fd9f98af390ba', 'general'),
    ('https://www.gunviolencearchive.org/incident/2955139', 'general'),
    ('https://www.wired.com/story/airbnbopoly-reid-hoffman-lina-khan-ftc-antitrust/', 'general'),
    # general MISSes (should be conflict/political)
    ('https://apnews.com/article/israel-palestinians-hamas-war-news-07-01-2024-453808f05ef8b98eb1a6b9814441224a', 'conflict'),  # Israel evacuation Khan Younis
    ('https://www.syriahr.com/en/337635/', 'political'),  # anti-Turkish riots
    ('https://www.wired.com/story/institute-museum-library-services-layoffs/', 'political'),  # DOGE libraries
]


def hydrate():
    from services.data.bodies import fetch_article_page, fetch_wayback_page, is_junk_page_title
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    changed = False
    for url, _ in GOLD:
        if url in cache:
            continue
        title, body = fetch_article_page(url)
        if not body or is_junk_page_title(title):
            wb_t, wb_b = fetch_wayback_page(url)
            if wb_b:
                title, body = wb_t, wb_b
        cache[url] = {'title': title or '', 'content': body or ''}
        changed = True
        print(f'  hydrated ({len(body or "")}c): {url[:70]}')
    if changed:
        CACHE.write_text(json.dumps(cache))
    return cache


def main():
    from core.models import ArticleDocument
    from services.processing.annotator import ESCALATE_BELOW, NLPAnnotator
    from services.processing.refiner import LLMRefiner
    cache = hydrate()

    docs, gold, hydrated_urls = [], [], []
    for url, g in GOLD:
        c = cache.get(url, {})
        if not c.get('content'):
            continue
        docs.append(ArticleDocument(id=str(len(docs)), title=c['title'], content=c['content'], source_code='gold', published_on=''))
        gold.append(g); hydrated_urls.append(url)

    feats = NLPAnnotator().annotate_batch(docs, lite_flags=True)
    cats = [f.category for f in feats]
    flagged = [i for i, f in enumerate(feats) if f.llm_error is None and f.confidence < ESCALATE_BELOW]
    verdicts = LLMRefiner().judge([(docs[i].title, docs[i].content) for i in flagged])
    for i, v in zip(flagged, verdicts):
        if v: cats[i] = v['category']

    hits = 0; misses = []
    for url, g, cat, doc in zip(hydrated_urls, gold, cats, docs):
        if cat == g: hits += 1
        else: misses.append((cat, g, doc.title[:50]))
    print(f'\nHydrated gold set: {hits}/{len(gold)} = {hits/len(gold):.0%} category-correct')
    for cat, g, t in misses:
        print(f'  MISS got={cat:<10} gold={g:<10} {t}')


if __name__ == '__main__':
    main()
