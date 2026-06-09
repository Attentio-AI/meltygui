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
import re as _re
import textwrap
import time
import types
from importlib import reload

from pathlib import Path
from src.lsd.gl_gui.melty import Melty

import libcst as cst

from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace
from src.lsd.gl_gui.view.core_conversion import hotswap_guard as _hotswap_guard
from src.lsd.gl_gui.view.core_conversion.address import Address, invalidate_address_cache, update_address_cache, is_editable_source, shift_sibling_linenos
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import invalidate_usage_cache


def _class_code_objects(cls: type) -> set:
    """All code-object ids reachable from a class's methods — used to attribute a
    runtime traceback frame back to a hotswapped class."""
    ids: set = set()
    for val in vars(cls).values():
        fn = None
        if isinstance(val, types.FunctionType):
            fn = val
        elif isinstance(val, (staticmethod, classmethod)):
            fn = val.__func__
        if fn is not None and isinstance(getattr(fn, "__code__", None), types.CodeType):
            ids |= _hotswap_guard.collect_code_ids(fn.__code__)
    return ids


def _snapshot_class(cls: type) -> type:
    """A throwaway clone of `cls` whose members mirror the current class — copies
    of each method (so its live __code__ is preserved even as the original is
    patched in place) plus a snapshot of every other attribute. Rolling back means
    `_hotswap_class(cls, snapshot)`, which writes these members back over cls."""
    members = {}
    for name, val in list(vars(cls).items()):
        if name in ("__dict__", "__weakref__", "__slots__"):
            continue
        if isinstance(val, types.MemberDescriptorType):
            # Slot descriptors: copying one alongside __slots__ means type()
            # raises "conflicts with class variable", and the original class
            # keeps its own descriptors anyway - nothing to snapshot.
            continue
        if isinstance(val, types.FunctionType):
            clone = types.FunctionType(val.__code__, val.__globals__, val.__name__,
                                       val.__defaults__, val.__closure__)
            clone.__kwdefaults__ = val.__kwdefaults__
            clone.__annotations__ = dict(val.__annotations__ or {})
            clone.__doc__ = val.__doc__
            members[name] = clone
        elif isinstance(val, (staticmethod, classmethod)):
            inner = val.__func__
            clone = types.FunctionType(inner.__code__, inner.__globals__, inner.__name__,
                                       inner.__defaults__, inner.__closure__)
            clone.__kwdefaults__ = inner.__kwdefaults__
            clone.__annotations__ = dict(inner.__annotations__ or {})
            members[name] = type(val)(clone)
        else:
            members[name] = val
    return type(f"_snapshot_{cls.__name__}", (), members)


def _register_hotswap(source, restore, code, line_base=0):
    """Register a successful hotswap with the rollback guard. `code` is either a
    single code object (function) or a set of code-object ids (class/module)."""
    if isinstance(code, types.CodeType):
        code_ids = _hotswap_guard.collect_code_ids(code)
    else:
        code_ids = set(code)
    _hotswap_guard.register(source, restore, code_ids, line_base=line_base)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  I/O callbacks (used as load_data / save_data)                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _detect_newline(data: bytes) -> str:
    """The file's DOMINANT line ending, used as the OUTPUT newline when re-joining
    spliced lines. Count-based (not "CRLF if any CRLF") so a stray CRLF doesn't
    flip a mostly-LF file. Splitting uses `_split_lines` (universal), not this — so
    detection only decides the join, never the line boundaries."""
    crlf = data.count(b"\r\n")
    lone_lf = data.count(b"\n") - crlf
    return "\r\n" if crlf > lone_lf else "\n"


_LINE_SPLIT_RE = _re.compile(r"\r\n|\r|\n")


def _split_lines(text: str):
    """Split on UNIVERSAL newlines (CRLF / CR / LF), matching `str.split('\\n')`'s
    element model (a trailing newline yields a final "" element). The span
    load/save splice MUST split this way: a single detected newline (`_detect_newline`)
    can mismatch the line boundaries when the file has mixed endings OR when the
    libcst-emitted `code_str` uses a different newline than the file — and
    `code_str.split("\\r\\n")` over LF content does NOT split, collapsing a whole
    function span onto one line (the reported comment/code merge). Universal split
    can never fail to break a real line, and it matches the true line numbers that
    getsourcelines/co_firstlineno produce."""
    return _LINE_SPLIT_RE.split(text)


