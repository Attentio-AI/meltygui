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
import builtins
import dis
import inspect
import json
import textwrap
import time
import types

from pathlib import Path
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_conversion.converter_register import converter

import libcst as cst

from src.lsd.gl_gui.view.core_conversion.fileref import FileRef, get_original_value, ValueDict, ORIGINAL


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



########################### MODULE CONVERTERS
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  TextSpan - address type                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Internal cache                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _SpanFileState:
    """Whole-file cache shared across all spans into the same file.

    We cache at the file level (not per-span) so that multiple spans
    into the same file share one read and see a consistent snapshot.
    """
    __slots__ = ("path", "mtime", "size", "lines", "newline")

    def __init__(self, path: Path, mtime: float, size: int):
        self.path = path
        self.mtime = mtime
        self.size = size
        self.lines: list[str] | None = None
        self.newline: str = "\n"


# resolved absolute path → file state
_span_file_cache: dict[Path, _SpanFileState] = {}

# (resolved path, start, end) → last returned data string
_span_data_cache: dict[tuple[Path, int, int | None], str] = {}


def clear_span_cache():
    """Clear all span-related caches.  Useful for tests."""
    _span_file_cache.clear()
    _span_data_cache.clear()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Helpers                                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _stat_file(path: Path) -> tuple[Path, float, int]:
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"No such file: {resolved}")
    if resolved.is_dir():
        raise IsADirectoryError(f"Is a directory, not a file: {resolved}")
    stat = resolved.stat()
    return resolved, stat.st_mtime, stat.st_size


def _read_lines(data: bytes) -> tuple[list[str], str]:
    """Decode bytes into lines (without trailing newlines) + newline style."""
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")

    lines = text.split(newline)

    return lines, newline


def _ensure_file_loaded(resolved: Path, mtime: float, size: int,
                        apply: bool) -> _SpanFileState | None:
    """Load or refresh the whole-file cache.

    Returns the state with .lines populated if apply=True and the
    file was read successfully, or None if apply=False and we
    haven't read it yet / it changed.
    """
    state = _span_file_cache.get(resolved)

    if state is None:
        state = _SpanFileState(resolved, mtime, size)
        _span_file_cache[resolved] = state

    changed = (mtime != state.mtime or size != state.size)

    if changed or state.lines is None:
        if not apply:
            return None
        data = resolved.read_bytes()
        state.lines, state.newline = _read_lines(data)
        state.mtime = mtime
        state.size = size

    return state


def _extract_span(lines: list[str], start: int, end: int | None,
                  newline: str) -> str:
    """Slice lines and rejoin into a string."""
    selected = lines[start:end]
    return newline.join(selected)


class _FuncEntry:
    __slots__ = ("func", "source")

    def __init__(self, func: types.FunctionType, source: str):
        self.func = func
        self.source = source


# Keyed on (resolved path, start) - not end, because end can shift
# when the edit changes line count.
_span_to_entry: dict[tuple[Path, int], _FuncEntry] = {}


def clear_function_span_cache():
    """Clear the span → function lookup.  Useful for tests."""
    _span_to_entry.clear()


def _cache_key(span: FileRef) -> tuple[Path, int]:
    return (span.path.resolve(), span.start)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Recompilation                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _read_span_source(span: FileRef) -> str:
    """Read the current source for a span, preferring the file cache."""
    resolved = span.path.resolve()
    file_state = _span_file_cache.get(resolved)

    if file_state is not None and file_state.lines is not None:
        selected = file_state.lines[span.start:span.end]
        return file_state.newline.join(selected)

    # Fall back to disk
    text = resolved.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    selected = lines[span.start:span.end]
    return newline.join(selected)


# (resolved_path, start) → FileRef
# Keyed on start only (not end) because end can shift on edit.
_span_by_key: dict[tuple[Path, int], FileRef] = {}

# Same key → actual source string for dirty detection.
_source_by_key: dict[tuple[Path, int], str] = {}

