"""
libcst ↔ Python type converters for the Melty registry.

Individual CST node types get their own converter pairs. Compound types
(Dict, List, Tuple) call convert() recursively on their children.

Dict results carry __cst__ for lossless round-trip reconstruction.
The original immutable CST node is never serialized — just referenced.
"""

import enum
import inspect
import sys
from time import sleep

import libcst as cst

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_conversion.converter_register import converter
from src.lsd.gl_gui.view.core_conversion.path_finder import convert
from src.lsd.gl_gui.view.core_conversion.fileref import FileRef

# Sentinel for arguments with no default value.
# Shows up in the dict so the UI can display the parameter name,
# but signals "no default" on the reverse path.
NO_DEFAULT = type("NO_DEFAULT", (), {
    "__repr__": lambda self: "NO_DEFAULT",
    "__bool__": lambda self: False,
})()


def _cst_node_to_code(node):
    """Get the source code string for a CST expression node."""
    wrapper = cst.Module(body=[
        cst.SimpleStatementLine(body=[cst.Expr(value=node)])
    ])
    # .code gives us "expr\n", strip the trailing newline
    return wrapper.code.rstrip("\n")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  function / type → str (source code via inspect)                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

import types

# TextSpan → original function object, so the converter can return it
_span_to_func: dict[FileRef, types.FunctionType] = {}


def clear_function_span_cache():
    """Clear the span → function lookup.  Useful for tests."""
    _span_to_func.clear()


@converter(registry=Melty)
def function_to_str(value: types.FunctionType) -> str:
    """Get the source code of a function as a string."""
    return inspect.getsource(value)


@converter(registry=Melty)
def type_to_str(value: type) -> str:
    """Get the source code of a class as a string."""
    return inspect.getsource(value)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  str ↔ cst.Module                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def str_to_cst_module(value: str) -> cst.Module:
    return cst.parse_module(value)


@converter(registry=Melty)
def cst_module_to_str(value: cst.Module) -> str:
    return value.code


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.Module ↔ dict                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def cst_module_to_dict(value: cst.Module) -> dict:
    """Top-level statements become readable dict keys.

    Handles: assignments, annotated assignments, class definitions,
    function definitions (default args), and decorator kwargs.
    """
    readable = {}

    # Sleep to test threading behaviour
    if Toggles.slow_down_threads:
        sleep(1.0)

    for stmt in value.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            for node in stmt.body:
                # x = 0
                if isinstance(node, cst.Assign) and len(node.targets) == 1:
                    target = node.targets[0].target
                    if isinstance(target, cst.Name):
                        py_value = _cst_to_python_or_raw(node.value)
                        if py_value is not _UNREADABLE:
                            readable[target.value] = py_value
                # x: int = 0
                elif isinstance(node, cst.AnnAssign):
                    if isinstance(node.target, cst.Name) and node.value is not None:
                        py_value = _cst_to_python_or_raw(node.value)
                        if py_value is not _UNREADABLE:
                            readable[node.target.value] = py_value

        elif isinstance(stmt, cst.ClassDef):
            try:
                readable[stmt.name.value] = convert(stmt, dict, registry=Melty)
            except (TypeError, ValueError):
                pass

        elif isinstance(stmt, cst.FunctionDef):
            try:
                readable[stmt.name.value] = convert(stmt, dict, registry=Melty)
            except (TypeError, ValueError):
                pass

    readable["__cst__"] = value
    return readable


@converter(registry=Melty)
def dict_to_cst_module(value: dict) -> cst.Module:
    """Rebuild from __cst__, patching in any edited values.

    Handles assignments, ClassDef __init__ self-assignments, and
    decorator keyword arguments.
    """
    tree = value.get("__cst__")
    if tree is None:
        raise ValueError("Dict has no __cst__ key")
    if not isinstance(tree, cst.Module):
        raise TypeError(f"Expected cst.Module in __cst__, got {type(tree).__name__}")

    edits = {k: v for k, v in value.items()
             if not (k.startswith("__") and k.endswith("__"))}

    if not edits:
        return tree

    return tree.visit(_ModulePatcher(edits))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Leaf CST nodes ↔ Python primitives                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def cst_integer_to_int(value: cst.Integer) -> int:
    return int(value.value)


@converter(registry=Melty)
def int_to_cst_integer(value: int) -> cst.Integer:
    return cst.Integer(str(value))


@converter(registry=Melty)
def cst_float_to_float(value: cst.Float) -> float:
    return float(value.value)


@converter(registry=Melty)
def float_to_cst_float(value: float) -> cst.Float:
    return cst.Float(repr(value))


@converter(registry=Melty)
def cst_simplestring_to_str(value: cst.SimpleString) -> str:
    try:
        return eval(value.value)  # noqa: S307 - safe, it's a string literal
    except Exception:
        return value.value


@converter(registry=Melty)
def str_to_cst_simplestring(value: str) -> cst.SimpleString:
    return cst.SimpleString(repr(value))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.Dict ↔ dict (recursive, with __cst__ preservation)                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def cst_dict_to_dict(value: cst.Dict) -> dict:
    """Recursively convert a CST Dict node to a Python dict.

    Each key and value is converted via _cst_to_python (which dispatches
    through convert() for known types).  The original CST node is stashed
    under __cst__ so dict_to_cst_dict can graft it back.
    """
    result = {}
    for el in value.elements:
        if isinstance(el, cst.StarredDictElement):
            continue  # **splat - can't represent as a plain key
        key = _cst_to_python(el.key)
        if key is _UNREADABLE:
            continue
        val = _cst_to_python_or_raw(el.value)
        result[key] = val
    result["__cst__"] = value
    return result