def load_file_bytes(ref: Address) -> bytes:
    return ref.path.read_bytes()


def load_text(ref: Address) -> str:
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = _split_lines(text)
    return newline.join(lines[ref.start:ref.end])


def load_span_text(ref: Address) -> str:
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = _split_lines(text)
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
    if not is_editable_source(ref.path):
        print(f"[save_file_fn] refusing to write library source: {ref.path}")
        return None, ref
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(input_value, str):
        ref.path.write_text(input_value, encoding="utf-8")
    else:
        ref.path.write_bytes(input_value)
    invalidate_usage_cache(ref.path)
    return None, ref


@render_func(save_data=save_file_fn)
def rf_dict_to_path(input_value) -> bytes:
    """File metadata dict → bytes for save."""
    data = input_value.get("data")
    if data is None:
        raise ValueError("Dict has no 'data' — nothing to write")
    return None, data.encode("utf-8") if isinstance(data, str) else data


@render_func(load_data=load_span_text)
def rf_address_to_dict(input_value, data=None) -> dict:
    """Address → text dict."""
    return None, {
        "value": data,
        "__original_value__": data,
    }


@render_func()
def rf_dict_to_address(input_value) -> str:
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
    if not is_editable_source(ref.path):
        print(f"[recompile_fn] refusing to write library source: {ref.path}")
        return None, ref
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
    lines = _split_lines(text)
    new_lines = _split_lines(input_value)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    invalidate_usage_cache(ref.path)

    # An edit that changes the function's line count shifts all function definitions
    # BELOW it down (or up) in the file. Patch their co_firstlineno so the next
    # resolve via inspect.getsourcelines returns truthful line numbers - the same
    # fix _do_save applies in the chain save path. Without it a reload resolves
    # to a stale span (findsource jumps back to the parent def, or to line 0 →
    # wiped to head), which breaks rendering of every function below.
    if ref.start is not None and ref.end is not None:
        delta = (ref.start + len(new_lines)) - ref.end
        shift_sibling_linenos(function_ref if function_ref is not None else ref.source,
                              ref.path, after_lineno=ref.end, delta=delta)

    new_ref = Address(ref.path, ref.start, ref.start + len(new_lines))
    yellow = "\033[93m"
    reset = "\033[0m"
    print(f"{yellow}[File Write] Updated file {ref.path}{reset}")
    if function_ref is not None:
        update_address_cache(function_ref, new_ref)
    else:
        invalidate_address_cache(function_ref)
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
    if not is_editable_source(ref.path):
        print(f"[recompile_mod_fn] refusing to write library source: {ref.path}")
        return None, ref
    if module_ref is not None:
        try:
            _recompile_module(module_ref, input_value, str(ref.path))
        except Exception as e:
            return Pending(originated=recompile_mod_fn, status=str(e),
                           state=PendingState.ERROR), None
    # Module Addresses cover the whole file; write directly
    ref.path.write_text(input_value, encoding="utf-8")
    invalidate_usage_cache(ref.path)
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
def recompile_cls_fn(input_value, ref=None, class_ref=None,
                     hotswap_instances=True):
    """Save handler: hotswap class + write source to disk."""
    from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
    if not is_editable_source(ref.path):
        print(f"[recompile_cls_fn] refusing to write library source: {ref.path}")
        return None, ref
    if class_ref is not None:
        try:
            _recompile_class(class_ref, input_value, str(ref.path))
        except Exception as e:
            return Pending(originated=recompile_cls_fn, status=str(e),
                           state=PendingState.ERROR), None

        if hotswap_instances:
            _patch_instances(class_ref)

    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = _split_lines(text)
    new_lines = _split_lines(input_value)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    invalidate_usage_cache(ref.path)

    # Shift co_firstlineno of every function/class after this one when the one's
    # line count changes, so their next resolve lands on the right span (see the
    # matching block in recompile_fn / _do_save). The edited class's own methods
    # are handled by the recompile in _recompile_class; shift_sibling_linenos skips
    # the saved class and only corrects what comes after it in the file.
    if ref.start is not None and ref.end is not None:
        delta = (ref.start + len(new_lines)) - ref.end
        shift_sibling_linenos(class_ref if class_ref is not None else ref.source,
                              ref.path, after_lineno=ref.end, delta=delta)

    yellow = "\033[93m"
    reset = "\033[0m"
    print(f"{yellow}[File Write] Updated file {ref.path}{reset}")
    new_ref = Address(ref.path, ref.start, ref.start + len(new_lines))
    if class_ref is not None:
        update_address_cache(class_ref, new_ref)
    return None, new_ref


