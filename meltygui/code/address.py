"""
FileRef + file-watched converter wrappers.

Each cache_id gets fully isolated state: its own mtime/size tracking,
its own cached results, its own original_data for dirty detection.
No shared state between views.

Cross-view change detection happens naturally — each view stats the
file independently, so one view's write shows up as a disk change
to the other view on its next load.

    convert(my_func, dict, registry=Melty, apply=True, cache_id="view_1")
"""

from __future__ import annotations

import difflib
import functools
from pathlib import Path
from typing import Any, Callable

from src.lsd.gl_gui.view.core_conversion.path_finder import Pending


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileRef                                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class FileRef:
    __slots__ = ("path", "start", "end")

    def __init__(self, path: Path, start: int | None = None,
                 end: int | None = None):
        self.path = Path(path).resolve()
        self.start = start
        self.end = end

    def __repr__(self) -> str:
        if self.start is not None:
            end = self.end if self.end is not None else "..."
            return f"FileRef({self.path.name!r}, {self.start}:{end})"
        return f"FileRef({self.path.name!r})"

    def __eq__(self, other) -> bool:
        if not isinstance(other, FileRef):
            return NotImplemented
        return (self.path == other.path
                and self.start == other.start
                and self.end == other.end)

    def __hash__(self) -> int:
        return hash((self.path, self.start, self.end))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileRef resolution                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def to_fileref(value: Any) -> FileRef:
    """Convert common types to a FileRef.  Unwraps decorated functions."""
    if isinstance(value, FileRef):
        return value
    if isinstance(value, Path):
        return FileRef(value)
    import inspect, types
    if isinstance(value, types.FunctionType):
        unwrapped = inspect.unwrap(value)
        source_file = inspect.getfile(unwrapped)
        source_lines, start_lineno = inspect.getsourcelines(unwrapped)
        return FileRef(Path(source_file), start_lineno - 1,
                       start_lineno - 1 + len(source_lines))
    raise TypeError(f"Cannot convert {type(value).__name__} to FileRef")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Per-cache_id watch state (fully isolated per view)                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _WatchState:
    """Everything a single view needs for one converter on one file."""
    __slots__ = ("ref", "mtime", "size", "cached_result",
                 "original_data", "original_value")

    def __init__(self, ref: FileRef):
        self.ref = ref
        self.mtime: float = 0.0
        self.size: int = 0
        self.cached_result: Any = None
        self.original_data: Any = None
        self.original_value: Any = None

    def stat_file(self) -> tuple[float, int]:
        s = self.ref.path.stat()
        return s.st_mtime, s.st_size

    def is_stale(self) -> bool:
        mtime, size = self.stat_file()
        return mtime != self.mtime or size != self.size

    def mark_current(self):
        self.mtime, self.size = self.stat_file()


_all_caches: list[dict] = []


def _new_cache() -> dict[str, _WatchState]:
    cache: dict[str, _WatchState] = {}
    _all_caches.append(cache)
    return cache


def clear_watch_cache():
    """Clear all cached state.  Useful for tests."""
    for cache in _all_caches:
        cache.clear()


def get_original_value(cache_id: str | None = None,
                       ref: FileRef | None = None,
                       of_type: type | None = None) -> Any:
    """Find original_value across all caches.

    Search by cache_id (exact key) or by ref (scan for matching FileRef).
    of_type filters the result.
    """
    for cache in _all_caches:
        if cache_id is not None:
            watch = cache.get(cache_id)
            if watch is not None and watch.original_value is not None:
                if of_type is None or isinstance(watch.original_value, of_type):
                    return watch.original_value
        if ref is not None:
            for watch in cache.values():
                if watch.ref == ref and watch.original_value is not None:
                    if of_type is None or isinstance(watch.original_value, of_type):
                        return watch.original_value
    return None


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Helpers                                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _accepts_apply(fn: Callable) -> bool:
    import inspect as _inspect
    try:
        return "apply" in _inspect.signature(fn).parameters
    except (ValueError, TypeError):
        return False


def _call_fn(fn, args, apply, *, fn_takes_apply):
    if fn_takes_apply:
        return fn(*args, apply=apply)
    return fn(*args)


