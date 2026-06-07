"""
libcst ↔ Python type converters for the Melty registry.

Individual CST node types get their own converter pairs. Compound types
(Dict, List, Tuple) call convert() recursively on their children.

Dict results carry __cst__ for lossless round-trip reconstruction.
The original immutable CST node is never serialized — just referenced.
"""

import ast
import enum
import inspect
import math
import struct
import sys
from typing import Any

import libcst as cst
from libcst._nodes.internal import CodegenState as _CodegenState

from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.modes import Modes, _LazyMode
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.view.core_conversion.path_finder import convert, PendingState
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults, Core


def register(fn):
    """Lightweight converter registration — no wrapping, just registry lookup.

    Infers from_type from the first parameter annotation and to_type
    from the return annotation, then registers fn in Melty._converters.
    The function stays unwrapped (no apply/cache_id overhead).
    """
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    from_type = params[0].annotation if params and params[0].annotation is not inspect.Parameter.empty else None
    to_type = sig.return_annotation if sig.return_annotation is not inspect.Signature.empty else None
    if from_type is not None and to_type is not None:
        Melty._converters[(from_type, to_type)] = fn
    return fn

# Sentinel for arguments with no default value.
# Shows up in the dict so the UI can display the parameter name,
# but signals "no default" on the reverse path.

@defaults(show_bg=False, wrap=True, expanded=False, is_tree=False, shadow=False, align_header=False)
class NoDefault():
    """Sentinel for parameters with no default value.

    Shows up in the dict so the UI can display the parameter name,
    but signals "no default" on the reverse path.
    """
    def __repr__(self):
        return "NO_DEFAULT"


NO_DEFAULT = NoDefault()




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


@defaults(tint=(0.0, 0.1706498, 0.2930232286453247, 0.0), shadow=False, is_tree=False, use_cache=True, show_bg=False,
 view_func=RenderFuncs.draw_text, align_header=False)
class CodeLine(str):
    """A raw line/expression of code that couldn't be reduced to a Python value,
    as a str subclass for differentiated dispatch.

    isinstance(c, str) → True, so it works everywhere a string does (equality,
    dict membership, the reverse converter's existing str handling).
    isinstance(c, CodeLine) → True, so it's distinguishable from a genuine
    string literal: the UI can render it as code (monospace / syntax highlight)
    instead of a quoted string, and the reverse path splices it back as an
    expression rather than quoting it.

    The string value IS the source text (e.g. "some_func() + offset"). The text
    alone round-trips — the reverse re-parses it (or preserves the original node
    when it's unchanged), so no CST node needs to be carried here.
    """


@defaults(tint=(0.7, 0.406749, 0.0264792, 0.09), shadow=True, z_offset=-0.5, name_color=(1.0, 0.479, 0.0), font=Font.JETBRAINS_MONO_19,
 is_tree=False, bg_offset=1, header_same_line=True)
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

@defaults(tint=(0.02764737419784069, 0.33023256063461304, 0.2669007480144501, 0.232))
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


