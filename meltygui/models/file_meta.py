"""Per-file display attributes (tint, icon, order …) in ONE store shared
by every meltygui app on the machine — the studio, melty_code_editor, the
folder windows — as `file_meta_store()`: a FileMetaProxy, path string ->
FileMeta, backed by a pickle at ~/.melty/file_meta.pkl.

Shaped like GitProxy: the store is a dict subclass and THE SAME OBJECT for
every holder, so reads are plain dict reads and every holder sees updates
live. The file I/O is the proxy's job and stays off the render thread:

* Writes are debounced (`SAVE_DELAY_S` after the last edit) and atomic
  (tmp + os.replace) on a worker thread; an edit to an entry in place
  (`entry["tint"] = …`) reaches the store through FileMeta's change hooks.
  Empty entries are not written (the studio's folder scan setdefault()s
  thousands of them; they cost nothing in memory and re-create on demand).
* Other processes' writes: a poller thread stats the file every
  `POLL_S`; a new (mtime, size) marks a pending reload, which the NEXT read
  applies on the reading thread (never a swap under a reader's iteration),
  keeping entries this process edited since its last save (`_dirty`) and
  then repainting: `Melty.cache.invalidate_all()` + request_render. The
  poller also requests a render so an idle app wakes to read.
* The root save no longer carries the entries (FileMetaCollection is
  `@no_save("file_meta")`); a root loaded from an older save migrates the
  entries it still holds into the store on its on_load, once — the store
  wins for a path it already has.

`MELTY_FILE_META=/path.pkl` overrides the location (tests, a second user).
"""
import atexit
import contextlib
import os
import pickle
import tempfile
import threading
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import defaults
from meltygui.core.rendering.core_decoration import no_save

SAVE_DELAY_S = 0.4
POLL_S = 0.5
_FORMAT = 1


def _lock_fd(fd):
    """Block until this process holds the exclusive cross-process lock on fd
    (flock on POSIX; on Windows a one-byte region lock at offset 0)."""
    if os.name != "nt":
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)      # retries for ~10 s, then OSError
            return
        except OSError:
            continue


def _unlock_fd(fd):
    if os.name != "nt":
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass                                         # never acquired (the lock call itself raised)


def default_store_path():
    override = os.environ.get("MELTY_FILE_META")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".melty" / "file_meta.pkl"


