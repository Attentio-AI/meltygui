"""
pathlib.Path ↔ dict/bytes/str converters for the Melty registry.

Reads and writes files through the same converter graph as CST nodes,
so you can chain:

    Path → bytes → str → cst.Module → dict     (disk to editable dict)
    dict → cst.Module → str → bytes → Path      (editable dict back to disk)

Or use convert() with an explicit path for full control:

    convert(Path("app.py"), dict,
            registry=Melty,
            path=[Path, bytes, str, cst.Module, dict])

For reactive file watching, use FileWatch as an intermediate:

    convert(Path("app.py"), dict,
            registry=Melty,
            path=[Path, FileWatch, dict])

    First call:   reads file, caches state, returns dict.
    Later calls:  compares mtime/size.
                  Unchanged → returns cached dict.
                  Changed   → returns Pending(FileWatch).
                  Changed + apply=True → re-reads, returns fresh dict.

File dicts carry __path__ (like __cst__) for lossless round-trip:

    {
        "name":     "app.py",
        "stem":     "app",
        "suffix":   ".py",
        "size":     1234,
        "modified": 1709123456.0,
        "data":     b"import sys\\n...",
        "__path__": Path("/absolute/path/app.py"),
    }
"""

import os
from pathlib import Path

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_conversion.converter_register import converter
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileWatch - stateful file change gate with generic cache                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class FileWatch:
    """Pure change-detection gate for file paths.

    Tracks mtime/size only — never reads file content itself.  Downstream
    converters (FileWatch → dict, FileWatch → tensor, etc.) handle the
    actual reading and can stash their results in the cache dict.

    When a file changes, the cache is cleared so downstream converters
    know to re-read.

    Attributes:
        path:       Resolved absolute path.
        mtime:      Last known st_mtime.
        size:       Last known st_size.
        changed:    True if the file has changed since last check.
        cache:      Dict keyed by target type → cached converter result.
                    Cleared on change detection.  Downstream converters
                    own what goes in here.
    """
    # __slots__ = ("path", "mtime", "size", "changed", "cache")

    def __init__(self, path: Path, mtime: float, size: int,
                 changed: bool = False):
        self._path = path
        self.mtime = mtime
        self.size = size
        self.changed = changed
        self.cache: dict[type, object] = {}

    # def __repr__(self):
    #     status = "changed" if self.changed else "clean"
    #     cached = ", ".join(t.__name__ for t in self.cache) or "empty"
    #     return f"FileWatch({self.path.name!r}, {status}, cache=[{cached}])"


# Module-level cache: resolved path → FileWatch
_file_watch_cache: dict[Path, FileWatch] = {}


def clear_file_watch_cache():
    """Clear all cached FileWatch state.  Useful for tests."""
    _file_watch_cache.clear()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Path → FileWatch (check for changes)                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def path_to_filewatch(value: Path, apply: bool = False) -> FileWatch:
    """Convert a Path to a FileWatch, detecting file changes.

    Does NOT read the file — only stats it for mtime/size.

    First call (no cache):
        Creates FileWatch with changed=False.  Downstream converters
        will see an empty cache and do their initial read.

    Subsequent calls (cache exists):
        Compares current mtime/size to cached values.

        Unchanged → returns cached FileWatch (cache intact).
        Changed + apply=False → returns Pending(FileWatch) with
            changed=True.  Chain short-circuits so the UI can decide
            when to reload.
        Changed + apply=True → updates mtime/size, clears the cache
            so downstream converters re-read, returns FileWatch with
            changed=False.

    Raises FileNotFoundError if the path doesn't exist.
    Raises IsADirectoryError if the path is a directory.
    """
    value = Path(value).resolve()

    if not value.exists():
        raise FileNotFoundError(f"No such file: {value}")
    if value.is_dir():
        raise IsADirectoryError(f"Is a directory, not a file: {value}")

    stat = value.stat()
    cached = _file_watch_cache.get(value)

    # First time
    if cached is None:
        fw = FileWatch(
            path=value,
            mtime=stat.st_mtime,
            size=stat.st_size,
            changed=False,
        )
        _file_watch_cache[value] = fw
        return fw

    # Check for changes
    file_changed = (stat.st_mtime != cached.mtime or stat.st_size != cached.size)

    if not file_changed:
        return cached

    # File changed
    if apply:
        # Update stats, clear cache so downstream re-reads
        cached.mtime = stat.st_mtime
        cached.size = stat.st_size
        cached.changed = False
        cached.cache.clear()
        return cached
    else:
        # Signal change, UI will short-circuit on Pending
        cached.changed = True
        return Pending(cached)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileWatch → dict (read file, build metadata dict, cache result)           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def filewatch_to_dict(value: FileWatch) -> dict:
    """Read a file via FileWatch into the standard metadata dict.

    Uses FileWatch.cache[dict] to avoid re-reading unchanged files.
    On first call or after a change (cache cleared), reads bytes from
    disk and caches the result.

    Produces:
        {"name", "stem", "suffix", "size", "modified", "data", "__path__"}
    """
    cached = value.cache.get(dict)
    if cached is not None:
        return cached

    result = {
        "name": value._path.name,
        "stem": value._path.stem,
        "suffix": value._path.suffix,
        "size": value.size,
        "modified": value.mtime,
        "data": value._path.read_bytes(),
        "__filewatch__": value,
    }
    value.cache[dict] = result
    return result


