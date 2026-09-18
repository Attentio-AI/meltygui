"""
File-based converters for the Melty framework.

All converters are @render_func decorated, supporting both rendering
and background-thread converter modes. Each converter gets its own
draw_state, caching, and parameter injection.

I/O callbacks (load_text, load_file_bytes, etc.) are used as load_data
parameters on forward converters. Save handlers (recompile_fn, etc.)
are used as save_data parameters on reverse converters.
"""
import ast
import builtins
from meltygui.core.diagnostics.notifications import lag_traced

import dis
import inspect
import json
import re as _re
import textwrap
import time
import types
from enum import EnumMeta
from importlib import reload

from pathlib import Path
from meltygui.core.melty import Melty
from meltygui.core.definition_hotswap import patch_function
from meltygui.core.definition_hotswap import canonicalize_definitions

import libcst as cst

from meltygui.core.windowing.glfw_utils import print_stack_trace
import meltygui.code.hotswap_guard as _hotswap_guard
from meltygui.code.fileref import Address
from meltygui.code.fileref import invalidate_address_cache
from meltygui.code.fileref import update_address_cache
from meltygui.code.fileref import is_editable_source
from meltygui.code.fileref import shift_sibling_linenos
from meltygui.code.libcst_conversion import invalidate_usage_cache


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
            if val.__dict__.get("__melty_relocated__"):
                val = inspect.unwrap(val)
            clone = types.FunctionType(val.__code__, val.__globals__, val.__name__,
                                       val.__defaults__, val.__closure__)
            clone.__kwdefaults__ = val.__kwdefaults__
            clone.__annotations__ = dict(val.__annotations__ or {})
            clone.__doc__ = val.__doc__
            clone.__qualname__ = val.__qualname__
            members[name] = clone
        elif isinstance(val, (staticmethod, classmethod)):
            inner = val.__func__
            if inner.__dict__.get("__melty_relocated__"):
                inner = inspect.unwrap(inner)
            clone = types.FunctionType(inner.__code__, inner.__globals__, inner.__name__,
                                       inner.__defaults__, inner.__closure__)
            clone.__kwdefaults__ = inner.__kwdefaults__
            clone.__annotations__ = dict(inner.__annotations__ or {})
            clone.__qualname__ = inner.__qualname__
            members[name] = type(val)(clone)
        else:
            members[name] = val
    # Enum members are shared BY IDENTITY across the app and are patched IN
    # PLACE by _reconcile_enum_members (see there) - so the plain alias stored
    # here is NOT a snapshot: it's the same object, and it will already carry
    # the new value by the time a rollback runs. Snapshot their state instead;
    # _hotswap_class recognises this key as the rollback direction.
    if isinstance(cls, EnumMeta):
        members["_enum_member_state_"] = {
            name: dict(vars(m)) for name, m in cls.__members__.items()}
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

from meltygui.core.core_render import render_func


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
def rf_diclaudct_to_str(input_value) -> str:
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
    from meltygui.core.conversion.path_finder import Pending
    from meltygui.core.conversion.path_finder import PendingState
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
    from meltygui.core.conversion.path_finder import Pending
    from meltygui.core.conversion.path_finder import PendingState
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
    from meltygui.core.conversion.path_finder import Pending
    from meltygui.core.conversion.path_finder import PendingState
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

def _patch_constructor_literals(cls: type, source: str) -> None:
    """Backfill new literal state fields without rerunning live constructors.

    Only unconditional ``self.field = <literal>`` assignments are safe to
    initialize without constructor arguments or side effects. Existing values,
    including runtime edits and fields with computed initializers, stay intact.
    """
    import copy
    instances = vars(cls).get('_instances')
    if instances is None or not instances:
        return
    node = ast.parse(source)
    for name in cls.__qualname__.split('.'):
        node = next((child for child in node.body
                     if isinstance(child, ast.ClassDef) and child.name == name), None)
        if node is None:
            return
    init = next((child for child in node.body
                 if isinstance(child, ast.FunctionDef) and child.name == '__init__'), None)
    if init is None or not init.args.args:
        return
    self_name = init.args.args[0].arg
    for statement in init.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        else:
            continue
        try:
            default = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
        for target in targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == self_name):
                for instance in list(instances):
                    if target.attr not in vars(instance):
                        setattr(instance, target.attr, copy.deepcopy(default))


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
# ║  Internal : recompilation / hotswap                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_BUILTIN_NAMES = set(dir(builtins))


def _backfill_declared_imports(ns, filename) -> bool:
    """Exec into `ns` every module-scope import the file's CURRENT text
    (pending-inclusive) declares whose bound name is missing there. True when
    anything was added.

    Heals a STALE MODULE TWIN: the same file lives in sys.modules under two
    identities (src./non-src — see the dual-identity memory), and a twin
    imported before an import line was added to the file never re-runs it.
    Re-exec'ing a span in that twin's globals then NameErrors on the newer
    name at its decorator/default line (`@defaults(...)` was the hunt).
    Import statements only, missing names only — same live-heal the
    auto-import quick-fix applies."""
    import ast
    try:
        from meltygui.editor.pending_save import PendingSave
        text = PendingSave.current_file_text(Path(filename))
        if text is None:
            with open(filename, encoding="utf-8", errors="replace") as f:
                text = f.read()
        tree = ast.parse(text)
    except Exception:
        return False
    added = False
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        bound = [a.asname or (a.name.partition(".")[0]
                              if isinstance(node, ast.Import) else a.name)
                 for a in node.names if a.name != "*"]
        if not bound or all(b in ns for b in bound):
            continue
        seg = ast.get_source_segment(text, node)
        if not seg:
            continue
        try:
            exec(compile(textwrap.dedent(seg), str(filename), "exec"), ns)
            added = True
        except Exception:
            continue                # a failing import should never break recompile
    return added


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