@render_func(save_data=recompile_cls_fn)
def cst_to_cls(input_value):
    """Reverse: cst.Module → source string."""
    return None, input_value.code


# --- Address ↔ cst.Module ---

@render_func(load_data=load_text)
def ref_to_cst(input_value, data=None) -> cst.Module:
    """Forward: Address → cst.Module."""
    return None, cst.parse_module(data)


@render_func()
def save_span_fn(input_value, ref=None):
    """Save handler: write source string back to file span."""
    if not is_editable_source(ref.path):
        print(f"[save_span_fn] refusing to write library source: {ref.path}")
        return None, ref
    full_data = ref.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = _split_lines(text)
    new_lines = _split_lines(input_value)
    lines[ref.start:ref.end] = new_lines
    ref.path.write_text(newline.join(lines), encoding="utf-8")
    yellow = "\033[93m"
    reset = "\033[0m"
    print(f"{yellow}[File Write] Updated file {ref.path}{reset}")
    invalidate_usage_cache(ref.path)
    return None, Address(ref.path, ref.start, ref.start + len(new_lines))


@render_func(save_data=save_span_fn)
def cst_to_ref(input_value):
    """Reverse: cst.Module → source string."""
    return None, input_value.code


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Instance patching (DictConversion)                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _patch_instances(cls: type) -> None:
    """Add new field defaults to live instances after a class hotswap.

    Only touches DictConversion subclasses that track instances via
    _instances WeakSet.  Adds attributes introduced by the edit and
    removes attributes deleted from the class — existing values that
    the user set are never overwritten.
    """
    instances = getattr(cls, '_instances', None)
    if instances is None:
        return

    new_defaults = getattr(cls, '__field_defaults__', {})
    for inst in list(instances):
        # Add new fields the instance doesn't have yet
        for key, default in new_defaults.items():
            if key not in inst.__dict__:
                setattr(inst, key, default)

        # Remove instance attrs that are no longer class fields
        for key in list(inst.__dict__):
            if key.startswith('_'):
                continue
            if (key not in new_defaults
                    and key not in ('id', 'hash', 'name', 'tint')
                    and not hasattr(cls, key)):
                try:
                    delattr(inst, key)
                except AttributeError:
                    pass


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

    # for name, ref_type in first_ref.items():
    #     if name not in param_names and ref_type == "load":
    #         raise UnboundLocalError(
    #             f"cannot access local variable '{name}' where it is "
    #             f"not associated with a value"
    #         )

    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _validate_local_names(const)