@defaults(show_tint=False, show_name=True, show_excluded=True)
class FileMeta(dict):
    """One file's attribute entry in FileMetaCollection.file_meta.

    A plain dict to the framework — the codec kwargs layer merges its items,
    the folder tree syncs it, and it serializes like any dict — but a TYPE of
    its own, so file attributes have a home: class-level attrs declare the
    vocabulary and its defaults (and give draw_any / @defaults / the context
    menu a class to anchor on), while the dict itself holds only the values
    actually attached to the file. Reading `entry.tint` falls through to the
    class default when the file has nothing stored."""

    # Black transparent = UNPAINTED: no tint on the file's views, and every
    # consumer (tabs, trees, search rows) falls back to its own default. Get
    # tints through painted_tint(), not entry["tint"] directly.
    tint = (0.0, 0.0, 0.0, 0.0)
    icon = None  # glyph drawn before the file name (editor tabs); None = no icon
    # A FOLDER marked as a project (folder_project): a root for the editor's
    # global search, the symbol's usage graph, tabs and editability. Files
    # never store it; read it through is_project() or project_roots().
    project = False
    environment = None  # Project venv folder override; None means auto-detect.

    # The pre-09-02 class default; stored values of it are dropped at load
    # (folder_files._init_file_meta) so the files read as unpainted.
    _LEGACY_DEFAULT_TINT = (0.11, 0.122, 0.14)

    @staticmethod
    def painted_tint(entry):
        """The tint a user attached to `entry` (a FileMeta / dict / None) as
        an rgb(a) tuple, or None when the file is unpainted — no tint, a
        malformed one, or one with alpha 0 (the default)."""
        tint = entry.get("tint") if isinstance(entry, dict) else None
        if not (isinstance(tint, (tuple, list)) and len(tint) >= 3):
            return None
        if len(tint) >= 4 and not tint[3]:
            return None
        return tuple(tint)

    def __getattribute__(self, name):
        # Stored-first attribute access: `entry.tint` is the file's own value
        # when one is attached, the class default otherwise. (A plain
        # __getattr__ can't do this - normal lookup finds the class attr
        # first and the dict value would never win.) Dunder/internal names
        # skip the dict so machinery never collides with stored keys.
        if not name.startswith("_"):
            try:
                return dict.__getitem__(self, name)
            except KeyError:
                pass
        return super().__getattribute__(name)

    # ── Merged iteration: class defaults flow like stored values ───────────
    # The iteration protocol serves class the class together, so every
    # consumer that iterates - draw_collection, the codec kwargs layer, the
    # folder tree sync - sees every declared attribute on every entry, and
    # editing a default row simply attaches the value (__setitem__). This is
    # deliberate: the defaults ARE the entry's effective values. A consumer
    # that ever needs only what's explicitly attached reads dict.items(entry)
    # - the raw stored dict is always one super() away.

    @classmethod
    def _class_defaults(cls):
        out = {}
        for klass in reversed(cls.__mro__):
            if klass in (dict, object):
                continue
            for k, v in vars(klass).items():
                if (k.startswith("_") or callable(v)
                        or isinstance(v, (classmethod, staticmethod, property))):
                    continue
                out[k] = v
        return out

    def _merged(self):
        d = dict(type(self)._class_defaults())
        d.update(dict.items(self))
        return d

    def __iter__(self):
        return iter(self._merged())

    def __len__(self):
        return len(self._merged())

    def keys(self):
        return self._merged().keys()

    def items(self):
        return self._merged().items()

    def values(self):
        return self._merged().values()

    def __contains__(self, k):
        return dict.__contains__(self, k) or k in type(self)._class_defaults()

    def __getitem__(self, k):
        try:
            return dict.__getitem__(self, k)
        except KeyError:
            defaults = type(self)._class_defaults()
            if k in defaults:
                return defaults[k]
            raise

    def get(self, k, default=None):
        try:
            return self[k]
        except KeyError:
            return default

    # ── Mutation hooks: an entry edited in-place (`entry["tint"] = ...`) tells
    # the store that holds it (FileMetaProxy stamps `_owner` on insertion)
    # so the shared file gets written. dict-level raw edits (`dict.pop(entry,
    # ...)`) bypass these on purpose - you store.touch() after those.
    _owner = None
    _key = None

    def _changed(self):
        owner = self._owner
        if owner is not None:
            owner.touch(self._key)

    def __setitem__(self, k, v):
        dict.__setitem__(self, k, v)
        self._changed()

    def __delitem__(self, k):
        dict.__delitem__(self, k)
        self._changed()

    def pop(self, *args):
        out = dict.pop(self, *args)
        self._changed()
        return out

    def popitem(self):
        out = dict.popitem(self)
        self._changed()
        return out

    def clear(self):
        dict.clear(self)
        self._changed()

    def update(self, *args, **kwargs):
        dict.update(self, *args, **kwargs)
        self._changed()

    def setdefault(self, k, default=None):
        if dict.__contains__(self, k):
            return dict.__getitem__(self, k)
        dict.__setitem__(self, k, default)
        self._changed()
        return default


