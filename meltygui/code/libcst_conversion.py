"""
libcst ↔ Python type converters for the Melty registry.

Individual CST node types get their own converter pairs. Compound types
(Dict, List, Tuple) call convert() recursively on their children.

Dict results carry __cst__ for lossless round-trip reconstruction.
The original immutable CST node is never serialized — just referenced.
"""

import enum
import inspect
import math
import struct
import sys

import libcst as cst
from libcst._nodes.internal import CodegenState as _CodegenState

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_conversion.converter_register import converter
from src.lsd.gl_gui.view.core_conversion.path_finder import convert, PendingState
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending

# Sentinel for arguments with no default value.
# Shows up in the dict so the UI can display the parameter name,
# but signals "no default" on the reverse path.
NO_DEFAULT = type("NO_DEFAULT", (), {
    "__repr__": lambda self: "NO_DEFAULT",
    "__bool__": lambda self: False,
})()


class Comment(str):
    """A comment, as a str subclass for auto-rendering dispatch.

    isinstance(c, str) → True, so it works everywhere strings do.
    isinstance(c, Comment) → True, so the UI can render a comment widget.

    The string value IS the comment text (e.g. "# setup vars").
    The .inline attribute tracks whether it's a trailing comment.

    Used as dict keys (with custom __hash__/__eq__ so they don't collide
    with plain strings) and as dict values (editable in place).
    """

    def __new__(cls, text, inline=None):
        instance = super().__new__(cls, text)
        instance.inline = inline
        return instance

    @property
    def text(self):
        """The comment text — same as str(self). Provided for readability."""
        return str(self)

    def __repr__(self):
        if self.inline:
            return f"{self.inline}  {self}"
        return str.__repr__(self)

    def __eq__(self, other):
        if not isinstance(other, Comment):
            return False
        return str(self) == str(other) and self.inline == other.inline

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(("__comment__", str(self), self.inline))