@lag_traced("recompile fn (hotswap)", 50)
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
    else:
        code = compile(dedented, filename, "exec")

    def _exec_new():
        # annotation scope: a default arg / annotation that CALLS a render func
        # must return its carrier, not render on this (non-GL) thread.
        with Melty.annotation_scope():
            exec(code, namespace)
            if has_closure:
                return namespace["_closure_wrapper"](**closure_vals)
        return namespace.get(unwrapped.__name__)

    try:
        new_func = _exec_new()
    except NameError:
        # A stale module twin's globals may miss an import the file's editor
        # text declares (the re-run decorator/default is what trips it) -
        # backfill the REAL module namespace and retry once, so the hotswapped
        # body resolves the imports at runtime too.
        if not _backfill_declared_imports(unwrapped.__globals__, filename):
            raise
        for k, v in unwrapped.__globals__.items():
            namespace.setdefault(k, v)
        new_func = _exec_new()

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

        # Mirror the patch onto the file's other module identity (src./non-src
        # twin), so registry-resolved callers (RenderFuncs.<name>) update no
        # matter which twin's raw the editor resolved.
        _twins = _patch_twin_raws(unwrapped)

        _live_wrapper = _redirect_function_registrations(
            _pre_reg, new_wrapper, live_raw=unwrapped, live_func=func,
            new_raw=new_func)
        # Re-run decoration effects (@defaults) onto the live function: the exec
        # registered them under the throwaway exec produced, so copy them across
        # or the edited decoration will never reach the function the app renders.
        # Key on the resolved LIVE wrapper (the module-global the app calls),
        # not `func` - the editor may hand _recompile the raw, and an entry
        # left under the stale wrapper key would shadow the fresh one.
        _redirect_function_decorations(new_wrapper, new_func,
                                       _live_wrapper or func, unwrapped)

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

        def _restore(u=unwrapped, prev=_prev, twins=tuple(_twins)):
            u.__code__, u.__defaults__, u.__kwdefaults__, ann, u.__doc__ = prev
            u.__annotations__ = dict(ann)
            _restore_twin_raws(twins)
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
    _announce_hotswap(func)


def _announce_hotswap(live) -> None:
    """A function, class or module was swapped in place: its live values may
    have moved. The blit invalidations beside each call repaint the views
    drawn FROM the object; this tells the consumer that mirrors it as data
    (CodeDict registers 'definition_hotswapped')."""
    try:
        from meltygui.core.runtime.extensions import call
        call('definition_hotswapped', live)
    except Exception as e:
        print_stack_trace(exception=e)


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


@lag_traced("recompile class (hotswap)", 50)
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
        # annotation_scope: the class body re-evaluates field annotations that
        # CALL render funcs (`tint: draw_any(...)`) - they must return carriers
        # (annotation_track), not render on this background thread (no GL
        # context → FBO failure + imgui ID-stack corruption on the render
        # thread). Thread-local, so live rendering elsewhere is untouched.
        with Melty.annotation_scope():
            exec(code, namespace)
    except NameError:
        try:
            # A just-inserted import (e.g. the @defaults decorator) isn't in the live
            # module globals yet. Pull in the file's imports and retry once.
            with Melty.annotation_scope():
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

    _hotswap_class(cls, new_cls, src_map=_attr_source_map(dedented), qualname=cls.__name__)
    _redirect_class_registrations(cls, new_cls)
    Melty.cache.invalidate_up_by_obj(cls, max_depth=10)
    _announce_hotswap(cls)

    def _restore(c=cls, snap=_prev_cls):
        _hotswap_class(c, snap, force=True)
        _redirect_class_registrations(c, snap)
        Melty.cache.invalidate_up_by_obj(c, max_depth=10)
        _announce_hotswap(c)
    # Class bodies compile at buffer-relative line numbers (the editor shows the
    # whole class span starting at line 1), so no line base offset.
    _register_hotswap(cls, _restore, _class_code_objects(new_cls), line_base=0)
    return None


def module_for_path(path):
    """The live module loaded from `path` (resolved), or None.

    Whole-file editors resolve through TextFileCodec, whose Address carries the
    PATH as `source` — there is no module object to dispatch on until hotswap
    time, so recompile callers resolve it here (same scan as the MCP server's
    hotswap_file)."""
    import sys
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return None
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            if Path(f).resolve() == target:
                return mod
        except (OSError, ValueError):
            continue
    return None


