"""File-based symbol analysis in the running interpreter.

Applications may provide another context (for example an isolated interpreter).
The default has no project settings, environment selection, or package installs.
"""
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path
from meltygui.core.runtime.extensions import get


class SourceContext:
    def __init__(self, root):
        self.root = str(Path(root).expanduser().resolve())
        self.source_paths = (self.root,)
        if (Path(self.root) / 'src').is_dir():
            self.source_paths += (str(Path(self.root) / 'src'),)
        self.import_paths = tuple(dict.fromkeys((*self.source_paths, *sys.path)))
        self.environment = None

    @property
    def key(self):
        return self.root, self.import_paths, self.environment

    def refresh(self):
        return self

    def contains(self, path):
        return str(path) == self.root or str(path).startswith(self.root + os.sep)

    def owns(self, path, *, canonical_file=False):
        from meltygui.text_index import _SKIP_DIRS
        path = os.fspath(path)
        return self.contains(path) and not any(
            part in _SKIP_DIRS for part in path[len(self.root):].split(os.sep)[1:-1]) and owning_root(
                path, canonical_file=canonical_file) == self.root

    def resolves(self, path, *, canonical_file=False):
        return self.owns(path, canonical_file=canonical_file) or any(
            root and (str(path) == root or str(path).startswith(root + os.sep))
            for root in self.import_paths if root not in self.source_paths)

    @property
    def ownership_revision(self):
        return ownership_generation()

    def ownership_snapshot(self):
        return OwnershipSnapshot(self.root)


OWNERSHIP_CHECK_SECONDS = 2.0
OWNERSHIP_CACHE_LIMIT = 32768


class _OwnershipCache:
    """Share marker checks across files and ancestors, with bounded freshness.

    Directory entries hold a marker flag, not a fallback project: without any
    ancestor marker, each source directory remains its own implicit project.
    Normalized index files bypass the separate generic path-resolution memo.
    """
    epoch = 0  # Adopt caches retained across a definition hotswap.

    def __init__(self):
        self.directories = OrderedDict()
        self.owners = OrderedDict()
        self.paths = OrderedDict()
        self.epoch = 0

    @staticmethod
    def _get(cache, key, now):
        held = cache.get(key)
        if held is not None and held[0] > now:
            try:
                cache.move_to_end(key)
            except KeyError:  # another reader evicted it while checking its path
                pass
            return held

    @staticmethod
    def _put(cache, key, value):
        cache[key] = value
        try:
            cache.move_to_end(key)
        except KeyError:  # a concurrent bounded-cache insertion evicted it
            pass
        if len(cache) > OWNERSHIP_CACHE_LIMIT:
            cache.popitem(last=False)

    def _directory(self, path, now):
        # Preserve symlink/.. ordering for resolve(); abspath would collapse
        # '..' before following the preceding symlink. Cwd and HOME changes
        # must change the lexical memo key.
        lexical = os.path.expanduser(os.fspath(path))
        if not os.path.isabs(lexical):
            lexical = os.path.join(os.getcwd(), lexical)
        held = self._get(self.paths, lexical, now)
        if held is not None:
            return held[1]
        target = Path(lexical).resolve()
        directory = str(target if target.is_dir() else target.parent)
        self._put(self.paths, lexical, (now + OWNERSHIP_CHECK_SECONDS, directory))
        return directory

    def _markers(self, directory, now):
        held = self._get(self.directories, directory, now)
        if held is None:
            from meltygui.code.fileref import _PROJECT_MARKERS
            epoch = self.epoch
            marker = any(os.path.exists(os.path.join(directory, name)) for name in _PROJECT_MARKERS)
            held = (now + OWNERSHIP_CHECK_SECONDS, marker, None)
            if epoch == self.epoch:
                self._put(self.directories, directory, held)
        return held

    def root(self, path, *, canonical_file=False, marked_roots=frozenset(), now=None):
        if now is None:
            now = time.monotonic()
        if canonical_file:
            # The caller already resolved this source file (e.g. symbol tables).
            # No file stat or repeated resolution belongs in an N-file query.
            directory = os.path.dirname(os.fspath(path))
        else:
            directory = self._directory(path, now)
        marked_roots = frozenset(marked_roots)
        epoch = self.epoch
        parent = directory
        visited = []
        while True:
            key = (parent, marked_roots, epoch)
            held = self._get(self.owners, key, now)
            if held is not None:
                deadline, owner = held
                break
            deadline, marker, is_directory = self._markers(parent, now)
            visited.append((key, deadline))
            if parent in marked_roots and is_directory is None:
                is_directory = os.path.isdir(parent)
                if epoch == self.epoch:
                    self._put(self.directories, parent, (deadline, marker, is_directory))
            if marker or (parent in marked_roots and is_directory):
                owner = parent
                break
            ancestor = os.path.dirname(parent)
            if ancestor == parent:
                owner = None
                break
            parent = ancestor
        for key, checked_until in reversed(visited):
            deadline = min(deadline, checked_until)
            self._put(self.owners, key, (deadline, owner))
        return owner or directory


_ownership_cache = _OwnershipCache()


class OwnershipSnapshot:
    """One source-index scan's directory answers, checked off the draw thread.

    A large scan may outlast the normal freshness interval. Keep its answers
    for the whole scan instead of expiring and restatting the same directories
    for later files. The next snapshot still observes the normal interval.
    """
    def __init__(self, root, *, marked_roots=frozenset()):
        self.root = root
        self.marked_roots = marked_roots
        self.checked_at = time.monotonic()
        self._owners = {}

    def owns(self, path, *, canonical_file=False):
        from meltygui.text_index import _SKIP_DIRS
        path = os.fspath(path)
        if path != self.root and not path.startswith(self.root + os.sep):
            return False
        if any(part in _SKIP_DIRS for part in path[len(self.root):].split(os.sep)[1:-1]):
            return False
        if not canonical_file:
            return _ownership_cache.root(path, marked_roots=self.marked_roots,
                                         now=self.checked_at) == self.root
        directory = os.path.dirname(path)
        if directory not in self._owners:
            self._owners[directory] = _ownership_cache.root(
                path, canonical_file=True, marked_roots=self.marked_roots, now=self.checked_at)
        return self._owners[directory] == self.root


def owning_root(path, *, canonical_file=False, marked_roots=frozenset()):
    """Nearest marked/marker directory, or this file's own directory.

    ``canonical_file`` is for already-resolved absolute file paths only. Generic
    paths retain directory, relative-path and symlink resolution semantics.
    Markers and symlinks are rechecked at bounded intervals shared by directory.
    """
    return _ownership_cache.root(path, canonical_file=canonical_file, marked_roots=marked_roots)


def ownership_generation():
    return _ownership_cache.epoch


def invalidate_ownership(directory):
    """A watched marker changed; invalidate ancestor answers without a scan."""
    _ownership_cache.directories.pop(directory, None)
    _ownership_cache.epoch += 1


_contexts = {}


def analysis_project(project=None, path=None):
    provider = get('source_context')
    if provider is not None:
        return provider(project=project, path=path)
    if hasattr(project, 'refresh'):
        return project.refresh()
    if project is None:
        from meltygui.core.runtime.paths import application_root
        project = owning_root(path) if path is not None else application_root()
    root = str(Path(project).expanduser().resolve())
    if root not in _contexts:
        _contexts[root] = SourceContext(root)
    return _contexts[root]