class Conditional(dict):
    """An if/elif/else block's contents, as a dict subclass.

    isinstance(c, dict) → True, so iteration/access works normally.
    isinstance(c, Conditional) → True, so the UI can render a
    collapsible conditional block.

    The .condition attribute holds the full condition text
    (e.g. "if selected", "elif pressed", "else").
    """

    def __init__(self, *args, condition=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.condition = condition  # e.g. "if selected", "elif pressed", "else"

    def __bg_hash__(self) -> str:
        # Cheap hash: condition text + sorted key names + count.
        # Key names changing (structure edit) invalidates the cache;
        # value changes inside known-stable keys are ignored intentionally
        # for performance - the condition + structure is the identity.
        keys = ",".join(sorted(str(k) for k in self.keys() if not str(k).startswith("_")))
        return f"Conditional:{self.condition}:{keys}"

class Loop(dict):
    """A for-loop block's contents, as a dict subclass.

    isinstance(l, dict) → True, so iteration/access works normally.
    isinstance(l, Loop) → True, so the UI can render a loop widget.

    The .target attribute holds the loop variable(s) (e.g. "i", "x, y").
    The .iter attribute holds the iterator code (e.g. "range(10)", "items").

    For range() loops, a "range" key holds the editable args as a list:
        Loop({"range": [0, 100, 5], "x": "i * 2"}, target="i", iter="range(0, 100, 5)")

    For non-range loops, no "range" key — just body assignments:
        Loop({"z": "process(item)"}, target="item", iter="items")
    """

    def __init__(self, *args, target=None, iter=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = target    # e.g. "i", "x, y"
        self.iter = iter        # e.g. "range(10)", "items"
        self._bg_hash_cache: str | None = None


    def __bg_hash__(self) -> str:
            if self._bg_hash_cache is None:
                # hash() on a str uses a fast SipHash - O(n) once, then O(1)
                self._bg_hash_cache = str(hash(self.iter + self.target))
            return self._bg_hash_cache



class GeneralParse(dict):
    def __init__(self, *args, source="", **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source
        self._bg_hash_cache: str | None = None

    def __bg_hash__(self) -> str:
        if self._bg_hash_cache is None:
            # hash() on a str uses a fast SipHash - O(n) once, then O(1)
            self._bg_hash_cache = str(hash(self.source))
        return self._bg_hash_cache

class ParseError(dict):
    """A dict representing code that failed to parse.

    isinstance(d, dict) → True, so generic code can iterate it.
    isinstance(d, ParseError) → True, so the UI can show an error editor.

    Always contains:
      - "__source__": the raw source string
      - "__error__": the error message
      - "__line__": line number of the error (1-based)
      - "__column__": column number (1-based)

    May also contain successfully parsed entries from partial recovery.

    Attributes:
      .source  — raw source string
      .error   — error message string
      .line    — error line number
      .column  — error column number
    """

    def __bg_hash__(self) -> str:
        # Source text is the full content - hashing it is enough.
        return f"ParseError:{self.source}"

    def __init__(self, *args, source="", error="", line=0, column=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source
        self.error = error
        self.line = line
        self.column = column
        self["__source__"] = source
        self["__error__"] = error
        self["__line__"] = line
        self["__column__"] = column


def _cst_node_to_code(node):
    """Get the source code string for a CST expression node.

    Uses direct codegen instead of wrapping in a Module — ~5x faster.
    """
    state = _CodegenState(default_indent="    ", default_newline="\n")
    node._codegen(state)
    return "".join(state.tokens)


def _is_dunder(key):
    """True if key is a __dunder__ string — safe on non-string keys."""
    return isinstance(key, str) and key.startswith("__") and key.endswith("__")


# Pre-compiled struct for float32 round-trip tests (avoids per-call overhead)
_F32_PACK = struct.Struct("f")


def _float_decimal_places(s):
    """Count the number of decimal places in a float string like '0.03'.

    Returns None for scientific notation or strings without a decimal point.
    """
    if "e" in s.lower():
        return None
    if "." not in s:
        return 0
    return len(s.split(".")[1])


def _ensure_float_str(s):
    """Ensure a numeric string is a valid CST float (must contain a decimal point).

    '3' → '3.0', '100' → '100.0', '0.5' → '0.5' (unchanged)
    """
    if "." not in s and "e" not in s.lower():
        s += ".0"
    return s


def _strip_trailing_zeros(s):
    """Strip trailing zeros from a float string, keeping at least one decimal.

    '0.500' → '0.5', '3.140' → '3.14', '1.0' → '1.0' (kept)
    """
    if "." not in s or "e" in s.lower():
        return s
    s = s.rstrip("0")
    if s.endswith("."):
        s += "0"
    return s


def _clean_float(value):
    """Detect and clean float32 representation noise, returning a clean string.

    Values like 1.600000023841858 (float32 for 1.6) get cleaned to '1.6'.

    Uses a float32 round-trip test: if the value survives packing to
    float32 and back, tries progressively shorter %g representations
    (1-7 significant digits) until one also survives the same round-trip.

    Max 7 iterations for float32 values, instant exit for pure float64.
    Always returns a valid CST float string (with a decimal point).
    """
    if not math.isfinite(value):
        return repr(value)  # 'inf', 'nan' - caller must handle
    if value == 0.0:
        return repr(value)

    # First check: does this value survive float32 round-trip?
    f32_bytes = _F32_PACK.pack(value)
    f32 = _F32_PACK.unpack(f32_bytes)[0]
    if f32 != value:
        return _ensure_float_str(repr(value))  # pure float64 - no cleaning

    # Float32-representable: find shortest string that preserves it
    full = repr(value)
    for sig in range(1, 8):
        short = f"{value:.{sig}g}"
        if len(short) >= len(full):
            break  # not getting shorter
        if _F32_PACK.unpack(_F32_PACK.pack(float(short)))[0] == f32:
            return _ensure_float_str(short)

    return _ensure_float_str(full)


def _floats_match(a, b):
    """Check if two floats are the same value, accounting for float32 cleaning.

    1.6 matches 1.600000023841858 because both map to the same float32 bits.
    Also handles exact equality for pure float64 values.
    """
    if a == b:
        return True
    # Check if they're the same float32 value (one may have been cleaned)
    return _F32_PACK.pack(a) == _F32_PACK.pack(b)


def _float_to_str(value, old_str=None):
    """Format a float, capping decimal places to match the original.

    Uses _clean_float as base representation instead of repr() to avoid
    float32 noise in the output.

    If the old source had 2 decimal places (e.g. '0.03'), and the new
    value has significantly more (3+ extra), it's likely slider jitter
    and gets rounded to the original precision.

    Small precision increases (1-2 extra dp) are allowed — they indicate
    deliberate input, not noise.  Trailing zeros are always stripped.
    """
    clean = _clean_float(value)

    if old_str is None:
        return clean

    old_dp = _float_decimal_places(old_str)
    if old_dp is None:
        return clean

    new_dp = _float_decimal_places(clean)
    if new_dp is None:
        return clean

    # Only cap if the precision is significantly higher (slider noise)
    if new_dp > old_dp + 2:
        rounded = round(value, old_dp)
        return _ensure_float_str(_strip_trailing_zeros(_clean_float(rounded)))

    return clean


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  function / type → str (source code via inspect)                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

import types


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
    try:
        return cst.parse_module(value)
    except cst.ParserSyntaxError as e:
        return Pending(wrapped=ParseError(
            source=value,
            error=e.message,
            line=e.raw_line,
            column=e.raw_column,
        ), originated=str_to_cst_module, state=PendingState.ERROR, status=e.message)


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
    readable = GeneralParse(source=value.code)

    # Module header comments (top-of-file, before first statement)
    for ll in value.header:
        if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
            c = Comment(ll.comment.value)
            readable[c] = c

    _classdef_to_dict = Melty._converters.get((cst.ClassDef, dict))
    _funcdef_to_dict = Melty._converters.get((cst.FunctionDef, dict))

    for stmt in value.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            # Leading comments
            _extract_leading_comments(stmt, readable)

            last_key = None
            for node in stmt.body:
                # x = 0
                if isinstance(node, cst.Assign) and len(node.targets) == 1:
                    target = node.targets[0].target
                    if isinstance(target, cst.Name):
                        py_value = _cst_to_python_or_raw(node.value)
                        if py_value is not _UNREADABLE:
                            readable[target.value] = py_value
                            last_key = target.value
                # x: int = 0
                elif isinstance(node, cst.AnnAssign):
                    if isinstance(node.target, cst.Name) and node.value is not None:
                        py_value = _cst_to_python_or_raw(node.value)
                        if py_value is not _UNREADABLE:
                            readable[node.target.value] = py_value
                            last_key = node.target.value

            # Trailing inline comment
            _extract_trailing_comment(stmt, last_key, readable)

        elif isinstance(stmt, cst.ClassDef):
            _extract_leading_comments(stmt, readable)
            if _classdef_to_dict is not None:
                try:
                    readable[stmt.name.value] = _classdef_to_dict(stmt)
                except (TypeError, ValueError):
                    pass

        elif isinstance(stmt, cst.FunctionDef):
            _extract_leading_comments(stmt, readable)
            if _funcdef_to_dict is not None:
                try:
                    readable[stmt.name.value] = _funcdef_to_dict(stmt)
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
             if not (_is_dunder(k))
             and not isinstance(k, Comment)}

    try:
        result = tree
        if edits:
            result = result.visit(_ModulePatcher(edits))

        # Patch comments (module header & body)
        all_comment_edits = _collect_comment_edits(value)
        if all_comment_edits:
            result = _patch_module_comments(result, value)

        return result
    except cst.ParserSyntaxError as e:
        return Pending(wrapped=ParseError(
            source=tree.code,
            error=e.message,
            line=e.raw_line,
            column=e.raw_column,
        ), originated=dict_to_cst_module, state=PendingState.ERROR, status=e.message)
    except cst.CSTValidationError as e:
        print("Validation error during CST patching:", e, file=sys.stderr)
        return Pending(wrapped=ParseError(
            source=tree.code,
            error=str(e),
        ), originated=dict_to_cst_module, state=PendingState.ERROR, status=str(e))


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
    return float(_clean_float(float(value.value)))


@converter(registry=Melty)
def float_to_cst_float(value: float) -> cst.Float:
    if not math.isfinite(value):
        raise ValueError(f"Cannot represent {value!r} as cst.Float")
    return cst.Float(_clean_float(value))


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
             if not (_is_dunder(k))}

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

    Handles three patterns:
      1. body-level Assign:     invalidate_stack_trace = False
      2. body-level AnnAssign:  debug: bool = False  (dataclass fields)
      3. __init__ self.X = literal  (traditional classes)

    Comments are extracted via Comment keys.
    Decorators go in a "decorators" sub-dict.
    """
    readable = {}

    decorators = _extract_decorators(value.decorators)
    if decorators:
        readable["decorators"] = decorators

    # Body-level assignments and annotations
    for stmt in value.body.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue

        _extract_leading_comments(stmt, readable)

        last_key = None
        for node in stmt.body:
            # x = False  (plain assignment)
            if isinstance(node, cst.Assign) and len(node.targets) == 1:
                target = node.targets[0].target
                if isinstance(target, cst.Name):
                    readable[target.value] = _cst_to_python_or_raw(node.value)
                    last_key = target.value
            # debug: bool = False  (annotated assignment)
            elif isinstance(node, cst.AnnAssign) and isinstance(node.target, cst.Name):
                if node.value is not None:
                    readable[node.target.value] = _cst_to_python_or_raw(node.value)
                    last_key = node.target.value

        _extract_trailing_comment(stmt, last_key, readable)

    # __init__ self.X = literal
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
    """Patch class decorators, fields, and comments from edited dict values.

    Handles decorators, body-level Assign/AnnAssign, __init__ self.X, and comments.
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
             if not (_is_dunder(k))
             and k != "decorators"
             and not isinstance(k, Comment)}

    if edits:
        result = result.visit(_ClassPatcher(edits))

    # Patch comments in class body
    comment_text_map = _collect_comment_edits(value)
    if comment_text_map:
        new_body = _patch_body_direct(result.body, {}, comment_text_map)
        if new_body is not result.body:
            result = result.with_changes(body=new_body)

    return result


def _find_init(classdef):
    """Find the __init__ FunctionDef inside a ClassDef, or None."""
    for stmt in classdef.body.body:
        if isinstance(stmt, cst.FunctionDef) and stmt.name.value == "__init__":
            return stmt
    return None


class _ClassPatcher(cst.CSTTransformer):
    """Patches class field values.

    Handles:
      - Body-level Assign: x = False  (class variables)
      - Body-level AnnAssign: debug: bool = False  (dataclass fields)
      - __init__ self.X = val  (traditional classes)
      - __init__ self.X: type = val  (annotated init assignments)
    """

    def __init__(self, edits: dict):
        super().__init__()
        self.edits = {k: v for k, v in edits.items() if not isinstance(k, Comment)}
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
        if len(updated_node.targets) != 1:
            return updated_node

        target = updated_node.targets[0].target

        # Body-level: x = False
        if not self._in_init and isinstance(target, cst.Name):
            name = target.value
            if name in self.edits:
                new_cst = _python_to_cst_expr(self.edits[name], updated_node.value)
                if new_cst is not None:
                    return updated_node.with_changes(value=new_cst)
            return updated_node

        # __init__: self.x = val
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
    readable = GeneralParse(source=_cst_node_to_code(value))

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
    result = GeneralParse(source="\n".join(_cst_node_to_code(stmt) for stmt in params_node.params))

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

    Handles SimpleStatementLine (assignments), If chains, and comments.
    Comments are emitted as Comment entries following CST's attachment model:
      - Leading standalone comments → Comment entries before the assignment
      - Trailing inline comments → Comment entries after the assignment
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

    # Pass 2: assignments with occurrence-indexed keys + if/elif/else + comments
    result = GeneralParse(source="\n".join(_cst_node_to_code(stmt) for stmt in stmts))
    seen: dict[str, int] = {}
    for stmt in stmts:
        if isinstance(stmt, cst.SimpleStatementLine):
            # Leading comments (standalone lines above this statement)
            _extract_leading_comments(stmt, result)

            last_key = None
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
                last_key = key

            # Trailing inline comment on this statement
            _extract_trailing_comment(stmt, last_key, result)

        elif isinstance(stmt, cst.If):
            # Leading comments on the if statement itself
            _extract_leading_comments(stmt, result)
            _extract_if_chain(stmt, result)

        elif isinstance(stmt, cst.For):
            _extract_leading_comments(stmt, result)
            _extract_for_loop(stmt, result)

    return result


def _extract_leading_comments(stmt, result):
    """Extract standalone comment lines from a statement's leading_lines."""
    for ll in getattr(stmt, "leading_lines", ()):
        if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
            c = Comment(ll.comment.value)
            result[c] = c


def _extract_trailing_comment(stmt, var_key, result):
    """Extract an inline trailing comment from a statement."""
    tw = getattr(stmt, "trailing_whitespace", None)
    if tw is not None and hasattr(tw, "comment") and tw.comment is not None:
        c = Comment(tw.comment.value, inline=var_key)
        result[c] = c


def _collect_comment_edits(edits, text_map=None):
    """Recursively collect all Comment key→value pairs where text changed.

    Returns {old_text: new_text} for every edited comment in the tree.
    """
    if text_map is None:
        text_map = {}
    for k, v in edits.items():
        if isinstance(k, Comment) and isinstance(v, str) and str(k) != v:
            text_map[str(k)] = v
        elif isinstance(v, dict):
            _collect_comment_edits(v, text_map)
    return text_map


def _patch_module_comments(module, comment_edits):
    """Patch comments on a cst.Module (header + body) by direct walk."""
    text_map = _collect_comment_edits(comment_edits)
    if not text_map:
        return module

    result = module
    changed = False

    # Patch header comments
    new_header = list(module.header)
    for i, ll in enumerate(new_header):
        if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
            new_text = text_map.get(ll.comment.value)
            if new_text is not None:
                new_header[i] = ll.with_changes(
                    comment=cst.Comment(value=new_text))
                changed = True

    if changed:
        result = result.with_changes(header=new_header)

    # Patch body statement comments by direct walk
    new_body = list(result.body)
    body_changed = False
    for i, stmt in enumerate(new_body):
        new_stmt = _patch_stmt_comments(stmt, text_map)
        if new_stmt is not stmt:
            new_body[i] = new_stmt
            body_changed = True

    if body_changed:
        result = result.with_changes(body=new_body)

    return result


def _extract_if_chain(if_node, result):
    """Walk an if/elif/else chain, extracting each branch as a sub-dict."""
    # "if <condition>"
    condition = _cst_node_to_code(if_node.test)
    key = f"if {condition}"
    body = _extract_block_assignments(if_node.body.body)
    if body:
        result[key] = Conditional(body, condition=key)

    # Walk the orelse chain
    orelse = if_node.orelse
    while orelse is not None:
        if isinstance(orelse, cst.If):
            # elif
            condition = _cst_node_to_code(orelse.test)
            key = f"elif {condition}"
            body = _extract_block_assignments(orelse.body.body)
            if body:
                result[key] = Conditional(body, condition=key)
            orelse = orelse.orelse
        elif isinstance(orelse, cst.Else):
            # else
            body = _extract_block_assignments(orelse.body.body)
            if body:
                result["else"] = Conditional(body, condition="else")
            orelse = None
        else:
            break


def _extract_for_loop(for_node, result):
    """Extract a for loop as a Loop dict entry.

    Key is the full loop header: "for i in range(10)"
    Value is a Loop dict containing:
      - "range": [args...]  if the iterator is a range() call
      - body assignments (recursively extracted)
    """
    target_code = _cst_node_to_code(for_node.target)
    iter_code = _cst_node_to_code(for_node.iter)
    key = f"for {target_code} in {iter_code}"

    body = _extract_block_assignments(for_node.body.body)

    # Extract range() args as editable values
    range_args = _extract_range_args(for_node.iter)
    if range_args is not None:
        body["range"] = range_args

    result[key] = Loop(body, target=target_code, iter=iter_code)

    # for/else
    if for_node.orelse is not None and isinstance(for_node.orelse, cst.Else):
        else_body = _extract_block_assignments(for_node.orelse.body.body)
        if else_body:
            result[f"{key} else"] = Conditional(else_body, condition="else")


def _extract_range_args(iter_node):
    """Extract positional args from a range() call, or None if not range().

    range(10)       → [10]
    range(0, 100)   → [0, 100]
    range(0, 100, 5) → [0, 100, 5]
    """
    if not isinstance(iter_node, cst.Call):
        return None
    func = iter_node.func
    if not (isinstance(func, cst.Name) and func.value == "range"):
        return None

    args = []
    for arg in iter_node.args:
        if arg.keyword is not None:
            return None  # keyword in range() - unusual, bail
        val = _cst_to_python(arg.value)
        if val is _UNREADABLE:
            return None
        args.append(val)
    return args if args else None


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
                 if not (_is_dunder(k))
                 and v is not NO_DEFAULT}
        if edits:
            result = result.with_changes(
                params=_patch_params(result.params, edits))

    # Patch body assignments and comments from "locals" sub-dict
    local_edits = value.get("locals")
    if isinstance(local_edits, dict):
        edits = {k: v for k, v in local_edits.items()
                 if not (_is_dunder(k))
                 and not isinstance(k, Comment)}

        # Collect comment edits as a flat text→text map
        comment_text_map = _collect_comment_edits(local_edits)

        if edits or comment_text_map:
            new_body = _patch_body_direct(
                result.body, edits, comment_text_map or None)
            if new_body is not result.body:
                result = result.with_changes(body=new_body)

    return result


