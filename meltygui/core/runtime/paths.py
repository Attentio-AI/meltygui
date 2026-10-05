"""Locations shared by the installed framework and its host application.

iOS keeps editable projects in Documents, which the native host exposes in
Files, and private application data in Library. Resolve these locations when
needed: an app's sandbox container can move between launches. Desktop locations
retain their XDG defaults, including on macOS.

These helpers return paths without creating directories. The owner of a file
creates its parent when it writes.
"""
from pathlib import Path
import os
import sys
import tempfile

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def application_root():
    """Nearest project marker above the entry point, or its containing folder."""
    entry = getattr(sys.modules.get('__main__'), '__file__', None)
    start = Path(entry).resolve().parent if entry else Path.cwd()
    for folder in (start, *start.parents):
        if any((folder / marker).exists() for marker in ('.git', 'pyproject.toml', 'setup.py', 'setup.cfg')):
            return folder
    return start


def _app_directory(base, app_id):
    """An application id is a directory name, never a relative/absolute path."""
    if not isinstance(app_id, str):
        raise TypeError('app_id must be a string')
    if not app_id or app_id in ('.', '..') or any(char in app_id for char in '/\\:\0'):
        raise ValueError('app_id must be a single directory name')
    return Path(base) / app_id


def config_root(app_id='meltygui'):
    """Private settings: iOS Application Support, or desktop XDG config."""
    base = (Path.home() / 'Library' / 'Application Support' if sys.platform == 'ios'
            else Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'))
    return _app_directory(base, app_id)


def state_root(app_id='meltygui'):
    """Private saved sessions: iOS Application Support, or desktop XDG state."""
    base = (Path.home() / 'Library' / 'Application Support' if sys.platform == 'ios'
            else Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state'))
    return _app_directory(base, app_id)


def data_root(app_id='meltygui'):
    """Private durable data: iOS Application Support, or desktop XDG data."""
    base = (Path.home() / 'Library' / 'Application Support' if sys.platform == 'ios'
            else Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share'))
    return _app_directory(base, app_id)


def cache_root(app_id='meltygui'):
    """Disposable data: iOS Library/Caches, or desktop XDG cache."""
    base = (Path.home() / 'Library' / 'Caches' if sys.platform == 'ios'
            else Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache'))
    return _app_directory(base, app_id)


def documents_root():
    """The app's Files-visible Documents on iOS, or ~/Documents on desktop."""
    return Path.home() / 'Documents'


def workspace_root():
    """Default editable workspace, separate from installed/bundled source.

    The iOS host creates Documents/Projects and exposes Documents using native
    file-sharing settings. Python accesses those files normally; there is no
    virtual filesystem. Desktop hosts keep their entry-point project root.
    """
    return documents_root() / 'Projects' if sys.platform == 'ios' else application_root()


def default_file_directory():
    """Initial location for file/project pickers, retaining desktop home."""
    return workspace_root() if sys.platform == 'ios' else Path.home()


def debug_log_path(name):
    """Where a diagnostics trail (`tail -f` while reproducing) is written: the
    system temp folder, /tmp on Linux and %TEMP% on Windows."""
    return os.path.join(tempfile.gettempdir(), name)