@converter(registry=Melty)
def dict_to_cst_dict(value: dict) -> cst.Dict:
    """Reconstruct a cst.Dict from a Python dict.

    If __cst__ is present, uses it as the base and patches only the
    elements that changed — preserving all formatting, whitespace, and
    comments from the original.

    Handles three mutation types:
      - Edit:   key exists in both old and new → with_changes() on value
      - Delete: key exists in old but not new → element dropped, last
                element gets the original trailing comma style
      - Insert: key exists in new but not old → new element appended,
                formatted to match existing siblings
    """
    old_node = value.get("__cst__")
    edits = {k: v for k, v in value.items()
             if not (k.startswith("__") and k.endswith("__"))}

    if isinstance(old_node, cst.Dict):
        # Extract formatting templates from existing elements
        real_elements = [el for el in old_node.elements if isinstance(el, cst.DictElement)]
        inner_comma = None
        last_comma = cst.MaybeSentinel.DEFAULT
        colon_before = cst.SimpleWhitespace("")
        colon_after = cst.SimpleWhitespace(" ")

        if len(real_elements) >= 2:
            inner_comma = real_elements[0].comma
            last_comma = real_elements[-1].comma
            colon_before = real_elements[0].whitespace_before_colon
            colon_after = real_elements[0].whitespace_after_colon
        elif len(real_elements) == 1:
            last_comma = real_elements[0].comma
            colon_before = real_elements[0].whitespace_before_colon
            colon_after = real_elements[0].whitespace_after_colon
            lbrace_ws = old_node.lbrace.whitespace_after
            if isinstance(lbrace_ws, cst.ParenthesizedWhitespace):
                inner_comma = cst.Comma(
                    whitespace_before=cst.SimpleWhitespace(""),
                    whitespace_after=lbrace_ws,
                )
        else:
            lbrace_ws = old_node.lbrace.whitespace_after
            if isinstance(lbrace_ws, cst.ParenthesizedWhitespace):
                inner_comma = cst.Comma(
                    whitespace_before=cst.SimpleWhitespace(""),
                    whitespace_after=lbrace_ws,
                )

        # Pass 1: keep / edit / delete existing elements
        surviving = []
        for el in old_node.elements:
            if isinstance(el, cst.StarredDictElement):
                surviving.append(el)
                continue
            key = _cst_to_python(el.key)
            if key is _UNREADABLE:
                surviving.append(el)  # unreadable key - pass through
                continue
            if key in edits:
                new_val = edits.pop(key)
                new_cst_val = _python_to_cst_expr(new_val, el.value)
                if new_cst_val is not None and new_cst_val is not el.value:
                    surviving.append(el.with_changes(value=new_cst_val))
                else:
                    surviving.append(el)
            # else key was deleted (popped) - drop it

        # Pass 2: append new keys, formatted like siblings
        for key, val in edits.items():
            cst_key = _python_to_cst_expr(key)
            cst_val = _python_to_cst_expr(val)
            if cst_key is None or cst_val is None:
                continue
            new_el = cst.DictElement(
                key=cst_key,
                value=cst_val,
                comma=cst.MaybeSentinel.DEFAULT,
                whitespace_before_colon=colon_before,
                whitespace_after_colon=colon_after,
            )
            surviving.append(new_el)

        # Pass 3: fix commas - only override when needed:
        #   - Last element gets original last_comma style
        #   - Non-last elements with MaybeSentinel get inner_comma
        #     (e.g. previously last element after an insert)
        #   - Non-last elements with bare trailing commas (empty
        #     whitespace_after) get inner_comma - these were trailing
        #     commas that are now internal separators
        #   - Non-last elements with meaningful commas (spaces, newlines)
        #     are preserved as-is (keeps multiline formatting, etc.)
        if surviving:
            fixed = []
            for i, el in enumerate(surviving):
                if not isinstance(el, cst.DictElement):
                    fixed.append(el)
                    continue
                is_last = (i == len(surviving) - 1)
                if is_last:
                    fixed.append(el.with_changes(comma=last_comma))
                elif isinstance(el.comma, cst.MaybeSentinel):
                    # Previously no comma - needs one now
                    if inner_comma is not None:
                        fixed.append(el.with_changes(comma=inner_comma))
                    else:
                        fixed.append(el)
                elif (inner_comma is not None
                      and isinstance(el.comma, cst.Comma)
                      and isinstance(el.comma.whitespace_after, cst.SimpleWhitespace)
                      and el.comma.whitespace_after.value == ""):
                    # Bare trailing comma (no whitespace) - upgrade to
                    # inner comma since it's now a separator, not trailing
                    fixed.append(el.with_changes(comma=inner_comma))
                else:
                    fixed.append(el)
            surviving = fixed

        return old_node.with_changes(elements=surviving)

    # No __cst__ - build from scratch
    elements = []
    for key, val in edits.items():
        cst_key = _python_to_cst_expr(key)
        cst_val = _python_to_cst_expr(val)
        if cst_key is not None and cst_val is not None:
            elements.append(cst.DictElement(key=cst_key, value=cst_val))
    return cst.Dict(elements=elements)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.List ↔ list (recursive)                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def cst_list_to_list(value: cst.List) -> list:
    result = []
    for el in value.elements:
        if isinstance(el, cst.StarredElement):
            result.append(_cst_node_to_code(el))
            continue
        result.append(_cst_to_python_or_raw(el.value))
    return result


@converter(registry=Melty)
def list_to_cst_list(value: list) -> cst.List:
    elements = []
    for item in value:
        cst_item = _python_to_cst_expr(item)
        if cst_item is not None:
            elements.append(cst.Element(value=cst_item))
    return cst.List(elements=elements)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.Tuple ↔ tuple (recursive)                                             ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def cst_tuple_to_tuple(value: cst.Tuple) -> tuple:
    result = []
    for el in value.elements:
        if isinstance(el, cst.StarredElement):
            result.append(_cst_node_to_code(el))
            continue
        result.append(_cst_to_python_or_raw(el.value))
    return tuple(result)


@converter(registry=Melty)
def tuple_to_cst_tuple(value: tuple) -> cst.Tuple:
    elements = []
    for item in value:
        cst_item = _python_to_cst_expr(item)
        if cst_item is not None:
            elements.append(cst.Element(value=cst_item))
    return cst.Tuple(elements=elements)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.ClassDef ↔ dict (self.X assignments from __init__)                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def cst_classdef_to_dict(value: cst.ClassDef) -> dict:
    """Extract readable fields from a class definition.

    Handles two patterns:
      1. __init__ self.X = literal  (traditional classes)
      2. body-level annotated assignments (dataclasses, NamedTuple)

    Both produce the same dict shape:
        {"decorators": {...}, "field": value, ..., "__cst__": <ClassDef>}
    """
    readable = {}

    decorators = _extract_decorators(value.decorators)
    if decorators:
        readable["decorators"] = decorators

    # Pattern 1: body-level AnnAssign (dataclass fields)
    #   debug: bool = False
    for stmt in value.body.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for node in stmt.body:
            if isinstance(node, cst.AnnAssign) and isinstance(node.target, cst.Name):
                if node.value is not None:
                    readable[node.target.value] = _cst_to_python_or_raw(node.value)

    # Pattern 2: __init__ self.X = literal
    init_fn = _find_init(value)
    if init_fn is not None:
        for stmt in init_fn.body.body:
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for node in stmt.body:
                target = None
                val_node = None
                # self.x = 0
                if isinstance(node, cst.Assign) and len(node.targets) == 1:
                    target = node.targets[0].target
                    val_node = node.value
                # self.x: int = 0
                elif isinstance(node, cst.AnnAssign):
                    target = node.target
                    val_node = node.value

                if (target is not None and val_node is not None
                        and isinstance(target, cst.Attribute)
                        and isinstance(target.value, cst.Name)
                        and target.value.value == "self"):
                    attr_name = target.attr.value
                    if attr_name not in readable:  # body-level takes priority
                        readable[attr_name] = _cst_to_python_or_raw(val_node)

    readable["__cst__"] = value
    return readable