@lag_traced("recompile module (hotswap)", 50)
def _recompile_module(module: types.ModuleType, source: str,
                      filename: str) -> None:
    old_attrs = dict(module.__dict__)

    code = compile(source, filename, "exec")
    # The exec below re-runs every decorator in the module, registering throwaway
    # wrappers in Melty's registries (same problem as _recompile). Snapshot the
    # registries first so each function's registrations can be reconciled back to
    # its live wrapper after patching.
    _pre_reg = _snapshot_func_registrations()
    # The exec also re-runs the module's `extensions.register(...)` lines with the
    # throwaway functions. Success points those services at the live functions;
    # a rollback restores them, so a new callback never runs against old classes.
    from meltygui.core.runtime import extensions
    _pre_services = dict(extensions._services)
    # Per-member snapshot of the PREVIOUS compiled state, taken BEFORE patching so
    # in-place edits don't clobber it (old_attrs aliases the live objects, whose
    # __code__ we update below). Each entry is a zero-arg restore closure.
    _member_restores = []
    _swapped_funcs = []
    _swapped_classes = []
    new_code_ids = set()
    src_map = _attr_source_map(source)
    module_baseline = old_attrs.get("__hotswap_attr_src__")
    live_by_name = {}
    try:
        # annotation_scope: module bodies hold @window classes whose field
        # annotations CALL render funcs - same interception _recompile_class
        # needs, thread-local so live rendering is untouched.
        with Melty.annotation_scope():
            exec(code, module.__dict__)

        new_attrs = dict(module.__dict__)
        replacements = {
            id(new_attrs[name]): (new_attrs[name], old)
            for name, old in old_attrs.items()
            if name in new_attrs and new_attrs[name] is not old
            and ((isinstance(old, types.FunctionType) and isinstance(new_attrs[name], types.FunctionType))
                 or (isinstance(old, type) and isinstance(new_attrs[name], type)))
        }
        # Enum members are also identity-bearing definitions. Module tuples
        # such as LEFT_ANCHORS are evaluated with the throwaway enum during
        # exec; rebinding only the class leaves live window anchors outside
        # every classification tuple after a state-module hotswap.
        for new_definition, live_definition in list(replacements.values()):
            if isinstance(new_definition, EnumMeta) and isinstance(live_definition, EnumMeta):
                for member_name, member in new_definition.__members__.items():
                    live_member = live_definition.__members__.get(member_name)
                    if live_member is not None:
                        replacements[id(member)] = (member, live_member)
        canonicalize_definitions(replacements)
        for service, callback in list(extensions._services.items()):
            replacement = replacements.get(id(callback))
            if replacement is not None and replacement[0] is callback:
                extensions._services[service] = replacement[1]

        for name, old_obj in old_attrs.items():
            new_obj = new_attrs.get(name)
            if new_obj is old_obj or new_obj is None:
                continue

            if isinstance(old_obj, types.FunctionType) and isinstance(new_obj, types.FunctionType):
                # Patch the RAW function. For decorated functions (@render_func,
                # @wraps) old/new are both generic wrapper closures sharing the
                # same wrapper code object - copying that __code__ is a no-op and
                # the old wrapper keeps calling its OLD inner via its closure
                # cell. The behavior lives in the inner raw, so patch that.
                old_raw = inspect.unwrap(old_obj)
                new_raw = inspect.unwrap(new_obj)
                both_wrapped = old_raw is not old_obj and new_raw is not new_obj
                tgt, src = (old_raw, new_raw) if both_wrapped else (old_obj, new_obj)

                _member_restores.append(patch_function(tgt, src))
                old_obj.__module__ = new_obj.__module__
                old_obj.__qualname__ = new_obj.__qualname__
                module.__dict__[name] = old_obj
                # Mirror onto the function's other module identity (src./non-src
                # twin) so registry-resolved addresses update too.
                _twins = [] if tgt.__dict__.get("__melty_relocated__") else _patch_twin_raws(tgt)
                if _twins:
                    def _restore_twins_fn(t=tuple(_twins)):
                        _restore_twin_raws(t)
                    _member_restores.append(_restore_twins_fn)
                new_code_ids |= _hotswap_guard.collect_code_ids(src.__code__)

                # Reconcile the throwaway wrapper's registrations and decorator
                # state back onto the live objects - the same contract as
                # _recompile. Without this, render_funcs_by_name (and friends)
                # point at the throwaway while draw_state._view_func and the
                # module global keep executing the live one; whichever side a
                # later edit reaches, the other freezes.
                _redirect_function_registrations(_pre_reg, new_obj,
                                                 live_raw=old_raw, live_func=old_obj,
                                                 new_raw=new_raw)
                _redirect_function_decorations(new_obj, new_raw, old_obj, old_raw)
                if both_wrapped and getattr(new_obj, "__render_func__", False):
                    _transfer_wrapper_state(old_obj, new_obj, new_raw)
                # The reload shifts line numbers; a cached Address would make the
                # editor's next span-resolved save splice at stale offsets
                # (symptom: the function tail duplicated on every save).
                invalidate_address_cache(old_obj)
                if both_wrapped:
                    invalidate_address_cache(old_raw)
                _swapped_funcs.append(old_obj)
                live_by_name[name] = old_obj

            elif isinstance(old_obj, type) and isinstance(new_obj, type):
                _snap = _snapshot_class(old_obj)

                def _restore_cls(o=old_obj, s=_snap):
                    _hotswap_class(o, s, force=True)
                    _redirect_class_registrations(o, s)
                    invalidate_address_cache(o)
                _member_restores.append(_restore_cls)

                # Rebind the module attr to the LIVE class FIRST. The exec left
                # the throwaway bound there, and the patch below can run code
                # that accesses the that state - a Modes._LazyMode resolving
                # `Mode[...]` during the enum reconcile latched a member of the
                # throwaway and cached it forever (the mode edit then applied
                # everywhere EXCEPT views holding a lazy handle). old_obj is
                # what the binding ends up as regardless, so moving it up is
                # free and closes the window for every hotswappable class.
                module.__dict__[name] = old_obj
                class_source_map = src_map
                if new_obj.__module__ != module.__name__:
                    import sys
                    owner = sys.modules.get(new_obj.__module__)
                    owner_path = vars(owner).get("__file__") if owner is not None else None
                    if owner_path is not None:
                        class_source_map = _attr_source_map(Path(owner_path).read_text())
                _hotswap_class(old_obj, new_obj, src_map=class_source_map, qualname=new_obj.__qualname__)
                _redirect_class_registrations(old_obj, new_obj)
                invalidate_address_cache(old_obj)
                new_code_ids |= _class_code_objects(new_obj)
                _swapped_classes.append(old_obj)
                live_by_name[name] = old_obj

            elif (not (name.startswith("__") and name.endswith("__"))
                  and not isinstance(old_obj, (types.FunctionType, type, types.ModuleType))
                  and not isinstance(new_obj, (types.FunctionType, type, types.ModuleType))):
                # Module-level data binding (`registry = {}`, `host = RenderHost(...)`,
                # `event_handler = EventHandler()`): same rule as class attrs -
                # an unchanged source expression keeps the live object.
                if _keep_live_attr(module_baseline, name, old_obj, new_obj, src_map.get("")):
                    module.__dict__[name] = old_obj

        _stamp_attr_src(module, src_map.get("", {}))
        # Canonicalize immutable module constants too. The general definition
        # pass visits exports and function metadata, deliberately not arbitrary
        # runtime containers; classification tuples need this narrow pass.
        def live_constant(value):
            replacement = replacements.get(id(value))
            if replacement is not None and replacement[0] is value:
                return replacement[1]
            if isinstance(value, tuple):
                items = tuple(live_constant(item) for item in value)
                if any(old is not new for old, new in zip(value, items)):
                    return items
            return value

        for name, value in tuple(module.__dict__.items()):
            if isinstance(value, tuple):
                module.__dict__[name] = live_constant(value)
        _repoint_attribute_bindings(module, source, live_by_name)
    except Exception as e:
        # Roll back to old attributes on error
        module.__dict__.update(old_attrs)
        extensions._services.clear()
        extensions._services.update(_pre_services)
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

    # Repaint every view affected by the swapped functions - cached tiles keep
    # blitting old pixels (and old code) until something invalidates them.
    for fn in _swapped_funcs:
        Melty.cache.invalidate_up_by_func(fn, max_depth=10)
    # Same for class-driven views - the class-span route (_recompile_class)
    # invalidates by object; without this a whole-file swap leaves views
    # reading class attrs (Toggles etc.) blitting stale tiles.
    for c in _swapped_classes:
        _patch_constructor_literals(c, source)
        Melty.cache.invalidate_up_by_obj(c, max_depth=10)
    _announce_hotswap(module)


