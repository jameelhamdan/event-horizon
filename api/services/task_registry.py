"""Task name → callable / default queue — the single source of truth both
``manage.py run_task`` (the cron entry point) and the admin dashboard's
one-click triggers use, so a task can't drift onto a different queue in one
caller and not the other. Pipeline stages pick their own queue in
services/stages.py — pipeline_tick_task and dispatch_stage_task are light
dispatchers and correctly fall through to 'default' here.

HEAVY_TASKS: anything that loads a transformer/embedding model (MiniLM,
FinBERT, NER, MarianMT, mDeBERTa) must be listed here — worker-heavy is the
only queue sized for that memory footprint. BULK_TASKS: long one-shot jobs or
pure dispatchers that fan work out to heavy — never both in the same task.
"""
HEAVY_TASKS = frozenset({
    'refresh_topics_task',
    'discover_topics_task',
    'generate_newsletter_task',
    'aggregate_full_task',
})
BULK_TASKS = frozenset({
    'backfill_prices_task',
    'train_forecast_model_task',
    'run_forecast_task',
    'annotate_deferred_articles_task',
})


def queue_for_task(name: str) -> str:
    if name in BULK_TASKS:
        return 'bulk'
    if name in HEAVY_TASKS:
        return 'heavy'
    return 'default'


def resolve_task(name: str):
    """Find a *_task function in services.tasks or newsletter.tasks — the
    same lookup manage.py run_task and the dashboard's one-click triggers both
    use, so an unknown/renamed task fails identically everywhere."""
    from newsletter import tasks as newsletter_tasks
    from services import tasks as service_tasks
    for module in (service_tasks, newsletter_tasks):
        func = getattr(module, name, None)
        if callable(func):
            return func
    return None