@converter(registry=Melty)
def dict_to_cst_classdef(value: dict) -> cst.ClassDef:
    """Patch class decorators and fields from edited dict values.

    Handles decorators, body-level AnnAssign, and __init__ self.X assignments.
    """
    old_node = value.get("__cst__")
    if old_node is None or not isinstance(old_node, cst.ClassDef):
        raise TypeError("Dict has no __cst__ ClassDef")

    result = old_node

    # Patch decorators
    dec_edits = value.get("decorators")
    if isinstance(dec_edits, dict):
        result = _patch_decorators(result, dec_edits)

    edits = {k: v for k, v in value.items()
             if not (k.startswith("__") and k.endswith("__"))
             and k != "decorators"}

    if not edits:
        return result

    return result.visit(_ClassPatcher(edits))


def _find_init(classdef):
    """Find the __init__ FunctionDef inside a ClassDef, or None."""
    for stmt in classdef.body.body:
        if isinstance(stmt, cst.FunctionDef) and stmt.name.value == "__init__":
            return stmt
    return None


class _ClassPatcher(cst.CSTTransformer):
    """Patches class field values.

    Handles:
      - Body-level AnnAssign: debug: bool = False  (dataclass fields)
      - __init__ self.X = val  (traditional classes)
      - __init__ self.X: type = val  (annotated init assignments)
    """

    def __init__(self, edits: dict):
        super().__init__()
        self.edits = edits
        self._in_init = False

    def visit_FunctionDef(self, node):
        if node.name.value == "__init__":
            self._in_init = True
        return True

    def leave_FunctionDef(self, original_node, updated_node):
        if original_node.name.value == "__init__":
            self._in_init = False
        return updated_node

    def leave_AnnAssign(self, original_node, updated_node):
        # Body-level: debug: bool = False
        if not self._in_init and isinstance(updated_node.target, cst.Name):
            name = updated_node.target.value
            if name in self.edits and updated_node.value is not None:
                new_cst = _python_to_cst_expr(self.edits[name], updated_node.value)
                if new_cst is not None:
                    return updated_node.with_changes(value=new_cst)

        # __init__: self.x: int = 0
        if self._in_init and isinstance(updated_node.target, cst.Attribute):
            target = updated_node.target
            if (isinstance(target.value, cst.Name) and target.value.value == "self"
                    and updated_node.value is not None):
                name = target.attr.value
                if name in self.edits:
                    new_cst = _python_to_cst_expr(self.edits[name], updated_node.value)
                    if new_cst is not None:
                        return updated_node.with_changes(value=new_cst)

        return updated_node

    def leave_Assign(self, original_node, updated_node):
        if not self._in_init:
            return updated_node
        if len(updated_node.targets) != 1:
            return updated_node

        target = updated_node.targets[0].target
        if not (isinstance(target, cst.Attribute)
                and isinstance(target.value, cst.Name)
                and target.value.value == "self"):
            return updated_node

        attr_name = target.attr.value
        if attr_name not in self.edits:
            return updated_node

        new_cst_value = _python_to_cst_expr(self.edits[attr_name], updated_node.value)
        if new_cst_value is None:
            return updated_node

        return updated_node.with_changes(value=new_cst_value)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║         cst.FunctionDef ↔ dict (parameters + local assignments)                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_SKIP_PARAMS = {"self", "cls"}


@converter(registry=Melty)
def cst_funcdef_to_dict(value: cst.FunctionDef) -> dict:
    """Extract parameters, decorators, and body assignments from a function.

    @register(name="plugin", version=2)
    def my_func(param_one=0, param_two=1):
        some_local = 1
        return True

    → {
        "decorators": {"register": {"name": "plugin", "version": 2, ...}},
        "parameters": {"param_one": 0, "param_two": 1},
        "locals": {"some_local": 1},
        "__cst__": <FunctionDef>
      }
    """
    readable = {}

    decorators = _extract_decorators(value.decorators)
    if decorators:
        readable["decorators"] = decorators

    params = _extract_param_defaults(value.params)
    if params:
        readable["parameters"] = params

    # Body assignments under "locals"
    locals_ = _extract_body_assignments(value.body)
    if locals_:
        readable["locals"] = locals_

    readable["__cst__"] = value
    return readable


def _extract_param_defaults(params_node):
    """Extract all parameters as a dict.

    Parameters with defaults get their Python value.
    Parameters without defaults get NO_DEFAULT.
    """
    result = {}
    all_params = (list(params_node.params)
                  + list(params_node.posonly_params)
                  + list(params_node.kwonly_params))
    for param in all_params:
        if param.name.value in _SKIP_PARAMS:
            continue
        if param.default is not None:
            result[param.name.value] = _cst_to_python_or_raw(param.default)
        else:
            result[param.name.value] = NO_DEFAULT
    return result


def _extract_body_assignments(body_node):
    """Extract name = value assignments from a function/method body.

    Names assigned once get their plain name as key:
        x = 1  →  {"x": 1}

    Names assigned multiple times get indexed keys —
    the first keeps the plain name, subsequent ones get #1, #2, etc:
        x = 0
        x = 2
        →  {"x": 0, "x#1": 2}

    If/elif/else blocks become nested sub-dicts keyed by condition:
        if selected:
            x = 0.2
        elif pressed:
            x = 0.05
        else:
            x = 0.01
        →  {"if selected": {"x": 0.2},
            "elif pressed": {"x": 0.05},
            "else": {"x": 0.01}}

    Occurrence counters reset inside each scope.
    """
    if not isinstance(body_node, cst.IndentedBlock):
        return {}

    return _extract_block_assignments(body_node.body)


def _extract_block_assignments(stmts):
    """Extract assignments from a sequence of statements.

    Handles SimpleStatementLine (assignments) and If chains.
    """
    # Pass 1: count assignments per name (only direct assignments)
    counts: dict[str, int] = {}
    for stmt in stmts:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for node in stmt.body:
            name = _assign_target_name(node)
            if name is not None:
                counts[name] = counts.get(name, 0) + 1

    # Pass 2: extract with occurrence-indexed keys + if/elif/else
    result = {}
    seen: dict[str, int] = {}
    for stmt in stmts:
        if isinstance(stmt, cst.SimpleStatementLine):
            for node in stmt.body:
                name = _assign_target_name(node)
                if name is None:
                    continue
                val_node = _assign_value_node(node)
                if val_node is None:
                    continue

                occurrence = seen.get(name, 0)
                seen[name] = occurrence + 1

                if counts[name] == 1:
                    key = name
                elif occurrence == 0:
                    key = name
                else:
                    key = f"{name}#{occurrence}"

                result[key] = _cst_to_python_or_raw(val_node)

        elif isinstance(stmt, cst.If):
            _extract_if_chain(stmt, result)

    return result