def _recompile(func: types.FunctionType, source: str,
               filename: str) -> None:
    dedented = textwrap.dedent(source)
    dedented = "\n" * (func.__code__.co_firstlineno - 1) + dedented
    unwrapped = inspect.unwrap(func)
    namespace = dict(unwrapped.__globals__)

    # Snapshot the decorator registries BEFORE exec re-runs the decorators (which
    # register a throwaway wrapper), restored after the hotswap below.
    _pre_reg = _snapshot_func_registrations()

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
        print_stack_trace()

    if not callable(new_func):
        print_stack_trace()
        return TypeError(f"Recompiled object '{unwrapped.__name__}' is not callable")

    # Capture the freshly-DECORATED object that exec produced: re-running
    # @render_func/@window/etc. registered THIS throwaway object in Melty's
    # registries. We hotswap the original file function in place (below) - keeping
    # `from x import fn` refs to the original wrapper valid - then restore the
    # registries to that original wrapper (see _redirect_function_registrations).
    # Left registered, the throwaway causes stale renders and windows on entering
    # vars(module): its co_firstlineno rots → resolve_address returns start=0.
    new_wrapper = new_func
    new_func = inspect.unwrap(new_func)

    try:
        _validate_global_names(new_func.__code__, namespace)
        _validate_local_names(new_func.__code__)

        original_firstlineno = unwrapped.__code__.co_firstlineno

        # Snapshot the PREVIOUS compiled state before patching, so the hotswap
        # guard can roll it back if the new code throws at runtime (see register).
        _prev = (unwrapped.__code__, unwrapped.__defaults__,
                 unwrapped.__kwdefaults__, dict(unwrapped.__annotations__ or {}),
                 unwrapped.__doc__)

        unwrapped.__code__ = new_func.__code__
        unwrapped.__defaults__ = new_func.__defaults__
        unwrapped.__kwdefaults__ = new_func.__kwdefaults__
        unwrapped.__annotations__ = new_func.__annotations__
        unwrapped.__doc__ = new_func.__doc__

        unwrapped.__code__ = unwrapped.__code__.replace(
            co_firstlineno=original_firstlineno
        )

        _redirect_function_registrations(_pre_reg, new_wrapper,
                                         live_raw=unwrapped, live_func=func)
        # Re-run decoration effects (@defaults) onto the live function: the exec
        # registered them under the throwaway exec produced, so copy them across
        # or the edited decoration will never reach the function the app renders.
        _redirect_function_decorations(new_wrapper, new_func, func, unwrapped)

        # And carry the freshly-evaluated @render_func decoration state (closure
        # config + instance attrs) onto the live wrapper, so editing a decorator
        # line takes effect without rebinding anything (see _transfer_wrapper_state).
        if new_wrapper is not None and getattr(new_wrapper, "__render_func__", False):
            _lw = unwrapped.__globals__.get(unwrapped.__name__)
            if not (callable(_lw) and _lw is not new_wrapper
                    and getattr(_lw, "__render_func__", False)
                    and inspect.unwrap(_lw) is unwrapped):
                _lw = func if (func is not new_wrapper
                               and getattr(func, "__render_func__", False)) else None
            if _lw is not None:
                _transfer_wrapper_state(_lw, new_wrapper, new_func)

        def _restore(u=unwrapped, prev=_prev):
            u.__code__, u.__defaults__, u.__kwdefaults__, ann, u.__doc__ = prev
            u.__annotations__ = dict(ann)
            Melty.cache.invalidate_up_by_func(u, max_depth=10)
        # The installed code's co_firstlineno was reset to the function's file
        # position, so a traceback's line number is file-absolute → subtract
        # (firstlineno - 1) to map back to the editor buffer (1-based).
        _register_hotswap(func, _restore, unwrapped.__code__,
                          line_base=original_firstlineno - 1)

    except Exception as e:
        print_stack_trace(exception=e)
        return e

    Melty.cache.invalidate_up_by_func(func, max_depth=10)


def _exec_file_imports(filename: str, namespace: dict) -> None:
    """Best-effort: exec the file's top-level import lines into `namespace`.

    Used to resolve a name (e.g. a freshly-inserted `@defaults` import) that the
    running module's globals don't have yet. Each import is exec'd in isolation;
    failures are ignored (relative/conditional imports may not stand alone)."""
    try:
        text = Path(filename).read_text(encoding="utf-8")
    except OSError:
        return
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("import ") or s.startswith("from "):
            try:
                exec(s, namespace)
            except Exception:
                pass


def _recompile_class(cls: type, source: str, filename: str) -> None:
    import sys
    dedented = textwrap.dedent(source)

    mod = sys.modules.get(cls.__module__)
    namespace = dict(vars(mod)) if mod is not None else {}

    try:
        code = compile(dedented, filename, "exec")
    except SyntaxError as syntax_e:
        return syntax_e

    try:
        exec(code, namespace)
    except NameError:
        try:
            # A just-inserted import (e.g. the @defaults decorator) isn't in the live
            # module globals yet. Pull in the file's imports and retry once.
            _exec_file_imports(filename, namespace)
            exec(code, namespace)
        except Exception as e:
            return e

    new_cls = namespace.get(cls.__name__)
    if new_cls is None:
        return NameError(f"Class '{cls.__name__}' not found in recompiled code")

    # Snapshot the PREVIOUS compiled state before patching: a throwaway clone whose
    # members (incl. live method code objects) mirror the current class. Rollback
    # re-hotswaps this clone back over cls, keeping methods/attrs in place.
    _prev_cls = _snapshot_class(cls)

    _hotswap_class(cls, new_cls)
    _redirect_class_registrations(cls, new_cls)
    Melty.cache.invalidate_up_by_obj(cls, max_depth=10)

    def _restore(c=cls, snap=_prev_cls):
        _hotswap_class(c, snap)
        _redirect_class_registrations(c, snap)
        Melty.cache.invalidate_up_by_obj(c, max_depth=10)
    # Class bodies compile at buffer-relative line numbers (the editor shows the
    # whole class span starting at line 1), so no line base offset.
    _register_hotswap(cls, _restore, _class_code_objects(new_cls), line_base=0)
    return None


