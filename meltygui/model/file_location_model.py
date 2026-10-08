"""Filesystem and virtual browser locations; recent files use GNOME's XBEL store.

The recent location is a collection, not a directory: no parent, creation
location, or user-defined row order. Its entries retain their real file paths.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit
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

    def __post_init__(self):
        if self.is_remote:
            import posixpath
            uri = urlsplit(self.key)
            if not uri.hostname or uri.password is not None or uri.query or uri.fragment:
                raise ValueError('Invalid SSH file location')
            _ = uri.port  # Validate the optional port.
            path = posixpath.normpath('/' + unquote(uri.path).lstrip('/'))
            object.__setattr__(self, 'key', urlunsplit(('sftp', uri.netloc, quote(path, safe='/'), '', '')))

    @classmethod
    def parse(cls, value):
        return value if isinstance(value, cls) else cls(str(value))

    def __str__(self):
        return self.key

    @property
    def is_remote(self):
        return self.key.startswith('sftp://')

    @property
    def remote_path(self):
        return unquote(urlsplit(self.key).path) or '/'

    @property
    def local_path(self):
        return None if self.is_remote or self.is_recent else Path(self.key)

    @property
    def filesystem_id(self):
        return ('ssh', urlsplit(self.key).netloc) if self.is_remote else ('local',)

    @property
    def _path(self):
        return PurePosixPath(self.remote_path) if self.is_remote else Path(self.key)

    @property
    def name(self):
        return self._path.name

    @property
    def suffix(self):
        return self._path.suffix

    @property
    def stem(self):
        return self._path.stem

    @property
    def parts(self):
        return self._path.parts

    def _with_path(self, path):
        uri = urlsplit(self.key)
        return FileLocation(urlunsplit((uri.scheme, uri.netloc, quote(str(path), safe='/'), '', '')))

    def __truediv__(self, value):
        return self.joinpath(value)

    def joinpath(self, *parts):
        if not self.is_remote:
            return file_path(self.key).joinpath(*parts)
        return self._with_path(self._path.joinpath(*map(str, parts)))

    def with_name(self, name):
        return self._with_path(self._path.with_name(name))

    def relative_to(self, other):
        other = FileLocation.parse(other)
        if self.filesystem_id != other.filesystem_id:
            raise ValueError('Locations belong to different filesystems')
        return self._path.relative_to(other._path)

    def is_relative_to(self, other):
        try:
            self.relative_to(other)
            return True
        except ValueError:
            return False

    is_within = is_relative_to

    @property
    def parents(self):
        return tuple(self._with_path(p) for p in self._path.parents) if self.is_remote else self._path.parents

    def expanduser(self):
        return self if self.is_remote else self.local_path.expanduser()

    def resolve(self):
        # Remote canonicalization is a worker operation, never an implicit stat.
        return self if self.is_remote else self.local_path.resolve()

    absolute = resolve

    def is_absolute(self):
        return self.is_remote or self.local_path.is_absolute()

    def as_posix(self):
        return self.key

    def stat(self):
        from meltygui.model.ssh_file_model import cached_stat
        return cached_stat(self) if self.is_remote else self.local_path.stat()

    def exists(self):
        try:
            self.stat()
            return True
        except FileNotFoundError:
            return False

    def is_file(self):
        import stat
        try:
            return stat.S_ISREG(self.stat().st_mode)
        except FileNotFoundError:
            return False

    def is_dir(self):
        import stat
        try:
            return stat.S_ISDIR(self.stat().st_mode)
        except FileNotFoundError:
            return False

    def read_bytes(self):
        from meltygui.model.ssh_file_model import read_bytes
        return read_bytes(self) if self.is_remote else self.local_path.read_bytes()

    def read_text(self, encoding='utf-8', errors=None):
        return self.read_bytes().decode(encoding, errors or 'strict')

    @property
    def is_recent(self):
        return self.key == RECENT_URI

    @property
    def directory(self):
        return None if self.is_recent else file_path(self.key).expanduser()

    @property
    def parent(self):
        if self.is_remote:
            return self._with_path(self._path.parent)
        directory = self.directory
        return str(directory.parent) if directory is not None and directory.parent != directory else None

    @property
    def watch_directory(self):
        return None if self.is_remote else recent_store_path().parent if self.is_recent else self.directory


def file_path(value):
    """Real local Paths retain their behavior; SSH identities never become OS paths."""
    if isinstance(value, FileLocation):
        return value if value.is_remote else Path(value.key)
    return FileLocation(str(value)) if str(value).startswith('sftp://') else Path(value)


def is_remote(value):
    return str(value).startswith('sftp://')


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