def _extract_if_chain(if_node, result):
    """Walk an if/elif/else chain, extracting each branch as a sub-dict."""
    # "if <condition>"
    condition = _cst_node_to_code(if_node.test)
    key = f"if {condition}"
    body = _extract_block_assignments(if_node.body.body)
    if body:
        result[key] = body

    # Walk the orelse chain
    orelse = if_node.orelse
    while orelse is not None:
        if isinstance(orelse, cst.If):
            # elif
            condition = _cst_node_to_code(orelse.test)
            key = f"elif {condition}"
            body = _extract_block_assignments(orelse.body.body)
            if body:
                result[key] = body
            orelse = orelse.orelse
        elif isinstance(orelse, cst.Else):
            # else
            body = _extract_block_assignments(orelse.body.body)
            if body:
                result["else"] = body
            orelse = None
        else:
            break


def _assign_target_name(node):
    """Return the target name of a simple assignment, or None."""
    if isinstance(node, cst.Assign) and len(node.targets) == 1:
        target = node.targets[0].target
        if isinstance(target, cst.Name):
            return target.value
    elif isinstance(node, cst.AnnAssign):
        if isinstance(node.target, cst.Name) and node.value is not None:
            return node.target.value
    return None


def _assign_value_node(node):
    """Return the value CST node of a simple assignment, or None."""
    if isinstance(node, cst.Assign) and len(node.targets) == 1:
        return node.value
    elif isinstance(node, cst.AnnAssign) and node.value is not None:
        return node.value
    return None


def _extract_decorators(decorators):
    """Extract decorator kwargs as a dict.

    Call decorators → kwargs dict via cst_call_to_dict
    Bare decorators → raw code string
    """
    result = {}
    for dec in decorators:
        if isinstance(dec.decorator, cst.Call):
            func_name = _call_func_name(dec.decorator)
            if func_name:
                fn = Melty._converters.get((cst.Call, dict))
                if fn is not None:
                    try:
                        result[func_name] = fn(dec.decorator)
                    except (TypeError, ValueError):
                        result[func_name] = _cst_node_to_code(dec.decorator)
        else:
            # Bare decorator: @classmethod, @property, etc.
            code = _cst_node_to_code(dec.decorator)
            result[code] = code
    return result


@converter(registry=Melty)
def dict_to_cst_funcdef(value: dict) -> cst.FunctionDef:
    """Patch decorators, parameter defaults, and body assignments.

    "decorators" sub-dict patches decorator kwargs.
    "parameters" sub-dict patches param defaults.
    "locals" sub-dict patches body assignments.
    """
    old_node = value.get("__cst__")
    if old_node is None or not isinstance(old_node, cst.FunctionDef):
        raise TypeError("Dict has no __cst__ FunctionDef")

    result = old_node

    # Patch decorators
    dec_edits = value.get("decorators")
    if isinstance(dec_edits, dict):
        result = _patch_decorators(result, dec_edits)

    # Patch parameter defaults (skip NO_DEFAULT - means unchanged)
    param_edits = value.get("parameters")
    if isinstance(param_edits, dict):
        edits = {k: v for k, v in param_edits.items()
                 if not (k.startswith("__") and k.endswith("__"))
                 and v is not NO_DEFAULT}
        if edits:
            result = result.with_changes(
                params=_patch_params(result.params, edits))

    # Patch body assignments from "locals" sub-dict
    local_edits = value.get("locals")
    if isinstance(local_edits, dict):
        edits = {k: v for k, v in local_edits.items()
                 if not (k.startswith("__") and k.endswith("__"))}
        if edits:
            result = result.visit(_BodyAssignPatcher(edits))

    return result


class _BodyAssignPatcher(cst.CSTTransformer):
    """Patches name = value assignments at the top level of a function body.

    Matches edit keys to assignments by name and occurrence index:
      - "x"   → first (or only) assignment to x
      - "x#1" → second assignment to x
      - "x#2" → third, etc.

    If/elif/else sub-dicts are matched by condition text:
      - "if selected"   → patches body of the if block
      - "elif pressed"  → patches body of the elif
      - "else"          → patches body of the else

    Only patches direct assignments at depth 1.
    Nested blocks are handled via sub-dict recursion, not depth.
    """

    def __init__(self, edits: dict):
        super().__init__()
        self._depth = 0
        self._seen: dict[str, int] = {}

        # Separate assignment edits from block edits
        self._edits: dict[tuple[str, int], object] = {}
        self._block_edits: dict[str, dict] = {}

        for key, val in edits.items():
            if isinstance(val, dict) and (key.startswith("if ") or
                                          key.startswith("elif ") or
                                          key == "else"):
                self._block_edits[key] = val
            elif "#" in key:
                name, idx_str = key.rsplit("#", 1)
                try:
                    self._edits[(name, int(idx_str))] = val
                except ValueError:
                    pass
            else:
                self._edits[(key, 0)] = val

    def visit_IndentedBlock(self, node):
        self._depth += 1
        return True

    def leave_IndentedBlock(self, original_node, updated_node):
        self._depth -= 1
        return updated_node

    def _try_patch(self, name, updated_node_value):
        """Look up edit by (name, occurrence), return new CST value or None."""
        occurrence = self._seen.get(name, 0)
        self._seen[name] = occurrence + 1

        edit_val = self._edits.get((name, occurrence))
        if edit_val is None:
            return None
        return _python_to_cst_expr(edit_val, updated_node_value)

    def leave_Assign(self, original_node, updated_node):
        if self._depth != 1:
            return updated_node
        if len(updated_node.targets) != 1:
            return updated_node
        target = updated_node.targets[0].target
        if not isinstance(target, cst.Name):
            return updated_node

        new_cst = self._try_patch(target.value, updated_node.value)
        if new_cst is None:
            return updated_node
        return updated_node.with_changes(value=new_cst)

    def leave_AnnAssign(self, original_node, updated_node):
        if self._depth != 1:
            return updated_node
        if not isinstance(updated_node.target, cst.Name):
            return updated_node
        if updated_node.value is None:
            return updated_node

        new_cst = self._try_patch(updated_node.target.value, updated_node.value)
        if new_cst is None:
            return updated_node
        return updated_node.with_changes(value=new_cst)

    def leave_If(self, original_node, updated_node):
        if self._depth != 1 or not self._block_edits:
            return updated_node
        return _patch_if_chain(updated_node, self._block_edits)


def _patch_if_chain(if_node, block_edits):
    """Walk an if/elif/else chain, patching bodies from block_edits sub-dicts."""
    result = if_node

    # Patch the "if" branch's body
    condition = _cst_node_to_code(result.test)
    key = f"if {condition}"
    if key in block_edits:
        new_body = result.body.visit(_BodyAssignPatcher(block_edits[key]))
        result = result.with_changes(body=new_body)

    # Walk and patch the orelse chain
    result = _patch_orelse_chain(result, block_edits)
    return result