def _parse_edit_keys(edits):
    """Split a locals dict into assignment edits and block edits.

    Returns (assign_edits, block_edits) where:
      assign_edits = {(name, occurrence): value}
      block_edits = {"if cond": sub_dict, "elif ...": ..., "else": ...}
    """
    assign_edits: dict[tuple[str, int], object] = {}
    block_edits: dict[str, dict] = {}

    for key, val in edits.items():
        if isinstance(key, Comment):
            continue
        if isinstance(val, dict) and (key.startswith("if ") or
                                      key.startswith("elif ") or
                                      key == "else" or
                                      key.startswith("for ")):
            block_edits[key] = val
        elif isinstance(key, str) and "#" in key:
            name, idx_str = key.rsplit("#", 1)
            try:
                assign_edits[(name, int(idx_str))] = val
            except ValueError:
                pass
        elif isinstance(key, str):
            assign_edits[(key, 0)] = val

    return assign_edits, block_edits


def _patch_body_direct(body_node, edits, comment_text_map=None):
    """Patch assignments and comments in an IndentedBlock by direct statement walk.

    No CSTTransformer — walks body.body directly, patches matching
    assignments with with_changes(), handles if/elif/else by recursing,
    and patches comments inline.

    ~4000x less overhead than CSTTransformer for a noop walk.
    """
    if not isinstance(body_node, cst.IndentedBlock):
        return body_node

    assign_edits, block_edits = _parse_edit_keys(edits)
    if not assign_edits and not block_edits and not comment_text_map:
        return body_node

    new_stmts = list(body_node.body)
    changed = False
    seen: dict[str, int] = {}

    for i, stmt in enumerate(new_stmts):
        if isinstance(stmt, cst.SimpleStatementLine):
            new_stmt = _patch_simple_stmt(stmt, assign_edits, seen)
            if comment_text_map:
                new_stmt = _patch_stmt_comments(new_stmt, comment_text_map)
            if new_stmt is not stmt:
                new_stmts[i] = new_stmt
                changed = True

        elif isinstance(stmt, cst.If):
            new_stmt = stmt
            if comment_text_map:
                new_stmt = _patch_stmt_comments(new_stmt, comment_text_map)
            if block_edits:
                new_stmt = _patch_if_chain_direct(new_stmt, block_edits, comment_text_map)
            if new_stmt is not stmt:
                new_stmts[i] = new_stmt
                changed = True

        elif isinstance(stmt, cst.For):
            new_stmt = stmt
            if comment_text_map:
                new_stmt = _patch_stmt_comments(new_stmt, comment_text_map)
            if block_edits:
                new_stmt = _patch_for_loop_direct(new_stmt, block_edits, comment_text_map)
            if new_stmt is not stmt:
                new_stmts[i] = new_stmt
                changed = True

    if not changed:
        return body_node
    return body_node.with_changes(body=new_stmts)


