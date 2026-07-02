"""Celery app — broker/backend config comes from Django settings (CELERY_* namespace).

Workers are launched as ``celery -A app worker -Q <queue> ...`` (see
docker-compose.yml). ``on_after_finalize`` fires once, at worker startup
(accessing ``app.tasks`` auto-finalizes), before any task is consumed —
importing services.tasks/newsletter.tasks here registers every
``@shared_task``. Critically, it also imports ``services.queue``: that
module's ``task_success``/``task_failure`` signal handlers (which finalize
TaskRun rows) must be registered *before* the first task completes in this
process. Many leaf tasks (no fan-out) never import services.queue themselves,
so relying on a lazy in-task import would leave TaskRun rows stuck 'running'
for those tasks in a fresh worker process.
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.base')

app = Celery('app')
app.config_from_object('django.conf:settings', namespace='CELERY')


@app.on_after_finalize.connect
def _register_tasks(sender, **kwargs):
    import newsletter.tasks  # noqa: F401
    import services.tasks  # noqa: F401
    import services.queue  # noqa: F401 — registers TaskRun task_success/task_failure signal handlers