def _recompile_module(module: types.ModuleType, source: str,
                      filename: str) -> None:
    old_attrs = dict(module.__dict__)

    code = compile(source, filename, "exec")
    # Per-member snapshot of the PREVIOUS compiled state, taken BEFORE patching so
    # in-place edits don't clobber it (old_attrs aliases the live objects, whose
    # __code__ we update below). Each entry is a zero-arg restore closure.
    _member_restores = []
    new_code_ids = set()
    try:
        exec(code, module.__dict__)

        for name, old_obj in old_attrs.items():
            new_obj = module.__dict__.get(name)
            if new_obj is old_obj or new_obj is None:
                continue

            if isinstance(old_obj, types.FunctionType) and isinstance(new_obj, types.FunctionType):
                _prev = (old_obj.__code__, old_obj.__defaults__,
                         old_obj.__kwdefaults__, dict(old_obj.__annotations__ or {}),
                         old_obj.__doc__)

                def _restore_fn(o=old_obj, p=_prev):
                    o.__code__, o.__defaults__, o.__kwdefaults__, ann, o.__doc__ = p
                    o.__annotations__ = dict(ann)
                _member_restores.append(_restore_fn)

                old_obj.__code__ = new_obj.__code__
                old_obj.__defaults__ = new_obj.__defaults__
                old_obj.__kwdefaults__ = new_obj.__kwdefaults__
                old_obj.__annotations__ = new_obj.__annotations__
                old_obj.__doc__ = new_obj.__doc__
                module.__dict__[name] = old_obj
                new_code_ids |= _hotswap_guard.collect_code_ids(new_obj.__code__)

            elif isinstance(old_obj, type) and isinstance(new_obj, type):
                _snap = _snapshot_class(old_obj)

                def _restore_cls(o=old_obj, s=_snap):
                    _hotswap_class(o, s)
                    _redirect_class_registrations(o, s)
                    invalidate_address_cache(o)
                _member_restores.append(_restore_cls)

                _hotswap_class(old_obj, new_obj)
                _redirect_class_registrations(old_obj, new_obj)
                invalidate_address_cache(old_obj)
                module.__dict__[name] = old_obj
                new_code_ids |= _class_code_objects(new_obj)
    except Exception as e:
        # Roll back to old attributes on error
        module.__dict__.update(old_attrs)
        print(f"Error recompiling module '{module.__name__}': {e}")
        return e

    # Register a whole-module rollback: re-applying every member restore reverts
    # the module to its last compiled state if any patched member throws at
    # runtime. Module bodies compile at file/buffer line numbers → base 0.
    def _restore_module(restores=tuple(_member_restores)):
        for r in restores:
            try:
                r()
            except Exception as ex:
                print(f"[hotswap_guard] module member restore failed: {ex}")
    _hotswap_guard.register(module, _restore_module, new_code_ids, line_base=0)


def _hotswap_class(old_cls: type, new_cls: type) -> None:
    """Patch an existing class in place with new methods and attributes."""
    # NOTE: do NOT invalidate the address cache here.  The caller
    # (recompile_cls_fn) handles cache updates via update_address_cache.
    # Invalidating here creates a race window where a concurrent
    # background convert-in chain calls to_address, misses the cache,
    # and re-caches the OLD range from inspect.getsourcelines.

    for name in list(vars(old_cls)):
        if name.startswith("__") and name.endswith("__"):
            continue
        if name not in vars(new_cls):
            try:
                delattr(old_cls, name)
            except AttributeError:
                pass

    for name, new_val in vars(new_cls).items():
        if name in ("__dict__", "__weakref__", "_instances"):
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