# An enum's member bookkeeping. These are ordinary (non-dunder) class attrs, so
# the class attribute loop happily setattrs the THROWAWAY class's version over
# them - this silently repoints `_member_map_` at the new member objects while
# the class dict keeps the old ones (EnumMeta refuses to reassign a member).
# `Mode.TEXT` then resolves to the stale member forever. _reconcile_enum_members
# owns these; the loop must leave them alone.
_ENUM_INTERNALS = frozenset({
    "_member_map_", "_member_names_", "_value2member_map_",
    "_unhashable_values_", "_member_type_", "_value_repr_",
})



# ---------------------------------------------------------------------------
# Hotswap state preservation.
#
# Hotswap applies SOURCE edits and preserves RUNTIME state. A class body (or a
# module body) re-executed by a hotswap yields every data attribute's INITIAL
# value again; copying those over the live class is exactly what wiped
# `Melty.cache` to None on a meltygui.py swap (class-as-singleton: all its
# state is class attributes) and emptied `PendingSave.pending_saves`. The rule
# that separates an edit from runtime drift is the attribute's SOURCE
# EXPRESSION: unchanged text → keep the live value; changed text → apply the
# new one. The previous compile's expressions are the baseline, stamped as
# `__hotswap_attr_src__` on each class / module by `stamp_hotswap_baselines`
# at boot (latent_descent.main, background thread) and refreshed after every
# swap. Without a baseline (a module imported after the boot stamp, first
# swap) a compiled None / empty container over a populated live value is
# taken as runtime-filled state and kept; everything else applies.
# ---------------------------------------------------------------------------

def _norm_expr(text):
    return "".join(text.split()) if isinstance(text, str) else text


def _segment(lines, node):
    """ast.get_source_segment's byte-offset slicing over a PRE-SPLIT line
    list. The stdlib helper re-splits the ENTIRE source on every call
    (`_splitlines_no_ff`), making the boot baseline pass O(assignments ×
    file size) — measured as seconds of GIL-held CPU on the
    hotswap-baselines thread, convoying the render thread's per-GL-call
    GIL reacquisitions into the post-boot 1000ms frame burst."""
    try:
        if node.end_lineno is None or node.end_col_offset is None:
            return None
        lineno = node.lineno - 1
        end_lineno = node.end_lineno - 1
        col_offset = node.col_offset
        end_col_offset = node.end_col_offset
    except AttributeError:
        return None
    if end_lineno == lineno:
        return lines[lineno].encode()[col_offset:end_col_offset].decode()
    first = lines[lineno].encode()[col_offset:].decode()
    last = lines[end_lineno].encode()[:end_col_offset].decode()
    return "".join([first, *lines[lineno + 1:end_lineno], last])