# id(cst.Module) → cache key, populated on the forward pass so the
# reverse can find which span produced a given module even when
# multiple spans are active.
_module_id_to_key: dict[int, tuple[Path, int]] = {}


def clear_span_module_cache():
    """Clear all FileRef ↔ cst.Module caches.  Useful for tests."""
    _span_by_key.clear()
    _source_by_key.clear()
    _module_id_to_key.clear()


def _cache_key(span: FileRef) -> tuple[Path, int]:
    return (span.path.resolve(), span.start)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileRef → cst.Module                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


def _find_origin_span(module: cst.Module) -> tuple[FileRef, tuple[Path, int]]:
    """Find the FileRef that produced this module.

    Primary lookup: id(module) — works when the module object from
    the forward pass is passed back directly (clean round-trip).

    Fallback: the module was rebuilt by dict_to_cst_module (new
    object, different id).  In that case, check the dict's __cst__
    (the original module stashed during cst_module_to_dict) — its
    id should still be in the cache.

    Last resort: if only one span is active, use it.

    Raises LookupError if nothing matches.
    """
    # Direct id match (unmodified module)
    key = _module_id_to_key.get(id(module))
    if key is not None and key in _span_by_key:
        return _span_by_key[key], key

    # The module was likely rebuilt via .visit() - try to find the
    # original via the wrapper attribute that libcst sometimes adds,
    # or just scan for a matching key.
    # In practice, dict_to_cst_module builds from __cst__ (the original),
    # so if we find any stashed span, it's the right one for a
    # single-file scenario.
    if len(_span_by_key) == 1:
        key = next(iter(_span_by_key))
        return _span_by_key[key], key

    raise LookupError(
        "No FileRef origin found for this cst.Module — was "
        "text_span_to_cst_module called first?"
    )


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


@converter(registry=Melty, inverse_of=bytes_to_str)
def str_to_bytes(value: str) -> bytes:
    """Encode string to UTF-8 bytes."""
    return value.encode("utf-8")


@converter(registry=Melty)
def str_to_dict(value: str) -> dict:
    """Wrap a string in a dict for editing."""
    try:
        dict_value = json.loads(value)
        return dict_value

    except (TypeError, ValueError) as e:
        return {"error": f"Value is not JSON-serializable: {e}"}


@converter(registry=Melty, inverse_of=str_to_dict)
def dict_to_str(value: dict) -> str:
    """Dump a dict back to a string."""
    try:
        return str(value)
    except (TypeError, ValueError) as e:
        return f"Error serializing dict to JSON: {e}"



########################## NEW CONVERTERS


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  I/O callbacks                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def load_file_bytes(ref: FileRef) -> bytes:
    return ref.path.read_bytes()


def save_file_bytes(value, ref: FileRef, data: bytes, watch) -> FileRef | None:
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        ref.path.write_text(data, encoding="utf-8")
    else:
        ref.path.write_bytes(data)
    return None


def _detect_newline(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def load_span_text(ref: FileRef) -> str:
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.split(newline)
    return newline.join(lines[ref.start:ref.end])


def save_span_text(value, ref: FileRef, data: str, watch) -> FileRef:
    print(f"Saving span {ref} with new data (length {len(data)})")
    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)
    new_lines = data.split(newline)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    return FileRef(ref.path, ref.start, ref.start + len(new_lines))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Path ↔ dict (whole file)                                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty, from_type=Path, load_data=load_file_bytes, stateful=True,)
def path_to_dict(value, data: bytes, ref: FileRef) -> dict:
    return {
        "name": ref.path.name,
        "stem": ref.path.stem,
        "suffix": ref.path.suffix,
        "size": len(data),
        "modified": ref.path.stat().st_mtime,
        "data": data,
        "__path__": ref.path,
        "__original_data__": data,
    }


@converter(registry=Melty, to_type=Path, save_data=save_file_bytes,
           inverse_of=path_to_dict, stateful=True)
