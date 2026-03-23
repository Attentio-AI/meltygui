"""
File-based converters for the Melty framework.

All converters are @render_func decorated, supporting both rendering
and background-thread converter modes. Each converter gets its own
draw_state, caching, and parameter injection.

I/O callbacks (load_text, load_file_bytes, etc.) are used as load_data
parameters on forward converters. Save handlers (recompile_fn, etc.)
are used as save_data parameters on reverse converters.
"""
import builtins
import dis
import inspect
import json
import textwrap
import time
import types
from importlib import reload

from pathlib import Path
from src.lsd.gl_gui.melty import Melty

import libcst as cst

from src.lsd.gl_gui.view.core_conversion.fileref import FileRef, invalidate_fileref_cache, update_fileref_cache


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  I/O callbacks (used as load_data / save_data)                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _detect_newline(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def load_file_bytes(ref: FileRef) -> bytes:
    return ref.path.read_bytes()


def load_text(ref: FileRef) -> str:
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.split(newline)
    return newline.join(lines[ref.start:ref.end])


def load_span_text(ref: FileRef) -> str:
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.split(newline)
    return newline.join(lines[ref.start:ref.end])


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  @render_func converters                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

from src.lsd.gl_gui.view.core_views.core_render import render_func


# --- Basic type converters ---

@render_func()
def rf_path_to_bytes(input_value) -> bytes:
    """Path → bytes."""
    p = Path(input_value)
    if not p.exists():
        raise FileNotFoundError(f"No such file: {p}")
    return None, p.read_bytes()


@render_func()
def rf_bytes_to_str(input_value) -> str:
    """bytes → str (UTF-8, falls back to latin-1)."""
    try:
        return None, input_value.decode("utf-8")
    except UnicodeDecodeError:
        return None, input_value.decode("latin-1")


@render_func()
def rf_str_to_bytes(input_value) -> bytes:
    """str → bytes (UTF-8)."""
    return None, input_value.encode("utf-8")


@render_func()
def rf_str_to_dict(input_value) -> dict:
    """str → dict (JSON parse)."""
    try:
        return None, json.loads(input_value)
    except (TypeError, ValueError) as e:
        return None, {"error": f"Value is not JSON-serializable: {e}"}


@render_func()
def rf_dict_to_str(input_value) -> str:
    """dict → str."""
    try:
        return None, str(input_value)
    except (TypeError, ValueError) as e:
        return None, f"Error serializing dict to JSON: {e}"


# --- File I/O converters ---

@render_func(load_data=load_file_bytes)
def rf_path_to_dict(input_value, data=None, ref=None) -> dict:
    """Path → file metadata dict."""
    return None, {
        "name": ref.path.name,
        "stem": ref.path.stem,
        "suffix": ref.path.suffix,
        "size": len(data),
        "modified": ref.path.stat().st_mtime,
        "data": data,
        "__path__": ref.path,
        "__original_data__": data,
    }

# Keep old name as alias for backward compat
path_to_dict = rf_path_to_dict


@render_func()
def save_file_fn(input_value, ref=None):
    """Save handler: write bytes back to disk."""
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(input_value, str):
        ref.path.write_text(input_value, encoding="utf-8")
    else:
        ref.path.write_bytes(input_value)
    return None, ref


@render_func(save_data=save_file_fn)
def rf_dict_to_path(input_value) -> bytes:
    """File metadata dict → bytes for save."""
    data = input_value.get("data")
    if data is None:
        raise ValueError("Dict has no 'data' — nothing to write")
    return None, data.encode("utf-8") if isinstance(data, str) else data


@render_func(load_data=load_span_text)
def rf_fileref_to_dict(input_value, data=None) -> dict:
    """FileRef → text dict."""
    return None, {
        "value": data,
        "__original_value__": data,
    }


@render_func()
def rf_dict_to_fileref(input_value) -> str:
    """Text dict → str for save."""
    data = input_value.get("value")
    if data is None:
        raise ValueError("Dict has no 'value' — nothing to write")
    return None, data


# --- FunctionType ↔ cst.Module ---

@render_func(load_data=load_text)
def fn_to_cst(input_value, data=None) -> cst.Module:
    """Forward: function → cst.Module."""
    return None, cst.parse_module(data)


@render_func()
def recompile_fn(input_value, ref=None, function_ref=None):
    """Save handler: hotswap function + write source to disk."""
    from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
    if function_ref is not None:
        try:
            _recompile(function_ref, input_value, str(ref.path))
        except Exception as e:
            return Pending(originated=recompile_fn, status=str(e),
                           state=PendingState.ERROR), None

    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)
    new_lines = input_value.split(newline)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    new_ref = FileRef(ref.path, ref.start, ref.start + len(new_lines))
    if function_ref is not None:
        update_fileref_cache(function_ref, new_ref)
    else:
        invalidate_fileref_cache(function_ref)
    return None, new_ref


@render_func(save_data=recompile_fn)
def cst_to_fn(input_value):
    """Reverse: cst.Module → source string."""
    return None, input_value.code


# --- types.ModuleType ↔ cst.Module ---

@render_func(load_data=load_text)
def mod_to_cst(input_value, data=None) -> cst.Module:
    """Forward: module → cst.Module."""
    return None, cst.parse_module(data)


@render_func()
def recompile_mod_fn(input_value, ref=None, module_ref=None):
    """Save handler: hotswap module + write source to disk."""
    from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
    if module_ref is not None:
        try:
            _recompile_module(module_ref, input_value, str(ref.path))
        except Exception as e:
            return Pending(originated=recompile_mod_fn, status=str(e),
                           state=PendingState.ERROR), None
    # Module FileRefs cover the whole file so write directly
    ref.path.write_text(input_value, encoding="utf-8")
    return None, ref