def _patch_simple_stmt(stmt, assign_edits, seen):
    """Patch a SimpleStatementLine's assignments by name+occurrence.

    Returns the same stmt object if nothing changed (identity check).
    """
    new_body = list(stmt.body)
    changed = False

    for j, node in enumerate(new_body):
        name = _assign_target_name(node)
        if name is None:
            continue

        occurrence = seen.get(name, 0)
        seen[name] = occurrence + 1

        edit_val = assign_edits.get((name, occurrence))
        if edit_val is None:
            continue

        val_node = _assign_value_node(node)
        if val_node is None:
            continue

        new_cst = _python_to_cst_expr(edit_val, val_node)
        if new_cst is None or new_cst is val_node:
            continue

        if isinstance(node, cst.Assign):
            new_body[j] = node.with_changes(value=new_cst)
        elif isinstance(node, cst.AnnAssign):
            new_body[j] = node.with_changes(value=new_cst)
        changed = True

    if not changed:
        return stmt
    return stmt.with_changes(body=new_body)


def _patch_stmt_comments(stmt, text_map):
    """Patch leading and trailing comments on a statement by direct access."""
    result = stmt
    changed = False

    # Leading comments (EmptyLine nodes)
    if hasattr(result, "leading_lines") and result.leading_lines:
        new_lines = list(result.leading_lines)
        for j, ll in enumerate(new_lines):
            if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
                new_text = text_map.get(ll.comment.value)
                if new_text is not None:
                    new_lines[j] = ll.with_changes(
                        comment=cst.Comment(value=new_text))
                    changed = True
        if changed:
            result = result.with_changes(leading_lines=new_lines)

    # Trailing comment
    tw = getattr(result, "trailing_whitespace", None)
    if tw is not None and hasattr(tw, "comment") and tw.comment is not None:
        new_text = text_map.get(tw.comment.value)
        if new_text is not None:
            result = result.with_changes(
                trailing_whitespace=tw.with_changes(
                    comment=cst.Comment(value=new_text)))
            changed = True

    return result