def _attr_source_map(source: str) -> dict:
    """{qualname: {attr: expr_text}} for the module body ("" key) and every
    class body in `source` (nested classes under their dotted qualname). Only
    direct body assignments count; classes inside functions are skipped.
    Expression text is whitespace-normalized so a dedented class span and the
    whole module compare equal."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    try:
        lines = ast._splitlines_no_ff(source)   # exact get_source_segment lines
    except AttributeError:                      # private helper moved/renamed
        lines = source.splitlines(keepends=True)
    out = {}

    def _collect(body, key):
        m = out.setdefault(key, {})
        for node in body:
            if isinstance(node, ast.Assign):
                seg = _norm_expr(_segment(lines, node.value))
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        m[target.id] = seg
            elif (isinstance(node, ast.AnnAssign) and node.value is not None
                  and isinstance(node.target, ast.Name)):
                m[node.target.id] = _norm_expr(_segment(lines, node.value))
            elif isinstance(node, ast.ClassDef):
                _collect(node.body, f"{key}.{node.name}" if key else node.name)

    _collect(tree.body, "")
    return out


def _keep_live_attr(baseline, name, old_val, new_val, attr_src) -> bool:
    """True when a plain data attribute keeps its LIVE value across a swap:
    its source expression is unchanged against the baseline, or (no baseline)
    the compiled value is the empty shape of runtime-populated state."""
    if attr_src is not None and baseline is not None and name in attr_src and name in baseline:
        return baseline[name] == attr_src[name]
    return ((new_val is None or _is_empty_value(new_val))
            and not (old_val is None or _is_empty_value(old_val)))


def _stamp_attr_src(obj, attrs) -> None:
    try:
        if isinstance(obj, types.ModuleType):
            obj.__dict__["__hotswap_attr_src__"] = dict(attrs)
        else:
            type.__setattr__(obj, "__hotswap_attr_src__", dict(attrs))
    except Exception:
        pass


def _apply_attr_map(module: types.ModuleType, src_map: dict) -> None:
    """Stamp a precomputed _attr_source_map onto a module and its classes."""
    _stamp_attr_src(module, src_map.get("", {}))
    for qual, attrs in src_map.items():
        if not qual:
            continue
        obj = module
        for part in qual.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if isinstance(obj, type) and getattr(obj, "__module__", None) == module.__name__:
            _stamp_attr_src(obj, attrs)


def stamp_module_baseline(module: types.ModuleType, source: str = None) -> None:
    """Record the current source expressions of the module's data bindings
    and of every class defined in it (nested included) as the hotswap
    baseline. `source` defaults to the module's file on disk."""
    if source is None:
        f = getattr(module, "__file__", None)
        if not f:
            return
        source = Path(f).read_text(encoding="utf-8")
    _apply_attr_map(module, _attr_source_map(source))


def stamp_hotswap_baselines(delay: float = 0.0) -> int:
    """Boot-time baseline stamp for every loaded project module (both the
    `src.lsd.*` and `lsd.*` identities). Disk reads + ast only, so it runs on
    a background thread; `delay` lets startup imports land first. Returns the
    number of modules stamped.

    Dual-identity twins share one read + parse (grouped by __file__), and a
    short sleep between files keeps this CPU-bound pass from GIL-convoying
    the render thread's per-GL-call reacquisitions — this thread was the
    invisible source of the post-boot 1000ms frame burst (stall watchdog,
    08-24), amplified by get_source_segment's quadratic re-split (fixed in
    _segment)."""
    import sys as _sys
    from meltygui.code.fileref import is_editable_source
    from meltygui.core.diagnostics.perf_trace import span as _pt_span
    if delay:
        time.sleep(delay)
    by_file = {}
    for mod in list(_sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f or not f.endswith(".py") or not is_editable_source(f):
            continue
        by_file.setdefault(f, []).append(mod)
    n = 0
    with _pt_span("hotswap baselines stamp", files=len(by_file)):
        for f, mods in by_file.items():
            try:
                source = Path(f).read_text(encoding="utf-8")
                src_map = _attr_source_map(source)
            except Exception:
                continue
            for mod in mods:
                try:
                    _apply_attr_map(mod, src_map)
                    n += 1
                except Exception:
                    continue
            time.sleep(0.002)
    return n


def _repoint_attribute_bindings(module: types.ModuleType, source: str, live_by_name: dict) -> None:
    """Module-level `holder.attr = Name` statements re-ran during the exec and
    bound the THROWAWAY object (meltygui.py: `Core.melty = Melty`). Re-point each
    at the live object the module dict holds for that name."""
    if not live_by_name:
        return
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Name) or value.id not in live_by_name:
            continue
        live = live_by_name[value.id]
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            try:
                holder = eval(compile(ast.Expression(target.value), "<hotswap>", "eval"),
                              module.__dict__)
                if getattr(holder, target.attr, None) is not live:
                    setattr(holder, target.attr, live)
            except Exception:
                continue