def _patch_orelse_chain(node, block_edits):
    """Recursively patch elif/else branches in an If's orelse chain."""
    orelse = node.orelse
    if orelse is None:
        return node

    if isinstance(orelse, cst.If):
        # elif - patch its body if we have edits
        condition = _cst_node_to_code(orelse.test)
        key = f"elif {condition}"
        new_orelse = orelse
        if key in block_edits:
            new_body = orelse.body.visit(_BodyAssignPatcher(block_edits[key]))
            new_orelse = orelse.with_changes(body=new_body)
        # Recurse into this elif's own orelse
        new_orelse = _patch_orelse_chain(new_orelse, block_edits)
        return node.with_changes(orelse=new_orelse)

    elif isinstance(orelse, cst.Else):
        if "else" in block_edits:
            new_body = orelse.body.visit(_BodyAssignPatcher(block_edits["else"]))
            new_orelse = orelse.with_changes(body=new_body)
            return node.with_changes(orelse=new_orelse)

    return node


def _patch_decorators(func_node, dec_edits):
    """Patch decorator kwargs on a FunctionDef from a decorators dict.

    dec_edits maps decorator name → sub-dict of kwargs.
    Each sub-dict is passed through dict→cst.Call conversion.
    """
    call_to_dict = Melty._converters.get((cst.Call, dict))
    dict_to_call = Melty._converters.get((dict, cst.Call))
    if dict_to_call is None:
        return func_node

    new_decorators = []
    changed = False
    for dec in func_node.decorators:
        if isinstance(dec.decorator, cst.Call):
            func_name = _call_func_name(dec.decorator)
            if func_name and func_name in dec_edits:
                edit_sub = dec_edits[func_name]
                if isinstance(edit_sub, dict):
                    edit_sub["__cst__"] = dec.decorator
                    try:
                        new_call = dict_to_call(edit_sub)
                        new_decorators.append(dec.with_changes(decorator=new_call))
                        changed = True
                        continue
                    except (TypeError, ValueError):
                        pass
        new_decorators.append(dec)

    if changed:
        return func_node.with_changes(decorators=new_decorators)
    return func_node


def _patch_params(params, edits):
    """Patch default values in a Parameters node from an edits dict."""

    def _patch_param_list(param_list):
        result = []
        for param in param_list:
            name = param.name.value
            if name in edits and param.default is not None:
                new_default = _python_to_cst_expr(edits[name], param.default)
                if new_default is not None:
                    result.append(param.with_changes(default=new_default))
                    continue
            result.append(param)
        return result

    changes = {}
    if params.params:
        changes["params"] = _patch_param_list(params.params)
    if params.posonly_params:
        changes["posonly_params"] = _patch_param_list(params.posonly_params)
    if params.kwonly_params:
        changes["kwonly_params"] = _patch_param_list(params.kwonly_params)

    return params.with_changes(**changes) if changes else params


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.Call ↔ dict (keyword arguments)                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@converter(registry=Melty)
def cst_call_to_dict(value: cst.Call) -> dict:
    """Extract keyword arguments from a Call as readable keys.

    my_func(param=42, flag=True)
    → {"param": 42, "flag": True, "__cst__": <Call>}

    Positional args and kwargs with unresolvable values (variable
    references, complex expressions) are left in __cst__ and pass
    through untouched on reconstruction.
    """
    readable = {}

    for arg in value.args:
        if arg.keyword is not None:
            readable[arg.keyword.value] = _cst_to_python_or_raw(arg.value)

    readable["__cst__"] = value
    return readable


@converter(registry=Melty)
def dict_to_cst_call(value: dict) -> cst.Call:
    """Reconstruct a Call from a dict, patching kwargs and handling
    insert/pop with sibling-cloned formatting.
    """
    old_node = value.get("__cst__")
    if old_node is None or not isinstance(old_node, cst.Call):
        raise TypeError("Dict has no __cst__ Call")

    edits = {k: v for k, v in value.items()
             if not (k.startswith("__") and k.endswith("__"))}

    if not edits:
        return old_node

    # Grab formatting template from existing kwargs
    template_arg = None
    for arg in old_node.args:
        if arg.keyword is not None:
            template_arg = arg
            break

    # Find the inner comma (between args) and last comma style
    real_args = list(old_node.args)
    inner_comma = None
    last_comma = cst.MaybeSentinel.DEFAULT

    if len(real_args) >= 2:
        inner_comma = real_args[0].comma
        last_comma = real_args[-1].comma
    elif len(real_args) == 1:
        last_comma = real_args[0].comma

    # Pass 1: keep positional args, edit/drop kwargs
    surviving = []
    for arg in old_node.args:
        if arg.keyword is None:
            surviving.append(arg)  # positional - pass through
            continue
        kw_name = arg.keyword.value
        if kw_name in edits:
            new_val = edits.pop(kw_name)
            new_cst_val = _python_to_cst_expr(new_val, arg.value)
            if new_cst_val is not None and new_cst_val is not arg.value:
                surviving.append(arg.with_changes(value=new_cst_val))
            else:
                surviving.append(arg)
        # else arg was removed (popped) - drop it

    # Pass 2: append new kwargs
    for kw_name, val in edits.items():
        cst_val = _python_to_cst_expr(val)
        if cst_val is None:
            continue
        equal = template_arg.equal if template_arg is not None else cst.AssignEqual(
            whitespace_before=cst.SimpleWhitespace(""),
            whitespace_after=cst.SimpleWhitespace(""),
        )
        new_arg = cst.Arg(
            keyword=cst.Name(kw_name),
            value=cst_val,
            equal=equal,
        )
        surviving.append(new_arg)

    # Pass 3: fix commas
    if surviving:
        fixed = []
        for i, arg in enumerate(surviving):
            is_last = (i == len(surviving) - 1)
            if is_last:
                fixed.append(arg.with_changes(comma=last_comma))
            elif inner_comma is not None:
                fixed.append(arg.with_changes(comma=inner_comma))
            else:
                fixed.append(arg)
        surviving = fixed

    return old_node.with_changes(args=surviving)


def _call_func_name(call_node):
    """Extract the function name from a Call node.

    my_func(...)           → "my_func"
    mod.my_func(...)       → "my_func"
    obj.method(...)        → "method"
    complex(expr)(...)     → None
    """
    func = call_node.func
    if isinstance(func, cst.Name):
        return func.value
    if isinstance(func, cst.Attribute):
        return func.attr.value
    return None


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Dispatch: CST expression → Python value                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_UNREADABLE = object()

