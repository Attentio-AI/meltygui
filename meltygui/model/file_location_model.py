"""Filesystem and virtual browser locations; recent files use GNOME's XBEL store.

The recent location is a collection, not a directory: no parent, creation
location, or user-defined row order. Its entries retain their real file paths.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
import os
import xml.etree.ElementTree as ET

RECENT_URI = 'recent:///'
BOOKMARK_NS = 'http://www.freedesktop.org/standards/desktop-bookmarks'
MIME_NS = 'http://www.freedesktop.org/standards/shared-mime-info'


def recent_store_path():
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'recently-used.xbel'


@dataclass(frozen=True)
class FileLocation:
    key: str

    @property
    def is_recent(self):
        return self.key == RECENT_URI

    @property
    def directory(self):
        return None if self.is_recent else Path(self.key).expanduser()

    @property
    def parent(self):
        directory = self.directory
        return str(directory.parent) if directory is not None and directory.parent != directory else None

    @property
    def watch_directory(self):
        return recent_store_path().parent if self.is_recent else self.directory


def read_recent_files(filename):
    """Existing public local files, newest bookmark modification first (GVfs).

    Reading is deliberately separate from rendering and does not write history.
    Missing history is empty; malformed/unreadable history is reported by caller.
    """
    try:
        root = ET.parse(filename).getroot()
    except FileNotFoundError:
        return []
    entries = {}
    for bookmark in root.findall('bookmark'):
        uri = urlsplit(bookmark.get('href', ''))
        if uri.scheme != 'file' or uri.netloc not in ('', 'localhost'):
            continue
        if bookmark.find(f'.//{{{BOOKMARK_NS}}}private') is not None:
            continue
        mime = bookmark.find(f'.//{{{MIME_NS}}}mime-type')
        if mime is not None and mime.get('type') == 'inode/directory':
            continue
        path = Path(unquote(uri.path))
        if not path.is_absolute() or not path.is_file():
            continue
        try:
            stamp = datetime.fromisoformat(bookmark.get('modified', '').replace('Z', '+00:00')).timestamp()
        except ValueError:
            stamp = 0
        entries[path] = max(stamp, entries.get(path, 0))
    return [(path, False) for path in sorted(entries, key=lambda path: (-entries[path], str(path)))]


class RecentFiles:
    """One view's asynchronous snapshot; consume before drawing, dispatch after.

    A directory watcher invalidates the view on history changes. Metadata stat
    avoids reparsing unchanged history; a completed worker never mutates UI state.
    """
    def __init__(self):
        self.rows = []
        self.error = None
        self.loaded = False
        self._signature = None
        self._future = None
        self._closed = False

    def consume(self):
        if self._future is not None and self._future.done():
            self.rows, self.error = self._future.result()
            self._future = None
            self.loaded = True

    def dispatch(self, notify, submit):
        if self._closed or self._future is not None:
            return
        filename = recent_store_path()
        try:
            stat = filename.stat()
            signature = (str(filename), stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            signature = (str(filename), None)
        if signature == self._signature:
            return
        self._signature = signature
        def load():
            try:
                return read_recent_files(filename), None
            except (OSError, ET.ParseError, ValueError) as error:
                return [], f'Could not load recent files: {error}'
        self._future = submit(load)
        self._future.add_done_callback(lambda future: None if self._closed else notify())

    def close(self):
        self._closed = True
        if self._future is not None:
            self._future.cancel()