def dict_to_path(value: dict) -> bytes:
    data = value.get("data")
    if data is None:
        raise ValueError("Dict has no 'data' — nothing to write")
    return data.encode("utf-8") if isinstance(data, str) else data


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileRef ↔ dict (line range as plain text)                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty, from_type=FileRef, load_data=load_span_text, stateful=True,)
def fileref_to_dict(value, data: str, ref: FileRef) -> dict:
    return {
        "value": data,
        "__original_value__": data,
    }


@converter(registry=Melty, to_type=FileRef, save_data=save_span_text,
           inverse_of=fileref_to_dict, stateful=True)
def dict_to_fileref(value: dict) -> str:
    data = value.get("value")
    if data is None:
        raise ValueError("Dict has no 'value' — nothing to write")
    return data


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileRef ↔ cst.Module (line range parsed into CST)                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty, from_type=FileRef, load_data=load_span_text, stateful=True,)
def fileref_to_cst_module(value, data: str, ref: FileRef) -> cst.Module:
    return cst.parse_module(data)



# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Recompilation                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
"""
Example converters using FileRef + load_data / save_data + cache_id.

All caching is keyed on cache_id passed through convert().
No cache_id = no caching, always fresh.
"""

import inspect
import textwrap
import types
from pathlib import Path

import libcst as cst

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_conversion.converter_register import converter
from src.lsd.gl_gui.view.core_conversion.fileref import (
    FileRef, get_original_value,
)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  File/O callbacks                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def load_file_bytes(ref: FileRef) -> bytes:
    print(f"Loading bytes from {ref.path} for cache_id")
    return ref.path.read_bytes()


def _detect_newline(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"



def load_text(ref: FileRef) -> str:
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.split(newline)
    return newline.join(lines[ref.start:ref.end])


def save_span_text(value, ref: FileRef, data: str) -> FileRef:
    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)
    new_lines = data.split(newline)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    return FileRef(ref.path, ref.start, ref.start + len(new_lines))


def save_span_function(value, ref: FileRef, function: types.FunctionType) -> FileRef:
    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)

    data = value.code
    print(f"saving code")

    new_lines = data.split(newline)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    return FileRef(ref.path, ref.start, ref.start + len(new_lines))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Path ↔ dict (whole file)                                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty, from_type=Path, load_data=load_file_bytes)
def path_to_dict(value, data: bytes, ref: FileRef) -> dict:
    return {
        "name": ref.path.name,
        "stem": ref.path.stem,
        "suffix": ref.path.suffix,
        "size": len(data),
        "modified": ref.path.stat().st_mtime,
        "data": data,
        "__path__": ref.path,
        "__original_data__": data,
    }


@converter(registry=Melty, to_type=Path, save_data=save_file_bytes,
           inverse_of=path_to_dict, stateful=True)
def dict_to_path(value: dict) -> bytes:
    data = value.get("data")
    if data is None:
        raise ValueError("Dict has no 'data' — nothing to write")
    return data.encode("utf-8") if isinstance(data, str) else data


@converter(registry=Melty, from_type=FileRef, load_data=load_text, stateful=True, )
def fileref_to_dict(value, data: str, ref: FileRef) -> dict:
    return {
        "value": data,
        "__original_value__": data,
    }


@converter(registry=Melty, to_type=FileRef, save_data=save_span_text,
           inverse_of=fileref_to_dict)
def dict_to_fileref(value: dict) -> str:
    data = value.get("value")
    if data is None:
        raise ValueError("Dict has no 'value' — nothing to write")
    return data


@converter(registry=Melty, from_type=FileRef, load_data=load_text, stateful=True, )
def fileref_to_cst_module(value, data: str, ref: FileRef) -> cst.Module:
    return cst.parse_module(data)

@converter(registry=Melty, to_type=FileRef, save_data=save_span_text,
           inverse_of=fileref_to_cst_module, statful=True)
def cst_module_to_fileref(value: cst.Module) -> str:
    return value.code


