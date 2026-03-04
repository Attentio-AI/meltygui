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

The Path ↔ dict converters manage file watching internally:

    # First call — reads file, caches state, returns dict
    d = convert(Path("app.py"), dict, registry=Melty, apply=True)

    # Later calls — compares mtime/size
    result = convert(Path("app.py"), dict, registry=Melty, apply=False)
    # Unchanged → returns cached dict
    # Changed   → returns Pending(cached_dict)
    # Changed + apply=True → re-reads, returns fresh dict

File dicts carry __path__ for lossless round-trip:

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

from pathlib import Path

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_conversion.converter_register import converter
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Internal file state cache                                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _FileState:
    """Tracks mtime/size for change detection and caches the last read result.

    This is internal — callers never see it.  The public API is just
    Path ↔ dict with an apply flag.
    """
    __slots__ = ("path", "mtime", "size", "cached_dict", "original_data")

    def __init__(self, path: Path, mtime: float, size: int):
        self.path = path
        self.mtime = mtime
        self.size = size
        self.cached_dict: dict | None = None
        self.original_data: bytes | None = None


# Resolved absolute path → _FileState
_file_state_cache: dict[Path, _FileState] = {}


def clear_file_cache():
    """Clear all cached file state.  Useful for tests."""
    _file_state_cache.clear()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Path → dict (read file with caching and change detection)                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _stat_file(path: Path) -> tuple[Path, float, int]:
    """Resolve, validate, and stat a file path.

    Returns (resolved_path, mtime, size).
    Raises FileNotFoundError or IsADirectoryError.
    """
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"No such file: {resolved}")
    if resolved.is_dir():
        raise IsADirectoryError(f"Is a directory, not a file: {resolved}")
    stat = resolved.stat()
    return resolved, stat.st_mtime, stat.st_size


def _build_file_dict(resolved: Path, mtime: float, size: int,
                     data: bytes) -> dict:
    """Build the standard file metadata dict."""
    return {
        "name": resolved.name,
        "stem": resolved.stem,
        "suffix": resolved.suffix,
        "size": size,
        "modified": mtime,
        "data": data,
        "__path__": resolved,
        "__original_data__": data,
    }


def _build_metadata_shell(resolved: Path, mtime: float, size: int) -> dict:
    """Build a metadata-only dict (no file content)."""
    return {
        "name": resolved.name,
        "stem": resolved.stem,
        "suffix": resolved.suffix,
        "size": size,
        "modified": mtime,
        "__path__": resolved,
        "__original_data__": None,
    }


@converter(registry=Melty, stateful=True)
def path_to_dict(value: Path, apply: bool = False) -> dict:
    """Read a file from disk into a metadata dict with change detection.

    Manages an internal mtime/size cache per resolved path so that
    repeated conversions of the same Path are cheap:

    First call:
        apply=False → Pending with metadata shell (no file read).
        apply=True  → reads file, caches result, returns full dict.

    Subsequent calls (file unchanged):
        If cached dict exists → returns it directly.
        If no cached dict yet → Pending (still needs initial load).

    Subsequent calls (file changed on disk):
        apply=False → bare Pending (CONFIRM state) so the UI can
                      prompt the user to reload.
        apply=True  → re-reads file, updates cache, returns fresh dict.

    Raises FileNotFoundError if the path doesn't exist.
    Raises IsADirectoryError if the path is a directory.
    """
    resolved, mtime, size = _stat_file(Path(value))
    state = _file_state_cache.get(resolved)

    # ── First time (no cached state) ────────────────────────────────

    if state is None:
        state = _FileState(resolved, mtime, size)
        _file_state_cache[resolved] = state

        if not apply:
            return Pending(_build_metadata_shell(resolved, mtime, size))

        # Full load
        data = resolved.read_bytes()
        result = _build_file_dict(resolved, mtime, size, data)
        state.cached_dict = result
        state.original_data = data
        return result

    # ── Cached state exists - check for changes ────────────────────

    changed = (mtime != state.mtime or size != state.size)

    if not changed:
        # File unchanged - return cached dict if we have one
        if state.cached_dict is not None:
            return state.cached_dict
        # State exists but never loaded (lazy first read, no change yet).
        # Treat the same as first read - still needs initial load.
        if not apply:
            return Pending(_build_metadata_shell(resolved, mtime, size))
        data = resolved.read_bytes()
        result = _build_file_dict(resolved, mtime, size, data)
        state.cached_dict = result
        state.original_data = data
        return result

    # ── File changed on disk ────────────────────────────────────────

    if not apply:
        # Bare Pending with CONFIRM - UI should prompt user to reload
        if state.cached_dict is None:
            state.cached_dict = _build_file_dict(resolved, mtime, size, None)
        return Pending(state.cached_dict)

    # Re-read
    data = resolved.read_bytes()
    state.mtime = mtime
    state.size = size
    result = _build_file_dict(resolved, mtime, size, data)
    state.cached_dict = result
    state.original_data = data
    return result


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  dict → Path (write file back to disk)                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def dict_to_path(value: dict, apply: bool = False) -> Path:
    """Write file data back to disk from a metadata dict.

    If "data" is missing (metadata shell from a lazy first read),
    there's nothing to write — returns the Path directly when
    apply=False, or raises ValueError when apply=True.

    When "data" is present, checks whether it has actually changed
    (dirty detection) by comparing against "__original_data__".

    apply=False:
        No data / clean (unchanged) → returns the Path.
        Dirty (data changed)        → returns Pending(Path).

    apply=True:
        No data → ValueError.
        Writes data to disk, updates internal cache state so the
        file's own write isn't flagged as an external change, and
        returns the Path.

    Requires __path__ (target location).
    Creates parent directories if they don't exist.
    """
    path = value.get("__path__")
    if path is None:
        raise TypeError("Dict has no __path__ — can't determine write location")
    path = Path(path)

    data = value.get("data")

    # No data key means content was never loaded (metadata shell).
    # Nothing to write - just return the path.
    if data is None:
        if apply:
            raise ValueError("Dict has no 'data' key — nothing to write")
        return path

    # ── Dirty detection ─────────────────────────────────────────────

    original = value.get("__original_data__")
    is_dirty = (data != original)

    if not apply:
        if is_dirty:
            return Pending(path)
        return path

    # ── Write ───────────────────────────────────────────────────────

    if is_dirty:
        path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(data, bytes):
            path.write_bytes(data)
        elif isinstance(data, str):
            path.write_text(data)
        else:
            raise TypeError(
                f"'data' must be bytes or str, got {type(data).__name__}")

        # Update internal cache so we don't flag our own write as a change
        resolved = path.resolve()
        stat = resolved.stat()
        state = _file_state_cache.get(resolved)
        if state is not None:
            state.mtime = stat.st_mtime
            state.size = stat.st_size
            state.original_data = data if isinstance(data, bytes) \
                else data.encode("utf-8")
            # Update cached dict's original_data marker
            value["__original_data__"] = state.original_data
            state.cached_dict = value

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