from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'
    label = 'core'

    def ready(self):
        from django.db.models.signals import post_migrate
        post_migrate.connect(_bootstrap_on_migrate, sender=self)


def _bootstrap_on_migrate(sender, **kwargs):
    """Run bootstrap_initial_data_task once after migrate on a fresh deployment.

    The task is idempotent (cache flag + PriceBar-presence check), so calling it
    on every migrate is safe — it exits immediately if data is already present.
    Enqueued via RQ when TASK_QUEUE_ENABLED=True; runs synchronously in dev.
    Any error is swallowed so a missing Redis or import failure never blocks migrate.
    """
    try:
        from services.queue import enqueue
        from services.tasks import bootstrap_initial_data_task
        enqueue(bootstrap_initial_data_task, queue='default')
    except Exception:
        pass