def _patch_if_chain_direct(if_node, block_edits, comment_text_map=None):
    """Patch an if/elif/else chain by direct body walk — no CSTTransformer."""
    result = if_node
    changed = False

    # "if <cond>" body
    condition = _cst_node_to_code(result.test)
    key = f"if {condition}"
    if key in block_edits:
        new_body = _patch_body_direct(result.body, block_edits[key], comment_text_map)
        if new_body is not result.body:
            result = result.with_changes(body=new_body)
            changed = True

    # Patch the orelse chain
    new_result = _patch_orelse_direct(result, block_edits, comment_text_map)
    if new_result is not result:
        result = new_result
        changed = True

    return result


def _patch_orelse_direct(node, block_edits, comment_text_map=None):
    """Recursively patch elif/else branches by direct body walk."""
    orelse = node.orelse
    if orelse is None:
        return node

    if isinstance(orelse, cst.If):
        condition = _cst_node_to_code(orelse.test)
        key = f"elif {condition}"
        new_orelse = orelse
        if key in block_edits:
            new_body = _patch_body_direct(orelse.body, block_edits[key], comment_text_map)
            if new_body is not orelse.body:
                new_orelse = orelse.with_changes(body=new_body)
        # Recurse into this elif's own orelse
        recursed = _patch_orelse_direct(new_orelse, block_edits, comment_text_map)
        if recursed is not new_orelse:
            new_orelse = recursed
        if new_orelse is not orelse:
            return node.with_changes(orelse=new_orelse)

    elif isinstance(orelse, cst.Else):
        if "else" in block_edits:
            new_body = _patch_body_direct(orelse.body, block_edits["else"], comment_text_map)
            if new_body is not orelse.body:
                new_orelse = orelse.with_changes(body=new_body)
                return node.with_changes(orelse=new_orelse)

    return node


