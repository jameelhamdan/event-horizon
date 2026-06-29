"""Parse ``api/crontab`` for the admin operations dashboard."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from django.conf import settings

_RUN_TASK_RE = re.compile(r'run_task\s+(\w+)')


def crontab_path() -> str:
    return os.path.join(settings.BASE_DIR, 'crontab')


def parse_entries() -> list[dict]:
    """Return ``{task, cron, command}`` dicts for each active crontab line."""
    path = crontab_path()
    if not os.path.isfile(path):
        return []
    entries: list[dict] = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            cron = ' '.join(parts[:5])
            cmd = parts[5]
            m = _RUN_TASK_RE.search(cmd)
            task = m.group(1) if m else cmd
            entries.append({'task': task, 'cron': cron, 'command': cmd})
    return entries


def upcoming_runs(limit: int = 40) -> list[dict]:
    """Next scheduled time per crontab entry (sorted by soonest)."""
    try:
        from croniter import croniter
    except ImportError:
        return [
            {'task': e['task'], 'when': None, 'cron': e['cron']}
            for e in parse_entries()[:limit]
        ]

    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for entry in parse_entries():
        try:
            when = croniter(entry['cron'], now).get_next(datetime)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            out.append({'task': entry['task'], 'when': when})
        except Exception:  # noqa: BLE001 — bad cron expression
            out.append({'task': entry['task'], 'when': None})
    out.sort(key=lambda x: x['when'] or datetime.max.replace(tzinfo=timezone.utc))
    return out[:limit]