def _hotswap_class(old_cls: type, new_cls: type, src_map: dict = None,
                   qualname: str = None, force: bool = False) -> None:
    """Patch an existing class in place with new methods and attributes.

    Plain data attributes follow the state-preservation rule above
    (`_keep_live_attr`, keyed by `src_map[qualname]`); `force=True` (rollback
    to a snapshot) writes every member unconditionally. Nested classes are
    patched recursively so their identity survives too."""
    attr_src = src_map.get(qualname) if (src_map and qualname) else None
    baseline = vars(old_cls).get("__hotswap_attr_src__") if not force else None
    _is_enum = isinstance(old_cls, EnumMeta)
    # NOTE: do NOT invalidate the address cache here.  The caller
    # (recompile_cls_fn) handles cache updates via update_address_cache.
    # Invalidating here creates a race window where a concurrent
    # background convert-in chain calls to_address, misses the cache,
    # and re-caches the OLD range from inspect.getsourcelines.

    for name in list(vars(old_cls)):
        if name.startswith("__") and name.endswith("__"):
            continue
        if isinstance(vars(old_cls).get(name), types.MemberDescriptorType):
            # Slot descriptor: deleting it or removing the slot breaks every live
            # instance (AttributeError on access) - the descriptor can't be
            # removed in place anyway.
            continue
        if name not in vars(new_cls):
            try:
                delattr(old_cls, name)
            except AttributeError:
                pass

    for name, new_val in vars(new_cls).items():
        # __class__ is the metaclass slot, not a reconcilable member - a class
        # that defines `__class__` as a property (transparent-proxy pattern,
        # e.g. _LazyMode) puts it in vars(); setattr(old_cls, '__class__', prop)
        # then raises "must be set to a class". Never patch it in place.
        if name in ("__dict__", "__weakref__", "_instances", "__class__"):
            continue
        if _is_enum and name in _ENUM_INTERNALS:
            continue

        old_val = vars(old_cls).get(name)

        if (isinstance(new_val, types.MemberDescriptorType)
                or isinstance(old_val, types.MemberDescriptorType)):
            # Slot descriptors are tied to their defining class. Copying the
            # new class's onto the old one makes EVERY slot access on existing
            # instances raise "descriptor doesn't apply" (the stale-LiveHandle
            # crash). The old class keeps its own descriptors - the slot
            # layout can't change in place.
            continue

        if (isinstance(old_val, types.FunctionType)
                and isinstance(new_val, types.FunctionType)):
            if not (old_val.__qualname__.startswith(old_cls.__qualname__ + ".")
                    and new_val.__qualname__.startswith(new_cls.__qualname__ + ".")):
                # A function-valued attribute (e.g. view_func=draw_text) is
                # a closure, not a method defined by this class. Rebind it;
                # patching its code would mutate the shared renderer itself.
                setattr(old_cls, name, new_val)
                continue
            patch_function(old_val, new_val, force=force)
        elif type(old_val) is staticmethod and type(new_val) is staticmethod:
            patch_function(old_val.__func__, new_val.__func__, force=force)
        elif type(old_val) is classmethod and type(new_val) is classmethod:
            patch_function(old_val.__func__, new_val.__func__, force=force)
        elif isinstance(new_val, property):
            try:
                setattr(old_cls, name, new_val)
            except (AttributeError, TypeError):
                pass
        elif (isinstance(old_val, type) and isinstance(new_val, type)
              and old_val is not new_val
              and getattr(new_val, "__qualname__", "").startswith(new_cls.__qualname__ + ".")):
            # Nested class: patch in place (identity + runtime state survive)
            # and move its re-run decorator registrations onto the live one.
            _hotswap_class(old_val, new_val, src_map=src_map,
                           qualname=f"{qualname}.{name}" if qualname else None,
                           force=force)
            _redirect_class_registrations(old_val, new_val)
        else:
            if old_val is new_val:
                continue
            if not force and _keep_live_attr(baseline, name, old_val, new_val, attr_src):
                continue
            try:
                setattr(old_cls, name, new_val)
            except (AttributeError, TypeError):
                pass

    if attr_src is not None and not force:
        _stamp_attr_src(old_cls, attr_src)

    # AFTER the attribute loop: an edited enum __init__ (Mode's, which derives
    # `unwrapped` from the value) is patched above, and the member state copied
    # below was produced by the NEW body running under it.
    if isinstance(old_cls, EnumMeta):
        _reconcile_enum_members(old_cls, new_cls)


def _is_empty_value(value) -> bool:
    """An empty container — the shape a member has before module-level code
    populates it (see the class-route guard in _reconcile_enum_members)."""
    return isinstance(value, (dict, list, tuple, set, frozenset, str)) and not value