def _patch_for_loop_direct(for_node, block_edits, comment_text_map=None):
    """Patch a for loop's body and range args from block_edits."""
    target_code = _cst_node_to_code(for_node.target)
    iter_code = _cst_node_to_code(for_node.iter)
    key = f"for {target_code} in {iter_code}"

    if key not in block_edits:
        return for_node

    loop_edits = block_edits[key]
    result = for_node

    # Patch range() args if present
    range_edits = loop_edits.get("range")
    if range_edits is not None and isinstance(range_edits, list):
        new_iter = _patch_range_args(result.iter, range_edits)
        if new_iter is not result.iter:
            result = result.with_changes(iter=new_iter)

    # Patch body assignments (exclude "range" key)
    body_edits = {k: v for k, v in loop_edits.items() if k != "range"}
    if body_edits or comment_text_map:
        new_body = _patch_body_direct(result.body, body_edits, comment_text_map)
        if new_body is not result.body:
            result = result.with_changes(body=new_body)

    return result


def _patch_range_args(iter_node, new_args):
    """Patch positional args on a range() Call node.

    Returns the original node if it's not a range() call or args are unchanged.
    """
    if not isinstance(iter_node, cst.Call):
        return iter_node
    func = iter_node.func
    if not (isinstance(func, cst.Name) and func.value == "range"):
        return iter_node

    old_args = list(iter_node.args)
    if len(new_args) != len(old_args):
        # Arg count changed - rebuild all args
        new_cst_args = []
        for i, val in enumerate(new_args):
            new_expr = _python_to_cst_expr(val)
            if new_expr is None:
                return iter_node
            comma = cst.MaybeSentinel.DEFAULT
            if i < len(new_args) - 1:
                # Clone comma from old args if pre
                if i < len(old_args):
                    comma = old_args[i].comma
                else:
                    comma = cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
            new_cst_args.append(cst.Arg(value=new_expr, comma=comma))
        return iter_node.with_changes(args=new_cst_args)

    # Same arg count - patch in place
    changed = False
    patched = []
    for i, (old_arg, new_val) in enumerate(zip(old_args, new_args)):
        new_expr = _python_to_cst_expr(new_val, old_arg.value)
        if new_expr is not None and new_expr is not old_arg.value:
            patched.append(old_arg.with_changes(value=new_expr))
            changed = True
        else:
            patched.append(old_arg)

    if not changed:
        return iter_node
    return iter_node.with_changes(args=patched)


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
             if not (_is_dunder(k))}

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