def _redirect_class_registrations(old_cls: type, new_cls: type) -> None:
    """Repoint decorator-driven global registries from `new_cls` back to `old_cls`.

    `_recompile_class` re-execs the class body, which RE-RUNS its decorators
    (`@window`, `@defaults`, …). Those decorators don't just mutate the class —
    they register it in module-level registries. So the throwaway `new_cls` ends
    up registered while the rest of the app still holds the hotswapped `old_cls`:
    the two diverge, and edits made through the UI (which now draws `new_cls`)
    never reach the object everyone else reads. (Symptom: a class-var toggle like
    `Toggles.profile_mode` silently stops taking effect after a recompile.)

    Hotswap's contract is that `old_cls` stays canonical, so we move every fresh
    registration onto it — keeping the newly-parsed decoration kwargs (e.g. an
    edited `@window(tint=...)`) but bound to the original identity.

    Decorators that merely `setattr` dunders on the class (`@tint`, `@exclude`,
    `@no_save`, …) need no repair — hotswap already copied those onto `old_cls`.
    """
    if new_cls is old_cls:
        return

    # @window - name-keyed; the re-exec OVERWROTE the entry with new_cls.
    wins = getattr(Melty, "annotated_window_classes", None)
    if isinstance(wins, dict):
        entry = wins.get(new_cls.__name__)
        if entry is not None and entry[0] is new_cls:
            wins[new_cls.__name__] = (old_cls, entry[1])  # keep fresh kwargs

    # @defaults - class-keyed; the re-exec added a parallel new_cls entry. Move it
    # onto old_cls (replacing the pre-edit state) and drop the new_cls key.
    for reg_name in ("default_kwargs_by_type",
                     "default_kwargs_by_attrib_type",
                     "default_funcs_by_name_type"):
        reg = getattr(Melty, reg_name, None)
        if isinstance(reg, dict) and new_cls in reg:
            reg[old_cls] = reg.pop(new_cls)


# Registries the function decorators (@render_func is_default_for / converter /
# interrupt_source_for / is_lens_for, @window) write to. Snapshotted before a
# recompile's exec and restored after, so the re-run decorators don't leave a
# throwaway wrapper registered. render_funcs_by_name matters too: that's what
# RenderFuncs.<name> default handles resolve through - left pointing at the
# throwaway, every handle resolved after one recompile would freeze on it
# (later edits patch the original raw, never the throwaway).
_FUNC_REGISTRY_NAMES = ("default_funcs_by_type", "default_funcs_by_name",
                        "type_interrupts", "_converters",
                        "annotated_window_classes",
                        "render_funcs_by_name", "default_lenses_by_type")

# Registries KEYED BY the wrapper object (reverse of the above). The re-run
# decorator files the fresh entry under the throwaway key; move it onto the
# original wrapper so edited converter flags/types take effect.
_FUNC_KEYED_REGISTRY_NAMES = ("_converter_to_type", "converter_flags")


def _snapshot_func_registrations() -> dict:
    """Shallow-copy the function registries BEFORE a recompile's exec, so we can
    tell which entries the re-run decorators overwrote and restore them."""
    snap = {}
    for name in _FUNC_REGISTRY_NAMES:
        reg = getattr(Melty, name, None)
        if isinstance(reg, dict):
            snap[name] = dict(reg)
    return snap


