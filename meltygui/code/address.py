"""
Address + file-watched converter wrappers.

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
import hashlib
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import inspect

from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Library write guard                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
# The editor resolves a class/function/module to its source FILE and edits a line
# span in place. Nothing stops `type(value)` from being a class that lives in a
# third-party package - and a wrong resolve once silently deleted a field from
# libcst's own whitespace.py, breaking the parser. We are an editor for THIS
# project's code, never for installed libraries, so every resolve/write checks
# this gate. Returning False makes resolve_address return None (editor displays
# "can't resolve") and makes the save functions skip the write - fail closed.

# Project root: this file is .../src/lsd/gl_gui/view/core_conversion/address.py
# → parents[5] is the repo root (the dir that contains `src/`).
_PROJECT_ROOT = Path(__file__).resolve().parents[5]


# str(source_file) -> bool. Whether a path is project source never changes
# within a session, but resolve() walks every path component with an
# lstat+readlink - ~30 GIO round-trips - and this gate runs per FRAME per code
# view (resolve_address). Under a CPU-bound background thread each round-trip
# waits up to a poll interval, so the uncached gate alone stretched frames by
# hundreds of ms (all-thread sampling 2026-07-31: ~half the render thread's
# slow samples were in realpath under this call). Bounded and cleared on hotswap.
_EDITABLE_SOURCE_CACHE = {}


def is_editable_source(source_file) -> bool:
    """True only for source inside the project tree. Library code (site-packages
    / dist-packages / the venv / the stdlib) is read-only to the editor, so we
    never resolve or write to it."""
    key = str(source_file)
    got = _EDITABLE_SOURCE_CACHE.get(key)
    if got is not None:
        return got
    try:
        p = Path(source_file).resolve()
    except (OSError, ValueError):
        return False
    ok = True
    if {"site-packages", "dist-packages"} & set(p.parts):
        ok = False
    else:
        try:
            p.relative_to(_PROJECT_ROOT)
        except ValueError:
            ok = False
    if len(_EDITABLE_SOURCE_CACHE) > 4096:
        _EDITABLE_SOURCE_CACHE.clear()
    _EDITABLE_SOURCE_CACHE[key] = ok
    return ok


def is_writable_file(path) -> bool:
    """The gentler gate for PLAIN-FILE codecs (TextFileCodec and friends): the
    folder-tree windows mount arbitrary directories, so whole-file editing is
    allowed anywhere under $HOME — unlike code codecs, which hotswap live
    objects and stay pinned to the project tree (is_editable_source). Library
    installs are still refused: a venv lives under home too, and writing into
    site-packages through a folder window is the same disaster the strict gate
    exists to prevent."""
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return False
    if {"site-packages", "dist-packages", "venv", ".venv", "node_modules"} & set(p.parts):
        return False
    try:
        p.relative_to(Path.home())
    except ValueError:
        return False
    return True


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Address                                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
ORIGINAL = object()  # sentinel for "no original value found"
APPLY_ALL = object()  # sentinel for "apply to all views, not just the one that originated this Pending"

@dataclass
class FileMeta:

    mtime: float
    size: int

    def __eq__(self, other):
        if not isinstance(other, FileMeta):
            return NotImplemented

        return self.mtime == other.mtime and self.size == other.size

class Address:

    # An Address is identified by its LOCATION - (path, start, end) - not its
    # contents. Construction must stay cheap (it runs during resolve_address); hashing
    # the file (a full read + md5 on every construct) was both slow and the wrong
    # identity: two copies of the same span ARE the same address regardless of what
    # the span currently holds. Null defaults so __hash__/__eq__ work on a `self`
    # a live-view stack window captured pre-__init__.
    start = None
    end = None

    def __init__(self, path, start=None, end=None, source=None, watcher_ds=None):
        self.path = Path(path).resolve()
        self.start = start
        self.end = end
        self.source = source
        self._watcher_ds = watcher_ds

    def get_meta(self) -> FileMeta:
        s = self.path.stat()
        return FileMeta(mtime=s.st_mtime, size=s.st_size)

    def __eq__(self, other):
        if not isinstance(other, Address):
            return NotImplemented
        return (self.path == other.path
                and self.start == other.start
                and self.end == other.end)

    def __hash__(self):
        return hash((self.path, self.start, self.end))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Address resolution                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
class ValueDict:
    """An if/elif/else block's contents, as a dict subclass.

    isinstance(c, dict) → True, so iteration/access works normally.
    isinstance(c, Conditional) → True, so the UI can render a
    collapsible conditional block.

    The .condition attribute holds the full condition text
    (e.g. "if selected", "elif pressed", "else").
    """

    def __init__(self, cached_value, others=None):
        self.others = others or {}
        self.cached_value = cached_value


import types
import weakref

_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def invalidate_address_cache(obj):
    """Call after hotswapping an object in place."""
    _cache.pop(obj, None)


def update_address_cache(obj, ref: Address):
    """Store a known-correct Address after a recompile.

    Avoids re-resolving via inspect.getsourcelines, which can return
    wrong line numbers when linecache holds stale file content after
    a hotswap + file rewrite.
    """
    try:
        _cache[obj] = ref
    except TypeError:
        pass


def _unwrap(func):
    while hasattr(func, '__wrapped__'):
        func = func.__wrapped__
    return func


def to_address(value: Any) -> Address:
    """Convert common types to an Address.  Unwraps decorated functions."""
    if isinstance(value, Address):
        return value
    if isinstance(value, ValueDict):
        return to_address(value.cached_value)
    if isinstance(value, Path):
        return Address(value)

    try:
        cached = _cache[value]
        return cached
    except (KeyError, TypeError):
        # TypeError: value isn't weakly referenceable (shouldn't happen
        # for functions/modules/types, but better safe)
        pass

    result = _resolve(value)

    try:
        _cache[value] = result
    except TypeError:
        pass

    return result


def _evict_linecache(filename: str) -> None:
    """Force-evict a file from linecache.

    linecache.checkcache skips entries with mtime=None (loader-managed),
    so after a hotswap + file rewrite, stale content can persist and
    cause inspect.getsourcelines to return wrong line numbers.
    """
    import linecache
    linecache.cache.pop(filename, None)


def shift_sibling_linenos(saved_source, file_path, after_lineno: int, delta: int,
                          include_saved: bool = False) -> None:
    """Shift co_firstlineno of every code object in the same file
    whose first line is > after_lineno, by `delta` lines.

    Why: inspect.findsource walks backward from co_firstlineno hunting
    for `def`/`class`/`lambda`/`@`. After we expand a function in the
    file, sibling functions below have stale co_firstlineno that lands
    inside the expanded body — findsource then walks back and returns
    the wrong function's source. Patching co_firstlineno keeps every
    other function's line numbers truthful without touching their code.

    include_saved=True also shifts the saved source itself. Use this for an
    edit ABOVE the saved span (e.g. an inserted import): the saved function's
    own def moved down too, so its co_firstlineno must move with it — otherwise
    the next resolve walks findsource back past the moved def to line 0.
    """
    if delta == 0 or saved_source is None:
        return

    if isinstance(saved_source, types.ModuleType):
        module = saved_source
    else:
        module_name = getattr(saved_source, "__module__", None)
        module = sys.modules.get(module_name) if module_name else None
        if module is None:
            return

    try:
        target = Path(file_path).resolve()
    except (OSError, ValueError):
        return

    def _same_file(code) -> bool:
        try:
            return Path(code.co_filename).resolve() == target
        except (OSError, ValueError):
            return code.co_filename == str(target)

    # Compare/shift the UNWRAPPED function. A decorated function (@core_func,
    # @window, ...) stored in module/class vars is the WRAPPER: its __code__ lives
    # in the decorator's file (core_render.py), so _same_file(wrapper) is False
    # and it would be skipped - leaving the real function's co_firstlineno stale.
    # function_to_address resolves via inspect.getsourcelines(inspect.unwrap(func)),
    # which keys on the unwrapped co_firstlineno; that's the one that must shift so
    # the next resolve doesn't walk findsource back to the wrong def (or line 0).
    saved_inner = (saved_source if isinstance(saved_source, types.ModuleType)
                   else inspect.unwrap(saved_source))

    def _maybe_shift(func):
        # inspect.unwrap can hand back a non-function (a wrapper method /
        # builtin / slot for an overridden dunder) with no internal __code__. It
        # can also raise on a pathological __wrapped__ chain. EITHER WAY this must
        # not abort the walk: an unhandled error here would skip every function
        # defined BELOW the offending member, leaving their co_firstlineno stale -
        # which is exactly how draw_context_menu kept resolving to the wrong def
        # after draw_collection grew above it.
        try:
            inner = inspect.unwrap(func)
        except Exception:
            return
        if inner is saved_inner and not include_saved:
            return
        code = getattr(inner, "__code__", None)
        if not isinstance(code, types.CodeType):
            return
        if not _same_file(code):
            return
        if code.co_firstlineno > after_lineno:
            try:
                inner.__code__ = code.replace(
                    co_firstlineno=code.co_firstlineno + delta)
            except (AttributeError, TypeError):
                # Read-only / non-Python code object - can't shift, but don't
                # let it stop the rest of the walk.
                pass

    def _walk_class(cls):
        if cls is saved_source and not include_saved:
            return
        for val in list(vars(cls).values()):
            try:
                if isinstance(val, types.FunctionType):
                    _maybe_shift(val)
                elif isinstance(val, type):
                    _walk_class(val)
                elif isinstance(val, (staticmethod, classmethod)):
                    inner = getattr(val, "__func__", None)
                    if isinstance(inner, types.FunctionType):
                        _maybe_shift(inner)
            except Exception:
                # One bad member must never strand the functions after it.
                continue

    for val in list(vars(module).values()):
        try:
            if isinstance(val, types.FunctionType):
                _maybe_shift(val)
            elif isinstance(val, type):
                _walk_class(val)
        except Exception:
            continue


def _resolve(value: Any) -> Address | None:
    import inspect, types
    if isinstance(value, types.FunctionType):
        unwrapped = inspect.unwrap(value)
        source_file = inspect.getfile(unwrapped)
        _evict_linecache(source_file)
        source_lines, start_lineno = inspect.getsourcelines(unwrapped)
        return Address(Path(source_file), start_lineno - 1,
                       start_lineno - 1 + len(source_lines))


    if isinstance(value, types.ModuleType):
        return Address(Path(value.__file__))

    if isinstance(value, type):
        if value.__module__ in ('builtins', '_collections_abc'):
            return None
        try:
            import inspect
            source_file = inspect.getfile(value)
            _evict_linecache(source_file)
            source_lines, start_lineno = inspect.getsourcelines(value)
            return Address(Path(source_file), start_lineno - 1,
                           start_lineno - 1 + len(source_lines))
        except (TypeError, OSError):
            return None

    return None
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Per-cache_id watch state (fully isolated per view)                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _WatchState:
    """Everything a single view needs for one converter on one file."""
    __slots__ = ("ref", "mtime", "size", "cached_result",
                 "original_output_load", "original_input_load",
                 "original_output_save", "original_input_save")

    def __init__(self, ref: Address):
        self.ref = ref
        self.mtime: float = 0.0
        self.size: int = 0
        self.cached_result: Any = None
        self.original_output_load: Any = None
        self.original_input_load: Any = None
        self.original_output_save: Any = None
        self.original_input_save: Any = None

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
                       ref: Address | None = None,
                       of_type: type | None = None) -> Any:
    """Find original_value across all caches.

    Search by cache_id (exact key) or by ref (scan for matching Address).
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
                if watch.ref == ref and watch.original_input_load is not None:
                    if of_type is None or isinstance(watch.original_input_load, of_type):
                        return watch.original_input_load
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