# Pre-built literals for Name nodes - avoids dict allocation on every call
_NAME_LITERALS = {"True": True, "False": False, "None": None}

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
        if node.value in _NAME_LITERALS:
            return _NAME_LITERALS[node.value]
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
            _is_dunder(k) for k in val):
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
        self.edits = {k: v for k, v in edits.items() if not isinstance(k, Comment)}
        # Cache converter lookups to avoid BFS on every leave_* call
        self._classdef_fn = Melty._converters.get((dict, cst.ClassDef))
        self._funcdef_fn = Melty._converters.get((dict, cst.FunctionDef))

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
        fn = self._classdef_fn
        if fn is not None:
            try:
                return fn(edit_dict)
            except (TypeError, ValueError):
                pass
        return updated_node

    def leave_FunctionDef(self, original_node, updated_node):
        func_name = updated_node.name.value
        if func_name not in self.edits:
            return updated_node

        edit_dict = self.edits[func_name]
        if not isinstance(edit_dict, dict):
            return updated_node

        edit_dict["__cst__"] = updated_node
        fn = self._funcdef_fn
        if fn is not None:
            try:
                return fn(edit_dict)
            except (TypeError, ValueError):
                pass
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
            fn = Melty._converters.get((dict, cst.Call))
            if fn is not None:
                try:
                    return fn(py_value)
                except (TypeError, ValueError):
                    pass
        elif isinstance(cst_node, cst.Dict):
            fn = Melty._converters.get((dict, cst.Dict))
            if fn is not None:
                try:
                    return fn(py_value)
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
        if not math.isfinite(py_value):
            return None  # inf/nan can't be represented as cst.Float
        if py_value < 0:
            if isinstance(old_node, cst.UnaryOperation) and isinstance(old_node.operator, cst.Minus):
                old_expr = old_node.expression
                if isinstance(old_expr, cst.Float) and _floats_match(abs(py_value), float(old_expr.value)):
                    return old_node  # unchanged - preserve original repr
                return old_node.with_changes(
                    expression=old_expr.with_changes(
                        value=_float_to_str(abs(py_value),
                                            old_expr.value if isinstance(old_expr, cst.Float) else None)))
            return cst.UnaryOperation(operator=cst.Minus(), expression=cst.Float(_clean_float(abs(py_value))))
        if isinstance(old_node, cst.Float):
            if _floats_match(py_value, float(old_node.value)):
                return old_node  # unchanged - preserve original repr
            return old_node.with_changes(value=_float_to_str(py_value, old_node.value))
        return cst.Float(_clean_float(py_value))

    if py_value is None:
        if isinstance(old_node, cst.Name):
            return old_node.with_changes(value="None")
        return cst.Name("None")

    if isinstance(py_value, dict):
        # Plain dict without __cst__ - build from scratch
        fn = Melty._converters.get((dict, cst.Dict))
        if fn is not None:
            try:
                return fn(py_value)
            except (TypeError, ValueError):
                pass
        return None

    if isinstance(py_value, list):
        if isinstance(old_node, cst.List):
            return _patch_sequence(py_value, old_node, cst.List)
        fn = Melty._converters.get((list, cst.List))
        if fn is not None:
            try:
                return fn(py_value)
            except (TypeError, ValueError):
                pass
        return None

    if isinstance(py_value, tuple):
        if isinstance(old_node, cst.Tuple):
            return _patch_sequence(py_value, old_node, cst.Tuple)
        fn = Melty._converters.get((tuple, cst.Tuple))
        if fn is not None:
            try:
                return fn(py_value)
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