@converter(registry=Melty, from_type=types.FunctionType, load_data=load_text, stateful=True, )
def function_to_cst(value, data: str, ref: FileRef) -> cst.Module:
    return cst.parse_module(data)

def recompile(value, ref: FileRef, data: str, watch) -> FileRef:
    from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
    # source = inspect.getsource(function)
    if value is not None:
        function = watch.original_input_load
        source = data
        try:
            _recompile(function, source, str(ref.path))
        except Exception as e:
            return Pending(originated=recompile, status=str(e), state=PendingState.ERROR)
    # return ref
    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)
    new_lines = data.split(newline)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    return FileRef(ref.path, ref.start, ref.start + len(new_lines))


@converter(registry=Melty, to_type=types.FunctionType, save_data=recompile,
           inverse_of=function_to_cst, stateful=True)
def cst_module_to_function(value: cst.Module) -> str:
    return value.code


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  types.ModuleType ↔ cst.Module                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty, from_type=types.ModuleType, load_data=load_text, stateful=True)
def module_to_cst(value, data: str, ref: FileRef) -> cst.Module:
    return cst.parse_module(data)


def recompile_module(value, ref: FileRef, data: str, watch) -> FileRef:
    from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
    if value is not None:
        module = watch.original_input_load
        source = data
        try:
            _recompile_module(module, source, str(ref.path))
        except Exception as e:
            return Pending(originated=recompile_module, status=str(e), state=PendingState.ERROR)
    # Module FileRefs cover the whole file (start=None, end=None),
    # so write the data directly rather than splicing lines.
    ref.path.write_text(data, encoding="utf-8")
    return ref


@converter(registry=Melty, to_type=types.ModuleType, save_data=recompile_module,
           inverse_of=module_to_cst, stateful=True)
def cst_module_to_module(value: cst.Module) -> str:
    return value.code


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  type ↔ cst.Module                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty, from_type=type, load_data=load_text, stateful=True)
def type_to_cst(value, data: str, ref: FileRef) -> cst.Module:
    return cst.parse_module(data)


def recompile_class(value, ref: FileRef, data: str, watch) -> FileRef:
    from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
    if value is not None:
        cls = watch.original_input_load
        source = data
        try:
            _recompile_class(cls, source, str(ref.path))
        except Exception as e:
            return Pending(originated=recompile_class, status=str(e), state=PendingState.ERROR)
    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)
    new_lines = data.split(newline)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    return FileRef(ref.path, ref.start, ref.start + len(new_lines))


@converter(registry=Melty, to_type=type, save_data=recompile_class,
           inverse_of=type_to_cst, stateful=True)
def cst_module_to_type(value: cst.Module) -> str:
    return value.code


def _recompile_class(cls: type, source: str, filename: str) -> None:
    import sys
    dedented = textwrap.dedent(source)

    # Execute in the class's original module namespace so references resolve
    mod = sys.modules.get(cls.__module__)
    namespace = dict(vars(mod)) if mod is not None else {}

    code = compile(dedented, filename, "exec")
    exec(code, namespace)

    new_cls = namespace.get(cls.__name__)
    if new_cls is None:
        raise RuntimeError(f"Recompilation produced no class named '{cls.__name__}'")
    if not isinstance(new_cls, type):
        raise RuntimeError(f"'{cls.__name__}' is {type(new_cls).__name__}, not a class")

    _hotswap_class(cls, new_cls)