def _reconcile_enum_members(old_cls: type, new_cls: type) -> None:
    """Refresh a live enum's MEMBERS after its class body was recompiled.

    Enum members are shared by identity, not by copy: `Mode.CODE_UI` is ONE
    object that every view, draw_state kwarg (`current_mode`) and Modes handle
    holds a reference to. `_hotswap_class`'s normal attribute loop can't touch
    them — EnumMeta.__setattr__ refuses to reassign a member ("cannot reassign
    member"), so the setattr silently lands in its except and the recompiled
    values never reach the app. That's why editing mode.py used to need a
    restart even though the swap reported success.

    Rather than swap in the throwaway class's members (which would strand every
    reference already held), the EXISTING member objects are mutated in place:
    each keeps its identity and simply starts reporting the new `_value_` (plus
    whatever the enum's `__init__` derived from it — `Mode.unwrapped`). Every
    holder therefore sees the edit with no per-view update at all.

    Members ADDED by the edit are constructed onto the live class (the enum
    registries have to be extended by hand — EnumMeta only builds them at class
    creation). Members REMOVED are left in place: something in the app may still
    hold one, and a dangling reference is worse than a stale one.

    `new_cls` may also be a `_snapshot_class` clone carrying `_enum_member_state_`
    — the rollback direction, restoring the pre-swap state the same way.
    """
    snapshot = getattr(new_cls, "_enum_member_state_", None)
    if isinstance(snapshot, dict):
        new_state = snapshot
    elif isinstance(new_cls, EnumMeta):
        new_state = {name: dict(vars(m)) for name, m in new_cls.__members__.items()}
    else:
        return

    changed = []
    added = []
    for name, state in new_state.items():
        old_m = old_cls.__members__.get(name)
        if old_m is None:
            added.append((name, state))
            continue
        # `state` is the full copy of the fresh member's __dict__ (_name_,
        # _value_, and whatever the enum's __init__ derived - Mode.unwrapped),
        # so replacing wholesale also drops attrs the new body stopped setting.
        # EXCEPT when the copied member is empty and the live one isn't: a
        # CLASS-route recompile (`_recompile_class`) re-runs only the class
        # BODY, so anything module-level code filled in afterwards is missing.
        # Mode.CODE is literally `CODE = {}` in the body, populated below the
        # in by _populate_code_mode() - into `unwrapped`, not the dict - and
        # a wholesale copy blanks it. Empty-over-nonempty is never a change
        # worth applying; genuinely emptying a member needs a restart.
        merged = dict(state)
        for key, live_val in vars(old_m).items():
            if _is_empty_value(merged.get(key)) and not _is_empty_value(live_val):
                merged[key] = live_val
        if vars(old_m) == merged:
            continue
        old_m.__dict__.clear()
        old_m.__dict__.update(merged)
        # ...except __objclass__, which the copied state points at the
        # throwaway class. The member belongs to the LIVE class.
        old_m.__objclass__ = old_cls
        changed.append(old_m)

    for name, state in added:
        try:
            member = object.__new__(old_cls)
            member.__dict__.update(state)
            member._name_ = name
            member.__objclass__ = old_cls
            # type.__setattr__ bypasses EnumMeta's "cannot reassign member"
            # guard, which also blocks the initial ASSIGNMENT of a new one.
            type.__setattr__(old_cls, name, member)
            old_cls._member_map_[name] = member
            if name not in old_cls._member_names_:
                old_cls._member_names_.append(name)
            changed.append(member)
        except Exception as e:
            print(f"[hotswap] could not add enum member {old_cls.__name__}.{name}: {e}")

    if not changed:
        return
    # Value lookup (Mode(value)) indexes members by value at class creation.
    # Mode's values are dicts - unhashable - so the map only ever holds the
    # hashable ones and py3.12 keeps the rest in _unhashable_values_ for
    # _missing_ to scan; rebuild both from the current state.
    try:
        old_cls._value2member_map_ = {}
        unhashable = [] if hasattr(old_cls, "_unhashable_values_") else None
        for m in old_cls.__members__.values():
            try:
                old_cls._value2member_map_[m._value_] = m
            except TypeError:
                if unhashable is not None:
                    unhashable.append(m._value_)
        if unhashable is not None:
            old_cls._unhashable_values_ = unhashable
    except Exception as e:
        print(f"[hotswap] enum value map rebuild failed for {old_cls.__name__}: {e}")

    _invalidate_views_using_members(changed)


def _invalidate_views_using_members(members) -> None:
    """Repaint the views a just-changed enum member drives.

    The member objects kept their identity, so nothing in the app knows their
    config moved — cached tiles keep blitting pixels drawn from the OLD mode.
    The wrapper stamps the active mode onto each view's kwargs
    (`current_mode`, or `mode` for the recursive variant), so the cache's own
    draw_state table is the complete list of affected views: one scan, no
    per-view bookkeeping anywhere else.
    """
    cache = getattr(Melty, "cache", None)
    table = getattr(cache, "key_to_draw_state", None)
    if not table:
        return
    # Enum members hash/compare by identity, and every Modes._LazyMode handle
    # forwards both to the member it resolves to, so a kwarg holding either
    # spelling matches.
    targets = set(members)
    for ds in list(table.values()):
        kwargs = getattr(ds, "_kwargs", None)
        if not kwargs:
            continue
        for key in ("current_mode", "mode"):
            mode = kwargs.get(key)
            if mode is None:
                continue
            try:
                hit = mode in targets
            except Exception:
                hit = False
            if hit:
                ds.invalidate_up(max_depth=6)
                break


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
                        "render_funcs_by_name", "default_lenses_by_type",
                        # Shaped-keyed (shaped.py); same key → wrapper shape
                        # as the others, so the reconcile needs nothing extra.
                        "default_funcs_by_shape", "default_lenses_by_shape",
                        # FIM registries (fim.py): @fim_provider /
                        # @fim_context_source re-run on recompile too.
                        "_fim_providers", "_fim_context_sources")

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