class FileMetaProxy(dict):
    """path string -> FileMeta, the shared per-file attribute store (see the
    module docstring). Create one through file_meta_store()."""

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path) if path is not None else default_store_path()
        self._lock = threading.RLock()
        self._disk_sig = None       # (mtime_ns, size) of the file as last read / written
        self._pending = False       # the poller saw a newer sig: merge on the next read
        self._dirty = set()         # paths edited here since the last save
        self._dirty_all = False     # a clear()/whole-store edit: our copy wins entirely
        self.generation = 0         # bumped on every local edit and every save (memo key)
        self._timer = None
        self._poller = None
        self.listeners = []         # callables run(reading thread) after an external reload
        self.load()
        atexit.register(self.flush)

    # ── file ───────────────────────────────────────────────────────────────
    def _stat_sig(self):
        """Identity of the file's current contents. The inode is in it
        because every write is a fresh temp file renamed over the path:
        two writes inside one clock tick share mtime and can share size,
        the inode never repeats."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    @contextlib.contextmanager
    def _file_lock(self):
        """Serialize writers across processes (flock on a sibling .lock) so
        the stale check and the replace are one step."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock_fd(fd)
            yield
        finally:
            _unlock_fd(fd)
            os.close(fd)

    def _read_file(self):
        """{path: stored dict} from disk, {} when absent / unreadable."""
        try:
            with open(self.path, "rb") as fh:
                data = pickle.load(fh)
        except (OSError, EOFError, pickle.UnpicklingError, AttributeError,
                ImportError, ValueError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {str(k): dict(v) for k, v in entries.items() if isinstance(v, dict)}

    def load(self):
        """Replace the held entries with the file's (keeping locally dirty
        ones). Returns True when anything changed. Runs on the caller's
        thread — the reading thread, by design (see _sync)."""
        with self._lock:
            sig = self._stat_sig()
            fresh = self._read_file() if sig is not None else {}
            self._disk_sig = sig
            self._pending = False
            if self._dirty_all:
                return False
            keep = {p: dict.__getitem__(self, p) for p in self._dirty
                    if dict.__contains__(self, p)}
            before = {p: dict(dict.items(e)) for p, e in dict.items(self)}
            merged = {}
            for path, values in fresh.items():
                if path in self._dirty:
                    continue  # Includes locally deleted entries, absent from keep.
                entry = dict.get(self, path)
                if not isinstance(entry, FileMeta):
                    entry = self._adopt(FileMeta(), path)
                dict.clear(entry)
                dict.update(entry, values)
                merged[path] = entry
            merged.update(keep)
            after = {p: dict(dict.items(e)) for p, e in merged.items()}
            if after == before:
                return False
            dict.clear(self)
            dict.update(self, merged)
            self.generation += 1
            return True

    def _adopt(self, entry, key):
        if isinstance(entry, FileMeta):
            entry._owner = self
            entry._key = key
        return entry

    def _snapshot(self):
        with self._lock:
            return {p: dict(dict.items(e)) for p, e in dict.items(self)
                    if isinstance(e, dict) and len(dict.keys(e))}

    def _write(self):
        entries = self._snapshot()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".file_meta-", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                pickle.dump({"format": _FORMAT, "entries": entries}, fh,
                            protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
        with self._lock:
            self._disk_sig = self._stat_sig()
            self._dirty.clear()
            self._dirty_all = False

    def flush(self):
        """Write now if anything is pending (atexit; tests). Merges from
        disk first when another process wrote meanwhile."""
        with self._lock:
            timer, self._timer = self._timer, None
            dirty = bool(self._dirty or self._dirty_all)
        if timer is not None:
            timer.cancel()
        if dirty:
            with self._file_lock():
                if self._stat_sig() != self._disk_sig:
                    self.load()
                self._write()

    def touch(self, path=None):
        """Mark the store (or one entry) edited here and arm the debounced
        save. FileMeta entries call this through their change hooks."""
        with self._lock:
            self.generation += 1
            if path is None:
                self._dirty_all = True
            else:
                self._dirty.add(str(path))
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(SAVE_DELAY_S, self._save_timer)
            self._timer.daemon = True
            self._timer.start()
        self._ensure_poller()

    def _save_timer(self):
        with self._lock:
            self._timer = None
        with self._file_lock():
            stale = self._stat_sig() != self._disk_sig
            if not stale:
                self._merge_retries = 0
                self._write()
                return
            retries = self._merge_retries = getattr(self, "_merge_retries", 0) + 1
            if retries > 5:
                # Nobody in this process is reading (yet): merge here,
                # drop the lock, then write.
                self._merge_retries = 0
                self.load()
                self._write()
                return
        # Another process wrote since we last read: writing our snapshot
        # would overwrite its change. Merge first - load() keeps our dirty
        # entries - on the reading thread: flag it, wake a frame, retry.
        with self._lock:
            self._pending = True
        _request_render()
        with self._lock:
            if self._timer is None:
                self._timer = threading.Timer(POLL_S, self._save_timer)
                self._timer.daemon = True
                self._timer.start()

    # ── other processes ────────────────────────────────────────────────────
    def _ensure_poller(self):
        if self._poller is not None:
            return
        self._poller = threading.Thread(target=self._poll, name="file-meta-poll",
                                        daemon=True)
        self._poller.start()

    def _poll(self):
        while True:
            time.sleep(POLL_S)
            with self._lock:
                if self._pending or self._timer is not None:
                    continue          # already flagged or our own write is coming
                sig = self._stat_sig()
                changed = sig != self._disk_sig
                if changed:
                    self._pending = True
            if changed:
                _request_render()

    def _sync(self):
        if self._pending and self.load():
            self._repaint()

    def _repaint(self):
        try:
            from meltygui.core.melty import Melty
            cache = getattr(Melty, "cache", None)
            if cache is not None:
                cache.invalidate_all()
        except Exception:
            pass
        _request_render()
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    # ── dict surface: reads try a pending reload first, writes mark dirty ─
    def __getitem__(self, k):
        self._sync()
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._sync()
        return dict.get(self, k, default)

    def __contains__(self, k):
        self._sync()
        return dict.__contains__(self, k)

    def __iter__(self):
        self._sync()
        return dict.__iter__(self)

    def __len__(self):
        self._sync()
        return dict.__len__(self)

    def keys(self):
        self._sync()
        return dict.keys(self)

    def values(self):
        self._sync()
        return dict.values(self)

    def items(self):
        self._sync()
        return dict.items(self)

    def __setitem__(self, k, v):
        self._sync()
        if isinstance(v, dict) and not isinstance(v, FileMeta):
            v = FileMeta(v)
        with self._lock:
            dict.__setitem__(self, k, self._adopt(v, k))
        self.touch(k)

    def __delitem__(self, k):
        self._sync()
        with self._lock:
            dict.__delitem__(self, k)
        self.touch(k)

    def pop(self, k, *default):
        self._sync()
        with self._lock:
            had = dict.__contains__(self, k)
            out = dict.pop(self, k, *default)
        if had:
            self.touch(k)
        return out

    def popitem(self):
        self._sync()
        with self._lock:
            k, v = dict.popitem(self)
        self.touch(k)
        return k, v

    def setdefault(self, k, default=None):
        self._sync()
        if dict.__contains__(self, k):
            return dict.__getitem__(self, k)
        self[k] = default
        return dict.__getitem__(self, k)

    def update(self, *args, **kwargs):
        for k, v in dict(*args, **kwargs).items():
            self[k] = v

    def clear(self):
        with self._lock:
            dict.clear(self)
        self.touch()

    def __reduce__(self):
        # Pickled / deepcopopied as a plain dict of plain dicts: the proxy's
        # path, timer and poller never travel, and a save that reaches it by
        # mistake stays loadable.
        return (dict, (self._snapshot(),))

    def __repr__(self):
        return f"FileMetaProxy({self.path}, {dict.__len__(self)} entries)"


def _request_render():
    try:
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
    except Exception:
        pass


_store = None
_store_lock = threading.Lock()


def file_meta_store():
    """The process's shared FileMetaProxy (created on first use)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = FileMetaProxy()
                _store._ensure_poller()
    return _store


@no_save("file_meta")
class FileMetaCollection(DictConversion):
    """AppModel's handle on the shared per-file attribute store.

    `file_meta` IS the process's FileMetaProxy (path string -> FileMeta:
    `tint`, `expanded`, `order`, …), the same object file_meta_store()
    returns, so the folder tree, the editor tabs and the codec layer keep
    reading `root.file_meta_collection.file_meta` unchanged. It is no_save:
    the entries live in ~/.melty/file_meta.pkl, not the root save. A root
    loaded from an older save still carries them — on_load migrates those
    into the store (the store wins where it already has the path) and puts
    the proxy back."""

    def __init__(self):
        super().__init__()
        self.name = "File Metadata"
        self.file_meta = file_meta_store()

    def on_load(self, vis=None, root=None):
        store = file_meta_store()
        held = self.__dict__.get("file_meta")
        if held is store:
            return
        if isinstance(held, dict) and held:
            migrated = 0
            for path, entry in held.items():
                if not isinstance(entry, dict) or not entry:
                    continue
                stored = {k: v for k, v in dict.items(entry)
                          if not (isinstance(k, str) and k.startswith("__"))}
                if not stored or path in store:
                    continue
                store[str(path)] = FileMeta(stored)
                migrated += 1
            if migrated:
                print(f"[file_meta] migrated {migrated} entries from the root save "
                      f"into {store.path}")
        self.file_meta = store