def _update_ref_for_id(cache_id: str, ref: FileRef) -> None:
    """Update ref on all watches for a cache_id across all caches.

    Called after a save that may have changed the line range so every
    converter in the chain uses the correct range on its next reload.
    """
    for cache in _all_caches:
        watch = cache.get(cache_id)
        if watch is not None:
            watch.ref = ref


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Load wrapper                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def make_load_wrapper(fn: Callable, load_data: Callable,
                      from_type: type) -> Callable:
    """Wrap a forward converter with file watching + caching.

    cache_id=None → no caching, always load fresh.
    cache_id="x"  → cache result per view, detect file changes via mtime/size.

    First call always loads (no Pending on first visit).
    Subsequent calls return cached result if file unchanged.
    If file changed on disk → Pending(cached_result) until apply=True.
    """
    local_cache = _new_cache()
    fn_takes_apply = _accepts_apply(fn)

    @functools.wraps(fn)
    def wrapper(value, apply=False, cache_id=None):

        # ── No cache_id → stateless, always load ──────────────────

        if cache_id is None:
            ref = to_fileref(value)
            if not apply:
                return Pending(None)
            data = load_data(ref)
            return _call_fn(fn, (data, ref), apply, fn_takes_apply=fn_takes_apply)

        # ── Get or create this view's watch ─────────────────────────

        watch = local_cache.get(cache_id)
        if watch is None:
            ref = to_fileref(value)
            watch = _WatchState(ref)
            watch.original_value = value
            local_cache[cache_id] = watch

        if watch is None and not apply:
            return Pending(value)

        # ── Check this view's own mtime/size ────────────────────────

        stale = watch.is_stale() if watch is not None else True

        if not stale and watch.cached_result is not None:
            return watch.cached_result

        if not apply:
            return Pending(watch.cached_result)

        # ── Reload (uses watch.ref, not re-inspected) ───────────────

        data = load_data(watch.ref)
        result = _call_fn(fn, (data, watch.ref), apply, fn_takes_apply=fn_takes_apply)
        watch.cached_result = result
        watch.original_data = data
        watch.mark_current()
        return result

    wrapper._watched_from_type = from_type
    wrapper._local_cache = local_cache
    wrapper._accepts_apply = True
    wrapper._accepts_cache_id = True
    return wrapper


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Save wrapper                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def make_save_wrapper(fn: Callable, save_data: Callable,
                      to_type: type,
                      load_cache: dict | None = None) -> Callable:
    """Wrap an inverse converter with dirty detection + save.

    cache_id=None → stateless pass-through.
    cache_id="x"  → dirty detect against original_data from the paired load.

    Returns watch.original_value (the original input to the forward
    converter, e.g. the function reference) on all paths.
    """
    fn_takes_apply = _accepts_apply(fn)

    @functools.wraps(fn)
    def wrapper(value, apply=False, cache_id=None):

        # ── No cache_id → stateless pass-through ────────────────────

        if cache_id is None:
            return _call_fn(fn, (value,), apply, fn_takes_apply=fn_takes_apply)

        # ── Find this view's paired load watch ──────────────────────

        watch = load_cache.get(cache_id) if load_cache else None
        if watch is None:
            return value  # never loaded, nothing to save

        # ── Extract data from converter function ─────────────────────

        data = _call_fn(fn, (value,), apply, fn_takes_apply=fn_takes_apply)

        # ── Dirty detection ─────────────────────────────────────────

        is_dirty = (watch.original_data is not None
                    and data != watch.original_data)

        if not is_dirty:
            return watch.original_value

        if not apply:
            status = "dirty"
            try:
                lines1 = str(watch.original_data).splitlines(keepends=True)
                lines2 = str(data).splitlines(keepends=True)

                # Unified diff (like `git diff`)
                diff = difflib.unified_diff(lines1, lines2, fromfile="original.py", tofile="modified.py")
                print("".join(diff))
                status = diff
            except Exception as e:
                print(f"Diff failed: {e}")

            return Pending(watch.original_value, status=status)

        # ── Save ────────────────────────────────────────────────────

        ref = watch.ref
        updated_ref = save_data(ref, data)
        if isinstance(updated_ref, FileRef):
            ref = updated_ref

        watch.original_data = data
        watch.ref = ref
        watch.mark_current()
        _update_ref_for_id(cache_id, ref)

        return watch.original_value

    wrapper._watched_to_type = to_type
    wrapper._accepts_apply = True
    wrapper._accepts_cache_id = True
    return wrapper