def _redirect_function_registrations(pre_snapshot: dict, new_wrapper,
                                     live_raw=None, live_func=None) -> None:
    """Reconcile the decorator-driven function registries after a recompile.

    The function analog of `_redirect_class_registrations`. `_recompile` re-execs
    a function's source, which RE-RUNS its decorators (`@render_func` is_default_for
    / converter / `interrupt_source_for`, `@window`). Those register a FRESH,
    throwaway wrapper in Melty's registries, while the app keeps editing/calling
    the ORIGINAL wrapper (which `@wraps`-wraps the raw function we hotswap in
    place, lives in vars(module), and `inspect.unwrap`s to the raw so both
    resolve_address and shift_sibling_linenos stay correct). The throwaway is
    doubly wrong: it serves stale renders, and — never being in vars(module) —
    its co_firstlineno rots so resolve_address eventually returns start=0.

    We must point every registration the fresh source declares at the LIVE wrapper,
    NOT the throwaway, AND drop registrations the edit removed. Simply restoring the
    pre-edit entry (the old behaviour) only handled keys that already existed: an
    is_default_for type ADDED by the edit was left pointing at the dead throwaway,
    and one REMOVED was left stale — so editing is_default_for silently lost the new
    type. Reconciling against the live wrapper makes add/change/remove all take.

    live_wrapper resolution: the module-global binding (`name` in the raw's globals)
    IS the wrapper the app holds — we hotswap the raw in place and never rebind the
    name, so it stays valid across runs. We must NOT register the bare raw (the
    editor may hand `_recompile` the raw via draw_state._view_func): draw_any would
    then call it without the injected draw_state/depth/style_manager/meta. Fall back
    to a snapshot value that unwraps to the raw, then to the editor's object.
    """
    if new_wrapper is None:
        return

    def _unwraps_to(v, raw):
        if raw is None:
            return False
        try:
            return inspect.unwrap(v) is raw
        except Exception:
            return False

    # Resolve the live wrapper the app keeps calling for this function.
    live_wrapper = None
    if live_raw is not None:
        cand = getattr(live_raw, "__globals__", {}).get(
            getattr(live_raw, "__name__", None))
        if cand is not None and cand is not new_wrapper and _unwraps_to(cand, live_raw):
            live_wrapper = cand
        if live_wrapper is None:
            for snap in pre_snapshot.values():
                for v in snap.values():
                    if v is not new_wrapper and _unwraps_to(v, live_raw):
                        live_wrapper = v
                        break
                    if (isinstance(v, tuple) and len(v) == 2
                            and v[0] is not new_wrapper and _unwraps_to(v[0], live_raw)):
                        live_wrapper = v[0]
                        break
                if live_wrapper is not None:
                    break
    if live_wrapper is None:
        live_wrapper = live_func
    if live_wrapper is None:
        return  # no safe target - leave the registries untouched

    def _is_live(v):
        return (v is live_wrapper or v is new_wrapper or _unwraps_to(v, live_raw))

    for name, before in pre_snapshot.items():
        reg = getattr(Melty, name, None)
        if not isinstance(reg, dict):
            continue
        is_window = (name == "annotated_window_classes")

        # Keys the fresh exec just registered (value points at the throwaway).
        fresh = {}
        for key, val in list(reg.items()):
            if is_window:
                if isinstance(val, tuple) and len(val) == 2 and val[0] is new_wrapper:
                    fresh[key] = val[1]  # keep the fresh @window kwargs
            elif val is new_wrapper:
                fresh[key] = None

        # Keys this function owned BEFORE the edit.
        old_keys = set()
        for key, val in before.items():
            if is_window:
                if isinstance(val, tuple) and len(val) == 2 and _is_live(val[0]):
                    old_keys.add(key)
            elif _is_live(val):
                old_keys.add(key)

        # Install every fresh registration under the LIVE wrapper.
        for key, win_kwargs in fresh.items():
            reg[key] = (live_wrapper, win_kwargs) if is_window else live_wrapper

        # Drop registrations the edit removed (owned before, not re-registered now).
        # Guard against clobbering an entry another function has since claimed.
        for key in old_keys - set(fresh):
            cur = reg.get(key)
            if is_window:
                if isinstance(cur, tuple) and len(cur) == 2 and _is_live(cur[0]):
                    reg.pop(key, None)
            elif _is_live(cur):
                reg.pop(key, None)

    # Non-registereded registries: the fresh entry sits under the throwaway key;
    # re-key it onto the live wrapper so edited flags/types take effect.
    for reg_name in _FUNC_KEYED_REGISTRY_NAMES:
        reg = getattr(Melty, reg_name, None)
        if isinstance(reg, dict) and new_wrapper in reg:
            reg[live_wrapper] = reg.pop(new_wrapper)


