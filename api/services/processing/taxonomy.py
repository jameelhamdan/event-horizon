"""The two-level article taxonomy — single source of truth.

Consumed by every classification path: the LLM prompt client (analyzer.py)
builds its schema from CATEGORIES/SUB_CATEGORIES, the annotate stage
(annotator.py) classifies against PROTOTYPES (embedded exemplar sentences per
sub-category) and seeds its rule-based intensity from PRIORS, and the refine
stage (refiner.py) validates judge verdicts against the same sets. Tuning
classification is a data change in this file, not a code change.
"""

CATEGORIES = {'conflict', 'disaster', 'economic', 'political', 'health', 'general'}

SUB_CATEGORIES: dict[str, set[str]] = {
    'conflict':  {'war', 'airstrike', 'insurgency', 'terrorism', 'border-clash', 'other'},
    'disaster':  {'earthquake', 'flood', 'storm', 'wildfire', 'industrial-accident', 'infrastructure', 'other'},
    'economic':  {'monetary-policy', 'energy', 'trade', 'tariffs', 'labor', 'markets', 'sanctions', 'other'},
    'political': {'election', 'legislation', 'diplomacy', 'leadership-change', 'protest-policy', 'other'},
    'health':    {'outbreak', 'pandemic', 'healthcare-system', 'other'},
    'general':   {'other'},
}

# Exemplar sentences per (category, sub_category) — embedded once with the
# shared multilingual sentence-transformer; articles classify to the nearest
# prototype by cosine similarity (services.processing.annotator), taking
# the best match among a pair's prototypes. Wording matters more than length:
# pack in the vocabulary headlines actually use. A pair may carry several
# prototypes when it spans distinct vocabularies — general/other needs many,
# or one lone sentence loses every tech/sports/culture headline to the eight
# economic prototypes (measured: that single bias cost ~25pts of accuracy).
PROTOTYPES: dict[tuple[str, str], list[str]] = {
    ('conflict', 'war'): [
        'War between countries or armies: invasion, offensive, shelling, troops advance, front line fighting, ceasefire broken'],
    ('conflict', 'airstrike'): [
        'Military airstrike: bombing raid, missile strike, drone attack hits a target or building, strike kills civilians'],
    ('conflict', 'insurgency'): [
        'Insurgent or rebel attack: militants ambush soldiers, guerrilla fighters clash with the army, armed group seizes territory'],
    ('conflict', 'terrorism'): [
        'Terrorist attack: suicide bombing, mass shooting of civilians, hostage taking, extremists claim responsibility'],
    ('conflict', 'border-clash'): [
        'Border clash: skirmish between troops of neighbouring countries along a disputed border or frontier'],
    ('conflict', 'other'): [
        'Armed violence, military conflict, gunmen kill people',
        'Civilians killed by military forces: people shot dead while waiting for food or aid, troops open fire on a crowd, deadly assault on unarmed civilians, mass casualties in a conflict zone'],
    ('disaster', 'earthquake'): [
        'Earthquake: seismic tremor shakes region, magnitude reported, buildings collapse, aftershocks'],
    ('disaster', 'flood'): [
        'Flooding: heavy rains submerge towns, rivers overflow banks, flash floods, residents evacuated'],
    ('disaster', 'storm'): [
        'Storm: hurricane, typhoon, cyclone or tornado makes landfall, destructive winds, heavy snowfall',
        'Typhoon or cyclone approaches or hits coastal region, storm surge, residents evacuated ahead of landfall'],
    ('disaster', 'wildfire'): [
        'Wildfire: forest and bush fires spread, firefighters battle blaze, hectares burned, evacuations ordered'],
    ('disaster', 'industrial-accident'): [
        'Industrial or transport accident: factory explosion, mine collapse, chemical spill, building fire, plane or train crash',
        'Refinery or plant explosion: oil facility blast, storage tank fire, workers killed or injured in industrial blaze',
        'Building fire: apartment block or warehouse catches fire, blaze kills residents, firefighters respond to structure fire',
        'Road or traffic accident: truck or bus crash, multi-vehicle collision, highway accident kills or injures passengers'],
    ('disaster', 'infrastructure'): [
        'Power outage and infrastructure failure: electricity blackout, power grid collapse, mass power cut leaves a region without electricity, water or telecommunications outage'],
    ('disaster', 'other'): [
        'Natural disaster, deadly accident, landslide, drought, extreme weather',
        'Accident causes multiple deaths or injuries: collapse, crash, explosion, or fire with casualties, emergency responders at scene'],
    ('economic', 'monetary-policy'): [
        'Central bank monetary policy: interest rate decision, inflation figures, quantitative easing'],
    ('economic', 'energy'): [
        'Energy: oil and gas prices, OPEC production cuts, pipelines, fuel supply, electricity grid',
        'Energy security commentary: opinion essay or analysis on energy strategy, energy alliances, resource dependence'],
    ('economic', 'trade'): [
        'International trade: exports, imports, trade deal negotiated, trade deficit, supply chains'],
    ('economic', 'tariffs'): [
        'Tariffs: import duties imposed, trade war measures, customs levies on foreign goods'],
    ('economic', 'labor'): [
        'Labor and employment: workers strike, unemployment figures, wage negotiations, mass layoffs'],
    ('economic', 'markets'): [
        'Financial markets: stock index rises or falls, shares, bonds, currency exchange rates, company earnings'],
    ('economic', 'sanctions'): [
        'Economic sanctions imposed on a country: asset freezes, export bans, blacklisted companies'],
    ('economic', 'other'): [
        'Business and economy: companies, GDP growth, budget, investment, banking',
        'Fiscal policy and stimulus: government spending package, tax cuts, budget stimulus, economic relief measures debated to boost the economy'],
    ('political', 'election'): [
        'Election: voters go to the polls, presidential or parliamentary vote, campaign rallies, election results'],
    ('political', 'legislation'): [
        'Legislation: parliament passes law, bill debated, new regulation adopted, court ruling on statute'],
    ('political', 'diplomacy'): [
        'Diplomacy: summit between leaders, bilateral talks, treaty signed, ambassadors, foreign minister meets counterpart'],
    ('political', 'leadership-change'): [
        'Leadership change: president resigns, impeachment, coup, cabinet reshuffle, new prime minister sworn in'],
    ('political', 'protest-policy'): [
        'Protests and civil unrest: demonstrators march against government policy, riots, police crackdown on protesters',
        'State crackdown on dissent: government imposes a national security law, suppresses opposition, curtails autonomy or civil liberties, jails activists or journalists'],
    ('political', 'other'): [
        'Government and politics: minister statement, policy announcement, political party dispute',
        'Political commentary and analysis: opinion essay critiquing a domestic political trend, law, or policy, punditry'],
    ('health', 'outbreak'): [
        'Disease outbreak: virus spreads, infections rise, new cases confirmed, quarantine measures'],
    ('health', 'pandemic'): [
        'Pandemic: global health emergency, worldwide disease spread, vaccination campaign, WHO declaration'],
    ('health', 'healthcare-system'): [
        'Healthcare system: hospitals overwhelmed, doctors and nurses, drug approval, public health policy'],
    ('health', 'other'): [
        'Health and medicine: medical research, disease study, nutrition, mental health'],
    ('general', 'other'): [
        'Sports: match result, tournament, championship, team wins, athlete, football, tennis, olympics',
        'Consumer technology: new gadget, smartphone, tablet, app, product launch, software update, video game release',
        'Technology company news: startup, AI tool released, social media platform feature, tech firm announcement',
        'Culture and entertainment: film, music, celebrity, television series, arts, festival, viral trend, awards',
        'Ordinary crime and justice: arrest, police investigation, court trial, fraud, smuggling, drug bust, lawsuit settlement',
        'Human interest, science and lifestyle: profile of a person, research discovery, space, wildlife, education, travel, weather forecast',
    ],
}