@render_func(save_data=recompile_mod_fn)
def cst_to_mod(input_value):
    """Reverse: cst.Module → source string."""
    return None, input_value.code

# Keep old name as alias
recompile_module = recompile_mod_fn


# --- type (class) ↔ cst.Module ---

@render_func(load_data=load_text)
def cls_to_cst(input_value, data=None) -> cst.Module:
    """Forward: class → cst.Module."""
    return None, cst.parse_module(data)


@render_func()
def recompile_cls_fn(input_value, ref=None, class_ref=None):
    """Save handler: hotswap class + write source to disk."""
    from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
    if class_ref is not None:
        try:
            _recompile_class(class_ref, input_value, str(ref.path))
        except Exception as e:
            return Pending(originated=recompile_cls_fn, status=str(e),
                           state=PendingState.ERROR), None
    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)
    new_lines = input_value.split(newline)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    new_ref = FileRef(ref.path, ref.start, ref.start + len(new_lines))
    if class_ref is not None:
        update_fileref_cache(class_ref, new_ref)
    return None, new_ref


@render_func(save_data=recompile_cls_fn)
def cst_to_cls(input_value):
    """Reverse: cst.Module → source string."""
    return None, input_value.code


# --- FileRef ↔ cst.Module ---

@render_func(load_data=load_text)
def ref_to_cst(input_value, data=None) -> cst.Module:
    """Forward: FileRef → cst.Module."""
    return None, cst.parse_module(data)


@render_func()
def save_span_fn(input_value, ref=None):
    """Save handler: write source string back to file span."""
    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)
    new_lines = input_value.split(newline)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    return None, FileRef(ref.path, ref.start, ref.start + len(new_lines))


@render_func(save_data=save_span_fn)
def cst_to_ref(input_value):
    """Reverse: cst.Module → source string."""
    return None, input_value.code


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Backward compatibility aliases                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# Old names still imported by other modules
bytes_to_str = rf_bytes_to_str
recompile = recompile_fn


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Internal : recompilation / hotswap                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_BUILTIN_NAMES = set(dir(builtins))


def _validate_global_names(code, namespace: dict) -> None:
    """Check LOAD_GLOBAL names against namespace + builtins before hotswap."""
    for instr in dis.get_instructions(code):
        if instr.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
            name = instr.argval
            if name not in namespace and name not in _BUILTIN_NAMES:
                raise NameError(f"name '{name}' is not defined")
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _validate_global_names(const, namespace)


def _validate_local_names(code) -> None:
    """Check for local variables loaded before being stored."""
    n_params = code.co_argcount + code.co_kwonlyargcount
    if hasattr(code, 'co_posonlyargcount'):
        n_params += code.co_posonlyargcount
    param_names = set(code.co_varnames[:n_params])

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

    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _validate_local_names(const)


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

    _validate_global_names(new_func.__code__, namespace)
    _validate_local_names(new_func.__code__)

    original_firstlineno = unwrapped.__code__.co_firstlineno

    unwrapped.__code__ = new_func.__code__
    unwrapped.__defaults__ = new_func.__defaults__
    unwrapped.__kwdefaults__ = new_func.__kwdefaults__
    unwrapped.__annotations__ = new_func.__annotations__
    unwrapped.__doc__ = new_func.__doc__

    unwrapped.__code__ = unwrapped.__code__.replace(
        co_firstlineno=original_firstlineno
    )


def _recompile_class(cls: type, source: str, filename: str) -> None:
    import sys
    dedented = textwrap.dedent(source)

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
    old_attrs = dict(module.__dict__)

    code = compile(source, filename, "exec")
    exec(code, module.__dict__)

    for name, old_obj in old_attrs.items():
        new_obj = module.__dict__.get(name)
        if new_obj is old_obj or new_obj is None:
            continue

        if isinstance(old_obj, types.FunctionType) and isinstance(new_obj, types.FunctionType):
            old_obj.__code__ = new_obj.__code__
            old_obj.__defaults__ = new_obj.__defaults__
            old_obj.__kwdefaults__ = new_obj.__kwdefaults__
            old_obj.__annotations__ = new_obj.__annotations__
            old_obj.__doc__ = new_obj.__doc__
            module.__dict__[name] = old_obj

        elif isinstance(old_obj, type) and isinstance(new_obj, type):
            _hotswap_class(old_obj, new_obj)
            module.__dict__[name] = old_obj


def _hotswap_class(old_cls: type, new_cls: type) -> None:
    """Patch an existing class in place with new methods and attributes."""
    invalidate_fileref_cache(old_cls)

    for name in list(vars(old_cls)):
        if name.startswith("__") and name.endswith("__"):
            continue
        if name not in vars(new_cls):
            try:
                delattr(old_cls, name)
            except AttributeError:
                pass

    for name, new_val in vars(new_cls).items():
        if name in ("__dict__", "__weakref__"):
            continue

        old_val = vars(old_cls).get(name)

        if (isinstance(old_val, types.FunctionType)
                and isinstance(new_val, types.FunctionType)):
            old_val.__code__ = new_val.__code__
            old_val.__defaults__ = new_val.__defaults__
            old_val.__kwdefaults__ = new_val.__kwdefaults__
            old_val.__annotations__ = new_val.__annotations__
            old_val.__doc__ = new_val.__doc__
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
        elif isinstance(new_val, property):
            setattr(old_cls, name, new_val)
        else:
            try:
                setattr(old_cls, name, new_val)
            except (AttributeError, TypeError):
                pass