@converter(registry=Melty)
def dict_to_filewatch(value: dict, apply: bool = False) -> FileWatch:
    """Look up or create a FileWatch from a file metadata dict.

    Prefers __filewatch__ stored on the dict (round-trip from
    filewatch_to_dict).  Falls back to __path__ lookup in the
    module cache, or creates a new FileWatch if needed.

    When apply=False → returns Pending(fw) without writing.
    When apply=True  → writes "data" to disk, updates FileWatch
        mtime/size so it doesn't flag its own write as a change.
    """
    # Fast path: dict already carries its FileWatch
    fw = value.get("__filewatch__")

    if fw is not None:
        # Ensure it's in the module cache
        if fw._path not in _file_watch_cache:
            _file_watch_cache[fw._path] = fw
    else:
        path = value.get("__path__")
        if path is None:
            raise TypeError("Dict has no __filewatch__ or __path__ — can't resolve FileWatch")
        path = Path(path).resolve()

        fw = _file_watch_cache.get(path)
        if fw is None:
            stat = path.stat() if path.exists() else None
            fw = FileWatch(
                path=path,
                mtime=stat.st_mtime if stat else 0.0,
                size=stat.st_size if stat else 0,
            )
            _file_watch_cache[path] = fw

    # Update the dict cache so the round-trip is free
    fw.cache[dict] = value

    if not apply:
        return Pending(fw)

    data = value.get("data")
    if data is None:
        raise ValueError("Dict has no 'data' key — nothing to write")

    fw._path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(data, bytes):
        fw._path.write_bytes(data)
    elif isinstance(data, str):
        fw._path.write_text(data)
    else:
        raise TypeError(f"'data' must be bytes or str, got {type(data).__name__}")

    # Update FW so it doesn't flag its own write as a change
    stat = fw._path.stat()
    fw.mtime = stat.st_mtime
    fw.size = stat.st_size
    fw.changed = False

    return fw


@converter(registry=Melty)
def filewatch_to_path(value: FileWatch) -> Path:
    """Return the resolved path from a FileWatch."""
    return value._path


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Path → dict (read file into metadata)                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def path_to_dict(value: Path, apply: bool = False) -> dict:
    """Read a file from disk into a metadata dict.

    When apply=False (default), returns Pending with metadata but no
    file content — useful for file browsers that show name/size before
    deciding to load.

    When apply=True, reads the full file content into "data".

    Raises FileNotFoundError if the path doesn't exist.
    Raises IsADirectoryError if the path is a directory.
    """
    value = Path(value)

    if not value.exists():
        raise FileNotFoundError(f"No such file: {value}")
    if value.is_dir():
        raise IsADirectoryError(f"Is a directory, not a file: {value}")

    stat = value.stat()

    result = {
        "name": value.name,
        "stem": value.stem,
        "suffix": value.suffix,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "__path__": value.resolve(),
    }

    if apply:
        result["data"] = value.read_bytes()
        return result
    else:
        return Pending(result)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  dict → Path (write file back to disk)                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def dict_to_path(value: dict, apply: bool = False) -> Path:
    """Write file data back to disk from a metadata dict.

    When apply=False (default), returns Pending wrapping the Path
    without writing — the UI can preview what would be written.

    When apply=True, actually writes to disk and returns the Path.

    Requires __path__ (target location) and "data" (bytes or str).
    Creates parent directories if they don't exist.
    """
    path = value.get("__path__")
    if path is None:
        fw = value.get("__filewatch__")
        if fw is not None:
            path = fw._path
    if path is None:
        raise TypeError("Dict has no __path__ or __filewatch__ — can't determine write location")
    path = Path(path)

    if not apply:
        return Pending(path)

    data = value.get("data")
    if data is None:
        raise ValueError("Dict has no 'data' key — nothing to write")

    # Ensure parents exist
    path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(data, bytes):
        path.write_bytes(data)
    elif isinstance(data, str):
        path.write_text(data)
    else:
        raise TypeError(f"'data' must be bytes or str, got {type(data).__name__}")

    return path


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Path → bytes (raw read)                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def path_to_bytes(value: Path) -> bytes:
    """Read raw bytes from a file path."""
    value = Path(value)
    if not value.exists():
        raise FileNotFoundError(f"No such file: {value}")
    return value.read_bytes()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  bytes ↔ str (encoding bridge)                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def bytes_to_str(value: bytes) -> str:
    """Decode bytes to string. Tries UTF-8, falls back to latin-1."""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1")


@converter(registry=Melty)
def str_to_bytes(value: str) -> bytes:
    """Encode string to UTF-8 bytes."""
    return value.encode("utf-8")