# Baseline event_intensity per (category, sub_category) — rate_intensity()
# (annotator.py) starts here and adjusts with lexical severity cues (casualty
# counts, escalation vocabulary). Aligned with the 0-1 rubric in the LLM
# prompt: routine ≈ 0.1-0.2, notable ≈ 0.3-0.5, major ≈ 0.6-0.8.
PRIORS: dict[tuple[str, str], float] = {
    ('conflict', 'war'): 0.7,
    ('conflict', 'airstrike'): 0.6,
    ('conflict', 'insurgency'): 0.55,
    ('conflict', 'terrorism'): 0.65,
    ('conflict', 'border-clash'): 0.55,
    ('conflict', 'other'): 0.5,
    ('disaster', 'earthquake'): 0.55,
    ('disaster', 'flood'): 0.5,
    ('disaster', 'storm'): 0.5,
    ('disaster', 'wildfire'): 0.45,
    ('disaster', 'industrial-accident'): 0.45,
    ('disaster', 'infrastructure'): 0.4,
    ('disaster', 'other'): 0.4,
    ('economic', 'monetary-policy'): 0.45,
    ('economic', 'energy'): 0.35,
    ('economic', 'trade'): 0.35,
    ('economic', 'tariffs'): 0.4,
    ('economic', 'labor'): 0.3,
    ('economic', 'markets'): 0.35,
    ('economic', 'sanctions'): 0.45,
    ('economic', 'other'): 0.3,
    ('political', 'election'): 0.4,
    ('political', 'legislation'): 0.3,
    ('political', 'diplomacy'): 0.35,
    ('political', 'leadership-change'): 0.45,
    ('political', 'protest-policy'): 0.4,
    ('political', 'other'): 0.3,
    ('health', 'outbreak'): 0.5,
    ('health', 'pandemic'): 0.7,
    ('health', 'healthcare-system'): 0.3,
    ('health', 'other'): 0.25,
    ('general', 'other'): 0.15,
}