def _recompile_module(module: types.ModuleType, source: str,
                      filename: str) -> None:
    # Snapshot everything before exec so we can hotswap in place
    old_attrs = dict(module.__dict__)

    code = compile(source, filename, "exec")
    exec(code, module.__dict__)

    for name, old_obj in old_attrs.items():
        new_obj = module.__dict__.get(name)
        if new_obj is old_obj or new_obj is None:
            continue

        # --- Functions: patch code/defaults in place ---
        if isinstance(old_obj, types.FunctionType) and isinstance(new_obj, types.FunctionType):
            old_obj.__code__ = new_obj.__code__
            old_obj.__defaults__ = new_obj.__defaults__
            old_obj.__kwdefaults__ = new_obj.__kwdefaults__
            old_obj.__annotations__ = new_obj.__annotations__
            old_obj.__doc__ = new_obj.__doc__
            module.__dict__[name] = old_obj

        # --- Classes: patch methods and class-level attributes ---
        elif isinstance(old_obj, type) and isinstance(new_obj, type):
            _hotswap_class(old_obj, new_obj)
            module.__dict__[name] = old_obj


def _hotswap_class(old_cls: type, new_cls: type) -> None:
    """Patch an existing class in place with new methods and attributes."""
    # Remove attributes that were deleted in the new version
    for name in list(vars(old_cls)):
        if name.startswith("__") and name.endswith("__"):
            continue
        if name not in vars(new_cls):
            try:
                delattr(old_cls, name)
            except AttributeError:
                pass

    # Update all attributes from the new class
    for name, new_val in vars(new_cls).items():
        if name in ("__dict__", "__weakref__"):
            continue

        old_val = vars(old_cls).get(name)

        # Hotswap methods in place so existing references work
        if (isinstance(old_val, types.FunctionType)
                and isinstance(new_val, types.FunctionType)):
            old_val.__code__ = new_val.__code__
            old_val.__defaults__ = new_val.__defaults__
            old_val.__kwdefaults__ = new_val.__kwdefaults__
            old_val.__annotations__ = new_val.__annotations__
            old_val.__doc__ = new_val.__doc__
        # staticmethod / classmethod: unwrap, patch inner func, re-wrap
        elif type(old_val) is staticmethod and type(new_val) is staticmethod:
            old_fn = old_val.__func__
            new_fn = new_val.__func__
            old_fn.__code__ = new_fn.__code__
            old_fn.__defaults__ = new_fn.__defaults__
            old_fn.__kwdefaults__ = new_fn.__kwdefaults__
            old_fn.__annotations__ = new_fn.__annotations__
            old_fn.__doc__ = new_fn.__doc__
        elif type(old_val) is classmethod and type(new_val) is classmethod:
            old_fn = old_val.__func__
            new_fn = new_val.__func__
            old_fn.__code__ = new_fn.__code__
            old_fn.__defaults__ = new_fn.__defaults__
            old_fn.__kwdefaults__ = new_fn.__kwdefaults__
            old_fn.__annotations__ = new_fn.__annotations__
            old_fn.__doc__ = new_fn.__doc__
        # Properties: replace wholesale
        elif isinstance(new_val, property):
            setattr(old_cls, name, new_val)
        # Everything else (class vars, constants, nested classes, etc.)
        else:
            try:
                setattr(old_cls, name, new_val)
            except (AttributeError, TypeError):
                pass


_BUILTIN_NAMES = set(dir(builtins))


def _validate_global_names(code, namespace: dict) -> None:
    """Check LOAD_GLOBAL names against namespace + builtins before hotswap.

    Raises NameError for any global reference that can't be resolved,
    preventing a broken function from being patched into the live code.
    Recurses into nested code objects (comprehensions, lambdas, etc.).
    """
    for instr in dis.get_instructions(code):
        if instr.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
            name = instr.argval
            if name not in namespace and name not in _BUILTIN_NAMES:
                raise NameError(f"name '{name}' is not defined")
    # Check nested code objects (comprehensions, inner functions, etc.)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _validate_global_names(const, namespace)