@defaults(tint=(0.1, 0.1, 0.1, 0.0), shadow=True, is_tree=False, header_same_line=True, name_color=(1.0, 0.479, 0.0), bg_offset=-3, show_bg=True)
class Try(dict):
    """A try / except / else / finally branch's contents, as a dict subclass.

    isinstance(t, dict) → True, so iteration/access works normally.
    isinstance(t, Try)  → True, so the UI can render it as a try block.

    Each branch of a try statement (the try body, each except handler, the
    else, the finally) becomes its own Try entry under the enclosing scope,
    keyed by its header text. The .header attribute holds that header
    (e.g. "try", "except ValueError as e", "try else", "finally") and the
    dict body holds the branch's assignments — extracted recursively, so
    locals nested inside a try are no longer dropped.
    """

    def __init__(self, *args, header=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.header = header  # e.g. "try", "except ValueError as e", "finally"

    def __bg_hash__(self) -> str:
        keys = ",".join(sorted(str(k) for k in self.keys() if not str(k).startswith("_")))
        return f"Try:{self.header}:{keys}"


@defaults(tint=(0.1, 0.0, 0.0, 0.85), shadow=True, bg_offset=-3, show_bg=True)
class Except(dict):
    """A single except handler's body, as a dict subclass.

    Same shape and round-trip as Try (a branch keyed by its header text, with
    the header in the .header attribute), but a distinct type so the UI can
    render except handlers differently from the try/else/finally branches —
    e.g. "except ValueError as e". Block detection and reverse patching are
    keyed off the header STRING ("except ...", see _parse_edit_keys and
    _patch_try_block_direct), so nothing depends on Try vs Except by type.
    """

    def __init__(self, *args, header=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.header = header  # e.g. "except ValueError as e", "except: TypeError"

    def __bg_hash__(self) -> str:
        keys = ",".join(sorted(str(k) for k in self.keys() if not str(k).startswith("_")))
        return f"Except:{self.header}:{keys}"


@defaults(included="__symbol_usages__", disable_scroll=True)
class GeneralParse(dict):
    def __init__(self, *args, source="", file_path=None, line_offset=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source
        self.file_path: _Path | None = file_path
        self.address = Any | None
        self.line_offset: int = line_offset
        self.usages: dict[str, list['UsageRef']] = {}
        self._bg_hash_cache: str | None = None
        # this would be the address used to load, if available
        self.source_ref = Any | None
        self.symbol_usage = [None]

    # def __bg_hash__(self) -> str:
    #     if self._bg_hash_cache is None:
    #         # hash() on a str uses a fast SipHash - O(n) once, then O(1)
    #         self._bg_hash_cache = str(hash(self.source))
    #     return self._bg_hash_cache


@defaults(tint=(0.04, 0.17, 0.25, 0.016), bg_offset=2, font=Font.JETBRAINS_MONO_19, is_tree=False, shadow=False, z_offset=1, child_kwargs={"font":Font.JETBRAINS_MONO_19})
class CallParse(GeneralParse):
    """A function call's arguments, as a GeneralParse subclass.

    isinstance(c, dict) → True, so iteration/access works normally.
    isinstance(c, GeneralParse) → True, so it renders through draw_collection
        like any other parse (no extra Mode wiring needed).
    isinstance(c, CallParse) → True, so the UI can recognise it as a call.

    The dict is a key/value store of the call's ARGUMENTS keyed by the
    PARAMETER name each binds to. Keyword args key on their keyword; positional
    args are mapped to the parameter name they bind to, resolved from the
    callee's signature (see _call_positional_param_names). When the callee can't
    be resolved, positional args stay in __cst__ and pass through untouched.

    The .func_name attribute holds the called function's name
    (e.g. "my_func", "obj.method") for display.
    """

    def __init__(self, *args, func_name=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.func_name = func_name


@defaults(tint=(0.8139535, 0.6953772, 0.3, 0.7), icon="@", disable_scroll=True)
class DecorationParse(CallParse):
    """A decorator application (`@name(...)`), as a CallParse subclass.

    A decoration IS a call — same `cst.Call` underneath, same args-keyed-by-
    parameter-name structure — so it reuses every bit of CallParse's extraction
    and round-trip. The distinct type is purely for differentiation: the UI can
    route a DecorationParse to its own renderer (e.g. an `@`-prefixed widget)
    while a plain CallParse renders as an ordinary call.

    isinstance(d, CallParse) → True (it is a call), so any call-handling code
    still applies; add a DecorationParse entry ahead of CallParse/GeneralParse
    in the Mode map (MRO resolves most-derived first) to give it its own look.
    """


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


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Usage graph - intra-module (libcst) + cross-module (jedi)                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

from pathlib import Path as _Path


class UsageRef:
    """A single reference to a name from another scope or file.

    Designed as a routable type for draw_any — the UI can render it
    as a clickable link to the call site.
    """
    __slots__ = ("path", "line", "column", "scope", "module_name", "raw")

    def __init__(self, path: _Path | None, line: int, column: int = 0,
                 scope: str = "", module_name: str = ""):
        self.path = path
        self.line = line
        self.column = column
        self.scope = scope
        self.module_name = module_name
        self.raw = None

    def __repr__(self) -> str:
        loc = f"{self.path.name}:{self.line}" if self.path else f":{self.line}"
        return f"UsageRef({loc}, {self.scope!r})"

    def __eq__(self, other):
        if not isinstance(other, UsageRef):
            return NotImplemented
        return (self.path == other.path and self.line == other.line
                and self.column == other.column)

    def __hash__(self):
        return hash((self.path, self.line, self.column))


class SymbolUsage:
    """Complete usage data for a single symbol referenced in a view's source.

    - definition: where the symbol is defined (jedi goto) — drives "jump to def".
    - callers:    every reference to it across the project (jedi get_references),
                  each a UsageRef carrying the caller's file/line/col and the
                  enclosing function/class name in `scope`.
    - sites:      (line, col) of each occurrence of the symbol IN this view's
                  source (file-absolute), so a click can be mapped to the symbol.
    """
    __slots__ = ("name", "definition", "callers", "sites")

    def __init__(self, name, definition=None, callers=None, sites=None):
        self.name = name
        self.definition: 'UsageRef | None' = definition
        self.callers: list['UsageRef'] = callers if callers is not None else []
        self.sites: list[tuple[int, int]] = sites if sites is not None else []

    def __repr__(self):
        d = f"{self.definition.path.name}:{self.definition.line}" if self.definition and self.definition.path else "?"
        return f"SymbolUsage({self.name!r}, def={d}, callers={len(self.callers)})"


# ── Intra-module collector (libcst, fast) ─────────────────────

class _UsageCollector(cst.CSTVisitor):
    """Single-pass visitor that maps defined names → where they're referenced.

    Walks a Module or ClassDef body and tracks:
      - *definitions*: names on the LHS of assignments at the target scope
      - *references*: Name / self.attr nodes that appear in function bodies,
        decorator arguments, default values, etc.

    The result is ``usages``: ``{defined_name: {scope, scope, …}}``
    where each scope is the enclosing function/class name (or ``"<module>"``
    / ``"<class>"`` for top-level / class-body expressions).
    """

    def __init__(self, top_scope: str = "<module>"):
        self._top_scope = top_scope
        self._is_class = top_scope == "<class>"
        self._scope_stack: list[str] = [top_scope]
        self._definitions: dict[str, int] = {}  # name → line number
        self.usages: dict[str, set[str]] = {}

    # ── scope tracking ────────────────────────────────────────

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if self._in_top_scope():
            self._definitions[node.name.value] = 0
        self._scope_stack.append(node.name.value)
        return True

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope_stack.pop()

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if self._in_top_scope():
            self._definitions[node.name.value] = 0
        self._scope_stack.append(node.name.value)
        return True

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._scope_stack.pop()

    # ── definition harvesting (top scope only) ────────────────

    def _in_top_scope(self) -> bool:
        if self._is_class:
            return len(self._scope_stack) == 2
        return len(self._scope_stack) == 1

    def _add_def(self, name: str, node) -> None:
        pos = node.value if hasattr(node, 'value') else node
        # Try to get the CST position for jedi lookups later
        self._definitions[name] = 0  # line placeholder

    def visit_Assign(self, node: cst.Assign) -> None:
        if self._in_top_scope():
            for target in node.targets:
                if isinstance(target.target, cst.Name):
                    self._definitions[target.target.value] = 0

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        if self._in_top_scope():
            if isinstance(node.target, cst.Name):
                self._definitions[node.target.value] = 0

    # ── reference recording ───────────────────────────────────

    def _record(self, name: str) -> None:
        scope = self._scope_stack[-1]
        if self._in_top_scope():
            return
        if name in self._definitions:
            self.usages.setdefault(name, set()).add(scope)

    def visit_Name(self, node: cst.Name) -> None:
        self._record(node.value)

    def visit_Attribute(self, node: cst.Attribute) -> None:
        if (isinstance(node.value, cst.Name)
                and node.value.value == "self"):
            self._record(node.attr.value)


def _collect_intra_usages(tree, top_scope="<module>"):
    """Fast libcst pass — returns (usages_dict, defined_names_set)."""
    collector = _UsageCollector(top_scope)
    tree.visit(collector)
    return collector.usages, set(collector._definitions.keys())


# ── Cross-file reference cache (jedi) ─────────────────────────

# Cache: resolved_path → (mtime, {name: [UsageRef, ...]})
_xref_cache: dict[_Path, tuple[float, dict[str, list[UsageRef]]]] = {}

DISABLE_JEDI = False


# ── Jedi subprocess pool ──────────────────────────────────────
# Runs jedi in a child process so CPU-intensive parso parsing
# doesn't hold the main process GIL.

from concurrent.futures import ProcessPoolExecutor as _PPE
_jedi_pool: _PPE | None = None


def _get_jedi_pool() -> _PPE:
    global _jedi_pool
    # A studio restart-in-place ends the session, which fires concurrent.futures'
    # atexit (_python_exit) even though THIS process keeps running. That sets the
    # module-level _global_shutdown flag and kills the worker processes, so EVERY
    # ProcessPoolExecutor.submit() raises "after global shutdown" forever -
    # silently disabling jedi + every off-GIL task. Clear that stale signal (the
    # process is not actually exiting) and rebuild the pool.
    import concurrent.futures.process as _cfp
    stale = getattr(_cfp, "_global_shutdown", False)
    if stale:
        _cfp._global_shutdown = False
    if _jedi_pool is None or stale or getattr(_jedi_pool, "_shutdown_thread", False):
        _jedi_pool = _PPE(max_workers=4)
    return _jedi_pool


def _jedi_project():
    """A jedi Project scoped to latent-descent src.

    Scoping the project PATH to the src dir (rather than the old `path="."`,
    which resolved to the subprocess CWD and walked a huge tree) keeps jedi's
    reference search inside our code — ~13x faster get_references, same results.
    The repo root is on added_sys_path so `from src.lsd... import X` still
    resolves during inference."""
    import jedi
    src = _SRC_PREFIX.rstrip("/lsd")          # .../latent-descent/src
    repo = str(_Path(src).parent)          # .../latent-descent
    return jedi.Project(path=src, added_sys_path=[repo, src])


def _jedi_script(file_path):
    """jedi.Script on the src-scoped project."""
    import jedi
    return jedi.Script(path=str(file_path), project=_jedi_project())


def shutdown_jedi_pool():
    global _jedi_pool
    if _jedi_pool is not None:
        # Kill worker processes first because shutdown(cancel_futures=True) only
        # cancels pending futures, not ones already running in a subprocess.
        for pid, proc in list(getattr(_jedi_pool, '_processes', {}).items()):
            try:
                proc.kill()
            except Exception:
                pass
        _jedi_pool.shutdown(wait=False, cancel_futures=True)
        _jedi_pool = None


def _jedi_worker(file_path_str: str, names: set[str]) -> dict[str, list[tuple]]:
    """Top-level function executed in a child process.

    Returns {name: [(path_str|None, line, col, scope, module), ...]}.
    Tuples instead of UsageRef because it must be picklable.
    """
    import jedi
    script = _jedi_script(file_path_str)
    resolved = _Path(file_path_str).resolve()

    # Find name positions in the file
    full_lines = resolved.read_text().splitlines()
    name_positions: dict[str, tuple[int, int]] = {}
    for i, line_text in enumerate(full_lines):
        for name in names:
            if name in name_positions:
                continue
            stripped = line_text.lstrip()
            if (stripped.startswith(name)
                    and len(stripped) > len(name)
                    and stripped[len(name)] in (' ', ':', '=')):
                name_positions[name] = (i + 1, line_text.index(name))
            elif stripped.startswith(f"class {name}"):
                name_positions[name] = (i + 1, line_text.index(name))
            elif stripped.startswith(f"def {name}"):
                name_positions[name] = (i + 1, line_text.index(name))

    result: dict[str, list[tuple]] = {}
    for name, (line, col) in name_positions.items():
        try:
            refs = script.get_references(line, col)
            usage_list = []
            for ref in refs:
                ref_path = str(ref.module_path) if ref.module_path else None
                if (ref_path and _Path(ref_path).resolve() == resolved
                        and ref.line == line):
                    continue
                usage_list.append((
                    ref_path, ref.line, ref.column,
                    ref.full_name or "", ref.module_name or "",
                ))
            if usage_list:
                result[name] = usage_list
        except Exception:
            continue
    return result


def _jedi_subprocess(file_path_str: str,
                     names: set[str]) -> dict[str, list[UsageRef]]:
    """Submit jedi work to the child process and convert results to UsageRef."""
    pool = _get_jedi_pool()
    future = pool.submit(_jedi_worker, file_path_str, names)
    raw = future.result()  # blocks this thread, but NOT the main process GIL
    result: dict[str, list[UsageRef]] = {}
    for name, tuples in raw.items():
        result[name] = [
            UsageRef(
                path=_Path(t[0]) if t[0] else None,
                line=t[1], column=t[2],
                scope=t[3], module_name=t[4], raw=t,
            )
            for t in tuples
        ]
    return result


# ── Intra-module usage collection (off-GIL) ───────────────────
# The _UsageCollector visit is CPU-bound pure Python, so running it in a thread
# holds the GIL and stalls the render thread. For large trees we run it in the
# same child-process pool as jedi (separate interpreter = no GIL); small
# trees stay in-process because the IPC round-trip would cost more than the
# visit. Mirrors _jedi_subprocess. Its output is plain {name: [scopes]} - no
# libcst nodes cross the process boundary.
_USAGE_SUBPROCESS_MIN_CHARS = 2000


def _intra_usage_worker(source: str, is_class: bool) -> dict[str, list[str]]:
    """Top-level function executed in a child process: parse + collect usages.

    Returns {defined_name: [scopes]} (picklable). Re-parses from source rather
    than receiving a libcst tree, which doesn't pickle cheaply."""
    module = cst.parse_module(source)
    if is_class and module.body:
        tree = module.body[0]
        collector = _UsageCollector("<class>")
    else:
        tree = module
        collector = _UsageCollector("<module>")
    tree.visit(collector)
    return {name: sorted(scopes) for name, scopes in collector.usages.items()}


# ── Per-symbol caller index (jedi, on save) ───────────────────
# For each src symbol referenced in a view's source, resolve its definition
# (goto) and every caller (get_references), so the editor can offer caller
# shortcuts. Project-wide reference search is expensive, so it runs in the
# child-process pool and is cached per file mtime - effectively once per save.

_symbol_usage_cache: dict = {}  # (resolved_path, start, end) -> (mtime, {sym: SymbolUsage})

# File suffixes to drop from jedi search - a symbol DEFINED in one of these is
# skipped entirely, and any callers in them are filtered out. jedi only
# searches .py/.pyi to begin with (no per-extension search hook), so this is how
# you exclude by extension. str.endswith takes the tuple directly.
_JEDI_EXCLUDE_SUFFIXES: tuple = (".pyi",)


def _symbol_refs_worker(file_path: str, start_line: int, end_line: int) -> dict:
    """Child-process worker: for each distinct symbol occurring in
    [start_line, end_line], resolve its definition + project references.

    Returns {symbol: {"sites": [(l,c)], "definition": (path,l,c,mod),
                      "callers": [(path,l,c,enclosing,mod), ...]}}.
    Only symbols DEFINED under src are kept (no callers of len/print/etc.)."""
    import jedi
    script = _jedi_script(file_path)
    try:
        names = script.get_names(all_scopes=True, references=True, definitions=True)
    except Exception:
        return {}

    sites: dict = {}
    rep: dict = {}
    for n in names:
        ln = n.line
        if ln is None or ln < start_line or ln > end_line:
            continue
        sites.setdefault(n.name, []).append((ln, n.column))
        rep.setdefault(n.name, (ln, n.column))

    out = {}
    for sym, (line, col) in rep.items():
        try:
            defs = script.goto(line, col, follow_imports=True)
        except Exception:
            defs = []
        if not defs:
            continue
        d = defs[0]
        dp = str(d.module_path) if d.module_path else None
        if dp is None or not dp.startswith(_SRC_PREFIX) or dp.endswith(_JEDI_EXCLUDE_SUFFIXES):
            continue  # out-of-src or excluded-extension symbol
        callers = []
        try:
            refs = script.get_references(line, col, include_builtins=False)
        except Exception:
            refs = []
        for r in refs:
            rp = str(r.module_path) if r.module_path else None
            if rp and rp.endswith(_JEDI_EXCLUDE_SUFFIXES):
                continue  # drop callers in excluded-extension files
            try:
                ctx = r.get_context()
                enclosing = ctx.name if ctx is not None else ""
            except Exception:
                enclosing = ""
            callers.append((rp, r.line, r.column, enclosing, r.module_name or ""))
        out[sym] = {
            "sites": sites[sym],
            "definition": (dp, d.line, d.column, d.module_name or ""),
            "callers": callers,
        }
    return out


def _rebuild_symbol_usages(raw: dict) -> dict:
    """Rebuild {symbol: SymbolUsage} from the worker's plain-tuple output."""
    result = {}
    for sym, e in raw.items():
        dp, dl, dc, dm = e["definition"]
        definition = UsageRef(path=_Path(dp) if dp else None, line=dl, column=dc, module_name=dm)
        callers = [UsageRef(path=_Path(c[0]) if c[0] else None, line=c[1], column=c[2],
                            scope=c[3], module_name=c[4]) for c in e["callers"]]
        result[sym] = SymbolUsage(name=sym, definition=definition, callers=callers,
                                  sites=[tuple(s) for s in e["sites"]])
    return result


def _symbol_refs_local(file_path: str, start_line: int, end_line: int) -> dict:
    """Run the symbol-refs worker IN-PROCESS (main process). Call me on a
    BACKGROUND thread — jedi is CPU-bound and holds the GIL (~1s), briefly
    slowing the render thread. Much faster than the pool here (~1s vs ~20s)."""
    return _rebuild_symbol_usages(_symbol_refs_worker(file_path, start_line, end_line))


# ── Fast caller index (import resolution, no jedi) ──────────────────
# Builds the SAME {symbol: {sites, definition, callers}} as the jedi worker, but
# without jedi. Only two reference kinds resolved through each file's MODULE
# NAMESPACE:
#   - module-level symbols  -> bare-name refs, resolved by live-object identity
#   - class members         -> `ClassName.member` attribute refs, resolved by
#                              (class-object, attr)
# A walk all the loaded src files (ast - fast), cached per file mtime so a
# re-index only re-parses changed files. Misses what jedi catches: re-exports,
# `import x; x.Sym` chains, `from x import *`, `self.member` instance access, and
# locals. Toggle Toggles.jedi_correctness to A/B-test the full jedi path.

_index_refs_cache: dict = {}   # resolved_path -> (mtime, [(kind, key, line, col, scope)])


def _src_mod_map() -> dict:
    """{resolved_file: module} for every loaded src module. Dual import paths
    (`src.lsd...` vs `lsd...`) create DUPLICATE modules for the same file with
    DIFFERENT live objects; the app imports via `src.`, so prefer the
    `src.`-prefixed module so refs and targets resolve to the same objects."""
    mod_map = {}
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not (f and f.startswith(_SRC_PREFIX)):
            continue
        try:
            rp = _Path(f).resolve()
        except (OSError, ValueError):
            continue
        existing = mod_map.get(rp)
        if existing is None or (mod.__name__.startswith("src.")
                                and not existing.__name__.startswith("src.")):
            mod_map[rp] = mod
    return mod_map


def _collect_refs(tree) -> list:
    """Reference occurrences in a module AST:
      ("name", name, line, col, scope)            -- a bare Name
      ("attr", (base_name, attr), line, col, scope) -- `base_name.attr` access
    col is 0-indexed; scope is the nearest enclosing def/class."""
    out = []

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append(("name", child.name, child.lineno, child.col_offset, scope))
                walk(child, child.name)
            elif isinstance(child, ast.Attribute):
                if isinstance(child.value, ast.Name):
                    out.append(("attr", (child.value.id, child.attr),
                                child.lineno, child.col_offset, scope))
                walk(child, scope)          # also records the base case beneath
            elif isinstance(child, ast.Name):
                out.append(("name", child.id, child.lineno, child.col_offset, scope))
            else:
                walk(child, scope)

    walk(tree, "<module>")
    return out


def _file_index_refs(path, module) -> list:
    """Resolved references in one src file, cached by mtime:
      ("name", id(obj)|None, line, col, scope)         -- bare name -> object id
      ("attr", (id(base)|None, attr)|None, ...)        -- base.attr -> (base id, attr)
    Resolution is via the file's module namespace (no triggering)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _index_refs_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    od = getattr(module, "__dict__", None)
    refs = []
    if od is not None:
        try:
            for (kind, payload, line, col, scope) in _collect_refs(ast.parse(path.read_text())):
                if kind == "name":
                    obj = od.get(payload)
                    refs.append(("name", id(obj) if obj is not None else None, line, col, scope))
                else:
                    base = od.get(payload[0])
                    key = (id(base), payload[1]) if base is not None else None
                    refs.append(("attr", key, line, col, scope))
        except Exception:
            refs = []
    _index_refs_cache[path] = (mtime, refs)
    return refs


def _collect_targets(file_tree, module, s: int, e: int):
    """Resolve the symbols DEFINED in span [s, e] against live objects, walking
    the file's container chain (module -> class -> nested). Returns:
      obj_targets  {id(obj): name}            -- module-level defs/vars
      mem_targets  {(id(class), name): name}  -- class members
      sites        {name: [(line, col)]}      -- occurrences of the def in span
      def_lines    {name: line}               -- the def line
    Module-level symbols are found by bare-name callers; members by ClassName.member."""
    obj_targets, mem_targets, obj_by_name, sites, def_lines = {}, {}, {}, {}, {}
    _ModuleType = type(sys)

    def add(name, line, col, container, obj):
        if not (s <= line <= e):
            return
        sites.setdefault(name, []).append((line, col))
        def_lines.setdefault(name, line)
        if isinstance(container, _ModuleType):
            if obj is not None:
                obj_targets[id(obj)] = name
                obj_by_name[name] = obj
        elif container is not None:
            mem_targets[(id(container), name)] = name

    def walk(node, container):
        cd = getattr(container, "__dict__", None) or {}
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                obj = cd.get(child.name)
                add(child.name, child.lineno, child.col_offset, container, obj)
                walk(child, obj)             # recurse with the def's object as container
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                tgts = child.targets if isinstance(child, ast.Assign) else [child.target]
                for t in tgts:
                    if isinstance(t, ast.Name):
                        add(t.id, child.lineno, t.col_offset, container, cd.get(t.id))
            else:
                walk(child, container)

    walk(file_tree, module)
    return obj_targets, mem_targets, obj_by_name, sites, def_lines


def _symbol_refs_index(file_path: str, start_line: int, end_line: int) -> dict:
    """jedi-free fast path. Resolve the span's symbols (module-level + class
    members) against live objects, then find callers across loaded src files —
    bare-name refs for module-level, ClassName.member refs for members. Returns
    the same raw shape as _symbol_refs_worker."""
    resolved = _Path(file_path).resolve()
    mod_map = _src_mod_map()
    owning = mod_map.get(resolved)
    if owning is None:
        return {}
    try:
        file_tree = ast.parse(resolved.read_text())
    except Exception:
        return {}
    obj_targets, mem_targets, obj_by_name, sites, def_lines = _collect_targets(
        file_tree, owning, start_line, end_line)
    # Also target module-level names REFERENCED (not defined) in the span -
    # decorators (@window/@defaults), used imports (WindowMode) - so they're
    # clickable too. Member accesses (foo.attr) are already covered by mem_targets.
    od = getattr(owning, "__dict__", None) or {}
    for (kind, payload, line, col, scope) in _collect_refs(file_tree):
        if kind == "name" and start_line <= line <= end_line:
            obj = od.get(payload)
            if obj is not None and id(obj) not in obj_targets:
                obj_targets[id(obj)] = payload
                obj_by_name.setdefault(payload, obj)
                sites.setdefault(payload, []).append((line, col))
                def_lines.setdefault(payload, line)
    if not obj_targets and not mem_targets:
        return {}

    callers = {}
    for path, mod in mod_map.items():
        for (kind, key, line, col, scope) in _file_index_refs(path, mod):
            if key is None:
                continue
            nm = obj_targets.get(key) if kind == "name" else mem_targets.get(key)
            if nm is not None:
                callers.setdefault(nm, []).append(
                    (str(path), line, col, scope, getattr(mod, "__name__", "") or ""))

    rp_str = str(resolved)
    mod_name = getattr(owning, "__name__", "") or ""
    out = {}
    for nm in sites:                              # every target name has sites
        obj = obj_by_name.get(nm)
        if obj is not None:                       # module-level: real source via inspect
            try:
                df = inspect.getsourcefile(obj)
                dl = inspect.getsourcelines(obj)[1]
                dm = getattr(obj, "__module__", "") or mod_name
            except (TypeError, OSError):
                df, dl, dm = rp_str, def_lines.get(nm, 0), mod_name
        else:                                     # class member: defined in this file
            df, dl, dm = rp_str, def_lines.get(nm, 0), mod_name
        out[nm] = {
            "sites": sites[nm],
            "definition": (df, dl, 0, dm),
            "callers": callers.get(nm, []),
        }
    return out


def _distribute_by_name(gp, flat: dict) -> None:
    """Attach each symbol's usage to the GeneralParse node that DIRECTLY contains
    it (its immediate parent), recursing into nested GeneralParse children. A
    symbol never lands on a grandparent — each node owns only its own keys."""
    own = {}
    for k, v in list(gp.items()):
        if k == "__cst__":
            continue
        if isinstance(v, GeneralParse):
            _distribute_by_name(v, flat)
        if isinstance(k, str) and k in flat:
            own[k] = flat[k]
    if own:
        gp["__symbol_usages__"] = own


def compute_symbol_usages_for_address(address) -> dict:
    """Build {symbol: SymbolUsage} (callers + definition) for an address's source
    span, via in-process jedi. The entry point for the editor's manual trigger;
    run it on a background thread. Cached per file mtime, so a re-trigger on an
    unchanged file is free. Works for a module, class, or function span."""
    if DISABLE_JEDI or address is None or getattr(address, "path", None) is None:
        return {}
    resolved = _Path(address.path).resolve()
    start = (getattr(address, "start", 0) or 0) + 1     # address.start is a 0-indexed lower bound
    end = getattr(address, "end", None)
    if end is None:                                     # whole-file span
        try:
            end = resolved.read_text().count("\n") + 1
        except OSError:
            return {}
    return _compute_symbol_usages(resolved, start, end)


def _compute_symbol_usages(resolved, start, end) -> dict:
    """Cache + A/B branch core: {symbol: SymbolUsage} for a [start, end] span.
    Toggles.jedi_correctness picks the resolver — jedi (accurate, slow:
    re-exports / dotted access / locals) vs the import index (fast, direct
    imports of module-level symbols). Cached per (file mtime, resolver) so
    flipping the toggle re-computes for a clean comparison."""
    from src.lsd.gl_gui.toggles import Toggles   # lazy to avoid import cycle
    accurate = getattr(Toggles, "jedi_correctness", False)
    key = (resolved, start, end)
    try:
        mtime = resolved.stat().st_mtime
    except OSError:
        mtime = None
    cached = _symbol_usage_cache.get(key)
    if cached is not None and cached[0] == mtime and cached[2] == accurate:
        return cached[1]
    try:
        raw = (_symbol_refs_worker(str(resolved), start, end) if accurate
               else _symbol_refs_index(str(resolved), start, end))
        usages = _rebuild_symbol_usages(raw)
    except Exception:
        usages = {}
    _symbol_usage_cache[key] = (mtime, usages, accurate)
    return usages


def populate_symbol_usages(gp: GeneralParse) -> None:
    """Fill gp['__symbol_usages__'] for the src symbols in this view's source,
    via in-process jedi (run me on a background thread). Cached per file mtime."""
    file_path = getattr(gp, "file_path", None)
    if file_path is None or DISABLE_JEDI:
        return
    resolved = _Path(file_path).resolve()
    source = getattr(gp, "source", "") or ""
    start = (getattr(gp, "line_offset", 0) or 0) + 1   # lines are 1-indexed
    end = start + source.count("\n")
    usages = _compute_symbol_usages(resolved, start, end)
    gp["__symbol_usages__"] = usages
    gp.symbol_usage = usages


def invalidate_usage_cache(path: _Path | str | None = None) -> None:
    """Drop cached cross-file references for a path, or all if None."""
    print("Invalidating usage cache for", path if path else "ALL PATHS")
    if path is None:
        _xref_cache.clear()
        _symbol_usage_cache.clear()
    else:
        resolved = _Path(path).resolve()
        _xref_cache.pop(resolved, None)
        for k in [k for k in _symbol_usage_cache if k[0] == resolved]:
            _symbol_usage_cache.pop(k, None)


def _get_cross_file_usages(
    file_path: _Path,
    defined_names: set[str],
    source: str,
    line_offset: int = 0,
) -> dict[str, list[UsageRef]]:
    if DISABLE_JEDI:
        return {}
    """Look up cross-file references for defined_names using jedi.

    Results are cached per-file by mtime.  Only names in defined_names
    are queried — this keeps the jedi call count bounded.
    """
    resolved = file_path.resolve()

    # Check cache validity - only query jedi for names not yet cached
    try:
        cached_mtime, cached_result = _xref_cache.get(resolved, (0.0, None))
        actual_mtime = resolved.stat().st_mtime
        if cached_result is not None and cached_mtime == actual_mtime:
            missing = defined_names - cached_result.keys()
            if not missing:
                return {n: cached_result[n] for n in defined_names
                        if n in cached_result}
            # Only look up names not already in cache
            defined_names = missing
    except OSError:
        cached_result = None

    # Run jedi in a child process so its CPU-bound nature
    # doesn't hold the GIL and stall the UI thread.
    try:
        result = _jedi_subprocess(str(resolved), defined_names)
    except Exception:
        return {}

    # Merge new results into cache (don't overwrite prior lookups)
    try:
        if cached_result is not None:
            cached_result.update(result)
            result = cached_result
        _xref_cache[resolved] = (resolved.stat().st_mtime, result)
    except OSError:
        pass

    return {n: result[n] for n in defined_names if n in result}


# ── Combined collection ───────────────────────────────────────

def _collect_usages(
    tree: cst.Module | cst.ClassDef,
    top_scope: str = "<module>",
) -> dict[str, list[UsageRef]]:
    """Collect intra-module usages (the libcst visit).

    For large trees the visit runs in the jedi child-process pool so its
    CPU-bound pure-Python work doesn't hold the GIL and stall the render thread;
    small trees stay in-process because IPC would cost more than the visit.
    Cross-file references are populated separately via populate_usages().
    """
    is_class = top_scope == "<class>"
    if isinstance(tree, cst.Module):
        source = tree.code
    else:
        try:
            source = cst.Module(body=[tree]).code
        except Exception:
            source = None

    raw: dict[str, list[str]] | None = None
    if source is not None and len(source) >= _USAGE_SUBPROCESS_MIN_CHARS:
        # .result() blocks THIS thread (the background analysis thread) but
        # releases the GIL while the child works, so the render thread runs.
        try:
            raw = _get_jedi_pool().submit(
                _intra_usage_worker, source, is_class).result()
        except Exception:
            raw = None  # child unavailable/failed - fall back to in-process

    if raw is None:
        intra, _ = _collect_intra_usages(tree, top_scope)
        raw = {name: sorted(scopes) for name, scopes in intra.items()}

    return {
        name: [UsageRef(path=None, line=0, scope=s, module_name="") for s in scopes]
        for name, scopes in raw.items()
    }


def populate_usages(gp: GeneralParse) -> None:
    """Populate cross-file UsageRefs on a GeneralParse and its children.

    Intra-module usages are already populated during construction
    (cst_module_to_dict / cst_classdef_to_dict).  This adds cross-file
    references via jedi (cached per-file by mtime).

    Safe to call from a background thread — does not touch imgui
    or Melty state.  Call this OUTSIDE the stateful converter chain:

        Background.run(populate_usages,
                       func_kwargs={"gp": result},
                       stateful=False)
    """
    print("Populating cross-file usages for", gp.file_path)
    file_path = gp.file_path
    if file_path is not None:
        _populate_xrefs(gp, file_path)
    # Per-symbol usage/definition index for the symbols referenced in this view
    # (the data behind caller shortcuts). Cached per file mtime → reset per save.
    populate_symbol_usages(gp)


# Keep old name as alias
populate_cross_file_usages = populate_usages


def _populate_xrefs(gp, file_path: _Path) -> None:
    """Recursively populate cross-file usages on a GeneralParse tree."""
    defined = {k for k in gp
               if not _is_dunder(k)
               and not isinstance(k, Comment)
               and isinstance(k, str)
               and k not in ("decorators", "parameters", "locals")}
    if defined:
        xrefs = _get_cross_file_usages(file_path, defined, source="",
                                        line_offset=0)
        for name, refs in xrefs.items():
            gp.usages.setdefault(name, []).extend(refs)

    for key, child in gp.items():

        if _is_dunder(key):
            continue
        if isinstance(child, GeneralParse) and "__cst__" in child:
            # Propagate parent's usages for this key down to the child,
            # so viewing the function/class shows where IT is referenced.
            if key in gp.usages:
                child.usages.setdefault(key, []).extend(gp.usages[key])
            _populate_xrefs(child, file_path)


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


@register
def function_to_str(value: types.FunctionType) -> str:
    """Get the source code of a function as a string."""
    return inspect.getsource(value)


@register
def type_to_str(value: type) -> str:
    """Get the source code of a class as a string."""
    return inspect.getsource(value)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  str ↔ cst.Module                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@register
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


@register
def cst_module_to_str(value: cst.Module) -> str:
    return value.code


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Source spans - two-way line ↔ node map for the CST dict                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
#
# Every dict node produced below can carry the source position (`.span`) of the
# code it was parsed from, so a text editor showing the span can map a line to
# the node it belongs to and back. Lines are 1-INDEXED but RELATIVE to the
# parsed source - the root GeneralParse.source, i.e. the displayed function /
# class span; columns are 0-indexed. Add the span's file line offset
# (Address.start / GeneralParse.line_offset) for absolute file lines - Span
# and LineMap take that offset.
#
# Positions come from libcst's PositionProvider, resolved once per top-level
# conversion and published on a thread-local (mirrors _module_scope). The
# wrapper is built with unsafe_skip_copy=True so the provider keys on the SAME
# node objects the extractors walk - a deep copy would make every lookup miss.

import threading as _threading_spans
from libcst.metadata import (MetadataWrapper as _MetadataWrapper,
                             PositionProvider as _PositionProvider)


class Span:
    """A source position range for a node in the CST dict.

    Lines are 1-indexed and relative to the parsed source the node came from
    (the root GeneralParse.source); columns are 0-indexed. `start_char` aliases
    `start_col`. `.absolute(line_offset)` shifts the lines into absolute file
    coordinates — `line_offset` is the span's 0-indexed first file line (e.g.
    Address.start), so relative line 1 → file line line_offset + 1.
    """
    __slots__ = ("start_line", "start_col", "end_line", "end_col")

    def __init__(self, start_line, start_col, end_line, end_col):
        self.start_line = start_line
        self.start_col = start_col
        self.end_line = end_line
        self.end_col = end_col

    @property
    def start_char(self):
        return self.start_col

    def contains(self, line):
        return self.start_line <= line <= self.end_line

    def absolute(self, line_offset):
        return Span(self.start_line + line_offset, self.start_col,
                    self.end_line + line_offset, self.end_col)

    def __repr__(self):
        return (f"Span({self.start_line}:{self.start_col}"
                f"–{self.end_line}:{self.end_col})")

    def __eq__(self, other):
        return (isinstance(other, Span)
                and (self.start_line, self.start_col, self.end_line, self.end_col)
                == (other.start_line, other.start_col, other.end_line, other.end_col))

    def __hash__(self):
        return hash((self.start_line, self.start_col, self.end_line, self.end_col))


_span_scope = _threading_spans.local()


def _active_positions():
    return getattr(_span_scope, "positions", None)


class _position_map:
    """Publish a PositionProvider for `module` on the thread-local for the
    duration of a conversion, so nested extractors can stamp spans via
    `_span_of`. Failed/absent resolution degrades to no spans (None)."""

    def __init__(self, module):
        self._module = module

    def __enter__(self):
        self._prev = getattr(_span_scope, "positions", None)
        try:
            wrapper = _MetadataWrapper(self._module, unsafe_skip_copy=True)
            _span_scope.positions = wrapper.resolve(_PositionProvider)
        except Exception:
            _span_scope.positions = None
        return self

    def __exit__(self, *exc):
        _span_scope.positions = self._prev


def _span_of(node):
    """Span for a CST `node` from the active provider, or None when no provider
    is active (conversion outside _position_map) or the node isn't in it."""
    positions = _active_positions()
    if positions is None or node is None:
        return None
    cr = positions.get(node)
    if cr is None:
        return None
    return Span(cr.start.line, cr.start.column, cr.end.line, cr.end.column)


def _union_span(cst_nodes):
    """Span covering several cst nodes (None entries skipped), or None."""
    spans = [s for s in (_span_of(n) for n in cst_nodes) if s is not None]
    if not spans:
        return None
    start = min(spans, key=lambda s: (s.start_line, s.start_col))
    end = max(spans, key=lambda s: (s.end_line, s.end_col))
    return Span(start.start_line, start.start_col, end.end_line, end.end_col)


def _stamp_span(node_obj, cst_node):
    """Attach `.span` to a container dict node — the position of `cst_node`, or
    `cst_node` itself if it's already a Span. No-op without a range. Returns
    node_obj for call-site chaining."""
    span = cst_node if isinstance(cst_node, Span) else _span_of(cst_node)
    if span is not None:
        node_obj.span = span
    return node_obj


def _record_child(container, key, value, cst_node):
    """Record the span of a LEAF child (`container[key]`, source `cst_node`) in
    the container's `_child_spans` map. Skipped for dict-valued children — those
    are containers that carry their own `.span` and are found by tree walk.
    Lets literal assignments (a plain int/str/bool that can't hold a `.span`) be
    located by line."""
    if isinstance(value, dict):
        return
    span = _span_of(cst_node)
    if span is None:
        return
    cs = getattr(container, "_child_spans", None)
    if cs is None:
        cs = {}
        container._child_spans = cs
    cs[key] = span


def _merge_child_spans(dst, src):
    """Move src's `_child_spans` into dst's — needed wherever a container is
    rebuilt from another via dict-copy (Conditional.update(body), Loop(body),
    Try(body), …), which copies items but NOT the `_child_spans` attribute."""
    src_cs = getattr(src, "_child_spans", None)
    if not src_cs:
        return
    dst_cs = getattr(dst, "_child_spans", None)
    if dst_cs is None:
        dst_cs = {}
        dst._child_spans = dst_cs
    dst_cs.update(src_cs)


class NodeRef:
    """A node located by line lookup: the `value`, its `key` in `parent` (None
    for the root), the `parent` container, the `Span` it occupies (relative to
    the parse source), and the full key-`path` from the root."""
    __slots__ = ("value", "key", "parent", "span", "path")

    def __init__(self, value, key, parent, span, path):
        self.value = value
        self.key = key
        self.parent = parent
        self.span = span
        self.path = path

    def __repr__(self):
        return f"NodeRef(path={self.path!r}, {self.span!r})"


class LineMap:
    """Two-way map between source lines and the nodes of a CST dict.

    Build from a root parse: `lm = LineMap(general_parse)`. Pass `line_offset`
    (the span's 0-indexed first file line, e.g. Address.start) to query/return
    in absolute file lines.

      line → node:  lm.node_at_line(34)            # relative to the parse source
                    lm.node_at_line(412, absolute=True)
      node → line:  node.span                       # directly on the node
                    lm.span_of(node)                # also works for leaf values

    Reverse direction is really just a node's `.span`; this class indexes the
    forward direction and resolves leaf children (via `_child_spans`) that
    can't carry their own attribute.
    """

    def __init__(self, root, line_offset=0):
        self.root = root
        self.line_offset = line_offset
        self._entries = []  # list of (span, depth, NodeRef)
        self._build(root, key=None, parent=None, path=(), depth=0)

    def _build(self, node, key, parent, path, depth):
        span = getattr(node, "span", None)
        if isinstance(span, Span):
            self._entries.append((span, depth, NodeRef(node, key, parent, span, path)))
        if isinstance(node, dict):
            child_spans = getattr(node, "_child_spans", None) or {}
            for k, v in node.items():
                cpath = path + (k,)
                if isinstance(v, dict):
                    self._build(v, k, node, cpath, depth + 1)
                else:
                    cspan = child_spans.get(k)
                    if isinstance(cspan, Span):
                        self._entries.append(
                            (cspan, depth + 1, NodeRef(v, k, node, cspan, cpath)))

    def node_at_line(self, line, absolute=False):
        """The most specific NodeRef whose span contains `line`, or None.

        `line` is relative to the parse source unless `absolute=True`, when
        `line_offset` is subtracted first. 'Most specific' = deepest node, tie
        broken by narrowest line range — so an assignment inside an if-branch
        wins over the branch, which wins over the function."""
        if absolute:
            line = line - self.line_offset
        best = None  # (depth, size, ref)
        for span, depth, ref in self._entries:
            if span.start_line <= line <= span.end_line:
                size = span.end_line - span.start_line
                if best is None or depth > best[0] or (depth == best[0] and size < best[1]):
                    best = (depth, size, ref)
        return best[2] if best else None

    def span_of(self, node, absolute=False):
        """The span of `node` — via its `.span`, else by identity in the index.
        Returns None if unknown; `absolute=True` shifts into file lines."""
        span = getattr(node, "span", None)
        if not isinstance(span, Span):
            for s, _depth, ref in self._entries:
                if ref.value is node:
                    span = s
                    break
        if not isinstance(span, Span):
            return None
        return span.absolute(self.line_offset) if absolute else span


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.Module ↔ dict                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@register
def cst_module_to_dict(input_value: cst.Module, run_jedi=False, **kwargs) -> dict:
    """Top-level statements become readable dict keys.

    Handles: assignments, annotated assignments, class definitions,
    function definitions (default args), and decorator kwargs.
    """
    if not isinstance(input_value, cst.Module):
        print("Expected cst.Module, got", type(input_value).__name__, file=sys.stderr)
        return input_value
    readable = GeneralParse(source=input_value.code)
    _stamp_span(readable, input_value)

    # Publish the src global scope so every nested name/callable resolution
    # below (values, classdef/funcdef defaults) resolves against project src
    # only, no per-usage sys.modules scan. Built once here; nested classdef /
    # funcdef conversions inherit it. The _position_map publishes a
    # PositionProvider / the node span so extractors can stamp source spans
    # (.span / _child_spans) with the line ↔ node map.
    with _position_map(input_value), _module_scope(_build_src_scope()):
        # Module header comments (top-of-file, before first statement)
        for ll in input_value.header:
            if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
                c = Comment(ll.comment.value)
                readable[c] = c
                _merge_override_comment(c, readable)

        _classdef_to_dict = Melty._converters.get((cst.ClassDef, dict))
        _funcdef_to_dict = Melty._converters.get((cst.FunctionDef, dict))

        # Sibling defs let a bare top-level caller bind its positional args to
        # parameter names; call_seen keys repeat calls (configure()#1, ...).
        local_sigs = _collect_local_signatures(input_value.body)
        call_seen: dict[str, int] = {}

        for stmt in input_value.body:
            if isinstance(stmt, cst.SimpleStatementLine):
                # Leading comments (override comments routed to the field below)
                _extract_leading_comments(stmt, readable, skip_overrides=True)

                last_key = None
                for node in stmt.body:
                    # x = 0  /  x: int = 0 - keyed by the plain target name.
                    name = _assign_target_name(node)
                    if name is not None:
                        val_node = _assign_value_node(node)
                        py_value = (_cst_to_python_or_raw(val_node)
                                    if val_node is not None else _UNREADABLE)
                        if py_value is not _UNREADABLE:
                            readable[name] = py_value
                            _record_child(readable, name, py_value, node)
                            last_key = name
                        continue
                    # Surface a function call so its args are visible/editable: a bare
                    # call (configure(debug=True)) OR a call assigned to a NON-Name
                    # target (changed, new_dict = draw_collection(...)). The latter
                    # has to hit the Assign branch, fail the `isinstance Name` check,
                    # and surface nothing - the gap that made a lone call line parse
                    # to an empty dict. Mirrors _extract_block_assignments so a call
                    # statement surfaces the same at module level as in a method body.
                    call_node = _stmt_call_node(node)
                    if call_node is not None:
                        ck = _surface_call(call_node, readable, call_seen, local_sigs)
                        if ck is not None:
                            last_key = ck

                # Trailing statement comment
                _extract_trailing_comment(stmt, last_key, readable)
                _attach_field_override(stmt, last_key, readable)

            elif isinstance(stmt, cst.ClassDef):
                _extract_leading_comments(stmt, readable, skip_overrides=True)
                if _classdef_to_dict is not None:
                    try:
                        child = _classdef_to_dict(stmt)
                        _attach_leading_override(stmt, child)
                        readable[stmt.name.value] = child
                    except (TypeError, ValueError):
                        pass

            elif isinstance(stmt, cst.FunctionDef):
                _extract_leading_comments(stmt, readable, skip_overrides=True)
                if _funcdef_to_dict is not None:
                    try:
                        child = _funcdef_to_dict(stmt)
                        _attach_leading_override(stmt, child)
                        readable[stmt.name.value] = child
                    except (TypeError, ValueError):
                        pass

        readable["__cst__"] = input_value
        # readable.usages = _collect_usages(input_value, top_scope="<module>")

    # The "Jump" button rides its bool in as run_jedi (the chain passes **extra
    # through). When set, run jedi here - on the editor's main thread - and
    # hang the caller/definition index straight onto the gp. `jump_to` is the
    # resolved source address (file + line span) jedi needs; it rides in the chain
    # **extra. Because this gp IS what the editor draws, nothing downstream wires.

    if run_jedi:
        print("Running jedi for cross-file usages on", readable.file_path)
        address = kwargs.get("jump_to")
        if address is not None:
            try:
                # Flat {symbol: SymbolUsage} for the whole span (module-level
                # symbols AND class members), distributed down the gp tree: each
                # GeneralParse node gets __symbol_usages__ for its OWN keys, so a
                # member lands on its own node, not the class/module wrapper.
                flat = compute_symbol_usages_for_address(address)
                readable.symbol_usage = flat       # whole-span index (debug access)
                _distribute_by_name(readable, flat)
            except Exception:
                pass
    return readable


@register
def dict_to_cst_module(input_value: dict) -> cst.Module:
    """Rebuild from __cst__, patching in any edited values.

    Handles assignments, ClassDef __init__ self-assignments, and
    decorator keyword arguments.
    """
    tree = input_value.get("__cst__")
    if tree is None:
        raise ValueError("Dict has no __cst__ key")
    if not isinstance(tree, cst.Module):
        raise TypeError(f"Expected cst.Module in __cst__, got {type(tree).__name__}")

    edits = {k: v for k, v in input_value.items()
             if not (_is_dunder(k))
             and not isinstance(k, Comment)}

    try:
        result = tree
        if edits:
            # Resolve names against the SAME src scope the forward call used, so a
            # dict key written as a callable/constant (Conditional, draw_collection,
            # cst_module_to_dict, ...) resolves identically here. Without it those
            # keys read back as _UNREADABLE during the patch, so dict_to_cst_dict
            # keeps the original version AND appends a regenerated duplicate -
            # bloating every kwarg dict with single-argument copies on each parse.
            with _module_scope(_build_src_scope()):
                result = result.visit(_ModulePatcher(edits))

        # Patch comments (module header & body)
        all_comment_edits = _collect_comment_edits(input_value)
        if all_comment_edits:
            result = _patch_module_comments(result, input_value)

        result = _ensure_override_comment(result, input_value)
        result = _apply_field_overrides(result, input_value.get("__overrides__"))
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
# ║  Standalone wrappers for convert_in / convert_out chains                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝



def dict_to_cst(value) -> cst.Module:
    """Reverse: GeneralParse dict → cst.Module.

    Thin wrapper around dict_to_cst_module for convert_out chains.
    Returns (pending, value).  Propagates Pending on parse errors.
    """
    result = dict_to_cst_module(value)
    if isinstance(result, Pending):
        return result, value
    return None, result


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Leaf CST nodes ↔ Python primitives                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@register
def cst_integer_to_int(value: cst.Integer) -> int:
    return int(value.value)


@register
def int_to_cst_integer(value: int) -> cst.Integer:
    return cst.Integer(str(value))


@register
def cst_float_to_float(value: cst.Float) -> float:
    return float(_clean_float(float(value.value)))


@register
def float_to_cst_float(value: float) -> cst.Float:
    if not math.isfinite(value):
        raise ValueError(f"Cannot represent {value!r} as cst.Float")
    return cst.Float(_clean_float(value))


@register
def cst_simplestring_to_str(value: cst.SimpleString) -> str:
    try:
        return eval(value.value)  # noqa: S307 - safe, it's a string literal
    except Exception:
        return value.value


@register
def str_to_cst_simplestring(value: str) -> cst.SimpleString:
    return cst.SimpleString(repr(value))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.Dict ↔ dict (recursive, with __cst__ preservation)                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@register
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


@register
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

@register
def cst_list_to_list(value: cst.List) -> list:
    """Skip `*splat` elements — can't express them as plain Python values; the
    write side preserves them in place. Mirrors cst_dict_to_dict's handling of
    `**splat`. Previously we appended `_cst_node_to_code(el)` (which includes the
    trailing comma!), and _patch_sequence re-parsed that string as `_ = *x,` —
    successfully, as a 1-tuple — then assigned the resulting Tuple back into the
    outer StarredElement.value, doubling the star (`*x` → `**x,,`)."""
    result = []
    for el in value.elements:
        if isinstance(el, cst.StarredElement):
            continue
        result.append(_cst_to_python_or_raw(el.value))
    return result


@register
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

@register
def cst_tuple_to_tuple(value: cst.Tuple) -> tuple:
    """Skip `*splat` elements — see cst_list_to_list for the reasoning. The
    write side (_patch_sequence) preserves them in their original positions."""
    result = []
    for el in value.elements:
        if isinstance(el, cst.StarredElement):
            continue
        result.append(_cst_to_python_or_raw(el.value))
    return tuple(result)


@register
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

@register
def cst_classdef_to_dict(value: cst.ClassDef) -> dict:
    """Extract readable fields from a class definition.

    Handles three patterns:
      1. body-level Assign:     invalidate_stack_trace = False
      2. body-level AnnAssign:  debug: bool = False  (dataclass fields)
      3. __init__ self.X = literal  (traditional classes)

    Comments are extracted via Comment keys.
    Decorators go in a "decorators" sub-dict.
    """
    readable = GeneralParse(source=_cst_node_to_code(value))
    _stamp_span(readable, value)

    decorators = _extract_decorators(value.decorators)
    if decorators:
        readable["decorators"] = decorators

    # cst_classdef_to_dict is itself the (ClassDef, Dict) converter - reusing
    # it here gives nested classes the same recursive treatment.
    _classdef_to_dict = Melty._converters.get((cst.ClassDef, dict))
    _funcdef_to_dict = Melty._converters.get((cst.FunctionDef, dict))

    # Sibling defs let a bare caller (some_func(1, 2)) bind its positional args to
    # parameter names; call_seen keys repeat calls (func()#1, …).
    local_sigs = _collect_local_signatures(value.body.body)
    call_seen: dict[str, int] = {}

    # Body-level assignments, nested classes, and comments
    for stmt in value.body.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            _extract_leading_comments(stmt, readable, skip_overrides=True)

            last_key = None
            for node in stmt.body:
                # debug = False  (plain assignment)
                if isinstance(node, cst.Assign) and len(node.targets) == 1:
                    target = node.targets[0].target
                    if isinstance(target, cst.Name):
                        readable[target.value] = _cst_to_python_or_raw(node.value)
                        _record_child(readable, target.value, readable[target.value], stmt)
                        last_key = target.value
                # debug: bool = False  (annotated assignment)
                elif isinstance(node, cst.AnnAssign) and isinstance(node.target, cst.Name):
                    if node.value is not None:
                        readable[node.target.value] = _cst_to_python_or_raw(node.value)
                        _record_child(readable, node.target.value, readable[node.target.value], stmt)
                        last_key = node.target.value
                else:
                    # Bare call statement, e.g. some_func(1, 2)
                    ck = _extract_call_statement(node, readable, call_seen, local_sigs)
                    if ck is not None:
                        last_key = ck

            _extract_trailing_comment(stmt, last_key, readable)
            _attach_field_override(stmt, last_key, readable)

        # Nested class: recurse into a nested dict. The leading override comment
        # above only configures the child, not this scope.
        elif isinstance(stmt, cst.ClassDef) and _classdef_to_dict is not None:
            _extract_leading_comments(stmt, readable, skip_overrides=True)
            try:
                child = _classdef_to_dict(stmt)
                _attach_leading_override(stmt, child)
                readable[stmt.name.value] = child
            except (TypeError, ValueError):
                pass

        # Method / nested def: recurse via the (FunctionDef, dict) converter, the
        # same way the module converter handles top-level functions. __init__ is
        # skipped - its `self.X` assignments are surfaced as class fields below,
        # and surfacing it here too would double them up.
        elif (isinstance(stmt, cst.FunctionDef) and _funcdef_to_dict is not None
              and stmt.name.value != "__init__"):
            _extract_leading_comments(stmt, readable, skip_overrides=True)
            try:
                child = _funcdef_to_dict(stmt)
                _attach_leading_override(stmt, child)
                readable[stmt.name.value] = child
            except (TypeError, ValueError):
                pass

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
                        _record_child(readable, attr_name, readable[attr_name], stmt)

    readable["__cst__"] = value
    readable.usages = _collect_usages(value, top_scope="<class>")
    return readable


@register
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

    # _ClassPatcher only REWRITES existing assignments - it has no add or delete
    # path. Reconcile the edited field set against the class body. class_var lens
    # (and the generic lens) support Add/Delete, not just Update.
    #   - a key with no existing field  → NEW class var: synthesize `name = value`
    #     (body-level, so it takes effect for live instances via class-attr
    #     fallback - unlike a self.X buried in __init__)
    #   - an existing field absent from the edits → DELETED: remove its assignment
    #     (body-level and/or __init__ self.X).
    # Run outside the `if edits` predicate above: deleting the last field empties
    # `edits`, but the removal still has to happen.
    existing = _existing_class_field_names(old_node)
    new_fields = {k: v for k, v in edits.items()
                  if k not in existing and not isinstance(v, dict)}
    if new_fields:
        result = _inject_class_fields(result, new_fields)
    deleted = existing - set(edits.keys())
    if deleted:
        result = result.visit(_ClassFieldRemover(deleted))

    # Patch comments in class body
    comment_text_map = _collect_comment_edits(value)
    if comment_text_map:
        new_body = _patch_body_direct(result.body, {}, comment_text_map)
        if new_body is not result.body:
            result = result.with_changes(body=new_body)

    result = _patch_leading_override(result, value)
    result = _ensure_override_comment(result, value)
    result = _apply_field_overrides(result, value.get("__overrides__"))
    return result


def _assign_field_name(node, in_init: bool):
    """Field name an Assign/AnnAssign node defines, or None. In __init__ scope a
    field is `self.X`; at class-body scope it's a bare `Name`."""
    if isinstance(node, cst.Assign) and len(node.targets) == 1:
        tgt = node.targets[0].target
    elif isinstance(node, cst.AnnAssign):
        tgt = node.target
    else:
        return None
    if in_init:
        if (isinstance(tgt, cst.Attribute) and isinstance(tgt.value, cst.Name)
                and tgt.value.value == "self"):
            return tgt.attr.value
        return None
    return tgt.value if isinstance(tgt, cst.Name) else None


def _existing_class_field_names(classdef: cst.ClassDef) -> set:
    """Names cst_classdef_to_dict would surface as fields — body-level
    Assign/AnnAssign targets plus __init__ `self.X` assignments. Used to tell a
    NEW class-var edit (needs synthesizing) from an edit of an existing field.

    MUST stay symmetric with cst_classdef_to_dict, which only emits a dict entry
    for an AnnAssign that HAS a value (`x: int = 0`). A bare annotation
    (`value: str`, no `=`) carries no editable value, so the forward pass skips
    it and the dict never contains it — if we counted it here it would look
    DELETED on the way back and get stripped, silently mutating the source (this
    is what once deleted libcst's `Comment.value` field). So skip bare
    annotations: they pass through untouched via __cst__."""
    names = set()
    for stmt in classdef.body.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            for node in stmt.body:
                if isinstance(node, cst.AnnAssign) and node.value is None:
                    continue  # bare annotation - not a dict entry; leave it be
                nm = _assign_field_name(node, in_init=False)
                if nm is not None:
                    names.add(nm)
    init_fn = _find_init(classdef)
    if init_fn is not None:
        for stmt in init_fn.body.body:
            if isinstance(stmt, cst.SimpleStatementLine):
                for node in stmt.body:
                    nm = _assign_field_name(node, in_init=True)
                    if nm is not None:
                        names.add(nm)
    return names


class _ClassFieldRemover(cst.CSTTransformer):
    """Drop class fields named in `names` — body-level `name = …` (class body,
    depth 1) and `self.name = …` in __init__ (depth 2). Mirrors _ClassPatcher's
    depth/_in_init bookkeeping so it never touches assignments in other methods."""

    def __init__(self, names):
        super().__init__()
        self.names = set(names)
        self._in_init = False
        self._depth = 0

    def visit_IndentedBlock(self, node):
        self._depth += 1
        return True

    def leave_IndentedBlock(self, original_node, updated_node):
        self._depth -= 1
        return updated_node

    def visit_FunctionDef(self, node):
        if node.name.value == "__init__":
            self._in_init = True
        return True

    def leave_FunctionDef(self, original_node, updated_node):
        if original_node.name.value == "__init__":
            self._in_init = False
        return updated_node

    def leave_SimpleStatementLine(self, original_node, updated_node):
        body_scope = (not self._in_init and self._depth == 1)
        init_scope = (self._in_init and self._depth == 2)
        if not (body_scope or init_scope):
            return updated_node
        kept = [n for n in updated_node.body
                if _assign_field_name(n, init_scope) not in self.names]
        if not kept:
            return cst.RemovalSentinel.REMOVE
        if len(kept) != len(updated_node.body):
            return updated_node.with_changes(body=kept)
        return updated_node


def _inject_class_fields(classdef: cst.ClassDef, fields: dict) -> cst.ClassDef:
    """Prepend `name = value` class-body assignments for new class variables.

    Inserted at the top of the class body (after a leading docstring, if any) so
    they read as plain class variables regardless of whether the class otherwise
    stores state body-level or in __init__."""
    new_lines = []
    for name, value in fields.items():
        expr = _python_to_cst_expr(value, None)
        if expr is None:
            continue
        new_lines.append(cst.SimpleStatementLine(
            body=[cst.Assign(targets=[cst.AssignTarget(target=cst.Name(name))],
                             value=expr)]))
    if not new_lines:
        return classdef

    block = classdef.body
    body = list(block.body)
    insert_at = 0
    if (body and isinstance(body[0], cst.SimpleStatementLine) and len(body[0].body) == 1
            and isinstance(body[0].body[0], cst.Expr)
            and isinstance(body[0].body[0].value, cst.SimpleString)):
        insert_at = 1  # keep the docstring first
    body[insert_at:insert_at] = new_lines
    return classdef.with_changes(body=block.with_changes(body=tuple(body)))


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
        self._depth = 0  # class body = 1, __init__ body = 2, nested = 3+
        self._classdef_fn = Melty._converters.get((dict, cst.ClassDef))
        self._funcdef_fn = Melty._converters.get((dict, cst.FunctionDef))
        self._call_fn = Melty._converters.get((dict, cst.Call))
        # Bare-call edits (keyed `func()` / `func()#N`) grouped by callee name in
        # edit order, so leave_Expr can patch each `foo()` statement to its
        # matching edit. Assignment-valued calls (`x = foo()`, keyed `x`) are
        # excluded by the key check - they round-trip via leave_Assign.
        self._call_edits: dict[str, list] = {}
        for k, v in self.edits.items():
            if (_is_bare_call_key(k) and isinstance(v, dict)
                    and isinstance(v.get("__cst__"), cst.Call)):
                self._call_edits.setdefault(_call_func_name(v["__cst__"]), []).append(v)
        self._call_consumed: dict[str, int] = {}

    def leave_ClassDef(self, original_node, updated_node):
        # Nested class: name maps to a sub-dict whose __cst__ is a ClassDef.
        # The root class never matches - its own name isn't among the member
        # edits - and the __cst__ check keeps a dict-valued field from being
        # mistaken for a nested class.
        name = updated_node.name.value
        edit_dict = self.edits.get(name)
        if (self._classdef_fn is None
                or not isinstance(edit_dict, dict)
                or not isinstance(edit_dict.get("__cst__"), cst.ClassDef)):
            return updated_node

        edit_dict["__cst__"] = updated_node
        try:
            return self._classdef_fn(edit_dict)
        except (TypeError, ValueError):
            return updated_node

    def visit_IndentedBlock(self, node):
        self._depth += 1
        return True

    def leave_IndentedBlock(self, original_node, updated_node):
        self._depth -= 1
        return updated_node

    def visit_FunctionDef(self, node):
        if node.name.value == "__init__":
            self._in_init = True
        return True

    def leave_FunctionDef(self, original_node, updated_node):
        name = original_node.name.value
        if name == "__init__":
            self._in_init = False
            return updated_node  # __init__ patched via its self.X assignments, not whole-method
        # A directly-nested method whose dict was edited → re-convert via
        # (dict, FunctionDef), mirroring leave_ClassDef for nested classes. The
        # depth==1 guard keeps a method nested inside a method from being
        # mistaken for a class member of the same name.
        if self._depth == 1 and self._funcdef_fn is not None:
            edit_dict = self.edits.get(name)
            if isinstance(edit_dict, dict) and isinstance(edit_dict.get("__cst__"), cst.FunctionDef):
                ed = dict(edit_dict)
                ed["__cst__"] = updated_node
                try:
                    return self._funcdef_fn(ed)
                except (TypeError, ValueError):
                    pass
        return updated_node

    def leave_Expr(self, original_node, updated_node):
        # Bare class-level call statement (some_func(1, 2)). Patch it to the next
        # matching bare-call edit for this callee. Guard on depth/scope so calls
        # inside method bodies (not exposed as class members) are left alone.
        if self._depth != 1 or self._in_init:
            return updated_node
        call = updated_node.value
        if not isinstance(call, cst.Call) or self._call_fn is None:
            return updated_node
        fn_name = _call_func_name(call)
        queue = self._call_edits.get(fn_name)
        if not queue:
            return updated_node
        idx = self._call_consumed.get(fn_name, 0)
        if idx >= len(queue):
            return updated_node
        self._call_consumed[fn_name] = idx + 1
        ed = dict(queue[idx])
        ed["__cst__"] = call
        try:
            return updated_node.with_changes(value=self._call_fn(ed))
        except (TypeError, ValueError):
            return updated_node

    def leave_AnnAssign(self, original_node, updated_node):
        # Body-level: debug: bool = False (depth 1 = class body)
        if not self._in_init and self._depth == 1 and isinstance(updated_node.target, cst.Name):
            name = updated_node.target.value
            if name in self.edits and updated_node.value is not None:
                new_cst = _python_to_cst_expr(self.edits[name], updated_node.value)
                if new_cst is not None:
                    return updated_node.with_changes(value=new_cst)

        # __init__: self.x: int = 0 (depth 2 = __init__ direct body)
        if self._in_init and self._depth == 2 and isinstance(updated_node.target, cst.Attribute):
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

        # Body-level: x = val (depth 1 = class body)
        if not self._in_init and self._depth == 1 and isinstance(target, cst.Name):
            name = target.value
            if name in self.edits:
                new_cst = _python_to_cst_expr(self.edits[name], updated_node.value)
                if new_cst is not None:
                    return updated_node.with_changes(value=new_cst)
            return updated_node

        # __init__: self.x = val (depth 2 = __init__ direct body).
        # Note on _in_init check: every method body is also depth 2, so without
        # it a self.X assignment in any method would be patched with the value
        # extracted from __init__ (e.g. self._bvh_bbox = new_bbox → = None).
        if not (self._in_init and self._depth == 2):
            return updated_node
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


@register
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
    _stamp_span(readable, value)

    decorators = _extract_decorators(value.decorators)
    if decorators:
        readable["decorators"] = decorators

    params = _extract_param_defaults(value.params)
    if params:
        _stamp_span(params, value.params)
        readable["parameters"] = params

    # Body assignments under "locals"
    locals_ = _extract_body_assignments(value.body)
    if locals_:
        _stamp_span(locals_, value.body)
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
    # Sibling defs in THIS block let a bare caller's positional args bind to
    # parameter names; call_seen keys repeat calls (func_call#1, ...). Scoped per
    # block so the reverse patcher's per-body occurrence counting aligns.
    local_sigs = _collect_local_signatures(stmts)
    call_seen: dict[str, int] = {}
    # Per-keyword occurrence counters for conditional branches in THIS block, so
    # keys are stable (if##0, elif##0, else##0) regardless of edited condition
    # text. The reverse patcher walks the same statements with the same counters
    # so indices align. Weed per branch encountered (even empty ones, which
    # aren't emitted) to keep that alignment structural, not body-dependent.
    cond_counters: dict[str, int] = {"if": 0, "elif": 0, "else": 0}
    # Occurrence counter for for/try block headers, so duplicate headers in this
    # scope get hidden ##N suffixes instead of colliding (see _block_key).
    block_occ: dict[str, int] = {}
    for stmt in stmts:
        if isinstance(stmt, cst.SimpleStatementLine):
            # Leading comments (standalone lines above the statement);
            # override comments are routed to the field below instead.
            _extract_leading_comments(stmt, result, skip_overrides=True)

            last_key = None
            for node in stmt.body:
                name = _assign_target_name(node)
                if name is not None:
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
                    _record_child(result, key, result[key], stmt)
                    last_key = key
                    continue

                # Surface a function call so its arguments are plpatchable:
                # a bare call statement (foo(...)) or a call assigned to a
                # NON-Name target (a, b = foo(...) or obj.x = foo(...)). A plain
                # x = foo(...) stays keyed by its name (handled above).
                call_node = _stmt_call_node(node)
                if call_node is not None:
                    ck = _surface_call(call_node, result, call_seen, local_sigs)
                    if ck is not None:
                        last_key = ck

            # Trailing inline comment on this statement
            _extract_trailing_comment(stmt, last_key, result)
            _attach_field_override(stmt, last_key, result)

        elif isinstance(stmt, cst.If):
            # Leading comments on the if statement itself
            _extract_leading_comments(stmt, result)
            _extract_if_chain(stmt, result, cond_counters)

        elif isinstance(stmt, cst.For):
            _extract_leading_comments(stmt, result)
            _extract_for_loop(stmt, result, block_occ)

        elif isinstance(stmt, (cst.Try, cst.TryStar)):
            _extract_leading_comments(stmt, result)
            _extract_try_block(stmt, result, block_occ)

    return result


def _extract_leading_comments(stmt, result, skip_overrides=False):
    """Extract standalone comment lines from a statement's leading_lines.

    skip_overrides leaves '# [...]' comments out of `result` — used when the
    statement is a nested class/function, whose leading override comment is
    routed to the child's own __overrides__ via _attach_leading_override.
    """
    for ll in getattr(stmt, "leading_lines", ()):
        if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
            if skip_overrides and _parse_override_comment(ll.comment.value) is not None:
                continue
            c = Comment(ll.comment.value)
            result[c] = c
            _merge_override_comment(c, result)


def _attach_leading_override(stmt, child_dict):
    """Route a leading '# [...]' comment above a nested class/function into
    that child's __overrides__ (the first such comment wins)."""
    if not isinstance(child_dict, dict) or isinstance(child_dict.get("__overrides__"), dict):
        return
    for ll in getattr(stmt, "leading_lines", ()):
        if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
            parsed = _parse_override_comment(ll.comment.value)
            if parsed:
                child_dict["__overrides__"] = parsed
                return


def _patch_leading_override(node, value):
    """Update an override comment in node's OWN leading_lines from __overrides__.

    Mirrors the in-place text-map update, but for the comment that sits above
    a nested class/function (in its leading_lines) rather than in its body.
    Only rewrites when the values actually changed.
    """
    if not isinstance(value, dict):
        return node
    overrides = value.get("__overrides__")
    if not isinstance(overrides, dict):
        return node
    current = {k: v for k, v in overrides.items() if not _is_dunder(k)}
    lines = list(getattr(node, "leading_lines", ()))
    for i, ll in enumerate(lines):
        if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
            original = _parse_override_comment(ll.comment.value)
            if original is not None:
                if not current:
                    # All overrides deleted → drop the comment line entirely
                    # rather than leave an empty `# []`.
                    del lines[i]
                    return node.with_changes(leading_lines=lines)
                if current != original:
                    lines[i] = ll.with_changes(
                        comment=cst.Comment(value=_format_override_comment(current)))
                    return node.with_changes(leading_lines=lines)
                return node
    return node


def _attach_field_override(stmt, field_name, result):
    """Route a leading '# [...]' comment above a primitive field into the
    parent's __overrides__ under '__<field>__'.

    Primitives have no dict of their own to carry overrides, so the parent
    collection holds them namespaced; draw_collection applies them to the
    matching child at render time. The first override comment wins.
    """
    if field_name is None:
        return
    for ll in getattr(stmt, "leading_lines", ()):
        if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
            parsed = _parse_override_comment(ll.comment.value)
            if parsed:
                overrides = result.get("__overrides__")
                if not isinstance(overrides, dict):
                    overrides = {}
                    result["__overrides__"] = overrides
                overrides.setdefault(f"__{field_name}__", parsed)
                return


def _patch_field_overrides(stmts, overrides):
    """Patch leading override comments above primitive fields from the parent's
    namespaced __overrides__['__<field>__'] entries. Returns new statements if
    anything changed, else None."""
    if not isinstance(overrides, dict):
        return None
    field_ovs = {k[2:-2]: v for k, v in overrides.items()
                 if isinstance(k, str) and len(k) > 4
                 and k.startswith("__") and k.endswith("__")
                 and isinstance(v, dict)}
    if not field_ovs:
        return None

    new_stmts = list(stmts)
    changed = False
    for i, stmt in enumerate(new_stmts):
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        name = next((_assign_target_name(n) for n in stmt.body
                     if _assign_target_name(n) is not None), None)
        if name is None or name not in field_ovs:
            continue
        # Reuse the leading-line patcher with a synthetic overrides wrapper.
        patched = _patch_leading_override(stmt, {"__overrides__": field_ovs[name]})
        if patched is not stmt:
            new_stmts[i] = patched
            changed = True
    return new_stmts if changed else None


def _apply_field_overrides(node, overrides):
    """Patch field leading override comments on node's body from `overrides`'
    namespaced '__<field>__' entries (no-op if nothing changed)."""
    if isinstance(node, cst.Module):
        new = _patch_field_overrides(node.body, overrides)
        if new is not None:
            return node.with_changes(body=new)
    elif isinstance(node, (cst.ClassDef, cst.FunctionDef)) and isinstance(node.body, cst.IndentedBlock):
        new = _patch_field_overrides(node.body.body, overrides)
        if new is not None:
            return node.with_changes(body=node.body.with_changes(body=new))
    return node


def _extract_trailing_comment(stmt, var_key, result):
    """Extract an inline trailing comment from a statement."""
    tw = getattr(stmt, "trailing_whitespace", None)
    if tw is not None and hasattr(tw, "comment") and tw.comment is not None:
        c = Comment(tw.comment.value, inline=var_key)
        result[c] = c
        _merge_override_comment(c, result)


# ─── Override comments: a tiny key=value store embedded in a comment ──────────
# A comment like  # [tint=(0.1, 0.2, 0.3), bg_offset=5]  parses into a dict
# stored under result["__overrides__"]. Parsing is best-effort: anything that
# doesn't match the shape is left as an ordinary comment and never raises.


def _parse_override_comment(text):
    """Parse a '# [k=v, ...]' override comment into a dict, or None.

    The bracketed body is read as keyword arguments (commas inside tuples,
    lists, etc. are respected) and each value is literal-eval'd. Returns None
    on any malformed input — callers treat None as "not an override comment".
    """
    if not isinstance(text, str):
        return None
    body = text.lstrip("#").strip()
    if not (body.startswith("[") and body.endswith("]")):
        return None
    inner = body[1:-1].strip()
    if not inner:
        return None
    try:
        call = ast.parse(f"dict({inner})", mode="eval").body
        if not isinstance(call, ast.Call) or call.args:
            return None
        parsed = {}
        for kw in call.keywords:
            if kw.arg is None:  # reject **kwarg
                return None
            parsed[kw.arg] = ast.literal_eval(kw.value)
        return parsed or None
    except (SyntaxError, ValueError, TypeError):
        return None


def _format_override_value(value):
    """Render an override value, cleaning float32 noise the same way the rest
    of the converter does (e.g. 0.10000000149 → 0.1). Recurses into tuples and
    lists so values like tint=(0.1, 0.2, 0.3) round-trip cleanly."""
    if isinstance(value, bool):
        return repr(value)  # bool is-a int/float; keep True/False
    if isinstance(value, float):
        return _clean_float(value)
    if isinstance(value, tuple):
        inner = ", ".join(_format_override_value(v) for v in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    if isinstance(value, list):
        return "[" + ", ".join(_format_override_value(v) for v in value) + "]"
    return repr(value)


# Sentinel in a comment text_map meaning "delete this comment line" (vs. a str,
# which rewrites it). Used when an override comment's last key is removed so we
# drop the `# [...]` line instead of leaving an empty `# []`.
_REMOVE_COMMENT = object()


def _format_override_comment(overrides):
    """Render an overrides dict back into a '# [k=v, ...]' comment string."""
    parts = [f"{k}={_format_override_value(v)}" for k, v in overrides.items()
             if not _is_dunder(k)]
    return "# [" + ", ".join(parts) + "]"


def _merge_override_comment(comment, result):
    """If `comment` is an override comment, store its pairs in __overrides__.

    The first override comment in a scope owns __overrides__; any later
    '# [...]' comments at the same level stay as ordinary comments. This keeps
    write-back unambiguous (one comment to regenerate) and round-trips stable.
    """
    if isinstance(result.get("__overrides__"), dict):
        return
    parsed = _parse_override_comment(str(comment))
    if parsed:
        result["__overrides__"] = parsed


def _iter_direct_comment_texts(node):
    """Yield comment texts attached directly in node's own scope.

    Module header + top-level statement comments, or a class/function body's
    statement comments. Does not descend into nested class/function bodies.
    """
    if isinstance(node, cst.Module):
        for ll in node.header:
            if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
                yield ll.comment.value
        stmts = node.body
    elif isinstance(node, (cst.ClassDef, cst.FunctionDef)) and isinstance(node.body, cst.IndentedBlock):
        # The node's own leading lines count too: a leading-line comment
        # above this class/function means body insertion should duplicate it.
        for ll in getattr(node, "leading_lines", ()):
            if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
                yield ll.comment.value
        stmts = node.body.body
    else:
        stmts = ()
    for stmt in stmts:
        for ll in getattr(stmt, "leading_lines", ()):
            if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
                yield ll.comment.value
        tw = getattr(stmt, "trailing_whitespace", None)
        if tw is not None and getattr(tw, "comment", None) is not None:
            yield tw.comment.value


def _ensure_override_comment(node, value):
    """Insert a '# [...]' comment for __overrides__ when none exists yet.

    In-place edits to an existing override comment are handled by the
    comment-edit text map; this only covers the "add a brand-new comment"
    case, prepending it to the first statement of node's scope.
    """
    if not isinstance(value, dict):
        return node
    overrides = value.get("__overrides__")
    if not isinstance(overrides, dict):
        return node
    pairs = {k: v for k, v in overrides.items() if not _is_dunder(k)}
    if not pairs:
        return node
    # Already present? The text-map path keeps it in sync; don't duplicate.
    if any(_parse_override_comment(t) is not None for t in _iter_direct_comment_texts(node)):
        return node

    comment_line = cst.EmptyLine(indent=True, comment=cst.Comment(value=_format_override_comment(pairs)))

    if isinstance(node, cst.Module):
        body = list(node.body)
        if not body:
            return node.with_changes(header=[comment_line, *node.header])
        body[0] = body[0].with_changes(leading_lines=[comment_line, *body[0].leading_lines])
        return node.with_changes(body=body)

    if isinstance(node, (cst.ClassDef, cst.FunctionDef)) and isinstance(node.body, cst.IndentedBlock):
        stmts = list(node.body.body)
        if not stmts:
            return node
        stmts[0] = stmts[0].with_changes(leading_lines=[comment_line, *stmts[0].leading_lines])
        return node.with_changes(body=node.body.with_changes(body=stmts))

    return node


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

    # Override comments: if __overrides__ changed relative to the comment it
    # was parsed from, regenerate that comment. Done after the loop so it wins
    # over a direct edit to the same comment. Only the first override comment at
    # this level is rewritten; an unchanged override leaves its comment verbatim.
    overrides = edits.get("__overrides__")
    if isinstance(overrides, dict):
        current = {kk: vv for kk, vv in overrides.items() if not _is_dunder(kk)}
        for k in edits:
            if not isinstance(k, Comment):
                continue
            original = _parse_override_comment(str(k))
            if original is None:
                continue
            if current != original:
                # Empty → remove the comment line entirely (not `# []`).
                text_map[str(k)] = _format_override_comment(current) if current else _REMOVE_COMMENT
            break
    return text_map


def _patch_module_comments(module, comment_edits):
    """Patch comments on a cst.Module (header + body) by direct walk."""
    text_map = _collect_comment_edits(comment_edits)
    if not text_map:
        return module

    result = module
    changed = False

    # Patch header comments (a _REMOVE_COMMENT mapping drops the line)
    new_header = []
    for ll in module.header:
        if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
            new_text = text_map.get(ll.comment.value)
            if new_text is _REMOVE_COMMENT:
                changed = True
                continue
            if new_text is not None:
                new_header.append(ll.with_changes(comment=cst.Comment(value=new_text)))
                changed = True
                continue
        new_header.append(ll)

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


def _condition_to_editable(test_node):
    """Surface an if/elif test as an editable value (see _extract_if_chain).

    A bare call test (`if is_pressed(btn):`) becomes a CallParse whose args are
    editable. A call wrapped in a subscript (`if button(...)[0]:`) surfaces the
    INNER call as a CallParse too — the subscript wrapper is preserved on
    round-trip by _patch_condition_expr. Everything else (comparisons, names,
    …) falls back to an editable CodeLine."""
    if isinstance(test_node, cst.Subscript) and isinstance(test_node.value, cst.Call):
        inner = _cst_to_python_or_raw(test_node.value)
        if isinstance(inner, CallParse):
            return inner
        return CodeLine(_cst_node_to_code(test_node))
    return _cst_to_python_or_raw(test_node)


def _condition_key(test_node, keyword):
    """The dict key under which an if/elif test is surfaced inside its branch.

    Both forms carry a trailing `##keyword` suffix that the UI hides, so the
    keyword label never clutters the display:
      - call (or subscripted call): `name()##keyword` → shows as `name()`, with
        its args nested and editable; the `()` keeps it distinct from a
        same-named body call so `branch.update(body)` can't clobber it.
      - everything else (comparisons, names, boolops): `##keyword` → shows as
        just the bare CodeLine expression (e.g. `len(x.params) > 0`), with no
        redundant "if"/"elif" prefix.
    The suffix also makes the key STABLE (it doesn't move when the condition text
    is edited) and unable to collide with body assignments/calls. Forward and
    reverse both derive the key from the test node, so they always agree."""
    call = test_node.value if isinstance(test_node, cst.Subscript) else test_node
    if isinstance(call, cst.Call):
        name = _call_func_name(call) or "call"
        return f"{name}()##{keyword}"
    return f"##{keyword}"


def _extract_if_chain(if_node, result, counters):
    """Walk an if/elif/else chain, extracting each branch as a sub-dict.

    Branches are keyed by per-keyword occurrence index — if##0, elif##0, else##0
    (counters is the block-scoped {keyword: next_index} map) — so the key is
    STABLE while the condition text is live-edited (the old `if <cond>` key
    shifted every keystroke, churning draw-state and caches). Counters advance
    for every branch encountered, including empty ones that aren't emitted, so
    the reverse patcher's identical walk lines indices up structurally.

    Each if/elif branch also surfaces its test as an editable value under
    _condition_key (a CallParse for a call/subscripted-call test — see
    _condition_to_editable — else a CodeLine); the reverse converts it back into
    the test node. The else branch has no condition.

    Every if/elif branch is surfaced, even one whose body has nothing
    extractable (a guard clause `if not ready: return`, a one-liner), so its
    condition is still editable — an empty body just yields a branch holding
    only its condition. The reverse patcher has no body guard either, so an
    emitted-but-empty branch round-trips unchanged. An else is still only
    surfaced when it has a body: with no condition and nothing extractable there
    is nothing to show or edit (its else## counter still advances, keeping the
    reverse walk aligned).
    """
    # if
    if_idx = counters["if"]; counters["if"] += 1
    key = f"if##{if_idx}"
    body = _extract_block_assignments(if_node.body.body)
    branch = Conditional(condition=key)
    cond_key = _condition_key(if_node.test, "if")
    branch[cond_key] = _condition_to_editable(if_node.test)
    branch.update(body)
    _merge_child_spans(branch, body)               # update() copies items, not _child_spans
    _record_child(branch, cond_key, branch[cond_key], if_node.test)
    _stamp_span(branch, _union_span([if_node.test, if_node.body]))
    result[key] = branch

    # Walk the orelse chain
    orelse = if_node.orelse
    while orelse is not None:
        if isinstance(orelse, cst.If):
            # elif
            elif_idx = counters["elif"]; counters["elif"] += 1
            key = f"elif##{elif_idx}"
            body = _extract_block_assignments(orelse.body.body)
            branch = Conditional(condition=key)
            cond_key = _condition_key(orelse.test, "elif")
            branch[cond_key] = _condition_to_editable(orelse.test)
            branch.update(body)
            _merge_child_spans(branch, body)
            _record_child(branch, cond_key, branch[cond_key], orelse.test)
            _stamp_span(branch, _union_span([orelse.test, orelse.body]))
            result[key] = branch
            orelse = orelse.orelse
        elif isinstance(orelse, cst.Else):
            # else - only surfaced when it has a body (no condition to edit
            # otherwise). The counter still advances so the reverse walk aligns.
            else_idx = counters["else"]; counters["else"] += 1
            key = f"else##{else_idx}"
            body = _extract_block_assignments(orelse.body.body)
            if body:
                branch = Conditional(body, condition=key)
                _merge_child_spans(branch, body)
                _stamp_span(branch, orelse)
                result[key] = branch
            orelse = None
        else:
            break


def _occ_key(base, occ_counter):
    """Disambiguate a block key that may repeat within one scope.

    The first occurrence of `base` keeps the bare base; later ones get a hidden
    ##N suffix (N = occurrence index) so duplicate for/try headers don't collide
    in the dict (two `for arg in args:` loops, three `try:` blocks, …). The UI
    hides the ##N just like the if##N indices. Forward and reverse both call this
    for every structural block in the same walk order, so the suffix a key gets
    is identical on both sides — N=0 → no suffix keeps the common (no-collision)
    case byte-for-byte unchanged."""
    n = occ_counter.get(base, 0)
    occ_counter[base] = n + 1
    return base if n == 0 else f"{base}##{n}"


def _extract_for_loop(for_node, result, block_occ):
    """Extract a for loop as a Loop dict entry.

    Key is the full loop header: "for i in range(10)" (a repeat header gets a
    hidden ##N suffix via _occ_key so duplicate loops don't collide).
    Value is a Loop dict containing:
      - "range": [args...]  if the iterator is a range() call
      - body assignments (recursively extracted)
    """
    target_code = _cst_node_to_code(for_node.target)
    iter_code = _cst_node_to_code(for_node.iter)
    key = _occ_key(f"for {target_code} in {iter_code}", block_occ)

    body = _extract_block_assignments(for_node.body.body)

    # Extract range() args as editable values
    range_args = _extract_range_args(for_node.iter)
    if range_args is not None:
        body["range"] = range_args

    loop = Loop(body, target=target_code, iter=iter_code)
    _merge_child_spans(loop, body)
    if range_args is not None:
        _record_child(loop, "range", range_args, for_node.iter)
    _stamp_span(loop, for_node)
    result[key] = loop

    # for/else
    if for_node.orelse is not None and isinstance(for_node.orelse, cst.Else):
        else_body = _extract_block_assignments(for_node.orelse.body.body)
        if else_body:
            else_branch = Conditional(else_body, condition="else")
            _merge_child_spans(else_branch, else_body)
            _stamp_span(else_branch, for_node.orelse)
            result[f"{key} else"] = else_branch


def _try_handler_header(handler):
    """Build the header text for an except handler, e.g.:
        except                  (bare)
        except ValueError       (typed)
        except ValueError as e  (typed + bound name)
    The leading keyword is "except*" for an ExceptStarHandler (PEP 654)."""
    keyword = "except*" if isinstance(handler, cst.ExceptStarHandler) else "except"
    parts = [keyword]
    if handler.type is not None:
        parts.append(_cst_node_to_code(handler.type))
    if handler.name is not None:
        parts.append("as")
        parts.append(_cst_node_to_code(handler.name.name))
    return " ".join(parts)


def _extract_try_block(try_node, result, block_occ):
    """Extract a try/except/else/finally statement.

    Each branch becomes its own entry under `result`, keyed by header. The
    try/else/finally branches are Try entries; each except handler is an
    Except entry (a distinct type so the UI can style handlers separately):
      try:               → "try"            (Try)
      except X as e:      → "except X as e"  (Except)
      else:               → "try else"   (prefixed to avoid colliding with if/for else)
      finally:            → "finally"
    Bodies are extracted recursively, so assignments wrapped in a try are
    surfaced as locals instead of being dropped. Empty branches are skipped,
    mirroring the if/for extractors.

    Every header is run through _occ_key (against the block-shared `block_occ`),
    advanced for each STRUCTURAL branch that exists regardless of body emptiness,
    so repeated trys (three `try:` blocks, two `except OSError:`) get hidden ##N
    suffixes instead of clobbering each other — and the reverse, walking the same
    structure, derives the same keys.
    """
    try_key = _occ_key("try", block_occ)
    body = _extract_block_assignments(try_node.body.body)
    if body:
        tryobj = Try(body, header="try")
        _merge_child_spans(tryobj, body)
        _stamp_span(tryobj, try_node.body)
        result[try_key] = tryobj

    for handler in try_node.handlers:
        header = _try_handler_header(handler)
        hkey = _occ_key(header, block_occ)
        hbody = _extract_block_assignments(handler.body.body)
        if hbody:
            excobj = Except(hbody, header=header)
            _merge_child_spans(excobj, hbody)
            _stamp_span(excobj, handler)
            result[hkey] = excobj

    if try_node.orelse is not None and isinstance(try_node.orelse, cst.Else):
        else_key = _occ_key("try else", block_occ)
        else_body = _extract_block_assignments(try_node.orelse.body.body)
        if else_body:
            elseobj = Try(else_body, header="try else")
            _merge_child_spans(elseobj, else_body)
            _stamp_span(elseobj, try_node.orelse)
            result[else_key] = elseobj

    if try_node.finalbody is not None and isinstance(try_node.finalbody, cst.Finally):
        fin_key = _occ_key("finally", block_occ)
        fin_body = _extract_block_assignments(try_node.finalbody.body.body)
        if fin_body:
            finobj = Try(fin_body, header="finally")
            _merge_child_spans(finobj, fin_body)
            _stamp_span(finobj, try_node.finalbody)
            result[fin_key] = finobj


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

    Call decorators → DecorationParse (a CallParse subclass) via cst_call_to_dict
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
                        result[func_name] = fn(dec.decorator, result_cls=DecorationParse)
                    except (TypeError, ValueError):
                        result[func_name] = _cst_node_to_code(dec.decorator)
        else:
            # Bare decorator: @classmethod, @property, etc.
            code = _cst_node_to_code(dec.decorator)
            result[code] = code
    return result


@register
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

    # A leading override comment (before the def) lives on the top funcdef dict;
    # body overrides (and field overrides) live under "locals".
    result = _patch_leading_override(result, value)
    if isinstance(local_edits, dict):
        result = _ensure_override_comment(result, local_edits)
        result = _apply_field_overrides(result, local_edits.get("__overrides__"))
    return result


def _block_base(key):
    """Strip a trailing ##<digits> occurrence suffix from a block key.

    if##0 → if, for x in y##1 → for x in y, try##2 → try. A non-numeric ##
    suffix (e.g. button()##if — a condition key, never seen at this level) or no
    ## is returned unchanged."""
    if isinstance(key, str) and "##" in key:
        base, suf = key.rsplit("##", 1)
        if suf.isdigit():
            return base
    return key


def _is_block_key(key):
    """True if `key` names an if/elif/else/for/try block branch (with or without
    an ##N occurrence suffix). Used to route it to block_edits."""
    base = _block_base(key)
    return (base == "if" or base == "elif" or base == "else"
            or base.startswith("for ") or base == "try" or base == "try else"
            or base == "finally" or base == "except"
            or base.startswith("except ") or base.startswith("except* "))


def _parse_edit_keys(edits):
    """Split a locals dict into assignment, block, and call edits.

    Returns (assign_edits, block_edits, call_edits) where:
      assign_edits = {(name, occurrence): value}
      block_edits = {"if cond": sub_dict, "elif ...": ..., "else": ...}
      call_edits  = {func_name: [CallParse, ...]}  (source order, consumed by
                    occurrence in _patch_simple_stmt — mirrors _ClassPatcher)
    """
    assign_edits: dict[tuple[str, int], object] = {}
    block_edits: dict[str, dict] = {}
    call_edits: dict[str, list] = {}

    for key, val in edits.items():
        if isinstance(key, Comment):
            continue
        # Surfaced calls (`func()` / `func()#N`) - keyed before the "#" occurren
        # added so the occurrence suffix isn't mistaken for an assignment index.
        if (_is_bare_call_key(key) and isinstance(val, dict)
                and isinstance(val.get("__cst__"), cst.Call)):
            call_edits.setdefault(_call_func_name(val["__cst__"]), []).append(val)
        elif isinstance(val, dict) and isinstance(key, str) and _is_block_key(key):
            block_edits[key] = val
        elif isinstance(key, str) and "#" in key:
            name, idx_str = key.rsplit("#", 1)
            try:
                assign_edits[(name, int(idx_str))] = val
            except ValueError:
                pass
        elif isinstance(key, str):
            assign_edits[(key, 0)] = val

    return assign_edits, block_edits, call_edits


def _patch_body_direct(body_node, edits, comment_text_map=None):
    """Patch assignments and comments in an IndentedBlock by direct statement walk.

    No CSTTransformer — walks body.body directly, patches matching
    assignments with with_changes(), handles if/elif/else by recursing,
    and patches comments inline.

    ~4000x less overhead than CSTTransformer for a noop walk.
    """
    if not isinstance(body_node, cst.IndentedBlock):
        return body_node

    assign_edits, block_edits, call_edits = _parse_edit_keys(edits)
    if not assign_edits and not block_edits and not call_edits and not comment_text_map:
        return body_node

    new_stmts = list(body_node.body)
    changed = False
    seen: dict[str, int] = {}
    call_consumed: dict[str, int] = {}
    # Per-keyword conditional counters, advanced for every If chain encountered
    # so the indexed keys (if##0, elif##0, else##0) line up with the forward
    # extractor's identical walk - see _extract_if_chain.
    cond_counters: dict[str, int] = {"if": 0, "elif": 0, "else": 0}
    # for/try header occurrence counter, advanced for every for/try encountered
    # so the ##N disambiguation matches the forward walk (see _occ_key).
    block_occ: dict[str, int] = {}

    for i, stmt in enumerate(new_stmts):
        if isinstance(stmt, cst.SimpleStatementLine):
            new_stmt = _patch_simple_stmt(stmt, assign_edits, seen, call_edits, call_consumed)
            if comment_text_map:
                new_stmt = _patch_stmt_comments(new_stmt, comment_text_map)
            if new_stmt is not stmt:
                new_stmts[i] = new_stmt
                changed = True

        elif isinstance(stmt, cst.If):
            new_stmt = stmt
            if comment_text_map:
                new_stmt = _patch_stmt_comments(new_stmt, comment_text_map)
            # Always call (even with empty block_edits) so cond_counters advance
            # in lock-step with the forward walk; it no-ops when nothing matches.
            new_stmt = _patch_if_chain_direct(new_stmt, block_edits, cond_counters, comment_text_map)
            if new_stmt is not stmt:
                new_stmts[i] = new_stmt
                changed = True

        elif isinstance(stmt, cst.For):
            new_stmt = stmt
            if comment_text_map:
                new_stmt = _patch_stmt_comments(new_stmt, comment_text_map)
            # Always call so block_occ advances in lock-step with the forward walk.
            new_stmt = _patch_for_loop_direct(new_stmt, block_edits, block_occ, comment_text_map)
            if new_stmt is not stmt:
                new_stmts[i] = new_stmt
                changed = True

        elif isinstance(stmt, (cst.Try, cst.TryStar)):
            new_stmt = stmt
            if comment_text_map:
                new_stmt = _patch_stmt_comments(new_stmt, comment_text_map)
            # Always call so block_occ advances in lock-step with the forward walk.
            new_stmt = _patch_try_block_direct(new_stmt, block_edits, block_occ, comment_text_map)
            if new_stmt is not stmt:
                new_stmts[i] = new_stmt
                changed = True

    if not changed:
        return body_node
    return body_node.with_changes(body=new_stmts)


def _patch_simple_stmt(stmt, assign_edits, seen, call_edits=None, call_consumed=None):
    """Patch a SimpleStatementLine's assignments by name+occurrence, and any
    surfaced calls (bare or non-Name-target assigns) by callee+occurrence.

    Returns the same stmt object if nothing changed (identity check).
    """
    new_body = list(stmt.body)
    changed = False

    for j, node in enumerate(new_body):
        name = _assign_target_name(node)
        if name is not None:
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
            continue

        # Surfaced call: patch the call node's args from the matching CallParse.
        # Consumed in FIFO order per callee, mirroring _extract_call's key order
        # (and _ClassPatcher.leave_Expr for the instance/class path).
        if not call_edits:
            continue
        call_node = _stmt_call_node(node)
        if call_node is None:
            continue
        fn_name = _call_func_name(call_node)
        queue = call_edits.get(fn_name)
        if not queue:
            continue
        idx = call_consumed.get(fn_name, 0)
        if idx >= len(queue):
            continue
        call_consumed[fn_name] = idx + 1
        call_fn = Melty._converters.get((dict, cst.Call))
        if call_fn is None:
            continue
        ed = dict(queue[idx])
        ed["__cst__"] = call_node
        try:
            new_call = call_fn(ed)
        except (TypeError, ValueError):
            continue
        if new_call is call_node:
            continue
        new_body[j] = node.with_changes(value=new_call)
        changed = True

    if not changed:
        return stmt
    return stmt.with_changes(body=new_body)


def _patch_stmt_comments(stmt, text_map):
    """Patch leading and trailing comments on a statement by direct access."""
    result = stmt
    changed = False

    # Leading comments (EmptyLine nodes); a _REMOVE_COMMENT mapping drops the line
    if hasattr(result, "leading_lines") and result.leading_lines:
        new_lines = []
        for ll in result.leading_lines:
            if isinstance(ll, cst.EmptyLine) and ll.comment is not None:
                new_text = text_map.get(ll.comment.value)
                if new_text is _REMOVE_COMMENT:
                    changed = True
                    continue
                if new_text is not None:
                    new_lines.append(ll.with_changes(comment=cst.Comment(value=new_text)))
                    changed = True
                    continue
            new_lines.append(ll)
        if changed:
            result = result.with_changes(leading_lines=new_lines)

    # Trailing comment
    tw = getattr(result, "trailing_whitespace", None)
    if tw is not None and hasattr(tw, "comment") and tw.comment is not None:
        new_text = text_map.get(tw.comment.value)
        if new_text is _REMOVE_COMMENT:
            result = result.with_changes(
                trailing_whitespace=tw.with_changes(comment=None))
            changed = True
        elif new_text is not None:
            result = result.with_changes(
                trailing_whitespace=tw.with_changes(
                    comment=cst.Comment(value=new_text)))
            changed = True

    return result


def _patch_condition_expr(test_node, branch_edits, cond_key):
    """Patch an if/elif test expression from its editable surfaced value.

    The condition is surfaced (see _extract_if_chain / _condition_to_editable)
    under the branch keyword (`cond_key`, "if"/"elif") as a CallParse for a call
    (or subscripted-call) test, a CodeLine otherwise. _python_to_cst_expr handles
    each (a CallParse round-trips via dict_to_cst_call). For a subscripted call
    (`button(...)[0]`) the surfaced value is the INNER call, so patch the call
    and keep the subscript wrapper. Returns the original `test_node` when the key
    is absent or unchanged (so callers can identity-check), else the rebuilt node.
    """
    if cond_key not in branch_edits:
        return test_node
    edit = branch_edits[cond_key]
    # Call wrapped in a subscript: the surfaced value is the inner call's
    # CallParse - patch the call, preserve the subscript (mirrors the forward
    # _condition_to_editable, which surfaces test_node.value).
    if (isinstance(test_node, cst.Subscript)
            and isinstance(test_node.value, cst.Call)
            and isinstance(edit, dict)
            and isinstance(edit.get("__cst__"), cst.Call)):
        new_call = _python_to_cst_expr(edit, test_node.value)
        if new_call is not None and new_call is not test_node.value:
            return test_node.with_changes(value=new_call)
        return test_node
    new_test = _python_to_cst_expr(edit, test_node)
    return new_test if new_test is not None else test_node


def _patch_if_chain_direct(if_node, block_edits, counters, comment_text_map=None):
    """Patch an if/elif/else chain by direct body walk — no CSTTransformer.

    Branches are matched by their stable indexed key (if##N, elif##N, else##N).
    `counters` is the block-scoped {keyword: next_index} map, advanced for every
    branch encountered (mirroring _extract_if_chain) so the index a key refers to
    is identical on both sides regardless of edited condition text.
    """
    result = if_node
    changed = False

    # If
    if_idx = counters["if"]; counters["if"] += 1
    key = f"if##{if_idx}"
    if key in block_edits:
        branch_edits = block_edits[key]
        cond_key = _condition_key(result.test, "if")
        new_test = _patch_condition_expr(result.test, branch_edits, cond_key)
        if new_test is not result.test:
            result = result.with_changes(test=new_test)
            changed = True
        body_edits = {k: v for k, v in branch_edits.items() if k != cond_key}
        new_body = _patch_body_direct(result.body, body_edits, comment_text_map)
        if new_body is not result.body:
            result = result.with_changes(body=new_body)
            changed = True

    # Patch the orelse chain
    new_result = _patch_orelse_direct(result, block_edits, counters, comment_text_map)
    if new_result is not result:
        result = new_result
        changed = True

    return result


def _patch_orelse_direct(node, block_edits, counters, comment_text_map=None):
    """Recursively patch elif/else branches by direct body walk.

    Advances `counters` for every elif/else encountered so the indexed keys
    align with the forward walk even for unedited branches."""
    orelse = node.orelse
    if orelse is None:
        return node

    if isinstance(orelse, cst.If):
        elif_idx = counters["elif"]; counters["elif"] += 1
        key = f"elif##{elif_idx}"
        new_orelse = orelse
        if key in block_edits:
            branch_edits = block_edits[key]
            cond_key = _condition_key(new_orelse.test, "elif")
            new_test = _patch_condition_expr(new_orelse.test, branch_edits, cond_key)
            if new_test is not new_orelse.test:
                new_orelse = new_orelse.with_changes(test=new_test)
            body_edits = {k: v for k, v in branch_edits.items() if k != cond_key}
            new_body = _patch_body_direct(new_orelse.body, body_edits, comment_text_map)
            if new_body is not new_orelse.body:
                new_orelse = new_orelse.with_changes(body=new_body)
        # Recurse into this elif's own orelse
        recursed = _patch_orelse_direct(new_orelse, block_edits, counters, comment_text_map)
        if recursed is not new_orelse:
            new_orelse = recursed
        if new_orelse is not orelse:
            return node.with_changes(orelse=new_orelse)

    elif isinstance(orelse, cst.Else):
        else_idx = counters["else"]; counters["else"] += 1
        key = f"else##{else_idx}"
        if key in block_edits:
            new_body = _patch_body_direct(orelse.body, block_edits[key], comment_text_map)
            if new_body is not orelse.body:
                new_orelse = orelse.with_changes(body=new_body)
                return node.with_changes(orelse=new_orelse)

    return node


def _patch_for_loop_direct(for_node, block_edits, block_occ, comment_text_map=None):
    """Patch a for loop's body and range args from block_edits.

    Advances `block_occ` for the loop header (matching _extract_for_loop) so a
    repeated header resolves to the same ##N-suffixed key on both sides."""
    target_code = _cst_node_to_code(for_node.target)
    iter_code = _cst_node_to_code(for_node.iter)
    key = _occ_key(f"for {target_code} in {iter_code}", block_occ)

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


def _patch_try_block_direct(try_node, block_edits, block_occ, comment_text_map=None):
    """Patch a try/except/else/finally statement's branches from block_edits.

    Mirrors _extract_try_block's keying: "try", "except <...>", "try else",
    "finally", each disambiguated through _occ_key against the shared `block_occ`
    so repeated trys/handlers resolve to the same ##N-suffixed keys on both
    sides. _occ_key is advanced for every STRUCTURAL branch that exists (try +
    each handler always; else/finally only when present) — exactly the forward
    walk — so the occurrence counts stay aligned. Each matching branch's body is
    recursively patched.
    """
    result = try_node
    changed = False

    # try body
    try_key = _occ_key("try", block_occ)
    if try_key in block_edits:
        new_body = _patch_body_direct(result.body, block_edits[try_key], comment_text_map)
        if new_body is not result.body:
            result = result.with_changes(body=new_body)
            changed = True

    # excepts
    new_handlers = list(result.handlers)
    handlers_changed = False
    for idx, handler in enumerate(new_handlers):
        hkey = _occ_key(_try_handler_header(handler), block_occ)
        if hkey in block_edits:
            new_hbody = _patch_body_direct(handler.body, block_edits[hkey], comment_text_map)
            if new_hbody is not handler.body:
                new_handlers[idx] = handler.with_changes(body=new_hbody)
                handlers_changed = True
    if handlers_changed:
        result = result.with_changes(handlers=new_handlers)
        changed = True

    # else (advance _occ_key only when the branch exists - mirror the forward)
    if result.orelse is not None and isinstance(result.orelse, cst.Else):
        else_key = _occ_key("try else", block_occ)
        if else_key in block_edits:
            new_eb = _patch_body_direct(result.orelse.body, block_edits[else_key], comment_text_map)
            if new_eb is not result.orelse.body:
                result = result.with_changes(orelse=result.orelse.with_changes(body=new_eb))
                changed = True

    # finally
    if result.finalbody is not None and isinstance(result.finalbody, cst.Finally):
        fin_key = _occ_key("finally", block_occ)
        if fin_key in block_edits:
            new_fb = _patch_body_direct(result.finalbody.body, block_edits[fin_key], comment_text_map)
            if new_fb is not result.finalbody.body:
                result = result.with_changes(finalbody=result.finalbody.with_changes(body=new_fb))
                changed = True

    return result if changed else try_node


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


def _build_decorator(name, kwargs_dict):
    """Synthesize a brand-new `@name(k=v, ...)` decorator node from scratch (no
    template Call). Used when the UI adds a decorator the source didn't have.
    Returns None if no renderable kwargs."""
    args = []
    for k, v in kwargs_dict.items():
        if _is_dunder(k):
            continue
        cst_val = _python_to_cst_expr(v)
        if cst_val is None:
            continue
        args.append(cst.Arg(keyword=cst.Name(k), value=cst_val,
                            equal=cst.AssignEqual(
                                whitespace_before=cst.SimpleWhitespace(""),
                                whitespace_after=cst.SimpleWhitespace(""))))
    if not args:
        return None
    return cst.Decorator(decorator=cst.Call(func=cst.Name(name), args=args))


def _patch_decorators(func_node, dec_edits):
    """Patch / add / drop decorator kwargs on a ClassDef or FunctionDef so the UI
    can add, update, and delete decorations and have it round-trip.

    dec_edits maps decorator name → {kwarg: value}.
      - existing decorator, kwargs present → patch via dict→cst.Call
      - existing decorator, kwargs emptied → drop the decorator (clean delete)
      - name not on the node               → synthesize `@name(k=v, ...)`
    """
    dict_to_call = Melty._converters.get((dict, cst.Call))
    if dict_to_call is None:
        return func_node

    edits = {k: v for k, v in dec_edits.items()
             if isinstance(k, str) and not _is_dunder(k) and isinstance(v, dict)}

    new_decorators = []
    seen = set()
    changed = False
    for dec in func_node.decorators:
        func_name = _call_func_name(dec.decorator) if isinstance(dec.decorator, cst.Call) else None
        if func_name and func_name in edits:
            seen.add(func_name)
            edit_sub = dict(edits[func_name])
            kw_pairs = {k: v for k, v in edit_sub.items() if not _is_dunder(k)}
            if not kw_pairs:
                # No kwargs in the edited dict. "Empties → delete" only holds for a
                # decorator the UI actually modeled: a KEYWORD-arg decorator like
                # @defaults(tint=...) whose kwargs the user cleared. Decide by what
                # the SOURCE decorator had, not by the empty edits:
                #   - no kwargs in source - a bare @window() or a positional-only
                #     @no_save("key") - empty kw_pairs just mirrors the source, NOT a
                #     deletion. Dropping it here ate those decorators on every
                #     round-trip (and only surfaced on first-load, the one time the
                #     chain auto-reed). Keep them untouched.
                #   - had kwargs in source, now all gone → a real delete.
                orig_has_kwargs = isinstance(dec.decorator, cst.Call) and any(
                    a.keyword is not None for a in dec.decorator.args)
                if not orig_has_kwargs:
                    new_decorators.append(dec)
                    continue
                changed = True
                continue
            edit_sub["__cst__"] = dec.decorator
            try:
                new_decorators.append(dec.with_changes(decorator=dict_to_call(edit_sub)))
                changed = True
                continue
            except (TypeError, ValueError):
                pass
        new_decorators.append(dec)

    # Synthesize decorators the edits added but the node lacked (the @ line lands
    # just above the def; libcst indents it from the surrounding block).
    for name, sub in edits.items():
        if name in seen:
            continue
        new_dec = _build_decorator(name, sub)
        if new_dec is not None:
            new_decorators.append(new_dec)
            changed = True

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

@register
def cst_call_to_dict(value: cst.Call, pos_names_override=None, result_cls=CallParse) -> dict:
    """Extract a Call's arguments into a CallParse keyed by PARAMETER name.

    `result_cls` lets a caller mint a CallParse subclass instead (e.g.
    _extract_decorators passes DecorationParse) — same extraction, distinct type.

    Returns a CallParse (a dict subclass); the annotation stays `dict` so
    @register keys it under the canonical (cst.Call, dict) registry slot that
    _cst_to_python and _extract_decorators look up.

    my_func(42, flag=True)   # def my_func(count, flag=False)
    → CallParse({"count": 42, "flag": True, "__cst__": <Call>})

    Keyword args key on their keyword name. Positional args are mapped to the
    parameter name they bind to — that's what makes the dict a key/value store
    over the *parameters*, not just the explicitly-named kwargs. The binding
    comes from `pos_names_override` when given (used for same-source callers,
    where the callee is a local def not importable at runtime — see
    _extract_call_statement) otherwise from resolving the callee's runtime
    signature (_call_positional_param_names). When neither yields a binding the
    positional args stay in __cst__ and pass through untouched.

    The binding actually used is stored under "__pos_names__" so dict_to_cst_call
    can map the same positions back without re-resolving — the round-trip stays
    self-contained on the object, even for a callee that isn't runtime-resolvable.

    kwargs with unresolvable values (variable references, complex expressions)
    are surfaced as their raw source string and round-trip via __cst__.
    """
    readable = result_cls(source=_cst_node_to_code(value),
                          func_name=_call_func_name(value))

    # An explicit override (same-source caller's params) wins. Otherwise resolve the
    # callee's runtime signature - but that scans sys.modules, so only pay for it
    # when there's an actual positional arg to bind to a parameter name.
    has_positional = any(a.keyword is None and a.star == "" for a in value.args)
    if pos_names_override is not None:
        pos_names = pos_names_override
    else:
        pos_names = _call_positional_param_names(value) if has_positional else None
    # Callee unresolvable (e.g. imgui.text, a C function with no introspectable
    # signature) but it has plain positional args - surface them under synthetic
    # arg0/arg1/... keys so the arguments are still visible and editable instead of
    # vanishing into __cst__ (the call rendering as a bare function). The names are
    # stamped into __pos_names__ below, so the reverse maps them back by position.
    if pos_names is None and has_positional:
        n_plain = sum(1 for a in value.args if a.keyword is None and a.star == "")
        existing_kw = {a.keyword.value for a in value.args if a.keyword is not None}
        pos_names = [f"arg{i}" for i in range(n_plain)]
        # Avoid a rare clash with a real kwarg literally named argN.
        if any(n in existing_kw for n in pos_names):
            pos_names = None
    pos_idx = 0

    for arg in value.args:
        if arg.keyword is not None:
            readable[arg.keyword.value] = _cst_to_python_or_raw(arg.value)
            _record_child(readable, arg.keyword.value, readable[arg.keyword.value], arg)
        elif arg.star == "**" and isinstance(arg.value, cst.Dict):
            # `**{**kwargs, 'clint': True}`: surface the literal string-keyed entries
            # as editable kwargs (the leading **splat passes through via __cst__).
            # This is how a kwarg lives on a call that also splats **kwargs without
            # a runtime "multiple values" collision; see dict_to_cst_call.
            for el in arg.value.elements:
                if isinstance(el, cst.DictElement) and isinstance(el.key, cst.SimpleString):
                    try:
                        key = ast.literal_eval(el.key.value)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(key, str):
                        readable[key] = _cst_to_python_or_raw(el.value)
                        _record_child(readable, key, readable[key], el)
        elif arg.star == "":
            # Plain positional arg → surface under the parameter name it binds
            # to, if we could resolve the signature. Advance the positional
            # cursor either way so an unsurfaced arg (e.g. past *args, or
            # an unresolved callee) won't shift later bindings.
            if pos_names is not None and pos_idx < len(pos_names):
                readable[pos_names[pos_idx]] = _cst_to_python_or_raw(arg.value)
                _record_child(readable, pos_names[pos_idx], readable[pos_names[pos_idx]], arg)
            pos_idx += 1
        # else: `*args` splat - passes through via __cst__

    if pos_names:
        # Remember the binding so the reverse maps these positions back without
        # re-resolving (and so a same-source / non-importable callee round-trips).
        readable["__pos_names__"] = list(pos_names)
    readable["__cst__"] = value
    _stamp_span(readable, value)
    return readable


def _patch_starstar_dict(star_arg, edits):
    """Update/drop the literal string-keyed entries of a `**{...}` dict arg from
    `edits` (popping consumed keys). Keeps splat (`**x`) and non-string-key
    elements. The mirror of cst_call_to_dict's read of merged-dict kwargs."""
    d = star_arg.value
    new_elements = []
    for el in d.elements:
        if isinstance(el, cst.DictElement) and isinstance(el.key, cst.SimpleString):
            try:
                key = ast.literal_eval(el.key.value)
            except (ValueError, SyntaxError):
                new_elements.append(el)
                continue
            if isinstance(key, str) and key in edits:
                new_val = edits.pop(key)
                cst_v = _python_to_cst_expr(new_val, el.value)
                new_elements.append(el.with_changes(value=cst_v) if cst_v is not None else el)
            elif isinstance(key, str):
                pass  # key absent from edits → deleted → drop the element
            else:
                new_elements.append(el)
        else:
            new_elements.append(el)  # **splat / non-string key - keep
    # Collapse a now-redundant `**{**x}` back to plain `**x`.
    if len(new_elements) == 1 and isinstance(new_elements[0], cst.StarredDictElement):
        return star_arg.with_changes(value=new_elements[0].value)
    return star_arg.with_changes(value=d.with_changes(elements=new_elements))


def _merge_new_into_starstar(star_arg, new_kwargs):
    """Fold new kwargs INTO a `**` arg as a merged dict literal:
    `**kwargs` → `**{**kwargs, 'k': v, ...}`. This is what makes adding a kwarg to
    a call that splats **kwargs safe — `f(**kwargs, k=v)` raises "multiple values"
    at runtime if kwargs already has k, but the merged dict can't collide."""
    val = star_arg.value
    elements = list(val.elements) if isinstance(val, cst.Dict) else [cst.StarredDictElement(value=val)]
    for k, v in new_kwargs.items():
        cst_v = _python_to_cst_expr(v)
        if cst_v is None:
            continue
        elements.append(cst.DictElement(key=cst.SimpleString(repr(k)), value=cst_v))
    return star_arg.with_changes(value=cst.Dict(elements=elements))


@register
def dict_to_cst_call(value: dict) -> cst.Call:
    """Reconstruct a Call from a dict, patching kwargs and handling
    insert/pop with sibling-cloned formatting.

    Kwargs added to a call that splats **kwargs are folded into a merged dict
    literal (`**{**kwargs, 'k': v}`) rather than appended as `k=v` — the latter
    raises "multiple values for keyword 'k'" at runtime if kwargs already has k.
    """
    old_node = value.get("__cst__")
    if old_node is None or not isinstance(old_node, cst.Call):
        raise TypeError("Dict has no __cst__ Call")

    edits = {k: v for k, v in value.items()
             if not (_is_dunder(k))}

    # Re-apply the SAME positional→name binding cst_call_to_dict used so a
    # positional arg surfaced under a parameter name gets written back to its slot
    # - not mistaken for a new keyword arg. Prefer the binding stamped into the
    # dict (handles a same-source / non-importable callee); else resolve the
    # runtime signature, but skip that sys.modules scan when there are no plain
    # positional args to bind.
    n_positional = sum(1 for a in old_node.args if a.keyword is None and a.star == "")
    pos_names = value.get("__pos_names__")
    if pos_names is None and n_positional:
        pos_names = _call_positional_param_names(old_node)
    has_readable_positional = bool(pos_names) and n_positional > 0

    # `not edits` can mean "no changes" OR "every arg was deleted". Only short-
    # circuit when the call actually has no readable args to drop - otherwise a
    # delete of the last kwarg/positional would be silently ignored.
    has_readable_kwargs = any(a.keyword is not None for a in old_node.args) or any(
        a.star == "**" and isinstance(a.value, cst.Dict)
        and any(isinstance(e, cst.DictElement) and isinstance(e.key, cst.SimpleString)
                for e in a.value.elements)
        for a in old_node.args)
    if not edits and not has_readable_kwargs and not has_readable_positional:
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

    # Pass 1: edit/drop kwargs AND positional args surfaced under a parameter
    # name. A `**{...}` dict arg gets its string-keyed entries updated/dropped
    # too; note its index for Pass 2.
    surviving = []
    starstar_idx = None
    pos_idx = 0
    for arg in old_node.args:
        if arg.keyword is None:
            if arg.star == "**":
                if isinstance(arg.value, cst.Dict):
                    arg = _patch_starstar_dict(arg, edits)
                starstar_idx = len(surviving)
                surviving.append(arg)
                continue
            if arg.star == "*":
                surviving.append(arg)  # `*args` splat - leave untouch
                continue
            # Plain positional arg. If it was surfaced under a parameter name,
            # patch it in place (key present) or drop it (key deleted). An
            # unsurfaced position (unresolved callee, or past the named params)
            # passes through untouched. Advance the cursor for every plain
            # arg so the binding stays aligned with the forward pass.
            surfaced_key = pos_names[pos_idx] if (pos_names and pos_idx < len(pos_names)) else None
            pos_idx += 1
            if surfaced_key is None:
                surviving.append(arg)
            elif surfaced_key in edits:
                new_val = edits.pop(surfaced_key)
                new_cst_val = _python_to_cst_expr(new_val, arg.value)
                if new_cst_val is not None and new_cst_val is not arg.value:
                    surviving.append(arg.with_changes(value=new_cst_val))
                else:
                    surviving.append(arg)
            # else: surfaced but absent from edits → deleted → drop it
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

    # Pass 2: add new kwargs. If the call splats **mapping, fold them INTO it as a
    # merged dict (collision-proof); otherwise make plain `k=v` keyword args.
    if edits and starstar_idx is not None:
        surviving[starstar_idx] = _merge_new_into_starstar(surviving[starstar_idx], edits)
        edits = {}
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

    # Pass 3: fix commas. Only touch commas that need it - args left over
    # from the original keep their own Comma node (and its newline/indent
    # whitespace), so multi-line kwargs don't collapse to one line. A
    # MaybeSentinel comma means the arg was newly appended or was previously
    # last, so it needs an inner comma now that something follows it.
    if surviving:
        fixed = []
        for i, arg in enumerate(surviving):
            is_last = (i == len(surviving) - 1)
            if is_last:
                fixed.append(arg.with_changes(comma=last_comma))
            elif isinstance(arg.comma, cst.MaybeSentinel) and inner_comma is not None:
                fixed.append(arg.with_changes(comma=inner_comma))
            else:
                fixed.append(arg)
        surviving = fixed

    return old_node.with_changes(args=surviving)


def _call_func_name(call_node):
    """Extract the (dotted) function name from a Call node.

    my_func(...)           → "my_func"
    mod.my_func(...)        → "mod.my_func"
    imgui.text(...)         → "imgui.text"
    pkg.mod.fn(...)         → "pkg.mod.fn"
    obj.method()(...)       → "method"   (chain broken by a Call → just the attr)
    complex(expr)(...)      → None

    The full dotted path is kept so the surfaced call key reads `imgui.text()`
    rather than a bare `text()` (and so two distinct callees with the same final
    attr — `imgui.text` vs `self.text` — don't share an occurrence slot). Forward
    keying and reverse matching both call this, so they stay in agreement."""
    func = call_node.func
    if isinstance(func, cst.Name):
        return func.value
    if isinstance(func, cst.Attribute):
        parts = _collect_attribute_parts(func)
        if parts is not None:
            return ".".join(parts)
        return func.attr.value
    return None


def _funcdef_param_names(funcdef):
    """Ordered positional parameter names of a FunctionDef node (positional-only
    then positional-or-keyword), dropping a leading self/cls. Lets a same-source
    caller bind its positional args to parameter names WITHOUT importing the
    callee at runtime — the def is right there in the tree being parsed."""
    params = funcdef.params
    names = []
    for p in (list(params.posonly_params) + list(params.params)):
        if not names and p.name.value in _SKIP_PARAMS:
            continue
        names.append(p.name.value)
    return names


def _collect_local_signatures(stmts):
    """Map {func_name: [param_names]} for FunctionDefs directly in `stmts` (a
    module/class body). Used to map a bare caller's positional args to the
    parameters of a sibling def — see _extract_call_statement."""
    sigs = {}
    for stmt in stmts:
        if isinstance(stmt, cst.FunctionDef):
            sigs[stmt.name.value] = _funcdef_param_names(stmt)
    return sigs


def _stmt_call_node(node):
    """Return the cst.Call a statement node should surface as a CallParse, or None.

    - bare call expression statement: `foo(...)`                  → the call
    - call assigned to a NON-Name target:
        `a, b = foo(...)`, `obj.x = foo(...)`, `d[k] = foo(...)`   → the call

    A single-Name-target call assignment (`x = foo()`) returns None: it stays
    keyed by its variable name with the call rendered as a code string, matching
    the module/class extractors. Surfacing the call here makes its arguments
    visible and editable wherever the call's return value isn't bound to a plain
    name we already key on."""
    if isinstance(node, cst.Expr) and isinstance(node.value, cst.Call):
        return node.value
    if isinstance(node, cst.Assign) and isinstance(node.value, cst.Call):
        if not (len(node.targets) == 1 and isinstance(node.targets[0].target, cst.Name)):
            return node.value
    if isinstance(node, cst.AnnAssign) and isinstance(node.value, cst.Call):
        if not isinstance(node.target, cst.Name):
            return node.value
    return None


def _surface_call(call_node, readable, call_seen, local_sigs):
    """Surface a cst.Call as a CallParse under `readable`, keyed `func()` —
    distinct from a `func` def key in the same scope — and return that key.
    Repeat calls to the same func get `func()#1`, `func()#2`, … (occurrence
    order, mirroring the assignment keying).

    Positional args bind to the sibling def's parameter names when `local_sigs`
    has them, so an in-file caller shows `a=1, b=2` even though the callee can't
    be imported. Returns None when the call converter is missing or the call
    can't be converted. Shared by bare call statements and non-Name-target call
    assignments (see _stmt_call_node)."""
    call_fn = Melty._converters.get((cst.Call, dict))
    if call_fn is None:
        return None
    fname = _call_func_name(call_node) or "call"
    occ = call_seen.get(fname, 0)
    call_seen[fname] = occ + 1
    key = f"{fname}()" if occ == 0 else f"{fname}()#{occ}"
    try:
        readable[key] = call_fn(call_node, pos_names_override=local_sigs.get(fname))
    except (TypeError, ValueError):
        return None
    return key


def _extract_call_statement(node, readable, call_seen, local_sigs):
    """If `node` is a bare expression-statement call (e.g. `some_func(1, 2)`),
    surface it as a CallParse keyed `func()`. Thin wrapper over _surface_call
    used by the module/class extractors (which only surface bare calls)."""
    if not (isinstance(node, cst.Expr) and isinstance(node.value, cst.Call)):
        return None
    return _surface_call(node.value, readable, call_seen, local_sigs)


def _is_bare_call_key(key):
    """True for a key minted by _extract_call_statement (`func()` / `func()#N`).
    Distinguishes a bare-call edit from an assignment whose value happens to be a
    call (`x = foo()`, keyed `x`) so the reverse patches each via the right slot."""
    if not isinstance(key, str):
        return False
    base = key.rsplit("#", 1)[0] if "#" in key else key
    return base.endswith("()")


def _call_positional_param_names(call_node):
    """Resolve the callee and return the ordered names of its positional
    parameters (positional-only + positional-or-keyword), so a positional
    argument at the call site can be surfaced under the parameter name it
    binds to.

    Returns None when the callee can't be resolved or inspected — callers then
    leave positional args untouched in __cst__. A leading ``self``/``cls`` is
    dropped so ``obj.method(x)`` binds ``x`` to the first *real* parameter.
    Stops at ``*args`` — positions past it can't be named.

    Used by BOTH cst_call_to_dict and dict_to_cst_call so the forward and
    reverse positional→name bindings are identical (same resolution machinery,
    same result, regardless of which value happens to be edited).
    """
    func = call_node.func
    obj = _UNREADABLE
    if isinstance(func, cst.Name):
        obj = _resolve_callable_by_name(func.value)
    elif isinstance(func, cst.Attribute):
        parts = _collect_attribute_parts(func)
        if parts is not None:
            obj = _resolve_callable_by_parts(parts)
    if obj is _UNREADABLE or not callable(obj):
        return None
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return None  # builtins, C functions with no introspectable signature

    names = []
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            if not names and p.name in _SKIP_PARAMS:
                continue  # unbound or called via attr - drop self/cls
            names.append(p.name)
        elif p.kind == p.VAR_POSITIONAL:
            break
    return names


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

    A call wrapped in a subscript (`button(...)[0]`) surfaces the INNER call
    as a CallParse so its arguments stay visible/editable wherever the
    expression appears (assignment RHS, call args, collection elements, …).
    The subscript wrapper round-trips via _python_to_cst_expr, which re-wraps
    when old_node is the Subscript. Handled centrally here so every context
    that routes through this function gets it. Falls through to the normal
    CodeLine when the inner call can't be surfaced (e.g. `foo()[0]`, no args).
    """
    if isinstance(node, cst.Subscript) and isinstance(node.value, cst.Call):
        inner = _cst_to_python_or_raw(node.value)
        if isinstance(inner, CallParse):
            return inner

    val = _cst_to_python(node)
    if val is _UNREADABLE:
        return CodeLine(_cst_node_to_code(node))
    # Catch dict where every key is a dunder (nothing readable extracted)
    if isinstance(val, dict) and all(
            _is_dunder(k) for k in val):
        return CodeLine(_cst_node_to_code(node))
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


# ── Scoped name resolution ──────────────────────────────────────────────────
# Resolving a used name/callable to a live object used to scan all of
# sys.modules (thousands of entries) for every usage, and getattr() on the
# lazily-imported ones (numpy, torch, ...) triggered real imports as a side
# effect - pathological on every keystroke of the live code editor. Now the
# converter that owns a module publishes a name->object scope built from that
# file's OWN imports; the resolvers consult builtins and that scope (O(1))
# first. The fallback scan that remains reads module __dict__s directly, which
# never fall through to a module-level lazy __getattr__, so it triggers no
# imports.
import threading as _threading

_resolution_scope = _threading.local()


def _active_scope():
    return getattr(_resolution_scope, "names", None)


class _module_scope:
    """Publish the import scope for a module conversion. A nested conversion (a
    classdef converted while its module converts) inherits the outer scope
    instead of replacing it, and only the outermost clears it."""
    __slots__ = ("scope", "_owned")

    def __init__(self, scope):
        self.scope = scope
        self._owned = False

    def __enter__(self):
        if getattr(_resolution_scope, "names", None) is None:
            _resolution_scope.names = self.scope
            self._owned = True
        return self

    def __exit__(self, *exc):
        if self._owned:
            _resolution_scope.names = None
        return False


def _attr_no_trigger(obj, attr):
    """getattr that never fires a module's lazy __getattr__.

    Modules are read through __dict__ (PEP 562 lazy imports live behind
    __getattr__, which we must not trigger); classes/objects use normal getattr
    so inherited members still resolve."""
    if isinstance(obj, type(sys)):            # a module
        d = getattr(obj, "__dict__", None)
        return d.get(attr) if d is not None else None
    return getattr(obj, attr, None)


_SRC_PREFIX = str(_Path(__file__).resolve().parents[4]) + "/"  # .../latent-descent/src/


def _build_src_scope():
    """name -> live object for every top-level symbol defined in latent-descent
    src. THIS is the resolution scope: a name resolves only if it names a src
    symbol (function / class / enum / src module); stdlib and third-party are
    out of scope and intentionally left as raw source. Built once per analysis —
    its size is bounded by the project's symbol count, not the file size, and it
    replaces the per-usage sys.modules scans entirely."""
    src_mods = {}
    for modname, mod in list(sys.modules.items()):
        if mod is None:
            continue
        f = getattr(mod, "__file__", None)
        if f and f.startswith(_SRC_PREFIX):
            src_mods[modname] = mod
    src_names = set(src_mods)

    scope = {}
    for modname, mod in src_mods.items():
        # The module itself, keyed by its import leaf (a.b.melty -> "melty"), so
        # dotted names like `melty.Melty` resolve through it.
        scope.setdefault(modname.rsplit(".", 1)[-1], mod)
        d = getattr(mod, "__dict__", None)
        if not d:
            continue
        for name, obj in d.items():
            if name.startswith("__"):
                continue
            # Only import objects DEFINED in src - a src module's `import numpy as
            # np` puts `np` in its __dict__, but numpy is out of scope. Re-exports
            # of other src modules (__module__ is a different src module) are fine.
            om = getattr(obj, "__module__", None)
            if om not in src_names:
                continue
            if om == modname:
                scope[name] = obj                  # canonical definition wins
            else:
                scope.setdefault(name, obj)         # src re-export fills gaps
    return scope


def _unwrap_lazy_enum(obj):
    """Return the real enum member `obj` IS or stands in for, else None.

    Handles a live enum member directly and a `_LazyMode` proxy — `Modes.WINDOW`
    is a deferred stand-in (not an enum.Enum) that resolves to the real
    `Mode.WINDOW`, so the editor sees a proper enum member instead of raw source.
    """
    if isinstance(obj, enum.Enum):
        return obj
    if isinstance(obj, _LazyMode):
        try:
            member = obj._resolve()
        except Exception:
            return None
        if isinstance(member, enum.Enum):
            return member
    return None


def _resolve_as_enum(parts):
    """Resolve ClassName.MEMBER (or mod.ClassName.MEMBER) to an enum member.

    Authoritative: resolves the class through the src scope only — no
    sys.modules scan. Enum classes outside src stay unresolved (-> raw source).

    Also resolves a proxy container whose members stand in for enum members —
    `Modes.WINDOW`, where `Modes` is a singleton holding `_LazyMode` deferrals to
    real `Mode` members (so a file can reference a mode without importing
    view.mode). The member is unwrapped to the actual `Mode` enum member.
    """
    if len(parts) < 2:
        return _UNREADABLE
    scope = _active_scope()
    if scope is None:
        return _UNREADABLE
    cls = scope.get(parts[0])
    if cls is None:
        return _UNREADABLE
    for attr_name in parts[1:-1]:        # walk to the class (skip the MEMBER)
        cls = _attr_no_trigger(cls, attr_name)
        if cls is None:
            return _UNREADABLE
    if isinstance(cls, type) and issubclass(cls, enum.Enum):
        member = cls.__members__.get(parts[-1])
        if member is not None:
            return member
        return _UNREADABLE
    # Proxy container (e.g. `Modes`): resolve the member attribute and unwrap a
    # _LazyMode (or live enum member) to the real enum member.
    resolved = _unwrap_lazy_enum(_attr_no_trigger(cls, parts[-1]))
    if resolved is not None:
        return resolved
    return _UNREADABLE


def _resolve_callable_by_name(name):
    """Resolve a bare name to a callable defined in src (or a builtin).

    Authoritative: consults builtins and the src scope only — no sys.modules
    scan. Names outside src (stdlib, third-party) are intentionally left
    unresolved, so they round-trip as raw source instead of a live object.

    draw_header → <function draw_header>
    type        → <class 'type'>
    """
    import builtins
    obj = getattr(builtins, name, None)
    if obj is not None and callable(obj):
        return obj
    scope = _active_scope()
    if scope is not None:
        obj = scope.get(name)
        if obj is not None and callable(obj):
            return obj
    return _UNREADABLE


def _resolve_callable_by_parts(parts):
    """Resolve a dotted name like ["module", "func"] to a callable.

    Authoritative: resolves the root through the src scope and walks the rest —
    no sys.modules scan. Out-of-src roots stay unresolved.

      ["melty", "Melty", "draw"] → src module melty -> Melty -> draw
      ["MyClass", "method"]      → MyClass.method (MyClass defined in src)
    """
    scope = _active_scope()
    if scope is None:
        return _UNREADABLE
    obj = scope.get(parts[0])
    if obj is None:
        return _UNREADABLE
    for attr_name in parts[1:]:
        obj = _attr_no_trigger(obj, attr_name)
        if obj is None:
            return _UNREADABLE
    return obj if callable(obj) else _UNREADABLE


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
        self._call_fn = Melty._converters.get((dict, cst.Call))
        # depth 0 = module top level; >0 = inside a class/function body. Bare
        # calls are only surfaced (and patched) at the top level.
        self._depth = 0
        # Bare-call edits (commented `func()` / `func()#arg`) grouped by callee name in
        # source order - see _ClassPatcher for the full rationale.
        self._call_edits: dict[str, list] = {}
        for k, v in self.edits.items():
            if (_is_bare_call_key(k) and isinstance(v, dict)
                    and isinstance(v.get("__cst__"), cst.Call)):
                self._call_edits.setdefault(_call_func_name(v["__cst__"]), []).append(v)
        self._call_consumed: dict[str, int] = {}

    def visit_IndentedBlock(self, node):
        self._depth += 1
        return True

    def leave_IndentedBlock(self, original_node, updated_node):
        self._depth -= 1
        return updated_node

    def _consume_call_patch(self, call):
        """Patch a surfaced call (a bare `Expr` call, or the value of a non-Name
        call assignment) to the next matching bare-call edit for its callee, in
        source order. Returns the new Call node, or None when there's no pending
        edit / the patch fails. Shared so leave_Expr and leave_Assign/AnnAssign
        consume from the SAME per-callee queue, keeping the occurrence order the
        forward _surface_call assigned."""
        if not isinstance(call, cst.Call) or self._call_fn is None:
            return None
        fn_name = _call_func_name(call)
        queue = self._call_edits.get(fn_name)
        if not queue:
            return None
        idx = self._call_consumed.get(fn_name, 0)
        if idx >= len(queue):
            return None
        self._call_consumed[fn_name] = idx + 1
        ed = dict(queue[idx])
        ed["__cst__"] = call
        try:
            return self._call_fn(ed)
        except (TypeError, ValueError):
            return None

    def leave_Expr(self, original_node, updated_node):
        # Bare top-level call statement. Patch it to the next matching bare-call
        # edit for this callee; depth filter keeps calls nested in bodies alone.
        if self._depth != 0:
            return updated_node
        patched = self._consume_call_patch(updated_node.value)
        if patched is None:
            return updated_node
        return updated_node.with_changes(value=patched)

    def leave_Assign(self, original_node, updated_node):
        if len(updated_node.targets) != 1:
            return updated_node
        target = updated_node.targets[0].target
        if not isinstance(target, cst.Name):
            # A call assignment to a NON-Name target (eg, new_dict = defaultdict(...))
            # was surfaced as a bare-call edit by the forward pass - patch its value
            # the same way leave_Expr patches a bare call (same queue and source order).
            if self._depth == 0:
                patched = self._consume_call_patch(updated_node.value)
                if patched is not None:
                    return updated_node.with_changes(value=patched)
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
            # Same as leave_Assign - a non-Name annotated call assignment surfaced as
            # a bare-call edit, so patch its value from the shared queue.
            if self._depth == 0:
                patched = self._consume_call_patch(updated_node.value)
                if patched is not None:
                    return updated_node.with_changes(value=patched)
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
    # Already-made CST expression - pass it straight through. Lets the caller place
    # an exact node into the edits (e.g. a Call like `f("x")`) and have it used
    # verbatim, instead of being coerced from a Python value.
    if isinstance(py_value, cst.BaseExpression):
        return py_value

    # Value unchanged from what old_node already encodes - keep old_node verbatim.
    # _cst_to_python resolves a reference like `RenderFuncs.draw_type` to the live
    # callable (or `Some.ENUM` to the member); without this, write-back rebuilds
    # it from the bare object and drops the qualifier (RenderFuncs.draw_type →
    # draw_type) or the whole node. Mirrors the str branch's "py_value == old_code"
    # guard for non-string values. Identity compare - resolved callables and enum
    # members may be singletons, and `==` on arbitrary objects can be unsafe.
    if old_node is not None and not isinstance(py_value, (str, bool, int, float)):
        try:
            if _cst_to_python(old_node) is py_value:
                return old_node
        except Exception:
            pass

    # CodeLine - a raw code expression, never a string literal. Always parse it
    # as code (or keep old_node verbatim when unchanged), regardless of old_node:
    # a freshly-inserted CodeLine (old_node is None) must splice as `foo()`, not
    # quote as `"foo()"`. Checked before the plain-str branch since CodeLine is-a str.
    if isinstance(py_value, CodeLine):
        if old_node is not None and py_value == _cst_node_to_code(old_node):
            return old_node  # unchanged - preserve original formatting
        try:
            wrapper = cst.parse_module(f"_ = {py_value}\n")
            return wrapper.body[0].body[0].value
        except cst.ParserSyntaxError:
            return old_node  # unparseable (mid-edit?) - keep original, may be None

    # Strings - behavior depends on what old_node was:
    #   old_node is SimpleString → just/ literal (preserve quotes)
    #   old_node is something else → code expression, compare/parse
    #   old_node is None → string literal (safe fallback for new inserts)
    if isinstance(py_value, str):
        if old_node is None or isinstance(old_node, cst.SimpleString):
            # String literal
            if isinstance(old_node, cst.SimpleString):
                # Unchanged → keep the original literal verbatim. The naive
                # re-render below only escapes backslash + the quote char, so it
                # mangles control chars ('\n' → a literal newline) and chokes on
                # prefixed literals (r'...', b'...' - value[0] is the prefix, not
                # the quote). Preserving the node when the Python value matches
                # sidesteps all of that for the common no-edit round-trip.
                try:
                    if _cst_to_python(old_node) == py_value:
                        return old_node
                except Exception:
                    pass
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
                    new_call = fn(py_value)
                    # Surfaced from a `call(...)[k]` subscript (see
                    # _cst_to_python_dict_raw): the Call still holds the inner call,
                    # so re-wrap the rebuilt call in the original subscript to
                    # preserve the `[k]` on round-trip.
                    if (isinstance(old_node, cst.Subscript)
                            and isinstance(old_node.value, cst.Call)):
                        return old_node.with_changes(value=new_call)
                    return new_call
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
            # Keep the original qualifier verbatim (leading whitespace + base name)
            # and swap only the member. The base may not be the actual enum's
            # class name - a proxy stand-in (`Modes.WINDOW`, where Modes defers to
            # Mode) should round-trip back to `Modes.<member>`, not be rewritten to
            # `Mode.<member>` (which would force the view.mode import the proxy
            # exists to avoid). The members mirror the class 1:1, so swapping just
            # the attr is valid. Also preserves any dotted path like `mod.Enum`.
            return old_node.with_changes(attr=cst.Name(member_name))
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
      - Surviving non-starred positions: update value, keep comma/whitespace
      - StarredElement positions (`*x`): pass through unchanged in place — they
        don't appear in py_values (cst_tuple/list_to_tuple skip them), so they
        aren't matched against py_values, just preserved
      - New positions (sequence grew): appended at the end, comma cloned
      - Removed positions (sequence shrank): drop trailing non-starred slots
      - Last element: strip trailing comma only if original had none
    """
    old_els = list(old_node.elements)
    new_els = []

    # Determine the comma style to clone for new/promoted elements. Get it from
    # the first non-starred element (a starred element's whitespace may differ).
    non_starred_old = [el for el in old_els if not isinstance(el, cst.StarredElement)]
    inner_comma = cst.Comma(whitespace_after=cst.SimpleWhitespace(""))
    if len(non_starred_old) >= 2:
        inner_comma = non_starred_old[0].comma
    elif len(old_els) >= 2:
        inner_comma = old_els[0].comma

    # Did the original have a trailing comma on its last element?
    had_trailing = False
    if old_els and not isinstance(old_els[-1].comma, cst.MaybeSentinel):
        had_trailing = True

    # Walk old_els; advance py_values only for non-starred slots. Starred
    # elements (`*x`) pass through unchanged at their original positions.
    py_iter = iter(py_values)
    py_exhausted = object()
    for old_el in old_els:
        if isinstance(old_el, cst.StarredElement):
            new_els.append(old_el)
            continue
        py_val = next(py_iter, py_exhausted)
        if py_val is py_exhausted:
            continue  # this non-starred slot was dropped
        new_value = _python_to_cst_expr(py_val, old_el.value)
        if new_value is None:
            new_value = old_el.value
        new_els.append(old_el.with_changes(value=new_value))

    # Any py_values left over → new slots appended at the end.
    for py_val in py_iter:
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


def _node_leaf_name(node):
    """The trailing identifier of a bare Name or dotted Attribute, else None.
    Name("f") → "f"; Attribute(RenderFuncs, draw_text) → "draw_text"."""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return node.attr.value
    return None


def _callable_to_cst_expr(py_value, old_node=None):
    """Convert a callable (function, class, builtin) to a CST expression.

    Uses __qualname__ to determine the name shape:
      my_func        → Name("my_func")
      MyClass.method → Attribute(Name("MyClass"), Name("method"))

    Preserves formatting from old_node via with_changes() when the
    node shape matches.
    """
    # If old_node already refers to this exact callable, keep it verbatim.
    # __qualname__ strips the module (e.g. numpy.uint32 → "uint32"), so
    # rebuilding from it would lose any qualifier the node actually had.
    if old_node is not None:
        try:
            if _cst_to_python(old_node) is py_value:
                return old_node
        except (TypeError, ValueError):
            pass
        # Scope-less fallback: the reverse runs WITHOUT the resolution scope the
        # forward built up, so _cst_to_python above can't re-resolve old_node to
        # confirm identity and the guard misses. If old_node already names this
        # callable by its leaf (a bare Name, or the final `.attr` of a dotted
        # ref), the reference is unchanged - preserve it verbatim so the source
        # qualifier isn't stripped (e.g. RenderFuncs.draw_text → draw_text, whose
        # _LazyRenderFunc proxy has __qualname__ None but __name__ "draw_text").
        leaf = getattr(py_value, "__name__", None)
        if leaf is not None and _node_leaf_name(old_node) == leaf:
            return old_node

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


# ── Background cache warmer ───────────────────────────────────
# Keeps the fast caller-index cache (_index_refs_cache) hot on a worker thread
# so the editor's Index is instant. Placed at module end so the index infra it
# leans on (_threading, _index_refs_cache, _src_mod_map, _file_index_refs) is
# already defined when this runs at import time.
import time as _time
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window as _window


def build_index_cache() -> tuple:
    """(Re)resolve every loaded src file's references into _index_refs_cache so
    the fast Index is warm before it's clicked. Only files whose mtime changed
    are re-parsed (the rest hit the cache). Returns (n_src_files, n_reparsed).
    Pure index work — safe on a background thread (no imgui / Melty)."""
    mod_map = _src_mod_map()
    reparsed = 0
    for path, mod in mod_map.items():
        prev = _index_refs_cache.get(path)
        _file_index_refs(path, mod)
        if _index_refs_cache.get(path) is not prev:
            reparsed += 1
    return len(mod_map), reparsed


@_window
class SymbolIndexCache:
    """Keeps the fast caller-index cache warm on a background thread, so the
    editor's Index button is instant. Flip `auto` off to stop the periodic
    refresh; call rebuild() for a one-shot. Status fields below are live."""
    auto = False              # keep the cache warm in the background
    interval_s = 15.0        # seconds between background refresh passes
    startup_delay_s = 0.2    # wait for src modules to finish importing first
    # ── status (written by the worker) ──
    building = False
    src_files = 0
    last_reparsed = 0
    last_secs = 0.0
    builds = 0

    @classmethod
    def _build_once(cls):
        if cls.building:
            return
        cls.building = True
        t0 = _time.perf_counter()
        try:
            cls.src_files, cls.last_reparsed = build_index_cache()
        except Exception:
            pass
        finally:
            cls.last_secs = round(_time.perf_counter() - t0, 3)
            cls.builds += 1
            cls.building = False

    @classmethod
    def rebuild(cls):
        """Kick a one-off cache build on a background thread."""
        _threading.Thread(target=cls._build_once, daemon=True,
                          name="symbol-index-rebuild").start()


def _symbol_index_daemon():
    # Re-fetch the class from sys.modules each pass rather than closing over the
    # module-load-time SymbolIndexCache: a restart-in-place reload swaps this
    # module for a fresh instance, and the sys-guard below keeps THIS daemon running
    # the old one - so it must target whatever class is live now, building into
    # the live module's _index_refs_cache (its method's __globals__), not the
    # orphaned original. On a clean start there's only one instance and this is
    # a no-op.
    modname = __name__

    def live_cls():
        mod = sys.modules.get(modname)
        return getattr(mod, "SymbolIndexCache", None) or SymbolIndexCache

    _time.sleep(max(0.0, getattr(live_cls(), "startup_delay_s", 8.0)))
    live_cls()._build_once()                             # one warm build at startup
    while True:                                          # then keep cache fresh
        cls = live_cls()
        if getattr(cls, "auto", True):
            cls._build_once()
        _time.sleep(max(1.0, getattr(cls, "interval_s", 15.0)))


# Guard on `sys` (shared across the src./lsd. module universes) so the daemon starts
# exactly once even though this module can be imported under two names.
if not getattr(sys, "_symbol_index_daemon_started", False):
    sys._symbol_index_daemon_started = True
    _threading.Thread(target=_symbol_index_daemon, daemon=True,
                      name="symbol-index-daemon").start()