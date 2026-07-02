import sys
import subprocess
from pathlib import Path

from .celery import app as celery_app

__all__ = ('celery_app',)

__version__ = 'NONE'

__build__ = ''


VERSION_FILE = 'version.txt'


try:
    with open(VERSION_FILE) as f:
        __version__ = f.read().strip()
except Exception as e:
    raise RuntimeError('Unable to find version string in %s.' % (VERSION_FILE,)) from e

try:
    commit_id = subprocess.check_output(
        ["git", "describe", "--always"],
        cwd=Path(__file__).resolve().parent.parent,
    ).decode('utf-8').strip()
    __build__ = f'backend-{__version__}-{commit_id}'
except Exception:
    __build__ = f'backend-{__version__}'

print('RUNNING VERSION: %s on %s' % (__build__, sys.platform))