# Map CST node types to their natural Python target types.
# convert() is called through the registry, so any registered
# converter pair works - even chained ones.
_CST_TYPE_MAP = {
    cst.Integer: int,
    cst.Float: float,
    cst.SimpleString: str,
    cst.Dict: dict,
    cst.List: list,
    cst.Tuple: tuple,
    cst.Call: dict,
}


def _cst_to_python(node):
    """Convert a CST expression node to a Python value by dispatching
    directly to a registered converter in the Melty registry.

    Uses direct registry lookup (not BFS) to avoid the path finder
    routing through generic converters like object_to_dict.

    Falls back to manual handling for Name (True/False/None),
    UnaryOperation (negative literals), and ConcatenatedString.
    Returns _UNREADABLE for anything that can't be represented.
    """
    # Direct registry lookup for known types - bypasses BFS
    target = _CST_TYPE_MAP.get(type(node))
    if target is not None:
        fn = Melty._converters.get((type(node), target))
        if fn is not None:
            try:
                return fn(node)
            except (TypeError, ValueError):
                return _UNREADABLE

    # Name: True, False, None, or a callable reference
    if isinstance(node, cst.Name):
        _LITERALS = {"True": True, "False": False, "None": None}
        if node.value in _LITERALS:
            return _LITERALS[node.value]
        # Try to resolve as a callable (function, class, builtin)
        resolved = _resolve_callable_by_name(node.value)
        if resolved is not _UNREADABLE:
            return resolved
        return _UNREADABLE

    # Negative literals: -42, -3.14
    if isinstance(node, cst.UnaryOperation):
        if isinstance(node.operator, cst.Minus):
            inner = _cst_to_python(node.expression)
            if isinstance(inner, (int, float)):
                return -inner
        if isinstance(node.operator, cst.Plus):
            return _cst_to_python(node.expression)
        return _UNREADABLE

    # Concatenated strings: "hello" "world"
    if isinstance(node, cst.ConcatenatedString):
        parts = []
        for part in [node.left, node.right]:
            val = _cst_to_python(part)
            if val is _UNREADABLE or not isinstance(val, str):
                return _UNREADABLE
            parts.append(val)
        return "".join(parts)

    if isinstance(node, cst.Set):
        elements = []
        for el in node.elements:
            if isinstance(el, cst.StarredElement):
                return _UNREADABLE
            val = _cst_to_python(el.value)
            if val is _UNREADABLE:
                return _UNREADABLE
            elements.append(val)
        return set(elements)

    # Attribute access: SomeEnum.VALUE, module.func, etc.
    if isinstance(node, cst.Attribute):
        parts = _collect_attribute_parts(node)
        if parts is not None:
            # Try enum first (more specific)
            resolved = _resolve_as_enum(parts)
            if resolved is not _UNREADABLE:
                return resolved
            # Try callable (function, class, builtin)
            resolved = _resolve_callable_by_parts(parts)
            if resolved is not _UNREADABLE:
                return resolved

    return _UNREADABLE


def _cst_to_python_or_raw(node):
    """Like _cst_to_python, but returns the raw source code string
    instead of _UNREADABLE.

    Also catches "empty" compound results — e.g. a Call with no kwargs
    produces {"__cst__": <Call>} which isn't useful, so we return
    the raw code string "some_func(1, 2)" instead.
    """
    val = _cst_to_python(node)
    if val is _UNREADABLE:
        return _cst_node_to_code(node)
    # Catch dict where every key is a dunder (nothing readable extracted)
    if isinstance(val, dict) and all(
            k.startswith("__") and k.endswith("__") for k in val):
        return _cst_node_to_code(node)
    return val


def _collect_attribute_parts(node):
    """Walk a chain of cst.Attribute nodes and collect the dotted name parts.

    SomeEnum.VALUE           → ["SomeEnum", "VALUE"]
    mod.SomeEnum.VALUE       → ["mod", "SomeEnum", "VALUE"]
    pkg.mod.SomeEnum.VALUE   → ["pkg", "mod", "SomeEnum", "VALUE"]

    Returns None if the chain contains anything other than Name/Attribute.
    """
    parts = []
    while isinstance(node, cst.Attribute):
        parts.append(node.attr.value)
        node = node.value
    if isinstance(node, cst.Name):
        parts.append(node.value)
        parts.reverse()
        return parts
    return None


def _resolve_as_enum(parts):
    """Try to resolve a dotted name like ["SomeEnum", "VALUE"] to an
    actual enum member by searching sys.modules.

    Tries progressively longer module prefixes:
      ["SomeEnum", "VALUE"]           → look for SomeEnum in all modules
      ["mod", "SomeEnum", "VALUE"]    → try mod.SomeEnum, then SomeEnum in mod
      ["pkg", "mod", "Enum", "VALUE"] → try pkg.mod.Enum, etc.

    Returns _UNREADABLE if nothing resolves to an enum member.
    """
    # We expect at least ClassName.MEMBER (2 parts)
    if len(parts) < 2:
        return _UNREADABLE

    member_name = parts[-1]

    # Strategy 1: the last-but-one element is the enum class name,
    # everything before it is a module path
    for split in range(len(parts) - 1, 0, -1):
        # parts[:split] should be a module path, parts[split-1] the class
        # e.g. for ["mod", "SomeEnum", "VALUE"]:
        #   split=2 → module_parts=["mod", "SomeEnum"], but that's not valid
        #   We want to try resolving "mod.SomeEnum" as a class in modules

        # Try: look for a class at parts[split-1] in module ".".join(parts[:split-1])
        class_name = parts[split - 1]
        module_path = ".".join(parts[:split - 1]) if split > 1 else None

        if module_path:
            mod = sys.modules.get(module_path)
            if mod is not None:
                cls = getattr(mod, class_name, None)
                if cls is not None and isinstance(cls, type) and issubclass(cls, enum.Enum):
                    member = cls.__members__.get(member_name)
                    if member is not None:
                        return member

    # Strategy 2: just the class name (no module prefix), scan all modules
    class_name = parts[-2]
    for mod in sys.modules.values():
        cls = getattr(mod, class_name, None)
        if cls is not None and isinstance(cls, type) and issubclass(cls, enum.Enum):
            member = cls.__members__.get(member_name)
            if member is not None:
                return member

    return _UNREADABLE