def _patch_twin_raws(unwrapped):
    """Propagate an in-place hotswap to the SAME function's twin raw object(s).

    One source file can sit in sys.modules under two names (the src./non-src
    dual identity: saved-state loaders import by the stored dotted path, which
    resurrects e.g. `lsd.gl_gui...` next to `src.lsd.gl_gui...`). Each twin
    module owns its OWN raw function and wrapper. A recompile patches whichever
    raw the editor resolved, but RenderFuncs.<name> handles resolve through
    render_funcs_by_name, which can hold the OTHER twin's wrapper — that twin
    keeps serving the stale code (the "RenderFuncs.button never updates" bug).
    Copy the freshly-patched state onto every same-qualname raw in every twin
    module so both identities run the edit.

    Returns [(twin_raw, prev_state), ...] so the caller can fold the twins into
    its hotswap-guard rollback (see _restore_twin_raws)."""
    import sys
    code = getattr(unwrapped, "__code__", None)
    if code is None:
        return []
    try:
        target = Path(code.co_filename).resolve()
    except (OSError, ValueError):
        return []
    if "<locals>" in unwrapped.__qualname__:
        return []  # locals aren't reachable by attribute walk
    qual = unwrapped.__qualname__.split(".")
    target_name = target.name
    patched = []
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        # Cheap basename gate before the syscall-heavy resolve (same pattern as
        # chain_converters._modules_for_file).
        if not f or f.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] != target_name:
            continue
        try:
            if Path(f).resolve() != target:
                continue
        except (OSError, ValueError):
            continue
        obj = mod
        for part in qual:
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if obj is None or not callable(obj):
            continue
        try:
            twin = inspect.unwrap(obj)
        except Exception:
            continue
        if twin is unwrapped or getattr(twin, "__code__", None) is None:
            continue
        prev = (twin.__code__, twin.__defaults__, twin.__kwdefaults__,
                dict(twin.__annotations__ or {}), twin.__doc__)
        try:
            twin.__code__ = code
        except ValueError as e:
            # Mismatched freevars (differently-shaped closure twin) -
            # leave that twin alone rather than half-patch it.
            print(f"twin hotswap skipped for {mod.__name__}."
                  f"{unwrapped.__qualname__}: {e}")
            continue
        twin.__defaults__ = unwrapped.__defaults__
        twin.__kwdefaults__ = unwrapped.__kwdefaults__
        twin.__annotations__ = dict(unwrapped.__annotations__ or {})
        twin.__doc__ = unwrapped.__doc__
        patched.append((twin, prev))
    return patched


def _restore_twin_raws(twins) -> None:
    """Rollback half of _patch_twin_raws — used by the hotswap guard so a
    runtime-throwing edit reverts on BOTH module identities, not just the one
    the editor patched."""
    for twin, prev in twins:
        try:
            twin.__code__, twin.__defaults__, twin.__kwdefaults__, ann, twin.__doc__ = prev
            twin.__annotations__ = dict(ann)
        except Exception as ex:
            print(f"[hotswap_guard] twin restore failed: {ex}")


def _redirect_function_registrations(pre_snapshot: dict, new_wrapper,
                                     live_raw=None, live_func=None,
                                     new_raw=None):
    """Reconcile the decorator-driven function registries after a recompile.
    Returns the resolved LIVE wrapper the entries were pointed at (None when no
    safe target was found and the registries were left alone).

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
        return None

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
        return None  # no safe target - leave the registries untouched

    def _is_live(v):
        return (v is live_wrapper or v is new_wrapper or _unwraps_to(v, live_raw))

    def _is_fresh(v):
        # The re-run decorators register the throwaway WRAPPER - or, in a
        # `@window`/`@defaults` written below @render_func, the throwaway RAW
        # (render_func's _adopt_raw_registrations normally re-points it, but
        # we the raw, so an un-adopted entry never survives as a rotting
        # object that draws the window / drifts resolve_address).
        return v is new_wrapper or (new_raw is not None and v is new_raw)

    for name, before in pre_snapshot.items():
        reg = getattr(Melty, name, None)
        if not isinstance(reg, dict):
            continue
        is_window = (name == "annotated_window_classes")

        # Keys the fresh exec just registered (value points at the throwaway).
        fresh = {}
        for key, val in list(reg.items()):
            if is_window:
                if isinstance(val, tuple) and len(val) == 2 and _is_fresh(val[0]):
                    fresh[key] = val[1]  # keep the fresh @window kwargs
            elif _is_fresh(val):
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
    return live_wrapper


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
    # The wrapper's closure also holds the helpers `render_func` defines local
    # to it (draw_inner_main, _auto_state_params, ...). Those are NOT config:
    # they share the live wrapper's wrapper, so their config reached them
    # already - and the throwaway's copies close over the throwaway raw, so
    # copying draw_inner_main made the live wrapper run the throwaway body
    # (exec'd into a COPY of the module globals: the file browser kept
    # reading a stale `current` after a tint-chip edit, 09-13). Skip every
    # sibling helper and anything else that closes over the throwaway.
    nested_prefix = new_wrapper.__code__.co_qualname.rsplit(".", 1)[0] + "."

    def _is_throwaway_helper(content):
        if not isinstance(content, types.FunctionType):
            return False
        if content.__code__.co_qualname.startswith(nested_prefix):
            return True
        for cell in content.__closure__ or ():
            try:
                v = cell.cell_contents
            except ValueError:
                continue
            if v is new_raw or v is new_wrapper:
                return True
        return False

    for live_cell, new_cell in zip(lc, nc):
        try:
            content = new_cell.cell_contents
        except ValueError:
            continue
        if content is new_raw or content is new_wrapper or _is_throwaway_helper(content):
            continue
        try:
            live_cell.cell_contents = content
        except ValueError:
            pass

    # Decorator-set wrapper ATTRIBUTES (not closure): chain-dispatch handles,
    # search flag, header defaults. Copy fresh values; drop ones the edit
    # removed. NEVER __wrapped__ - it must keep pointing at the live raw.
    for attr in ("_load_data", "_save_data", "_searchable",
                 "__header_defaults__", "__params__", "multi_instance"):
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
