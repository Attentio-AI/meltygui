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
import inspect
import textwrap
import types

from pathlib import Path
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_conversion.converter_register import converter

import libcst as cst

from src.lsd.gl_gui.view.core_conversion.fileref import FileRef, get_original_value, ValueDict


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

########################## NEW CONVERTERS


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  I/O callbacks                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def load_file_bytes(ref: FileRef) -> bytes:
    return ref.path.read_bytes()


def save_file_bytes(value, ref: FileRef, data: bytes) -> FileRef | None:
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


def save_span_text(ref: FileRef, data: str) -> FileRef:
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
           inverse_of=path_to_dict,)
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
           inverse_of=fileref_to_dict,)
def dict_to_fileref(value: dict) -> str:
    data = value.get("value")
    if data is None:
        raise ValueError("Dict has no 'value' — nothing to write")
    return data




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
    return ref.path.read_bytes()


def save_file_bytes(ref: FileRef, data: bytes) -> FileRef | None:
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
           inverse_of=path_to_dict)
def dict_to_path(value: dict) -> bytes:
    data = value.get("data")
    if data is None:
        raise ValueError("Dict has no 'data' — nothing to write")
    return data.encode("utf-8") if isinstance(data, str) else data


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileRef ↔ dict (line range as plain text)                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty, from_type=FileRef, load_data=load_span_text)
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



def load_span_code(ref: FileRef) -> types.FunctionType:
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.split(newline)
    return newline.join(lines[ref.start:ref.end])


def save_span_code(value, ref: FileRef, data: str) -> FileRef:
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
# ║  FileRef ↔ cst.Module (line range parsed into CST)                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty, from_type=FileRef, load_data=load_span_text, stateful=True, )
def fileref_to_cst_module(value, data: str, ref: FileRef) -> cst.Module:
    return cst.parse_module(data)


@converter(registry=Melty, to_type=FileRef, save_data=save_span_text,
           inverse_of=fileref_to_cst_module)
def cst_module_to_fileref(value: cst.Module) -> str:
    return value.code


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FileRef ↔ cst.Module (line range parsed into CST)                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


# ╚══════════════════════════════════════════════════════════════════════════════╝
def recompile_function(value:types.FunctionType, ref: FileRef, source: str) -> FileRef:
    """Recompile the function in place.  No file write."""
    # func = get_original_value(ref=ref, of_type=types.FunctionType)
    if value is not None:
        _recompile(value, source, str(ref.path))
    return ref

@converter(registry=Melty)
def value_dict_to_cst_module(data: ValueDict) -> cst.Module:
    return cst.parse_module(data.others)

@converter(registry=Melty,
           inverse_of=value_dict_to_cst_module)
def cst_module_to_value_dict(value: cst.Module) -> ValueDict:
    return ValueDict(cached_value=value, others=value.code)

@converter(registry=Melty, from_type=types.FunctionType, load_data=load_span_text)
def function_to_value_dict(value:types.FunctionType, data: str, ref) -> ValueDict:
    return ValueDict(cached_value=value, others=data)


@converter(registry=Melty, from_type=ValueDict, to_type=types.FunctionType,
           save_data=recompile_function,
           inverse_of=function_to_value_dict)
def value_dict_to_function(value: ValueDict) -> types.FunctionType:
    func = value.cached_value
    source = value.others
    return func

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Recompilation                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _recompile(func: types.FunctionType, source: str,
               filename: str) -> None:
    dedented = textwrap.dedent(source)
    unwrapped = inspect.unwrap(func)
    namespace = dict(unwrapped.__globals__)

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