def _resolve_callable_by_name(name):
    """Try to resolve a bare name to a callable via sys.modules.

    Checks builtins first, then scans all loaded modules for a
    matching attribute that is callable.

    draw_header → <function draw_header>
    type        → <class 'type'>

    Returns _UNREADABLE if nothing resolves.
    """
    import builtins
    # Builtins first (type, int, len, print, etc.)
    obj = getattr(builtins, name, None)
    if obj is not None and callable(obj):
        return obj

    # Scan all loaded modules
    for mod in sys.modules.values():
        if mod is None:
            continue
        obj = getattr(mod, name, None)
        if obj is not None and callable(obj):
            # Verify this is the canonical location (not a re-export)
            obj_module = getattr(obj, "__module__", None)
            obj_qualname = getattr(obj, "__qualname__", None)
            if obj_module and obj_qualname:
                # Accept if the object's __name__ matches what we're looking for
                obj_name = obj_qualname.rsplit(".", 1)[-1]
                if obj_name == name:
                    return obj
            else:
                # No metadata - accept if name matches
                return obj

    return _UNREADABLE


def _resolve_callable_by_parts(parts):
    """Try to resolve a dotted name like ["module", "func"] to a callable.

    Tries progressively longer module prefixes:
      ["os", "path", "join"]    → os.path.join
      ["mymod", "MyClass"]      → mymod.MyClass (if callable)
      ["Foo", "bar"]            → Foo.bar (class method, scanned)

    Returns _UNREADABLE if nothing resolves to a callable.
    """
    # Strategy 1: fully-qualified - try splitting into module path + attr chain
    for split in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:split])
        mod = sys.modules.get(module_path)
        if mod is None:
            continue
        # Walk remaining parts as attribute chain
        obj = mod
        for attr_name in parts[split:]:
            obj = getattr(obj, attr_name, None)
            if obj is None:
                break
        if obj is not None and callable(obj):
            return obj

    # Strategy 2: bare class/function name, scan all modules
    # e.g. ["MyClass", "method"] - find MyClass, then .method
    root_name = parts[0]
    for mod in sys.modules.values():
        if mod is None:
            continue
        obj = getattr(mod, root_name, None)
        if obj is None:
            continue
        # Walk remaining parts
        for attr_name in parts[1:]:
            obj = getattr(obj, attr_name, None)
            if obj is None:
                break
        if obj is not None and callable(obj):
            return obj

    return _UNREADABLE


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Python value → CST expression (for patching edits back in)                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _ModulePatcher(cst.CSTTransformer):
    """Patches the module tree from a readable dict.

    Handles:
      - Simple assignments: name = value
      - Annotated assignments: name: type = value
      - ClassDef: patches decorators, __init__ self.X, and body-level AnnAssign
      - FunctionDef: patches decorators, default parameter values, and body assignments
    """

    def __init__(self, edits: dict):
        super().__init__()
        self.edits = edits

    def leave_Assign(self, original_node, updated_node):
        if len(updated_node.targets) != 1:
            return updated_node
        target = updated_node.targets[0].target
        if not isinstance(target, cst.Name):
            return updated_node
        if target.value not in self.edits:
            return updated_node

        new_py_value = self.edits[target.value]
        new_cst_value = _python_to_cst_expr(new_py_value, updated_node.value)
        if new_cst_value is None:
            return updated_node

        return updated_node.with_changes(value=new_cst_value)

    def leave_AnnAssign(self, original_node, updated_node):
        if not isinstance(updated_node.target, cst.Name):
            return updated_node
        if updated_node.target.value not in self.edits:
            return updated_node
        if updated_node.value is None:
            return updated_node

        new_py_value = self.edits[updated_node.target.value]
        new_cst_value = _python_to_cst_expr(new_py_value, updated_node.value)
        if new_cst_value is None:
            return updated_node

        return updated_node.with_changes(value=new_cst_value)

    def leave_ClassDef(self, original_node, updated_node):
        class_name = updated_node.name.value
        if class_name not in self.edits:
            return updated_node

        edit_dict = self.edits[class_name]
        if not isinstance(edit_dict, dict):
            return updated_node

        edit_dict["__cst__"] = updated_node
        try:
            return convert(edit_dict, cst.ClassDef, registry=Melty)
        except (TypeError, ValueError):
            return updated_node

    def leave_FunctionDef(self, original_node, updated_node):
        func_name = updated_node.name.value
        if func_name not in self.edits:
            return updated_node

        edit_dict = self.edits[func_name]
        if not isinstance(edit_dict, dict):
            return updated_node

        edit_dict["__cst__"] = updated_node
        try:
            return convert(edit_dict, cst.FunctionDef, registry=Melty)
        except (TypeError, ValueError):
            return updated_node