def _transfer_wrapper_state(live_wrapper, new_wrapper, new_raw) -> None:
    """Copy decoration-time state from the freshly-exec'd wrapper onto the LIVE one.

    @render_func computes its config ONCE at decoration time — o_kwargs,
    header_defaults, wanted_params, the param-injection tables — into the
    wrapper's closure cells and a few wrapper attributes. Hotswap patches the
    raw function's __code__ in place and keeps the ORIGINAL wrapper canonical,
    so without this transfer an edited decorator line
    (`@render_func(tint=..., use_cache=...)`) or a changed signature default
    re-runs onto the throwaway wrapper only and never reaches the wrapper the
    app actually calls — "decorations aren't rerun".

    Both wrappers are instances of the SAME core_render `wrapper` code object,
    so their co_freevars align cell-for-cell. Copy every cell EXCEPT the
    identity ones: a cell holding the throwaway raw (`func`) must keep pointing
    at the live raw we patch in place, and a self-reference cell (`wrapper`)
    must keep pointing at the live wrapper."""
    lc = getattr(live_wrapper, "__closure__", None)
    nc = getattr(new_wrapper, "__closure__", None)
    if (live_wrapper.__code__ is not new_wrapper.__code__
            or lc is None or nc is None or len(lc) != len(nc)):
        return
    for live_cell, new_cell in zip(lc, nc):
        try:
            content = new_cell.cell_contents
        except ValueError:
            continue
        if content is new_raw or content is new_wrapper:
            continue
        try:
            live_cell.cell_contents = content
        except ValueError:
            pass

    # Decorator-set wrapper ATTRIBUTES (not closure): chain-dispatch handles,
    # search flag, header defaults. Copy fresh values; drop ones the edit
    # removed. NEVER __wrapped__ - it must keep pointing at the live raw.
    for attr in ("_load_data", "_save_data", "_searchable",
                 "__header_defaults__", "__params__"):
        if hasattr(new_wrapper, attr):
            try:
                setattr(live_wrapper, attr, getattr(new_wrapper, attr))
            except (AttributeError, TypeError):
                pass
        elif attr in ("_load_data", "_save_data", "_searchable"):
            try:
                delattr(live_wrapper, attr)
            except AttributeError:
                pass


def _redirect_function_decorations(new_wrapper, new_raw,
                                   live_wrapper, live_raw) -> None:
    """Re-key the `@defaults`-style registrations a function recompile re-ran.

    The function analog of the `@defaults` handling in
    `_redirect_class_registrations`. `_recompile`'s exec re-runs the function's
    decorators, so a `@defaults(...)` above it (functions can carry it too — e.g.
    `icon_tint` in toggles.py) re-registers, but KEYED TO THE THROWAWAY function
    exec produced (`@defaults` keys by the object it decorates). Like the wrapper
    registries (`_redirect_function_registrations`, which re-points each entry at the
    LIVE wrapper rather than the throwaway), we want the FRESH value to win —
    otherwise editing a function's `@defaults` decoration would register the new
    value under the throwaway and leave the live function on the stale one. The
    difference is keying: those registries hold the wrapper, these hold the value
    keyed BY the function, so we move/clear by the live function object instead.

    To make EVERY run (not just the first) reflect exactly the current source, we
    fully swap rather than merge: pull the freshly-registered entry off the
    throwaway, drop the live function's prior entry under EITHER key, then reinstall
    the fresh one under the matching live key. This way a decoration whose value
    changed is updated, and one that was deleted entirely leaves no fresh entry, so
    the stale value is simply cleared instead of lingering across runs. Depending on
    decorator order `@defaults` keys by the wrapper or the raw function, so check
    both; clearing both live keys also sidesteps the no-wrapper case (wrapper IS
    raw) double-popping the entry we just installed.
    """
    pairs = ((new_wrapper, live_wrapper), (new_raw, live_raw))
    for reg_name in ("default_kwargs_by_type",
                     "default_kwargs_by_attrib_type",
                     "default_funcs_by_name_type"):
        reg = getattr(Melty, reg_name, None)
        if not isinstance(reg, dict):
            continue
        # Lift the fresh entry off whichever throwaway key the decorator used, and
        # remember the live key it should land on (wrapper-keyed → live wrapper,
        # raw-keyed → live raw, matching where the prior import-time registration
        # - and thus the render-time lookup - lives).
        fresh, target = None, None
        for new_key, live_key in pairs:
            if new_key is not None and new_key in reg:
                fresh, target = reg.pop(new_key), live_key
                break
        # Drop the live function's stale entry under either key (handles a
        # removed/renamed decoration), then reinstall the fresh one if present.
        for live_key in (live_wrapper, live_raw):
            if live_key is not None:
                reg.pop(live_key, None)
        if fresh is not None and target is not None:
            reg[target] = fresh