def _update_address_for_id(cache_id: str, ref: Address) -> None:
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
        apply_load = apply and  apply.__name__ == load_data.__name__ or apply is APPLY_ALL

        # if apply:
        #     if not apply_load:
        #         print(
        #             f"Converter {fn.__name__} got apply={apply} but will not apply because it did not originate from {value.__name__}")
        #     else:
        #         print(f"Applying converter {fn.__name__} with apply={apply}")

        if cache_id is None:
            ref = to_address(value)
            if not apply_load:
                return Pending(originated=load_data, wrapped=None)
            data = load_data(ref)
            return _call_fn(fn, (data, ref), apply, fn_takes_apply=fn_takes_apply)

        # ── Get or create this view's watch ─────────────────────────

        watch = local_cache.get(cache_id)
        if watch is None:
            ref = to_address(value)
            watch = _WatchState(ref)
            local_cache[cache_id] = watch

        if watch is None and not apply_load:
            return Pending(originated=load_data, wrapped=value)

        watch.original_input_load = value

        # ── Check this view's own mtime/size ────────────────────────

        stale = watch.is_stale() if watch is not None else True

        if not stale and watch.cached_result is not None:
            return watch.cached_result

        if not apply_load:
            return Pending(originated=load_data, wrapped=watch.cached_result)

        # ── Reload (uses watch.ref, not re-inspected) ───────────────

        data = load_data(watch.ref)
        result = _call_fn(fn, (value, data, watch.ref), apply, fn_takes_apply=fn_takes_apply)
        watch.cached_result = result
        watch.original_output_load = data
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
    def wrapper(converter_input, apply=False, cache_id=None):

        apply_save = apply and apply.__name__ == save_data.__name__ or apply is APPLY_ALL

        # if apply:
        #     if not apply_save:
        #         print(f"Converter {fn.__name__} got apply={apply} but will not apply because it did not originate from {save_data.__name__}")
        #     else:
        #         print(f"Applying converter {fn.__name__} with apply={apply}")

        # ── No cache_id → stateless pass-through ────────────────────

        if cache_id is None:
            return _call_fn(fn, (converter_input,), apply, fn_takes_apply=fn_takes_apply)

        # ── Find this view's paired load watch ──────────────────────
        watch: _WatchState = load_cache.get(cache_id) if load_cache else None
        if watch is None:
            return converter_input  # never loaded, nothing to save

        # ── Extract data from converter function ─────────────────────

        converter_output = _call_fn(fn, (converter_input,), apply, fn_takes_apply=fn_takes_apply)

        # ── Dirty detection ─────────────────────────────────────────

        is_dirty = (watch.original_output_load is not None
                    and converter_output != watch.original_output_load)

        if not is_dirty:

            return watch.original_input_load

        if not apply_save:
            status = "dirty"
            try:
                lines1 = str(watch.original_output_load).splitlines(keepends=True)
                lines2 = str(converter_output).splitlines(keepends=True)

                # Unified diff (like `git diff`)
                diff = difflib.unified_diff(lines1, lines2, fromfile="original.py", tofile="modified.py")
                # print("".join(diff))
                status = "".join(diff)
            except Exception as e:
                print(f"Diff failed: {e}")

            return Pending(originated=save_data, wrapped=converter_output, status=status)

        # ── Save ────────────────────────────────────────────────────

        ref = watch.ref
        updated_ref = save_data(converter_input, ref, converter_output, watch)
        if isinstance(updated_ref, Pending):
            return updated_ref
        if isinstance(updated_ref, Address):
            ref = updated_ref

        watch.original_output_load = converter_output
        watch.ref = ref
        watch.mark_current()
        _update_address_for_id(cache_id, ref)

        return watch.original_input_load

    wrapper._watched_to_type = to_type
    wrapper._accepts_apply = True
    wrapper._accepts_cache_id = True
    return wrapper