def _python_to_cst_expr(py_value, old_node=None):
    """Convert a Python value to a CST expression node.

    For dicts with __cst__, delegates to dict_to_cst_dict which handles
    the grafting.  For everything else, builds nodes directly, preserving
    formatting from old_node via with_changes() when types match.
    """
    # Strings - behavior depends on what old_node was:
    #   old_node is SimpleString → just/ literal (preserve quotes)
    #   old_node is something else → code expression, compare/parse
    #   old_node is None → string literal (safe fallback for new inserts)
    if isinstance(py_value, str):
        if old_node is None or isinstance(old_node, cst.SimpleString):
            # String literal
            if isinstance(old_node, cst.SimpleString):
                quote_char = old_node.value[0]
                escaped = py_value.replace("\\", "\\\\").replace(quote_char, f"\\{quote_char}")
                return old_node.with_changes(value=f"{quote_char}{escaped}{quote_char}")
            return cst.SimpleString(repr(py_value))
        else:
            # Code expression (Name,Name, Call, Attribute, etc.)
            old_code = _cst_node_to_code(old_node)
            if py_value == old_code:
                return old_node  # unchanged — pass through
            # Modified - try to parse as a new expression
            try:
                wrapper = cst.parse_module(f"_ = {py_value}\n")
                assign = wrapper.body[0].body[0]
                return assign.value
            except cst.ParserSyntaxError:
                return old_node  # unparseable - keep original

    # Dicts with __cst__ - route based on the type of the stashed CST node
    if isinstance(py_value, dict) and "__cst__" in py_value:
        cst_node = py_value["__cst__"]
        if isinstance(cst_node, cst.Call):
            try:
                return convert(py_value, cst.Call, registry=Melty)
            except (TypeError, ValueError):
                pass
        elif isinstance(cst_node, cst.Dict):
            try:
                return convert(py_value, cst.Dict, registry=Melty)
            except (TypeError, ValueError):
                pass

    # Enum members → Attribute(Name("ClassName"), Name("MEMBER"))
    # Must come before bool/int checks since IntEnum IS-A int
    if isinstance(py_value, enum.Enum):
        cls_name = type(py_value).__name__
        member_name = py_value.name
        if isinstance(old_node, cst.Attribute):
            # Preserve formatting (dot whitespace, parens) from old node
            return old_node.with_changes(
                value=old_node.value.with_changes(value=cls_name)
                    if isinstance(old_node.value, cst.Name) else cst.Name(cls_name),
                attr=cst.Name(member_name),
            )
        return cst.Attribute(
            value=cst.Name(cls_name),
            attr=cst.Name(member_name),
        )

    # Callables (functions, classes, builtins)
    # Must come before bool/int since type IS-A callable
    if callable(py_value) and hasattr(py_value, "__name__"):
        return _callable_to_cst_expr(py_value, old_node)

    if isinstance(py_value, bool):
        new_name = "True" if py_value else "False"
        if isinstance(old_node, cst.Name):
            return old_node.with_changes(value=new_name)
        return cst.Name(new_name)

    if isinstance(py_value, int):
        if py_value < 0:
            if isinstance(old_node, cst.UnaryOperation) and isinstance(old_node.operator, cst.Minus):
                old_expr = old_node.expression
                if isinstance(old_expr, cst.Integer) and int(old_expr.value) == abs(py_value):
                    return old_node  # unchanged - preserve original repr
                return old_node.with_changes(
                    expression=old_expr.with_changes(value=str(abs(py_value))))
            return cst.UnaryOperation(operator=cst.Minus(), expression=cst.Integer(str(abs(py_value))))
        if isinstance(old_node, cst.Integer):
            if int(old_node.value) == py_value:
                return old_node  # unchanged - preserve original repr
            return old_node.with_changes(value=str(py_value))
        return cst.Integer(str(py_value))

    if isinstance(py_value, float):
        if py_value < 0:
            if isinstance(old_node, cst.UnaryOperation) and isinstance(old_node.operator, cst.Minus):
                old_expr = old_node.expression
                if isinstance(old_expr, cst.Float) and float(old_expr.value) == abs(py_value):
                    return old_node  # unchanged - preserve original repr
                return old_node.with_changes(
                    expression=old_expr.with_changes(value=repr(abs(py_value))))
            return cst.UnaryOperation(operator=cst.Minus(), expression=cst.Float(repr(abs(py_value))))
        if isinstance(old_node, cst.Float):
            if float(old_node.value) == py_value:
                return old_node  # unchanged - preserve original repr
            return old_node.with_changes(value=repr(py_value))
        return cst.Float(repr(py_value))

    if py_value is None:
        if isinstance(old_node, cst.Name):
            return old_node.with_changes(value="None")
        return cst.Name("None")

    if isinstance(py_value, dict):
        # Plain dict without __cst__ - build from scratch
        try:
            return convert(py_value, cst.Dict, registry=Melty)
        except (TypeError, ValueError):
            pass
        return None

    if isinstance(py_value, list):
        if isinstance(old_node, cst.List):
            return _patch_sequence(py_value, old_node, cst.List)
        try:
            return convert(py_value, cst.List, registry=Melty)
        except (TypeError, ValueError):
            pass
        return None

    if isinstance(py_value, tuple):
        if isinstance(old_node, cst.Tuple):
            return _patch_sequence(py_value, old_node, cst.Tuple)
        try:
            return convert(py_value, cst.Tuple, registry=Melty)
        except (TypeError, ValueError):
            pass
        return None

    return None


def _patch_sequence(py_values, old_node, node_cls):
    """Patch a cst.List or cst.Tuple in-place, preserving comma formatting.

    Walks old elements in parallel with new Python values:
      - Surviving positions: update value, keep comma/whitespace
      - New positions (list grew): clone comma style from last old element
      - Removed positions (list shrank): drop extras
      - Last element: strip trailing comma only if original had none
    """
    old_els = list(old_node.elements)
    new_els = []

    # Determine which comma style to clone for new/promoted elements.
    # Use the first non-last element's comma (inner comma).
    inner_comma = cst.Comma(whitespace_after=cst.SimpleWhitespace(""))
    if len(old_els) >= 2:
        inner_comma = old_els[0].comma

    # Did the original have a trailing comma on its last element?
    had_trailing = False
    if old_els and not isinstance(old_els[-1].comma, cst.MaybeSentinel):
        had_trailing = True

    for i, py_val in enumerate(py_values):
        if i < len(old_els):
            # Surviving position - keep comma, update value
            old_el = old_els[i]
            new_value = _python_to_cst_expr(py_val, old_el.value)
            if new_value is None:
                new_value = old_el.value
            new_els.append(old_el.with_changes(value=new_value))
        else:
            # New position - build element, clone inner comma
            new_value = _python_to_cst_expr(py_val)
            if new_value is None:
                continue
            new_els.append(cst.Element(value=new_value, comma=inner_comma))

    # Ensure every non-last element has a real comma.
    # (Old last element had MaybeSentinel.DEFAULT and is no longer last.)
    for i in range(len(new_els) - 1):
        if isinstance(new_els[i].comma, cst.MaybeSentinel):
            new_els[i] = new_els[i].with_changes(comma=inner_comma)

    # Fix last element comma
    if new_els:
        last = new_els[-1]
        if had_trailing:
            # Preserve trailing comma style from original last element
            if old_els:
                new_els[-1] = last.with_changes(comma=old_els[-1].comma)
        else:
            new_els[-1] = last.with_changes(comma=cst.MaybeSentinel.DEFAULT)

    return old_node.with_changes(elements=new_els)


def _callable_to_cst_expr(py_value, old_node=None):
    """Convert a callable (function, class, builtin) to a CST expression.

    Uses __qualname__ to determine the name shape:
      my_func        → Name("my_func")
      MyClass.method → Attribute(Name("MyClass"), Name("method"))

    Preserves formatting from old_node via with_changes() when the
    node shape matches.
    """
    qualname = getattr(py_value, "__qualname__", None) or py_value.__name__
    parts = qualname.split(".")

    # Filter out <locals> from nested definitions
    parts = [p for p in parts if not p.startswith("<")]

    if not parts:
        return old_node

    if len(parts) == 1:
        # Simple name: draw_header, type, int, etc.
        name = parts[0]
        if isinstance(old_node, cst.Name):
            return old_node.with_changes(value=name)
        return cst.Name(name)

    # Dotted name: foo.bar, Class.method, etc.
    # Build right-to-left Attribute chain
    result = cst.Name(parts[0])
    for part in parts[1:]:
        result = cst.Attribute(value=result, attr=cst.Name(part))

    # Preserve formatting from old_node if it's also an Attribute
    if isinstance(old_node, cst.Attribute):
        return _graft_attribute_formatting(result, old_node)

    return result


def _graft_attribute_formatting(new_attr, old_attr):
    """Copy dot/whitespace formatting from old Attribute onto new one."""
    changes = {"dot": old_attr.dot}
    if isinstance(new_attr.value, cst.Name) and isinstance(old_attr.value, cst.Name):
        changes["value"] = old_attr.value.with_changes(value=new_attr.value.value)
    if isinstance(new_attr.attr, cst.Name) and isinstance(old_attr.attr, cst.Name):
        changes["attr"] = old_attr.attr.with_changes(value=new_attr.attr.value)
    return old_attr.with_changes(**changes)