def _validate_local_names(code) -> None:
    """Check for local variables loaded before being stored.

    Catches two cases that compile fine but raise UnboundLocalError:

    1. Typo — variable is never stored anywhere:
        some_int = 42
        print(some_intt)   # NameError-like, but local scope

    2. Load before store — variable is stored later (or in another
       branch), so Python marks it as local, but the load executes
       first in instruction order:
        print(some_int)    # LOAD_FAST — UnboundLocalError
        some_int = 42      # STORE_FAST exists, but too late

    Heuristic: in bytecode instruction order, if the first reference
    to a non-parameter variable is a LOAD_FAST, flag it.  This is
    conservative (may flag branch-dependent code that's actually safe)
    but prevents broken functions from being hotswapped in.

    Recurses into nested code objects (comprehensions, inner functions, etc.).
    """
    # Parameter count: positional + keyword-only + positional-only
    n_params = code.co_argcount + code.co_kwonlyargcount
    if hasattr(code, 'co_posonlyargcount'):
        n_params += code.co_posonlyargcount
    param_names = set(code.co_varnames[:n_params])

    # Track the first reference type for each local variable name.
    # "store" = safe, "load" = potentially unbound.
    first_ref: dict[str, str] = {}
    for instr in dis.get_instructions(code):
        if instr.opname in ("STORE_FAST", "STORE_NAME"):
            if instr.argval not in first_ref:
                first_ref[instr.argval] = "store"
        elif instr.opname in ("LOAD_FAST", "LOAD_FAST_CHECK",
                               "LOAD_FAST_AND_CLEAR"):
            if instr.argval not in first_ref:
                first_ref[instr.argval] = "load"

    for name, ref_type in first_ref.items():
        if name not in param_names and ref_type == "load":
            raise UnboundLocalError(
                f"cannot access local variable '{name}' where it is "
                f"not associated with a value"
            )

    # Check nested code objects (comprehensions, inner functions, etc.)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _validate_local_names(const)


def _recompile(func: types.FunctionType, source: str,
               filename: str) -> None:
    dedented = textwrap.dedent(source)
    unwrapped = inspect.unwrap(func)
    namespace = dict(unwrapped.__globals__)

    current_time = time.time()
    freevars = unwrapped.__code__.co_freevars
    has_closure = bool(freevars and unwrapped.__closure__)

    if has_closure:
        closure_vals = {}
        for name, cell in zip(freevars, unwrapped.__closure__):
            try:
                closure_vals[name] = cell.cell_contents
            except ValueError:
                closure_vals[name] = None

        param_list = ", ".join(freevars)
        wrapper_source = f"def _closure_wrapper({param_list}):\n"
        wrapper_source += textwrap.indent(dedented, "    ")
        wrapper_source += f"\n    return {unwrapped.__name__}\n"

        code = compile(wrapper_source, filename, "exec")
        exec(code, namespace)
        new_func = namespace["_closure_wrapper"](**closure_vals)
    else:
        code = compile(dedented, filename, "exec")
        exec(code, namespace)
        new_func = namespace.get(unwrapped.__name__)

    if new_func is None:
        raise RuntimeError(f"Recompilation produced no function named '{unwrapped.__name__}'")
    if not callable(new_func):
        raise RuntimeError(f"'{unwrapped.__name__}' is {type(new_func).__name__}, not a function")

    new_func = inspect.unwrap(new_func)

    # Validate that all global name lookups resolve before patching.
    # This catches typos like "Outlllj" that would compile but would
    # NameError at runtime - at which it's too late to undo.
    _validate_global_names(new_func.__code__, namespace)
    _validate_local_names(new_func.__code__)

    # Save the original line number before patching - compile() sets
    # co_firstlineno to 1 (because of dedented string), but inspect
    # needs the real line number in the file to find the source.
    original_firstlineno = unwrapped.__code__.co_firstlineno

    unwrapped.__code__ = new_func.__code__
    unwrapped.__defaults__ = new_func.__defaults__
    unwrapped.__kwdefaults__ = new_func.__kwdefaults__
    unwrapped.__annotations__ = new_func.__annotations__
    unwrapped.__doc__ = new_func.__doc__

    # Restore the correct line number so inspect.getsourcelines works
    unwrapped.__code__ = unwrapped.__code__.replace(
        co_firstlineno=original_firstlineno
    )