"""rqworker-pool with heavy ML models preloaded into the pool parent process.

Identical to django-rq's ``rqworker-pool`` command in every way (same args and
flags — it subclasses it), except it warms the FinBERT + sentence-transformer
caches *before* the pool forks its workers. Because ``WorkerPool`` forks from
this process, every worker — and every per-job work-horse it forks — inherits
the already-loaded weights via copy-on-write, so the model loads once at startup
instead of on every task.

Usage (drop-in replacement for rqworker-pool):
    python manage.py worker heavy --num-workers 2
"""
import importlib

# django-rq ships the command as ``rqworker-pool.py`` (hyphen) — not a valid
# identifier, so import the module by string and subclass its Command.
_pool_cmd = importlib.import_module("django_rq.management.commands.rqworker-pool")


class Command(_pool_cmd.Command):
    help = "rqworker-pool that preloads ML models into the parent (COW-shared to job horses)."

    def handle(self, *args, **options):
        from init_models import preload_into_memory

        preload_into_memory()
        super().handle(*args, **options)
