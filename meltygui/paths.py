"""Locations shared by the installed framework and its host application."""
from pathlib import Path
import os
import sys

PACKAGE_ROOT = Path(__file__).resolve().parent


def application_root():
    """Nearest project marker above the entry point, or its containing folder."""
    entry = getattr(sys.modules.get('__main__'), '__file__', None)
    start = Path(entry).resolve().parent if entry else Path.cwd()
    for folder in (start, *start.parents):
        if any((folder / marker).exists() for marker in ('.git', 'pyproject.toml', 'setup.py', 'setup.cfg')):
            return folder
    return start


def cache_root():
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'meltygui'
