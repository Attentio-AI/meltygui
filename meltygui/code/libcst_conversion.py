"""
libcst ↔ Python type converters for the Melty registry.

Individual CST node types get their own converter pairs. Compound types
(Dict, List, Tuple) call convert() recursively on their children.

Dict results carry __cst__ for lossless round-trip reconstruction.
The original immutable CST node is never serialized — just referenced.
"""

import ast
import enum
import functools
import inspect
import math
import re
import struct
import sys
import threading
import time
from typing import Any

import libcst as cst
from libcst._nodes.internal import CodegenState as _CodegenState

from meltygui.core.styling.fonts import Font
from meltygui.core.melty import Melty
from meltygui.core.rendering.modes import Modes
from meltygui.core.rendering.modes import _LazyMode
from meltygui.core.diagnostics.notifications import notify
from meltygui.core.diagnostics.notifications import lag_traced
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.windowing.glfw_utils import print_stack_trace
from meltygui.core.conversion.path_finder import convert
from meltygui.core.conversion.path_finder import PendingState
from meltygui.core.conversion.path_finder import Pending
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import defaults
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.diagnostics.perf_trace import trace as _ptrace
from meltygui.core.diagnostics.perf_trace import trace_rl as _ptrace_rl
from meltygui.core.diagnostics.perf_trace import span as _pspan
from meltygui.core.diagnostics.perf_trace import once as _ponce


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


@defaults(tint=(0.83, 0.58, 0.20, 0.0), shadow=False, is_tree=False, use_cache=True, show_bg=False,
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


@defaults(tint=(0.7, 0.406749, 0.0264792, 0.09), shadow=True, child_kwargs={"editable": False},
          z_offset=0, name_color=(1.0, 0.479, 0.0), font=Font.JETBRAINS_MONO_19, drop_tail_height=0.0,
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
        self.target = target  # e.g. "i", "x, y"
        self.iter = iter  # e.g. "range(10)", "items"
        self._bg_hash_cache: str | None = None

    def __bg_hash__(self) -> str:
        if self._bg_hash_cache is None:
            # hash() on a str uses a fast SipHash - O(n) once, then O(1)
            self._bg_hash_cache = str(hash(self.iter + self.target))
        return self._bg_hash_cache


@defaults(tint=(0.1, 0.1, 0.1, 0.0), shadow=True, is_tree=False, header_same_line=True, name_color=(1.0, 0.479, 0.0),
          bg_offset=-3, show_bg=True)
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


@defaults(disable_scroll=True, shadow=False, use_cache=True, child_kwargs={'show_bg': False})
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


# NOTE: the @defaults below DUPLICATE GeneralParse's on purpose. They are NOT
# inherited: default_kwargs_by_type (core_render.py) is an EXACT-type lookup, so
# a subclass with no @defaults of its own would render with none of GeneralParse's
# tint/bg/shadow. Declaring them here keeps the visual treatment identical to a
# plain GeneralParse today, while giving these types their own slot to diverge
# later (the whole point of splitting them out). Same pattern as CallParse below.
@defaults(disable_scroll=True, show_tint=True, shadow=True, excluded=("decorators"),
          use_cache=True)
class ClassParse(GeneralParse):
    """A class definition's parsed body, as a GeneralParse subclass.

    isinstance(c, dict)         → True, so iteration/access works normally.
    isinstance(c, GeneralParse) → True, so it renders through draw_collection and
        flows through every Mode/converter that handles a GeneralParse with no
        extra wiring (type routing walks the MRO).
    isinstance(c, ClassParse)   → True, so code can recognise a class parse BY
        TYPE instead of sniffing `__cst__` for a cst.ClassDef (the old
        _is_classdef_parse heuristic).

    Produced by cst_classdef_to_dict. Carries the same dict shape as a plain
    GeneralParse (body-level vars, nested classes/methods, __init__ self.X,
    decorators) — the distinct type is purely for differentiation, exactly like
    DecorationParse vs CallParse.
    """


@defaults(disable_scroll=True, show_bg=True, shadow=True, z_offset=2, excluded=("decorators"),
          use_cache=True, tint=(0.009, 0.2495, 0.39, 0.172))
class EnumParse(ClassParse):
    """An enum class definition's parse — a ClassParse specialisation.

    isinstance(e, ClassParse)   → True, so every class-handling path still applies
        unchanged: _is_classdef_parse, the live-apply class-var preview, and
        recompile-as-class all key off ClassParse, so an enum keeps getting them.
    isinstance(e, EnumParse)    → True, so an enum is now distinguishable BY TYPE
        from a plain class (route it to its own renderer / Mode, give its members a
        bespoke widget, etc.).

    Produced by cst_classdef_to_dict when the ClassDef is (syntactically) an enum —
    a base or metaclass whose name ends in 'Enum'/'Flag' (Enum, IntEnum, StrEnum,
    IntFlag, the project's RelaxedEnum, …); see _classdef_is_enum. The @defaults
    mirror ClassParse's so enums look like classes by default (they ARE classes),
    while owning their own exact-type slot to diverge later — same reason ClassParse
    duplicates GeneralParse's (default_kwargs_by_type is an exact-type lookup).
    """


@defaults(disable_scroll=True, show_bg=True, shadow=True, icon="def", use_cache=True, tint=(0.009, 0.2495, 0.39, 0.922))
class FunctionParse(GeneralParse):
    """A function / method definition's parse, as a GeneralParse subclass.

    Same contract as ClassParse: still a dict and a GeneralParse (so routing and
    rendering are unchanged), but recognisable BY TYPE rather than by the presence
    of 'parameters'/'locals' keys — the old _is_funcdef_parse heuristic, which a
    member literally named `parameters` would trip.

    Produced by cst_funcdef_to_dict. The "parameters" / "locals" sub-dicts inside
    it stay plain GeneralParse (they are not themselves funcdefs).
    """


@defaults(tint=(0.04, 0.17, 0.25, 0.016), bg_offset=2, font=Font.JETBRAINS_MONO_19, is_tree=False, shadow=False,
          z_offset=1, child_kwargs={"font": Font.JETBRAINS_MONO_19})
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


@defaults(tint=(0.86, 0.3345581, 0.07, 0.7), icon="@", disable_scroll=True)
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
from concurrent.futures import Future as _Future

# Two pools, same forkserver context. "index" (4 workers) runs the heavy
# symbol-index and usage jobs; "ac" (1 worker) is reserved for INTERACTIVE jobs
# (member completion, signature help). Interactive jobs have their own pool for
# two reasons: a multi-second index job is never run ahead of the popup; and
# every interactive job lands on the SAME worker process, so parso's per-process
# parse cache stays warm for the file being edited (~10-30ms per completion vs
# ~1s re-parsing cold on whichever index worker happened to be idle).
_jedi_pools: dict[str, _PPE] = {}
_jedi_pool_lock = threading.Lock()
_jedi_mp_ctx = None


def _get_jedi_mp_ctx():
    """The multiprocessing context the jedi pool forks workers from — `forkserver`,
    NOT the default `fork`.

    With `fork`, ProcessPoolExecutor forks its 4 workers straight from THIS process.
    The pool is built lazily (first autocomplete) and rebuilt on every restart-in-place
    — both AFTER the ~4.6 GB Mistral model is resident — so each worker COW-inherits the
    whole model-laden address space: 4 × 4.6 GB ≈ 18 GB of RSS the jedi workers never
    touch (and which slowly faults to real RAM as refcounts dirty the shared pages).
    That is the per-restart "memory overhead": almost entirely phantom model pages.

    `forkserver` re-execs a clean, minimal server process (no model — verified ~0.4 GB,
    torch but no CUDA context) and forks workers from THAT. The server is clean no matter
    when the pool is built, so restarts don't reintroduce the model. We preload this
    module so the server imports the worker fns' dependencies once and the 4 workers
    COW-share that ~0.4 GB base instead of each re-importing it. Built once and cached:
    the server persists across restarts, so rebuilt pools fork from the same clean base."""
    global _jedi_mp_ctx
    if _jedi_mp_ctx is None:
        import multiprocessing as _mp
        ctx = _mp.get_context("forkserver")
        try:
            ctx.set_forkserver_preload([__name__])
        except Exception:
            pass
        _jedi_mp_ctx = ctx
    return _jedi_mp_ctx


def _get_jedi_pool(kind: str = "index") -> _PPE:
    # A studio restart-in-place ends the session, which fires concurrent.futures'
    # atexit (_python_exit) even though THIS process keeps running. That sets the
    # module-level _global_shutdown flag and kills the worker processes, so EVERY
    # ProcessPoolExecutor.submit() raises "after global shutdown" forever -
    # silently disabling jedi + every off-GIL task. Clear that stale signal (the
    # process is not actually exiting) and rebuild BOTH pools.
    import concurrent.futures.process as _cfp
    with _jedi_pool_lock:
        if getattr(_cfp, "_global_shutdown", False):
            _cfp._global_shutdown = False
            _jedi_pools.clear()
        pool = _jedi_pools.get(kind)
        if pool is None or getattr(pool, "_shutdown_thread", False):
            pool = _PPE(max_workers=1 if kind == "ac" else 4,
                        mp_context=_get_jedi_mp_ctx())
            _jedi_pools[kind] = pool
        return pool


def warm_interactive_jedi():
    """Spin up the interactive jedi worker in the background so the FIRST
    completion popup of a session doesn't wait on it — cold it pays forkserver
    worker spawn + `import jedi` + grammar/typeshed load (seconds in the loaded
    app). Fully async and idempotent: on a warm pool this is one trivial ~30ms
    subprocess job. Called from Melty.init."""
    _submit_interactive(_jedi_complete_worker, "import os\nos.", 2, 3)


def _submit_interactive(worker, *args) -> _Future:
    """Future for `worker(*args)` on the interactive ("ac") jedi worker, without
    ever touching the pool on the CALLING thread. This runs on the render thread
    (per keystroke), and a cold pool's first submit blocks on spawning the
    forkserver — which preloads this module, >1s of imports — so pool get +
    submit happen on a short-lived daemon thread and the pool future's result is
    mirrored into the returned Future (same done()/add_done_callback contract)."""
    out = _Future()

    def _bg():
        try:
            f = _get_jedi_pool("ac").submit(worker, *args)
        except Exception as e:
            out.set_exception(e)
            return

        def _copy(f):
            try:
                out.set_result(f.result())
            except BaseException as e:
                out.set_exception(e)

        f.add_done_callback(_copy)

    threading.Thread(target=_bg, daemon=True, name="jedi-ac-submit").start()
    return out


_jedi_projects = {}


def _jedi_project(file_path=None):
    """A cached Jedi project using the file owner's source paths and venv."""
    import jedi
    from meltygui.code.source_context import analysis_project
    project = analysis_project(path=file_path)
    held = _jedi_projects.get(project.key)
    if held is None:
        held = _jedi_projects[project.key] = jedi.Project(
            path=project.root, environment_path=project.environment,
            added_sys_path=list(project.source_paths))
    return held


def _jedi_script(file_path, code=None):
    """jedi.Script on the file’s owning project. `code` (in-memory source) overrides
    the on-disk file so unsaved edits are analyzed; path still drives resolution."""
    import jedi
    return jedi.Script(code=code, path=str(file_path), project=_jedi_project(file_path))


def shutdown_jedi_pool():
    with _jedi_pool_lock:
        pools = list(_jedi_pools.values())
        _jedi_pools.clear()
    for pool in pools:
        # Kill worker processes first because shutdown(cancel_futures=True) only
        # cancels pending futures, not ones already running in a subprocess.
        for pid, proc in list(getattr(pool, '_processes', {}).items()):
            try:
                proc.kill()
            except Exception:
                pass
        pool.shutdown(wait=False, cancel_futures=True)


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


# ── Type-aware member completion (jedi, async) ────────────────
# Powers the editor's `imgui.`-style attribute popup. Two obstacles, two answers:
#   1. The buffer holds only a function/class SPAN, so `import imgui`, the
#      enclosing class (for `self.`), and every module-level name live above it -
#      jedi on the span and never sees them. Fixed by handing jedi the
#      WHOLE FILE with the live buffer spliced over the span (_full_file_context):
#      the span's Address knows its path + line range, and PendingSave supplies
#      the file text with all unsaved queued edits applied. With the file's real
#      path + the src-scoped Project, jedi resolves `self.`, locals built from
#      project classes, src imports, and import-statement completion. A plain
#      buffer with no Address falls back to the old dedent-the-span mode.
#   2. imgui (and torch, numpy) are compiled C-extension modules: jedi's STATIC
#      analysis finds no members in them (it works for pure-python like `os.`).
#      `jedi.Interpreter` solves that - it completes against LIVE names via C
#      introspection, so we hand it a namespace binding the names worth completing
#      (the live modules) and it resolves `imgui.<790 real members>`. To support
#      another module, add it to _COMPLETION_MODULES.
# (The editor also short-circuits dotted receivers it can resolve against the
# live module namespace in-process - see _ensure_member_completions - so jedi
# only sees the receivers that need static inference: locals, self, imports.)
_COMPLETION_MODULES = {
    "imgui": "imgui",
    "glfw": "glfw",
    "np": "numpy",
    "torch": "torch",
}
_completion_ns_cache = None


def _completion_namespace():
    """{alias: live module} for Interpreter completion, imported once per worker.
    A module that fails to import is simply omitted (no completion for it)."""
    global _completion_ns_cache
    if _completion_ns_cache is None:
        import importlib
        ns = {}
        for alias, mod in _COMPLETION_MODULES.items():
            try:
                ns[alias] = importlib.import_module(mod)
            except Exception:
                pass
        _completion_ns_cache = ns
    return _completion_ns_cache


def _jedi_complete_worker(code: str, line: int, col: int, path: str = None):
    """Child-process worker: jedi.Interpreter completions at (1-indexed `line`,
    0-indexed `col`) in `code`, resolving names against the live module namespace.
    `path` (full-file mode) is the real file the code came from — with it jedi
    gets the src-scoped Project, so relative/src imports resolve statically.
    Returns picklable [(name, type), ...] (type ∈ jedi's
    module/class/function/instance/param/keyword/statement/property/path)."""
    import jedi
    try:
        kw = {"path": path, "project": _jedi_project(path)} if path else {}
        script = (jedi.Script(code, **kw) if path else
                  jedi.Interpreter(code, [_completion_namespace()]))
        comps = script.complete(line, col)
    except Exception:
        return []
    return [(c.name, c.type) for c in comps if c.name]


def _completion_common_indent(text: str) -> int:
    """Smallest leading-space count among non-blank lines — the block indent that
    dedenting the span removes (so the caret column can be shifted to match)."""
    indents = [len(l) - len(l.lstrip(" ")) for l in text.split("\n") if l.strip()]
    return min(indents) if indents else 0


def _full_file_context(text: str, address):
    """(code, caret_line_shift, path_str) for jedi over the WHOLE file the edited
    span lives in: the file's current in-memory text (disk + every queued unsaved
    edit, via PendingSave) with the live buffer `text` spliced over the span's
    line range. The buffer keeps its file indentation, so lines splice verbatim
    and a caret at buffer line L sits at file line `shift + L` (same column).
    None when the span has no usable file context (a plain buffer, an unreadable
    file) — callers fall back to the dedented-span mode."""
    try:
        if address is None or getattr(address, "path", None) is None:
            return None
        if getattr(address, "start", None) is None:
            return text, 0, str(address.path)
        from meltygui.editor.pending_save import PendingSave
        file_text = PendingSave.current_file_text(address.path)
        if file_text is None:
            return None
        lines = file_text.split("\n")
        if not (0 <= address.start <= len(lines)):
            return None
        # Same trailing-newline convention as PendingSave.current_file_text /
        # apply_all_saves, so the splice matches how a save would land.
        buf = text[:-1] if text.endswith("\n") else text
        end = address.end if address.end is not None else address.start
        end = max(address.start, min(end, len(lines)))
        lines[address.start:end] = buf.split("\n")
        return "\n".join(lines), address.start, str(address.path)
    except Exception:
        return None


def submit_member_completion(text: str, line0: int, col: int, address=None):
    """Submit a jedi member-completion job for a caret at 0-indexed (`line0`,
    `col`) within editor `text` (a function/class span). With an `address` the
    job runs over the whole surrounding file (full context: self., src imports,
    project-typed locals — see _full_file_context); without one the span is
    dedented to column 0 and parsed alone. Returns a Future of
    [(name, type), ...] — or None if the pool is unavailable. Non-blocking;
    poll Future.done() from the render loop."""
    try:
        ctx = _full_file_context(text, address)
        if ctx is not None:
            code, shift, path = ctx
            return _submit_interactive(
                _jedi_complete_worker, code, shift + line0 + 1, col, path)
        ci = _completion_common_indent(text)
        dedented = "\n".join(l[ci:] if len(l) >= ci else l for l in text.split("\n"))
        return _submit_interactive(
            _jedi_complete_worker, dedented, line0 + 1, max(0, col - ci))
    except Exception:
        return None


def _jedi_signatures_worker(code: str, line: int, col: int, path: str = None):
    """Child-process worker: jedi.Interpreter signature help at (1-indexed `line`,
    0-indexed `col`) — the callee whose parens enclose the caret. `path` (full-
    file mode) scopes jedi to the src Project so src-defined callees resolve.
    Returns picklable [(call_name, [param_string, ...]), ...] (param strings like
    'x', 'y=0', '*args')."""
    import jedi
    try:
        kw = {"path": path, "project": _jedi_project(path)} if path else {}
        script = (jedi.Script(code, **kw) if path else
                  jedi.Interpreter(code, [_completion_namespace()]))
        sigs = script.get_signatures(line, col)
    except Exception:
        return []
    out = []
    for s in sigs:
        try:
            params = [p.to_string() for p in s.params]
        except Exception:
            params = []
        out.append((s.name, params))
    return out


def submit_signature_help(text: str, line0: int, col: int, address=None):
    """Submit a jedi signature-help job for a caret at 0-indexed (`line0`, `col`)
    inside a call's parens within editor `text`. Same full-file/fallback split as
    member completion: with an `address` jedi sees the whole surrounding file (so
    src-defined and self. callees resolve); without one, the dedented span + the
    live-module namespace (so `imgui.text(` still resolves). Returns a Future of
    [(call_name, [params]), ...] or None. Non-blocking."""
    try:
        ctx = _full_file_context(text, address)
        if ctx is not None:
            code, shift, path = ctx
            return _submit_interactive(
                _jedi_signatures_worker, code, shift + line0 + 1, col, path)
        ci = _completion_common_indent(text)
        dedented = "\n".join(l[ci:] if len(l) >= ci else l for l in text.split("\n"))
        return _submit_interactive(
            _jedi_signatures_worker, dedented, line0 + 1, max(0, col - ci))
    except Exception:
        return None


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

# ── Span-result persistence ───────────────────────────────────
# The span cache below holds the END RESULT of indexing ({symbol: SymbolUsage}
# per source span). Unlike _index_refs_cache its contents are pure
# paths/lines/names - no live-object id()s - so it can outlive the module
# across a restart-in-place, adopted through a sys-level attr (same trick as
# the daemon store; sys is shared across re-execs). It is NOT written to disk;
# a fresh process starts cold. _mtime_snapshot records each file's mtime when
# the results were computed, so the warmer's generation bump runs only files
# whose mtime moved past the snapshot.


def _reclass_adopted_spans(spans: dict) -> int:
    """Re-point the adopted store's SymbolUsage / UsageRef instances at THIS
    session's classes. An instance references its class, and the instances in
    a sys-adopted store were built by earlier sessions — so each one pinned
    that session's class → methods → __globals__ → its ENTIRE module graph
    (old Melty, draw_states, cst trees). The gc boot profile showed three
    whole sessions alive this way, rooted by 447k UsageRefs. The classes are
    identical source, so the layouts match and __class__ assignment is legal;
    an entry whose instances can't be re-classed (slots changed) is dropped
    so it recomputes. Returns the number of entries dropped."""
    dropped = []
    n_re = 0
    for key, val in spans.items():
        try:
            _sig, syms = val
            for su in syms.values():
                if type(su) is not SymbolUsage:
                    su.__class__ = SymbolUsage
                    n_re += 1
                d = su.definition
                if d is not None and type(d) is not UsageRef:
                    d.__class__ = UsageRef
                    n_re += 1
                for c in su.callers:
                    if type(c) is not UsageRef:
                        c.__class__ = UsageRef
                        n_re += 1
        except Exception:
            dropped.append(key)
    for key in dropped:
        spans.pop(key, None)
    print(f"symbol store: re-classed {n_re} adopted instances, dropped {len(dropped)} spans")
    return len(dropped)


def _load_symbol_store() -> dict:
    store = getattr(sys, "_symbol_index_store", None)
    if isinstance(store, dict):
        store.setdefault("hashes", {})  # adopt; backfill the hash dict if older
        store["origin"] = "sys"  # this process life got the spans from sys
        n_dropped = _reclass_adopted_spans(store.get("spans", {}))
        for k in list(store.get("hashes", {})):
            if k not in store["spans"]:
                store["hashes"].pop(k, None)
        if n_dropped:
            _ptrace("store: dropped un-reclassable adopted spans", n=n_dropped)
        _ptrace("store: adopted live symbol store (restart-in-place)",
                spans=len(store.get("spans", ())), gen=store.get("gen"))
        return store  # restart-in-place: adopt those dicts
    store = {"spans": {}, "gen": 0, "mtimes": {}, "hashes": {}, "origin": "fresh"}
    sys._symbol_index_store = store
    return store


def _prune_symbol_store():
    """Drop span entries whose file has moved on since they were computed
    (sig mtime != the file's CURRENT disk mtime, or the file is gone) —
    EXCEPT one stale entry per file, kept as the incremental SEED.

    A span's key is (path, start, end) and `end` tracks the file's length, so
    every content edit mints a NEW key — the superseded-sibling evict in
    _store_usages only fires when the live view walks off a span in-session.
    Stale keys from prior sessions therefore accumulate forever (observed:
    a 121MB pickle of ~200 dead whole-file spans at ~2MB each). One stat per
    unique path (content-free — the CLAUDE.md hashing ban stays respected);
    spans still in use have their sig mtime refreshed by the hash rescue on
    every serve, so anything failing this check was never served since its
    file changed on disk.

    Seed retention (stale_gen_incremental): the next session's first compute
    on a changed file can reuse a stale entry's cross-file half incrementally
    instead of cold-recomputing — but only if a stale entry survives the
    prune. Keep the best one per still-existing path (latest content, then
    widest span — the widest overlaps whatever span gets opened next); bounded
    at ONE per path so the unbounded-growth bug stays fixed."""
    try:
        from meltygui.core.runtime.toggles import Toggles  # lazy: breaks import cycle
        _keep_seeds = Toggles.TextEditor.SymbolUsages.stale_gen_incremental
    except Exception:
        _keep_seeds = True
    _unstatted = object()
    cur_mtime: dict = {}
    stale: list = []
    best_seed: dict = {}  # path -> ((mtime, span_width), key)
    for k in list(_symbol_usage_cache):
        path = k[0]
        m = cur_mtime.get(path, _unstatted)
        if m is _unstatted:
            try:
                m = path.stat().st_mtime
            except OSError:
                m = None
            cur_mtime[path] = m
        entry = _symbol_usage_cache.get(k)
        if entry is None or m is None or entry[0][0] != m:
            stale.append(k)
            if _keep_seeds and entry is not None and m is not None:
                rank = (entry[0][0] or 0, k[2] - k[1])
                cur = best_seed.get(path)
                if cur is None or rank > cur[0]:
                    best_seed[path] = (rank, k)
    seeds = {v[1] for v in best_seed.values()}
    for k in stale:
        if k in seeds:
            continue
        _symbol_usage_cache.pop(k, None)
        _span_text.pop(k, None)
        _span_hashes.pop(k, None)
        _usage_graph_source.pop(k, None)
    for path in [p for p in _mtime_snapshot if cur_mtime.get(p, 0) is None]:
        _mtime_snapshot.pop(path, None)


_symbol_store = _load_symbol_store()
_symbol_usage_cache: dict = _symbol_store[
    "spans"]  # (resolved_path, start, end) -> (sig, {sym: SymbolUsage}); sig = (mtime, pending_gen, accurate, gen)
_mtime_snapshot: dict = _symbol_store["mtimes"]  # resolved_path -> mtime at last counted change

# Content hash (whole current file text) each cached span was computed from, keyed
# by the same span key. PERSISTED alongside the spans (a parallel dict, so the
# span tuple index is unchanged → old/new pickles interop, no version bump). The
# longevity lever: a span's sig embeds the GLOBAL index generation, so ANY src file
# changing bumps gen and lapses EVERY span - an unchanged file recomputes just
# because something else moved (worst across sessions). The hash rescue
# (_compute_symbol_usages) lets a sig miss serve the cached result when the file's
# CONTENT is bit-identical, re-stamping the sig instead of recomputing. Tradeoff:
# cross-file callers can go (acceptably) stale in an unchanged file until it's
# edited and re-indexed. Content hashing for invalidation is normally banned
# here - Lukas allowed it for THIS cache given the recompute cost; it's only on
# a MISS, never to detect a hit.
_span_hashes: dict = _symbol_store["hashes"]  # (resolved_path, start, end) -> 16-byte content hash

# --- Usage-graph provenance (debug display) ------
# span key -> (base, incr_count): where that span's usage data came from.
# base ∈ "fresh" (full recompute this session) / "disk" (pickle warm-start) /
# "sys" (adopted from a restart-in-place); incr_count = how many incremental
# passes have refreshed it since its base was established. The editor's
# usage-source badge (draw_text top-right) reads it via usage_graph_source().
# sys-adopted (never pickled): a restart-in-place keeps real tags; a fresh
# process tags everything restored off the pickle "disk"; entries the map
# doesn't cover (computed before this tracking existed) fall back to the
# store's origin.
_usage_graph_source: dict = getattr(sys, "_symbol_usage_source", None)
if _usage_graph_source is None:
    _usage_graph_source = {}
    sys._symbol_usage_source = _usage_graph_source
for _k in _symbol_usage_cache:
    _usage_graph_source.setdefault(_k, (_symbol_store.get("origin", "disk"), 0))


def usage_graph_source(file_path, start_line: int, end_line: int):
    """Formatted provenance label for the usage graph covering
    [start_line, end_line] of `file_path` — e.g. "disk", "fresh", "sys+4i"
    (base plus how many incremental passes refreshed it) — or None when no
    tracked span overlaps. Exact span key first, then the best-overlapping
    same-file span (the key's line range drifts off the view's as edits
    shift it)."""
    resolved = _resolve_memo.get(file_path)
    if resolved is None:
        try:
            resolved = _Path(file_path).resolve()
        except (OSError, ValueError):
            return None
        if len(_resolve_memo) > 512:
            _resolve_memo.clear()
        _resolve_memo[file_path] = resolved
    e = _usage_graph_source.get((resolved, start_line, end_line))
    if e is None:
        best_ov = 0
        for (p, s, en), v in _usage_graph_source.items():
            if p != resolved:
                continue
            ov = min(end_line, en) - max(start_line, s)
            if ov > best_ov:
                best_ov, e = ov, v
    if e is None:
        return None
    base, n = e
    return base if not n else f"{base}+{n}i"


# file_path str -> resolved Path, for the per-frame badge lookup (Path.resolve
# is a syscall; never repeat it per frame). Bounded, harmless to lose.
_resolve_memo: dict = globals().get("_resolve_memo") or {}


def _content_hash(text: str) -> bytes:
    """Stable 128-bit hash of a file's current text (disk + pending overlay) for
    the usage cache's content-validity check. blake2b is collision-free for this
    use and stable across sessions (builtin hash() is per-process-salted)."""
    import hashlib
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


# The exact buffer text a cached span result was computed from, indexed by the
# same span key. Drives the position-only fast path (_line_offset_map): on a
# pending-edit miss it diffs this snapshot against the live buffer to detect a
# blank-line-only edit and remap positions instead of recomputing. Deliberately
# NOT in _symbol_store (not pickled - full file text would bloat the index
# pickle, and it's cheap to re-snapshot on the next compute). sys-adopted so it
# survives hotswap / restart-in-place alongside _symbol_usage_cache; empty on a
# fresh process (first edit per span recomputes, then offsets thereafter).
_span_text: dict = getattr(sys, "_symbol_span_text", None)
if _span_text is None:
    _span_text = {}
    sys._symbol_span_text = _span_text

# File suffixes to drop from jedi search - a symbol DEFINED in one of these is
# skipped entirely, and any callers in them are filtered out. jedi only
# searches .py/.pyi to begin with (no per-extension search hook), so this is how
# you exclude by extension. str.endswith takes the tuple directly.
_JEDI_EXCLUDE_SUFFIXES: tuple = (".pyi",)


def _symbol_refs_worker(file_path: str, start_line: int, end_line: int, text=None) -> dict:
    """Child-process worker: for each distinct symbol occurring in
    [start_line, end_line], resolve its definition + project references.

    `text` (the current file content incl. unsaved edits) is handed to jedi as
    in-memory source so usages reflect the live buffer, not the stale disk file.

    Returns {symbol: {"sites": [(l,c)], "definition": (path,l,c,mod),
                      "callers": [(path,l,c,enclosing,mod), ...]}}.
    Only symbols DEFINED under src are kept (no callers of len/print/etc.)."""
    import jedi
    script = _jedi_script(file_path, code=text)
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
    """Rebuild {symbol: SymbolUsage} from the worker's plain-tuple output.

    Drops SELF-callers on the way through: the ref scan records the
    `def foo` / `class Foo` / class-body binding statement itself as a
    module-scope reference, so every symbol listed its own declaration as its
    first "caller" — the usage dropdown led with the symbol itself and every
    caller count read one high. The declaration is the jump SOURCE (at_def),
    never a target; a caller on the definition's own line in the definition's
    own file is that artifact. (One-line self-recursion `def f(): return f()`
    is also dropped — acceptably rare.)"""
    result = {}
    for sym, e in raw.items():
        dp, dl, dc, dm = e["definition"]
        definition = UsageRef(path=_Path(dp) if dp else None, line=dl, column=dc, module_name=dm)
        callers = [UsageRef(path=_Path(c[0]) if c[0] else None, line=c[1], column=c[2],
                            scope=c[3], module_name=c[4]) for c in e["callers"]
                   if not (dp is not None and c[1] == dl and c[0] == dp)]
        # Display spelling defaults to the key, but a local-variable entry keys on
        # scope+name+line (collision-proof) and carries its bare identifier in "name"
        # - that's what the user highlights / completes against.
        result[sym] = SymbolUsage(name=e.get("name", sym), definition=definition,
                                  callers=callers, sites=[tuple(s) for s in e["sites"]])
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

_index_refs_cache: dict = {}  # resolved_path -> (mtime, [(kind, key, line, col, scope)])

# Perf-trace metric: bumps every time _file_index_refs actually re-parses a
# file (mtime cache miss). Callers snapshot it around their scan loop to report
# "N of src files re-parsed" - the difference between a warm ~10ms scan and a
# cold ~1s one.
_index_refs_reparses = 0

# Whole-FILE parse artifacts (the ast tree + the three sub-tree walks derived
# from it) for the file _symbol_refs_index is analyzing, cached so every span in
# the SAME file shares one parse+walk instead of redoing it. These depend only on
# the file's content + the index generation, NEVER on the [start, end] span - yet
# the old code re-parsed and re-walked the whole file (~55ms on a 7k-line file)
# for every span! Keyed exactly like the usage cache: mtime (disk writes),
# pending_gen (deferred edits that never touch disk), index gen (cross-file / live
# object moves). One entry per file (overwritten on a sig change), so it's bounded
# by the number of distinct files that had a span computed.
_file_parse_cache: dict = {}  # resolved_path -> (sig, (tree, imports, refs, bindings))

# Per-class definition LINE, cached by (defining file, qualname) and invalidated
# on the file's mtime. `inspect.getsourcelines(obj)` ast.parses the .src file it
# lives in on every call (~10ms/class — confirmed: 1 parse per class, 0 per
# function); the def-resolution loop calls it once per module-level symbol, and a
# heavily-referenced span (Mode) resolves dozens of classes from stable src. The
# cache skips the re-parse when the def file is unchanged. Key on qualname (not id),
# so it survives object churn and mtime guards staleness.
_def_line_cache: dict = {}  # (defining_file, qualname|id) -> (mtime, lineno)

# Bumped by the background cache warmer (build_index_cache) whenever any src
# file's mtime moved past _mtime_snapshot (a REAL content change - a mere
# refs-cache rebuild after reboot doesn't count). Span-level index results
# (_symbol_usage_cache) key on it, so a caller added in ANOTHER file
# invalidates this file's cached usages after one warmer pass - mtime alone
# only sees edits to THIS file. Doubles as a "results need servicable" gate
# (> 0) for the auto-index pass in cst_span_to_dict. Backed from the
# sys-level store so cached spans stay valid across restart-in-place; bumps write
# back to the store (ints rebind, dicts are shared by reference).
_index_generation = _symbol_store["gen"]

# Called (from the warmer's daemon thread) with the new generation after each
# bump. registered lazily by Editor (new_converters wakes the codecomplet
# cache so idle editors re-index) - a registry instead of an import to avoid
# the cycle, deduped by __name__ so hotswap re-registration doesn't stack.
_index_bump_callbacks: list = []


def _wait_for_no_drag(max_wait=30.0, poll=0.05, label=""):
    """Hold a background index pass while the user is mid-gesture — index CPU
    is GIL-bound, so it surfaces as dropped frames at exactly the moment frame
    pacing matters most. Reads Melty's per-frame drag flags (plain class
    attrs: cross-thread safe, at worst one frame stale; a held button keeps
    them True even with no frames flowing, and the release event always
    produces a frame that clears them). max_wait=0 is an instant probe.
    Returns False when the drag outlasted max_wait — callers bail and rely on
    a later retry (the editor-side nudge re-indexes any gp whose generation
    stamp is stale, so a skipped pass self-heals). `label` names the caller in
    the perf-trace timeline (a long wait here delays whatever that caller was
    about to compute)."""
    waited = 0.0
    while getattr(Melty, "window_drag", False) or getattr(Melty, "on_drag", False):
        if waited >= max_wait:
            if waited > 0:  # instant probes (max_wait=0) stay silent
                _ptrace(f"drag-wait GAVE UP after {waited:.2f}s", where=label)
            return False
        _time.sleep(poll)
        waited += poll
    if waited >= 0.1:
        _ptrace(f"drag-wait held {waited:.2f}s", where=label)
    return True


# (built_at, mod_map) - _src_mod_map costs ~30ms of GIL-bound Path.resolve()
# over every loaded src module, and the startup index burst calls it once per
# open editor, plus once per warmer pass. 5s TTL: plenty fresh (a module
# imported inside the TTL is picked up next pass), races are cheap (worst
# case two threads both rebuild).
_src_mod_map_memo: tuple = (0.0, None)
_SRC_MOD_MAP_TTL = 5.0


def _src_mod_map() -> dict:
    """{resolved_file: module} for every loaded src module. Dual import paths
    (`src.lsd...` vs `lsd...`) create DUPLICATE modules for the same file with
    DIFFERENT live objects; the app imports via `src.`, so prefer the
    `src.`-prefixed module so refs and targets resolve to the same objects."""
    global _src_mod_map_memo
    import time as _t
    built_at, cached = _src_mod_map_memo
    now = _t.monotonic()
    if cached is not None and now - built_at < _SRC_MOD_MAP_TTL:
        return cached
    from meltygui.code.fileref import is_editable_source
    mod_map = {}
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not (f and is_editable_source(f)):
            continue
        try:
            rp = _Path(f).resolve()
        except (OSError, ValueError):
            continue
        existing = mod_map.get(rp)
        if existing is None or (mod.__name__.startswith("src.")
                                and not existing.__name__.startswith("src.")):
            mod_map[rp] = mod
    _src_mod_map_memo = (now, mod_map)
    return mod_map


def _collect_refs(tree) -> list:
    """Reference occurrences in a module AST:
      ("name", name, line, col, scope)              -- a bare Name
      ("attr", (base, attr), line, col, scope)      -- `base.attr` access; `base`
            is the base NAME (str) for a one-level `A.attr`, or the dotted
            value-chain as a tuple of names for a chained `A.B.attr`
            (-> base=("A","B")) so the resolver can getattr-walk it.
    col is 0-indexed; scope is the nearest enclosing def/class.

    Hot: this runs once per file in the cold caller scan and was the single
    biggest primitive there (~half the cold compute), being a pure-Python walk
    over every AST node. Optimized for that: `type() is` dispatch instead of
    isinstance (ast nodes are never subclassed, so it's equivalent), Name tested
    first (the most common reference node), and `out.append` / the ast types /
    iter_child_nodes bound to locals to skip per-node global lookups."""
    out = []
    out_append = out.append
    iter_child = ast.iter_child_nodes
    Name = ast.Name;
    Attribute = ast.Attribute
    FunctionDef = ast.FunctionDef;
    AsyncFunctionDef = ast.AsyncFunctionDef
    ClassDef = ast.ClassDef

    def _attr_chain(node):
        # Names in a dotted Name/Attribute value-chain's root first, or None if it
        # bottoms out in a call/subscript/etc. (`a.b.c` -> ("a","b","c")).
        parts = []
        while node.__class__ is Attribute:
            parts.append(node.attr)
            node = node.value
        if node.__class__ is Name:
            parts.append(node.id)
            parts.reverse()
            return tuple(parts)
        return None

    def walk(node, scope):
        for child in iter_child(node):
            t = child.__class__
            if t is Name:
                out_append(("name", child.id, child.lineno, child.col_offset, scope))
            elif t is Attribute:
                v = child.value
                vt = v.__class__
                if vt is Name:
                    out_append(("attr", (v.id, child.attr),
                                child.lineno, child.col_offset, scope))
                elif vt is Attribute:
                    # Chained access `A.B.attr`: capture the LEAF member on its
                    # full-chain base so nested-class attributes resolve. Without
                    # this, `Toggles.Inner.attr` only ever captured the inner CLASS
                    # (`Inner`-on-`Toggles`, from the walk below) and the leaf
                    # attribute showed no usages.
                    chain = _attr_chain(v)
                    if chain is not None:
                        out_append(("attr", (chain, child.attr),
                                    child.lineno, child.col_offset, scope))
                walk(child, scope)  # also capture the base Name beneath
            elif t is FunctionDef or t is AsyncFunctionDef or t is ClassDef:
                out_append(("name", child.name, child.lineno, child.col_offset, scope))
                walk(child, child.name)
            else:
                walk(child, scope)

    walk(tree, "<module>")
    return out


def _resolve_static_obj(node, look):
    """Resolve a pure Name / attribute-chain AST node to a live object via `look`
    (name -> object) + getattr, or None. The root name goes through `look`;
    getattr walks the rest. Used to find the class a local is bound to (an alias
    RHS or a type annotation) so member access through the local is trackable."""
    cls = node.__class__
    if cls is ast.Name:
        return look(node.id)
    if cls is ast.Attribute:
        parts = []
        n = node
        while n.__class__ is ast.Attribute:
            parts.append(n.attr)
            n = n.value
        if n.__class__ is not ast.Name:
            return None
        obj = look(n.id)
        for p in reversed(parts):
            if obj is None:
                return None
            try:
                obj = getattr(obj, p, None)
            except Exception:
                return None
        return obj
    return None


def _local_class_bindings(tree, look):
    """{(scope, local_name): obj} for locals bound to a resolvable object, so a
    member access THROUGH a local resolves — the index otherwise resolves bases
    only via the module/import namespace, losing every usage reached via a local
    (`ts = Toggles.TerminalSettings; ts.min_width`, or a typed parameter
    `tab_state: TabState` then `tab_state.selected_tabs`). Scope is the SAME
    string _collect_refs assigns refs (nearest enclosing def/class name), so the
    binding and the ref it explains share a key. Two binding sources:
      • parameter / AnnAssign type annotation -> the local is bound to the TYPE
        (its class); member access resolves to that class's members.
      • a simple alias assignment `x = <Name | attr-chain>` -> the bound object.
    Only Name / attribute-chain annotations and RHS resolve; Subscript (List[X]),
    calls, and literals are skipped (conservative). Last binding in a scope wins."""
    bindings = {}
    Name = ast.Name;
    Attribute = ast.Attribute
    FunctionDef = ast.FunctionDef;
    AsyncFunctionDef = ast.AsyncFunctionDef
    ClassDef = ast.ClassDef;
    Assign = ast.Assign;
    AnnAssign = ast.AnnAssign
    iter_child = ast.iter_child_nodes

    def bind(scope, name, node):
        obj = _resolve_static_obj(node, look)
        if obj is not None:
            bindings[(scope, name)] = obj

    def walk(node, scope):
        for child in iter_child(node):
            t = child.__class__
            if t is FunctionDef or t is AsyncFunctionDef:
                a = child.args
                for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                    if arg.annotation is not None:
                        bind(child.name, arg.arg, arg.annotation)
                walk(child, child.name)
            elif t is ClassDef:
                walk(child, child.name)
            elif t is AnnAssign:
                if child.target.__class__ is Name and child.annotation is not None:
                    bind(scope, child.target.id, child.annotation)
            elif t is Assign:
                if (len(child.targets) == 1 and child.targets[0].__class__ is Name
                        and child.value.__class__ in (Name, Attribute)):
                    bind(scope, child.targets[0].id, child.value)
            else:
                walk(child, scope)

    walk(tree, "<module>")
    return bindings


def _imported_name_objects(tree) -> dict:
    """{local_name: live object} for every import statement in `tree` —
    INCLUDING function-local imports (the codebase lazy-imports heavily to
    break cycles, so names like `Mode` often never reach the module dict).
    Resolution is via sys.modules only — nothing is ever imported here."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # relative import - not used in src
            mod = sys.modules.get(node.module or "")
            if mod is None:
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                obj = getattr(mod, a.name, None)
                if obj is not None:
                    out.setdefault(a.asname or a.name, obj)
        elif isinstance(node, ast.Import):
            for a in node.names:
                mod = sys.modules.get(a.name)
                if mod is None:
                    continue
                if a.asname:
                    out.setdefault(a.asname, mod)
                else:
                    top = a.name.split(".", 1)[0]
                    tm = sys.modules.get(top)
                    if tm is not None:
                        out.setdefault(top, tm)
    return out


def _file_index_refs(path, module, text=None, tree=None, imports=None,
                     raw_refs=None) -> list:
    """Resolved references in one src file, cached by mtime:
      ("name", id(obj)|None, line, col, scope)         -- bare name -> object id
      ("attr", (id(base)|None, attr)|None, ...)        -- base.attr -> (base id, attr)
    Resolution is via the file's module namespace, falling back to the file's
    own import statements (incl. function-local lazy imports — see
    _imported_name_objects). Nothing is ever triggered/imported.

    `text` is the current content for THIS file (disk + unsaved edits); when the
    caller scan reaches the edited file it passes it so the file's own internal
    callers reflect the live buffer. The mtime cache is bypassed then — deferred
    saves don't bump mtime, so a cached entry would be stale (it's one file per
    compute, so re-parsing it is cheap).

    Files with QUEUED (unsaved) edits are handled internally: deferred saves
    never touch mtime, so the cache sig folds the file's pending edit
    generation in, and a miss with a nonzero gen scans the pending overlay
    (PendingSave.current_file_text) instead of disk — references then carry
    the same PENDING coordinates the editors display. The gen probe is a dict
    get; the overlay text is built only on a miss (the content-hash ban stays
    respected).

    `tree` / `imports` / `raw_refs` let the caller hand over an already-parsed
    ast, its resolved imports, and its raw `_collect_refs` output for THIS file,
    avoiding a redundant ast.parse + two full-tree walks — _symbol_refs_index
    passes the edited file's tree/imports/refs, which it already built for target
    resolution, so the edited file is parsed and walked once per compute, not
    twice."""
    global _index_refs_reparses
    use_text = text is not None
    if not use_text:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        from meltygui.editor.pending_save import PendingSave
        sig = (mtime, PendingSave.pending_gen_for(path))
        cached = _index_refs_cache.get(path)
        if cached is None or cached[0] != sig:
            # About to re-parse (30-170ms of GIL-bound CPU): defer to any
            # frames the render thread is mid-drawing first. Cache hits skip
            # this - they're dict lookups. Single choke point for each
            # caller (warmer sweep, watch batch, span re-search).
            _park_while_frame()
        if cached is not None and cached[0] == sig:
            return cached[1]
        _index_refs_reparses += 1
        if sig[1]:
            # Queued edits: scan the pending overlay, not stale disk. None
            # (read-only) falls through to the plain disk read below.
            text = PendingSave.current_file_text(path)
    _t_refs0 = _time.monotonic()
    od = getattr(module, "__dict__", None)
    refs = []
    if od is not None:
        try:
            if tree is None:
                tree = ast.parse(text if text is not None else path.read_text())
            if imports is None:
                imports = _imported_name_objects(tree)

            def look(n):
                v = od.get(n)
                return v if v is not None else imports.get(n)

            if raw_refs is None:
                raw_refs = _collect_refs(tree)
            # Locals bound to a class (alias or typed param) so aing
            # through them resolves; falls back to the global/import namespace.
            bindings = _local_class_bindings(tree, look)

            def look_base(name, scope):
                b = bindings.get((scope, name))
                return b if b is not None else look(name)

            for (kind, payload, line, col, scope) in raw_refs:
                if kind == "name":
                    obj = look(payload)
                    refs.append(("name", id(obj) if obj is not None else None, line, col, scope))
                else:
                    b = payload[0]
                    if b.__class__ is tuple:  # dotted base `A.B` -> getattr-walk
                        base = look_base(b[0], scope)
                        try:
                            for part in b[1:]:
                                if base is None:
                                    break
                                base = getattr(base, part, None)
                        except Exception:  # a property on the chain failed
                            base = None
                    else:
                        base = look_base(b, scope)
                    key = (id(base), payload[1]) if base is not None else None
                    refs.append(("attr", key, line, col, scope))
        except Exception:
            refs = []
    if not use_text:
        _index_refs_cache[path] = (sig, refs)
    _dt_refs = (_time.monotonic() - _t_refs0) * 1000.0
    if _dt_refs >= 20.0:  # individual slow file - worth a (rate-limited) trace
        _ptrace_rl(("file-refs", path), f"file refs re-parse {_dt_refs:.0f}ms",
                   min_interval=2.0, file=path.name)
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

    def walk(node, container, class_obj=None):
        cd = getattr(container, "__dict__", None) or {}
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                obj = cd.get(child.name)
                add(child.name, child.lineno, child.col_offset, container, obj)
                walk(child, obj, class_obj=obj)  # entering a class
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                obj = cd.get(child.name)
                # Only module-level defs and methods are targets here. A def
                # nested in a FUNCTION is a local of that function - owned by
                # the local-usage path (_local_var_bindings) - registering it as
                # a (id(func), name) member made a callerless ghost.
                if isinstance(container, (_ModuleType, type)):
                    add(child.name, child.lineno, child.col_offset, container, obj)
                walk(child, obj, class_obj=class_obj)  # method keeps its class
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                tgts = child.targets if isinstance(child, ast.Assign) else [child.target]
                for t in tgts:
                    if isinstance(t, ast.Name):
                        # Only module-level / class-body assignments are real targets
                        # here. A Name assigned inside a function body is a local
                        # variable (container is the function def) and handled by the
                        # local-usage path; adding it as a (id(func), name) member
                        # was a callerless ghost (and occasionally drew a spurious
                        # id-collision caller). Only descend the module/class chain.
                        if isinstance(container, (_ModuleType, type)):
                            add(t.id, child.lineno, t.col_offset, container, cd.get(t.id))
                    elif (class_obj is not None and isinstance(t, ast.Attribute)
                          and isinstance(t.value, ast.Name) and t.value.id == "self"):
                        # `self.x = ...` / `self.x: T` in a method -> a member of
                        # the enclosing CLASS. Instance attrs have no class-body
                        # def, so they'd otherwise never be a usage target (e.g.
                        # TabState.selected_tabs, set once in __init__).
                        add(t.attr, child.lineno, t.col_offset, class_obj, None)
            else:
                walk(child, container, class_obj=class_obj)

    walk(file_tree, module)
    return obj_targets, mem_targets, obj_by_name, sites, def_lines


def _def_stmt_line(lines, start) -> int:
    """The `def`/`class` STATEMENT line of a getsourcelines block. The block
    (and co_firstlineno) starts at the first DECORATOR line for decorated
    defs, while ast/libcst linenos (the usage SITES) point at the statement
    itself — a decorated def's recorded definition then never matched its own
    at-def occurrence, so Ctrl+B on `def button` jumped to `@render_func`,
    and its usages elsewhere landed on the decorator line."""
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith(("def ", "async def ", "class ")):
            return start + i
    return start


def _cached_def_line(target, df) -> int:
    """The def's STATEMENT line (decorators skipped — see _def_stmt_line),
    cached by (defining-file, qualname) and invalidated on the file's mtime.
    getsourcelines on a class ast.parses the whole file every call — this
    skips that when the file is unchanged (the common case in the
    def-resolution loop). `df` is the already-resolved getsourcefile (cheap,
    no parse). Raises like getsourcelines on a miss, so the caller's
    try/except still covers it."""
    qn = getattr(target, "__qualname__", None)
    try:
        mt = _Path(df).stat().st_mtime if df else None
    except OSError:
        mt = None
    key = (df, qn) if qn else (df, id(target))
    ce = _def_line_cache.get(key)
    if ce is not None and ce[0] == mt:
        return ce[1]
    lines, start = inspect.getsourcelines(target)
    dl = _def_stmt_line(lines, start)
    _def_line_cache[key] = (mt, dl)
    return dl


def _import_def_line(text, name):
    """1-based line of the column-0 `import …` / `from … import …` statement
    that binds `name` in `text`, or 0. The definition target for names whose
    object has no usable source line — C-extension modules (imgui), package
    modules (getsourcelines(module) starts at 0) — so a click jumps to the
    local import instead of the top of some file."""
    pat = re.compile(rf'^(?:from\s+[\w.]+\s+)?import\s.*\b{re.escape(name)}\b')
    for i, ln in enumerate(text.splitlines(), 1):
        s = ln.lstrip()   # indented too; `try`-guarded and function-local imports
        if s.startswith(("import", "from")) and pat.match(s):
            return i
    return 0


def _member_def_site(base, attr):
    """Best-effort (file, line) where member `attr` of class/module `base` is
    DEFINED. Functions/classes resolve via inspect; plain class vars and enum
    members (no source info of their own) fall back to scanning the base's
    source for the `attr = ...` / `attr: ...` assignment line."""
    try:
        m = getattr(base, attr)
        # property / cached_property expose the underlying function via fget/func;
        # the descriptor object itself has no source, so unwrap to it first (this
        # is why DrawState.get_clip_rect - a @property - resolved to nothing).
        m = getattr(m, "fget", None) or getattr(m, "func", None) or m
        val = inspect.unwrap(m)
        lines, start = inspect.getsourcelines(val)
        return inspect.getsourcefile(val), _def_stmt_line(lines, start)
    except Exception:
        pass
    try:
        lines, start = inspect.getsourcelines(base)
        pre = f"self.{attr}"  # instance attr assigned in a method body
        for i, ln in enumerate(lines):
            s = ln.lstrip()
            if s.startswith(attr) and len(s) > len(attr) and s[len(attr)] in ' =:(':
                return inspect.getsourcefile(base), start + i
            if s.startswith(pre) and len(s) > len(pre) and s[len(pre)] in ' =:':
                return inspect.getsourcefile(base), start + i
    except Exception:
        pass
    return None, 0


_NO_CONST = object()


def _const_def_site(name, obj, modules):
    """(file, line) of the module-level `name = ...` / `name: ...` assignment that
    DEFINES a plain constant (int/str/tuple/… — no inspect source of its own).
    Scans only modules that actually bind `name` to `obj` (a cheap __dict__
    identity check, so usually 1-3 files), at column-0 lines beginning with `name`
    immediately followed by `=`/`:`. That skips `from x import name` re-exports
    (col-0 line starts with `from`) and indented usages — so it lands on the real
    definer even when the name is imported into the viewed file. (None, 0) if not
    found. Without this, a constant's def fell back to def_lines.get(name), which
    for a name only REFERENCED in the span is its first USAGE line — making the
    definition look like a usage so every usage listed all the other usages."""
    for mod in modules:
        try:
            d = getattr(mod, "__dict__", None)
            if d is None or d.get(name, _NO_CONST) is not obj:
                continue
        except Exception:
            continue
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            text = Melty.read_code(_Path(f).resolve())
        except Exception:
            text = None
        if not text:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln or ln[0].isspace() or not ln.startswith(name):
                continue  # only column-0 (module-level) defs
            rest = ln[len(name):].lstrip()
            if rest[:1] in ('=', ':'):  # assignment or annotation, not `+=`
                return f, i
    return None, 0


def _is_src_object(base, mod_map) -> bool:
    """True when `base` (a module, or a class/object) is defined in one of the
    loaded src files the index covers — keeps reverse attr-targets scoped to
    project symbols rather than every `imgui.x` / stdlib access."""
    _ModuleType = type(sys)
    if isinstance(base, _ModuleType):
        f = getattr(base, '__file__', None)
        return f is not None and _Path(f).resolve() in mod_map
    bm = sys.modules.get(getattr(base, '__module__', '') or '')
    f = getattr(bm, '__file__', None) if bm is not None else None
    return f is not None and _Path(f).resolve() in mod_map


def _file_parse_artifacts(resolved, owning, text):
    """(tree, imports, refs, bindings) for `resolved`, cached across spans.

    The four whole-FILE artifacts _symbol_refs_index needs that depend only on the
    file content + index generation, never on the span: the ast tree (reused by
    _collect_targets per span and by the edited file's own caller scan), the
    resolved import-name objects, the full ref walk, and the local→class bindings.
    Caching them by (mtime, pending_gen, index gen) lets N spans of one file share
    ONE parse+walk (~55ms on a 7k-line file) instead of redoing it per span — the
    dominant startup cost when several editors open spans from the same file.

    Returns None when the text won't parse (caller bails, as before). The shared
    tree is safe: _collect_targets / _collect_refs / _imported_name_objects /
    _local_class_bindings all walk it read-only (no node mutation)."""
    from meltygui.editor.pending_save import PendingSave
    try:
        mtime = resolved.stat().st_mtime
    except OSError:
        mtime = None
    sig = (mtime, PendingSave.pending_gen_for(resolved), _index_generation)
    cached = _file_parse_cache.get(resolved)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        tree = ast.parse(text)
    except Exception:
        return None
    imports = _imported_name_objects(tree)
    refs = _collect_refs(tree)
    od = getattr(owning, "__dict__", None) or {}

    def look(n):
        v = od.get(n)
        return v if v is not None else imports.get(n)

    bindings = _local_class_bindings(tree, look)
    artifacts = (tree, imports, refs, bindings)
    _file_parse_cache[resolved] = (sig, artifacts)
    return artifacts


def _local_var_bindings(file_tree, start_line, end_line):
    """Binding sites of function-LOCAL variables overlapping the span — a
    parameter, or an assignment / annotation / aug-assign / walrus / for / with /
    except / comprehension target inside a function body, not declared
    global/nonlocal. Returns (bounds, declared_global, scope_parent, extent):
      bounds            {(scope, name): (line, col)}  first binding of each local
      declared_global   {(scope, name)}  names declared global/nonlocal
      scope_parent      {nested_def_name: enclosing scope}  for defs nested in a
                        function — lets a ref in a closure resolve to the
                        enclosing function's local (a captured param / var, or
                        a sibling helper) by walking up the chain
      extent            (lo, hi) file lines of the OUTERMOST functions
                        overlapping the span — the range a local's occurrences
                        must be gathered over so a one-line probe (the Ctrl+B
                        recheck on a binding line) still sees the uses further
                        down the function; equals the span when it covers
                        whole functions

    A def nested inside a function is itself a local of the enclosing scope
    (bound at its `def` line): its calls within that function — and from sibling
    closures, via scope_parent — link to it and it links back, exactly like a
    variable. Nested defs used to be registered as callerless "members" of the
    function object and linked in neither direction.

    A binding-ONLY walk: it records assignment-like targets and params, never
    every Name occurrence — the occurrences (sites) come for free from the span
    ref scan _symbol_refs_index already runs. Scope strings match _collect_refs
    (nearest enclosing def/class name, else "<module>"); only names bound inside a
    FUNCTION count. Functions/classes that don't overlap the span are pruned, so
    the walk is O(span), not O(file)."""
    Name = ast.Name
    FunctionDef = ast.FunctionDef;
    AsyncFunctionDef = ast.AsyncFunctionDef
    ClassDef = ast.ClassDef;
    iter_child = ast.iter_child_nodes
    Assign = ast.Assign;
    AnnAssign = ast.AnnAssign;
    AugAssign = ast.AugAssign
    NamedExpr = ast.NamedExpr;
    For = ast.For;
    AsyncFor = ast.AsyncFor
    With = ast.With;
    AsyncWith = ast.AsyncWith;
    ExceptHandler = ast.ExceptHandler
    comprehension = ast.comprehension;
    Tuple = ast.Tuple;
    List = ast.List
    Starred = ast.Starred;
    Global = ast.Global;
    Nonlocal = ast.Nonlocal
    bounds = {}
    declared_global = set()
    scope_parent = {}
    extent = [start_line, end_line]

    def record_bind(scope, name, line, col):
        k = (scope, name)
        cur = bounds.get(k)
        if cur is None or line < cur[0]:
            bounds[k] = (line, col)

    def targets(node):  # Name leaves of an assignment/for/with target
        t = node.__class__
        if t is Name:
            yield node
        elif t is Tuple or t is List:
            for el in node.elts:
                yield from targets(el)
        elif t is Starred:
            yield from targets(node.value)

    def walk(node, scope, in_func):
        for child in iter_child(node):
            t = child.__class__
            if t is FunctionDef or t is AsyncFunctionDef:
                if in_func:
                    # A nested def is a local of the enclosing function. Bind
                    # it BEFORE the span prune: a call to it elsewhere in that
                    # function (a one-line probe far below its def) must still
                    # resolve to this binding; only the walk INTO it is pruned.
                    record_bind(scope, child.name, child.lineno, child.col_offset)
                    scope_parent[child.name] = scope
                cend = getattr(child, "end_lineno", child.lineno) or child.lineno
                if child.lineno > end_line or cend < start_line:
                    continue  # span-pruned: O(span), not O(file)
                if not in_func:
                    if child.lineno < extent[0]:
                        extent[0] = child.lineno
                    if cend > extent[1]:
                        extent[1] = cend
                a = child.args
                params = (*a.posonlyargs, *a.args, *a.kwonlyargs)
                if a.vararg: params += (a.vararg,)
                if a.kwarg: params += (a.kwarg,)
                for arg in params:
                    record_bind(child.name, arg.arg, arg.lineno, arg.col_offset)
                walk(child, child.name, True)
            elif t is ClassDef:
                cend = getattr(child, "end_lineno", child.lineno) or child.lineno
                if child.lineno > end_line or cend < start_line:
                    continue
                walk(child, child.name, False)  # class body: not a function body
            elif t is Global or t is Nonlocal:
                for nm in child.names:
                    declared_global.add((scope, nm))
            elif in_func:
                if t is Assign:
                    for tgt in child.targets:
                        for n in targets(tgt):
                            record_bind(scope, n.id, n.lineno, n.col_offset)
                    walk(child, scope, True)
                elif t is AnnAssign or t is AugAssign or t is NamedExpr:
                    if child.target.__class__ is Name:
                        record_bind(scope, child.target.id,
                                    child.target.lineno, child.target.col_offset)
                    walk(child, scope, True)
                elif t is For or t is AsyncFor:
                    for n in targets(child.target):
                        record_bind(scope, n.id, n.lineno, n.col_offset)
                    walk(child, scope, True)
                elif t is With or t is AsyncWith:
                    for it in child.items:
                        if it.optional_vars is not None:
                            for n in targets(it.optional_vars):
                                record_bind(scope, n.id, n.lineno, n.col_offset)
                    walk(child, scope, True)
                elif t is ExceptHandler:
                    if child.name:
                        record_bind(scope, child.name, child.lineno, child.col_offset)
                    walk(child, scope, True)
                elif t is comprehension:
                    for n in targets(child.target):
                        record_bind(scope, n.id, n.lineno, n.col_offset)
                    walk(child, scope, True)
                else:
                    walk(child, scope, True)
            else:
                walk(child, scope, in_func)

    walk(file_tree, "<module>", False)
    return bounds, declared_global, scope_parent, tuple(extent)


def _build_local_entries(bounds, declared_global, local_sites,
                         start_line, end_line, rp_str, mod_name, extent=None):
    """Local-variable usage entries from the binding sites (_local_var_bindings)
    plus the occurrences the span ref scan gathered (local_sites: {(scope,name):
    [(line,col),...]}). Definition = the binding, sites = every in-span
    occurrence, callers = those OTHER than the binding — so a local washes and
    double-clicks to its uses like a cross-file symbol. A decl-only / unused local
    (no occurrence beyond its binding) is skipped so it doesn't wash. Keyed
    scope+name+def-line (NUL-joined, never a bare identifier) so two scopes' `i`,
    and a local shadowing a module global, get distinct entries; the display
    spelling rides in `name` (see _rebuild_symbol_usages).

    `extent` (lo, hi) — the enclosing functions' full line range from
    _local_var_bindings — is where CALLERS are gathered when given (the jump
    targets from the binding), while `sites` (the washed / clickable
    occurrences) stay inside the SPAN [start_line, end_line] like every other
    symbol's, so a span view never carries out-of-buffer sites. An entry is
    only emitted when it has an in-span site: a one-line probe builds entries
    just for the locals on that line, each carrying every use in its function
    — which is what lets Ctrl+B on the binding line list them."""
    raw = {}
    lo, hi = extent if extent is not None else (start_line, end_line)
    for (scope, name), defpos in bounds.items():
        if (scope, name) in declared_global:
            continue
        site_set = {defpos}
        site_set.update(local_sites.get((scope, name), ()))
        in_extent = sorted(p for p in site_set if lo <= p[0] <= hi)
        callers = [p for p in in_extent if p != defpos]
        if not callers:
            continue
        in_span = [p for p in in_extent if start_line <= p[0] <= end_line]
        if not in_span:
            continue
        dl, dc = defpos
        key = "%s\x1f%s\x1f%d" % (scope, name, dl)
        raw[key] = {
            "name": name,
            "sites": in_span,
            "definition": (rp_str, dl, dc, mod_name),
            "callers": [(rp_str, l, c, scope, mod_name) for (l, c) in callers],
        }
    return raw


def _symbol_refs_index(file_path: str, start_line: int, end_line: int, text=None,
                       prev=None) -> dict:
    """jedi-free fast path. Resolve the span's symbols (module-level + class
    members) against live objects, then find callers across loaded src files —
    bare-name refs for module-level, ClassName.member refs for members. Returns
    the same raw shape as _symbol_refs_worker.

    `text` is the current file content (disk + unsaved edits); when omitted it
    falls back to the on-disk read, but callers should pass it so the span's sites
    reflect the live buffer rather than the stale file.

    `prev` is the raw result of a PRIOR compute of this span whose expensive half
    is still (or acceptably) valid. At an unchanged index generation it is exact:
    no other file's content and no live object moved, only the local buffer did.
    With stale_gen_incremental the caller (_compute_symbol_usages) also passes a
    STALE-generation prev (cross-session restore / gen bump from another file's
    edit) — cross-file callers reused from it can then lag, and the caller marks
    the result to heal with one full pass at the next generation bump. Either
    way, a symbol's callers in OTHER files are reused, as is a DEFINITION living
    in another file (resolved against a live object via inspect — the ~65% cost). A definition in the EDITED file is NOT
    reusable: it moves with the buffer as lines shift, and a stale line breaks the
    editor's at_def direction (the declaration stops matching, jumps to a stale
    copy of itself, and hides its callers) — so in-file defs re-resolve fresh
    (cheap: def_lines for span-defined names, mtime-cached inspect otherwise).
    With `prev` we rescan just the edited file (always) + run the cross-file
    scan / inspect ONLY for names not already in `prev` (freshly typed symbols),
    reusing the rest. Cold path (prev=None): every name is "fresh", so the scan
    is full and behaviour is identical to before."""
    _t_idx0 = _time.monotonic()
    resolved = _Path(file_path).resolve()
    mod_map = _src_mod_map()
    _t_modmap = _time.monotonic()
    owning = mod_map.get(resolved)
    if owning is None:
        _ptrace("refs-index: file not in loaded module map", file=resolved.name)
        return {}
    if text is None:
        text = Melty.read_code(resolved)
    if text is None:
        return {}
    # Whole-file parse + the three full-tree walks, shared across every span of
    # this file (see _file_parse_artifacts). _collect_targets stays per-span - it
    # filters to [start, end] - but reuses the file tree instead of re-parsing.
    _t_art0 = _time.monotonic()
    artifacts = _file_parse_artifacts(resolved, owning, text)
    _t_art = _time.monotonic() - _t_art0
    if artifacts is None:
        return _PARSE_FAILED  # buffer doesn't parse - caller holds no-good
    file_tree, _file_imports, file_refs, _bindings = artifacts
    obj_targets, mem_targets, obj_by_name, sites, def_lines = _collect_targets(
        file_tree, owning, start_line, end_line)
    # Function-local variables: only the BINDING SITES matter (fast, binding-only
    # walk); each local's occurrences are gathered for free from the span ref scan
    # below into local_sites - no separate every-Name walk, no cross-file scan, and
    # they attach in the SAME flat dict as the module/member symbols so they stay
    # in sync. `local_keys` also lets the bare-name scan skip resolving a local
    # against the module namespace (a local shadows a same-named global). Gated by
    # Toggles.TextEditor.SymbolUsages.local_symbol_usages.
    from meltygui.core.runtime.toggles import Toggles
    if Toggles.TextEditor.SymbolUsages.local_symbol_usages:
        local_bounds, local_global, local_parent, (local_lo, local_hi) = (
            _local_var_bindings(file_tree, start_line, end_line))
        local_keys = local_bounds.keys() - local_global
    else:
        local_bounds, local_global, local_keys = {}, set(), set()
        local_parent, local_lo, local_hi = {}, start_line, end_line
    local_sites = {}  # (scope, name) -> [(line, col), ...]

    def _local_key(scope, name):
        # The local a bare name in `scope` refers to: its own binding first,
        # else the nearest enclosing function's (closure capture / sibling
        # reference) - scope_parent == None, not a local -> module resolution.
        seen_scopes = 0
        while scope is not None and seen_scopes < 16:
            k = (scope, name)
            if k in local_keys:
                return k
            scope = local_parent.get(scope)
            seen_scopes += 1
        return None
    # Also target objects REFERENCED (not defined) in the span, so usage sites
    # link back too (the REVERSE direction):
    #  - bare names - decorators (@window/@defaults), used enums (ProfileMode)
    #  - attr accesses on src objects - `Mode.WINDOW`, `Toggles.scroll_speed` -
    #    targeted as (id(base), attr), the same key the reverse scan matches, and
    #    named "Base.attr" so the editor washes/clicks the full dotted access.
    #    Their definition resolves into the BASE's source (see _member_def_site).
    od = getattr(owning, "__dict__", None) or {}

    def _lookup(n):
        v = od.get(n)
        return v if v is not None else _file_imports.get(n)

    member_bases = {}  # dotted name -> base object
    # Members DEFINED in the span (from _collect_targets); a reference to the
    # inside the span keeps the definition's site, not a re-registration. Snapshot
    # now so the loop can still record EVERY occurrence of a REFERENCED member.
    defined_member_keys = frozenset(mem_targets)

    # _file_imports / file_refs / _bindings come from the shared parse above
    # (locals bound to a class -> member access on them resolves; falls back
    # to the module/import namespace).
    def _lookup_base(name, scope):
        b = _bindings.get((scope, name))
        return b if b is not None else _lookup(name)

    for (kind, payload, line, col, scope) in file_refs:
        in_span = start_line <= line <= end_line
        if not in_span and not (local_keys and local_lo <= line <= local_hi):
            continue
        if kind == "name":
            _lk = _local_key(scope, payload) if local_keys else None
            if _lk is not None:
                # Local var: record every occurrence (the span ref scan IS the
                # occurrence source - no separate every-Name walk) and skip the
                # global resolution (a local shadows a same-named global).
                # Gathered over the enclosing function's extent, not just the
                # span, so a binding above top still lists the uses below.
                local_sites.setdefault(_lk, []).append((line, col))
                continue
            if not in_span:
                continue
            obj = _lookup(payload)
            if obj is not None:
                if id(obj) not in obj_targets:
                    obj_targets[id(obj)] = payload
                # Register per NAME, outside the per-OBJ guard: two names
                # bound to the same object (`import imgui` with an `_ig` alias,
                # a re-imported class) shares one obj_targets entry, but each
                # spelling still needs its own obj_by_name/def_lines entry -
                # otherwise the second name skipped registration, fell into
                # _resolve_def's "member defined in this file" branch, and
                # its definition came out as (this file, line 0) → every jump
                # on it scrolled the file to the very top.
                obj_by_name.setdefault(payload, obj)
                def_lines.setdefault(payload, line)
                # EVERY occurrence is a site: registration above runs once, but
                # each reference must link. Appending was inside that guard, so
                # only the FIRST of N references (e.g. draw_window called 9× in
                # draw_main) got a site - the rest had no reference recorded.
                sites.setdefault(payload, []).append((line, col))
        elif kind == "attr":
            if not in_span:
                continue
            base_repr, attr = payload
            if base_repr.__class__ is tuple:  # dotted base `A.B` -> re-walk
                base = _lookup_base(base_repr[0], scope)
                try:
                    for part in base_repr[1:]:
                        if base is None:
                            break
                        base = getattr(base, part, None)
                except Exception:
                    base = None
                base_str = ".".join(base_repr)
            else:
                base = _lookup_base(base_repr, scope)
                base_str = base_repr
            if base is None or not _is_src_object(base, mod_map):
                continue
            key = (id(base), attr)
            if key in defined_member_keys:  # span-defined member: sites come from the def
                continue
            nm = mem_targets.get(key)  # canonical name; None until first reference
            if nm is None:
                nm = f"{base_str}.{attr}"
                mem_targets[key] = nm
                member_bases.setdefault(nm, base)
            # Every occurrence is a site (was skipped for repeats with `key in
            # mem_targets`, so a member referenced N× - e.g. imgui.text - only
            # linked once).
            sites.setdefault(nm, []).append((line, col))
    if not obj_targets and not mem_targets and not local_bounds:
        _ptrace("refs-index: no targets in span", file=resolved.name,
                span=f"{start_line}-{end_line}")
        return {}

    rp_str = str(resolved)
    mod_name = getattr(owning, "__name__", "") or ""

    # Incremental reuse (see docstring): names already in `prev` keep their
    # definition + out-of-file callers; only the EDITED file is rescanned for
    # them. Names NOT in `prev` are "fresh" and get the full cross-file scan +
    # inspect. Cold path: prev is None → every name is fresh → full scan.
    reuse = prev or {}
    fresh = set(sites) - reuse.keys()
    skip_other_files = prev is not None and not fresh  # nothing left to look up

    _t_scan0 = _time.monotonic()
    _reparse0 = _index_refs_reparses
    _scan_slept = 0.0
    _files_scanned = 0
    callers = {}
    for fi, (path, mod) in enumerate(mod_map.items()):
        is_edited = path == resolved
        if skip_other_files and not is_edited:
            continue
        _files_scanned += 1
        if fi % 8 == 0:
            # GIL yield: this scan is the bulk of the span compute (~150 files ×
            # cached ref lists, plus ~5-10ms ast re-parse per stale file) and
            # runs on a plain thread - without the sleeps it holds the GIL in
            # one ~0.2s block and the render thread stutters. ~19 sleeps ≈
            # +20ms wall per compute. While the render thread is mid-frame,
            # a fixed 1ms isn't enough - each capture pass needs ~600
            # concurrent GIL acquisitions - so park until the frame ends
            # (_park_while_frame; no-op between frames).
            _s0 = _time.monotonic()
            if not _park_while_frame():
                _time.sleep(0.001)
            # Measured, not assumed: under GIL contention a 1ms sleep can take
            # far longer - the yield IS the contention signal.
            _scan_slept += _time.monotonic() - _s0
        # The edited file's own internal callers must come from the overlaid text
        # (deferred saves keep disk stale); others read disk via the mtime cache.
        cur = text if is_edited else None
        # On an incremental pass the other files only need scanning for fresh
        # names (reused names' out-of-file callers come from `prev`); the edited
        # file is always scanned in full (its own callers move as the user types).
        fresh_only = prev is not None and not is_edited
        # Reuse the edited file's already-parsed tree + imports + raw refs (built
        # above for target resolution) so it isn't parsed/walked again here.
        scan = (_file_index_refs(path, mod, cur, tree=file_tree,
                                 imports=_file_imports, raw_refs=file_refs)
                if is_edited else _file_index_refs(path, mod, cur))
        for (kind, key, line, col, scope) in scan:
            if key is None:
                continue
            nm = obj_targets.get(key) if kind == "name" else mem_targets.get(key)
            if nm is None or (fresh_only and nm not in fresh):
                continue
            callers.setdefault(nm, []).append(
                (str(path), line, col, scope, getattr(mod, "__name__", "") or ""))
    _t_scan = _time.monotonic() - _t_scan0

    out = {}

    def _resolve_def(nm):
        # Fresh (file, line, col, module) for `nm`, resolved against the live
        # buffer and obj definitions. Shared by the cold loop and the reuse loop
        # (which must re-resolve any definition living in the EDITED file).
        obj = obj_by_name.get(nm)
        base = member_bases.get(nm)
        if obj is not None:  # module-level: real def from inspect
            try:
                # Unwrap decorator chains (render_func etc.) for inspect:
                # getsourcelines follows __wrapped__ internally but
                # getsourcefile does not, so an un-unwrapped wrapper yields a
                # mismatched pair - the wrapper's FILE (core_render.py) with
                # the wrapped function's LINE. Unwrapping once keeps them
                # consistent. ValueError = unwrap's cycle guard.
                target = inspect.unwrap(obj)
                df = inspect.getsourcefile(target)
                dl = _cached_def_line(target, df)  # cached; skips per-class re-parse
                dm = getattr(target, "__module__", "") or mod_name
                if not dl and text:
                    # A MODULE object's source block starts at line 0 (`gl`,
                    # `cst`) - jumping there scrolled to the top of the
                    # __init__.py. The local import statement is the real
                    # definition site.
                    _il = _import_def_line(text, nm)
                    if _il:
                        df, dl = rp_str, _il
            except Exception:
                # No inspect source: either a plain CONSTANT (int/str/tuple) or a
                # wrapper object whose __getattr__/__class__ raised (a
                # _LazyConstant probed for __wrapped__ raised KeyError here once; an
                # uncaught error would silently zero the WHOLE span - see modes.py).
                # For a constant, resolve its real module-level assignment line so a
                # usage links to the DEFINITION and the definition lists the usages.
                # The previous def_lines.get(nm) fallback held the first USAGE line
                # for a referenced-only name, so def_here misfired and every usage
                # listed all the other usages. Only fall back to it on a true miss.
                df, dl = _const_def_site(nm, obj, (owning, *mod_map.values()))
                dm = mod_name
                if df is None and text:
                    # Sourceless module (C extension - imgui, glfw): no inspect
                    # source and no `nm = obj` assignment anywhere. The local
                    # import line is the definition.
                    _il = _import_def_line(text, nm)
                    if _il:
                        df, dl = rp_str, _il
                if df is None:
                    df, dl = rp_str, def_lines.get(nm, 0)
        elif base is not None:  # reverse ref: member of an
            df, dl = _member_def_site(base, nm.rsplit('.', 1)[-1])  # external base (leaf attr)
            dm = (getattr(base, '__module__', None)
                  or getattr(base, '__name__', '') or '')
            if df is None:
                # Unresolvable member def (dynamically-set attr, instance attr
                # from without class, ...): point at the BASE's own def: its file
                # AND its class-header line, not rp_str/0. Line 0 round-
                # tripped through the jump as "line 1" and scrolled the base's
                # file to the very top (`GlobalStyle.some_val` referenced in
                # the defining file did this on every click).
                try:
                    df = inspect.getsourcefile(base)
                    dl = _cached_def_line(base, df)
                except Exception:
                    df, dl = None, 0
        else:  # class member: defined in this file
            df, dl, dm = rp_str, def_lines.get(nm, 0), mod_name
        # Buffer truth beats the live object for defs in THIS file: the
        # inspect paths above read co_firstlineno, which is the line at the
        # object's LAST (re)compile - lines inserted above a function that
        # itself was never code-hotswapped leave it stale. A stale def line
        # breaks the editor's at_def match (the definition site stops being
        # recognized, so it hides its callers and re-enters itself as a
        # <module> self-caller) and lands Ctrl+B definition jumps near the
        # OLD line (draw_str's def served 4557 while the buffer had 4590).
        # Snap to the current ast's def statement for the name (or
        # same-named def when shadowed); non-def symbols (constants) have no
        # entry and pass through untouched - their paths already read the
        # current text.
        if df is not None and df in own_paths:
            _cand = local_def_lines.get(nm.rsplit('.', 1)[-1])
            if _cand and dl not in _cand:
                dl = (_cand[0] if len(_cand) == 1
                      else min(_cand, key=lambda l: abs(l - (dl or 0))))
        return (df, dl, 0, dm)

    # "This file" as a prior definition may have spelled it - rp_str is what the
    # member/const paths record; the module's own __file__ covers class-derived
    # paths in case the spellings ever diverge.
    own_paths = {rp_str}
    _own_file = getattr(owning, "__file__", None)
    if _own_file:
        own_paths.add(_own_file)

    # def/class STATEMENT lines per name from the CURRENT buffer's ast, for
    # _resolve_def's in-file verification above. ast linenos exclude the
    # decorator block, so they match the sites' coords (and _def_site_line's
    # convention). One walk of the already-parsed tree per compute.
    local_def_lines = {}
    for _n in ast.walk(file_tree):
        if _n.__class__ in (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef):
            local_def_lines.setdefault(_n.name, []).append(_n.lineno)

    _t_def0 = _time.monotonic()
    _def_slept = 0.0
    for si, nm in enumerate(sites):  # every seen name has sites
        pe = reuse.get(nm)
        if pe is not None:
            # Reuse the expensive parts: callers in OTHER files (everything not in
            # the edited file), the edited file's callers (rescanned above)
            # and the in-span sites from the live buffer. The DEFINITION is only
            # reused when it lives in ANOTHER file - an in-span definition moves
            # with the buffer as lines are inserted/removed in it, so a reused
            # line drifts off the real def; at_def then stops matching the
            # definition site, which made a symbol's declaration jump to a stale
            # copy of itself instead of listing its references (draw_legacy showed
            # no usages). Re-resolving in-span defs is cheap: span-defined
            # members come straight from def_lines, module-level names from the
            # mtime-keyed _cached_def_line cache.
            d = pe["definition"]
            if d is not None and d[0] in own_paths:
                d = _resolve_def(nm)
            out[nm] = {
                "sites": sites[nm],
                "definition": d,
                "callers": [c for c in pe["callers"] if c[0] != rp_str]
                           + callers.get(nm, []),
            }
            continue
        if si and si % 32 == 0:
            _s0 = _time.monotonic()
            _time.sleep(0.001)  # GIL yield - inspect.getsourcelines per def adds up
            _def_slept += _time.monotonic() - _s0
        out[nm] = {
            "sites": sites[nm],
            "definition": _resolve_def(nm),
            "callers": callers.get(nm, []),
        }
    # Local-variable usages: built from the binding sites + the occurrences the ref
    # scan gathered into local_sites. Collision-proof keys (scope+name+line) never
    # clash with the bare-name or "Base.attr" keys; the display spelling rides in
    # each entry's "name". Locals have no callers outside their scope -> no scan.
    _t_def = _time.monotonic() - _t_def0
    if local_bounds:
        out.update(_build_local_entries(local_bounds, local_global, local_sites,
                                        start_line, end_line, rp_str, mod_name,
                                        extent=(local_lo, local_hi)))
    # Performance summary per compute: where the time went. `slept` inside scan/defs is
    # the cooperative GIL-yield share - big slept vs small work = contention,
    # higher index cost. reparsed = files whose mtime cache missed this run.
    _ptrace(f"refs-index done in {(_time.monotonic() - _t_idx0) * 1000:.0f}ms",
            file=resolved.name, span=f"{start_line}-{end_line}",
            mode="incremental" if prev is not None else "cold",
            modmap=f"{(_t_modmap - _t_idx0) * 1000:.0f}ms",
            artifacts=f"{_t_art * 1000:.0f}ms",
            scan=f"{_t_scan * 1000:.0f}ms/{_files_scanned}files"
                 f"/{_index_refs_reparses - _reparse0}reparsed"
                 f"/slept{_scan_slept * 1000:.0f}ms",
            defs=f"{_t_def * 1000:.0f}ms/slept{_def_slept * 1000:.0f}ms",
            names=len(out), fresh=len(fresh))
    return out


def usage_data_for_line(file_path: str, line: int) -> dict:
    """SYNCHRONOUS fresh usage lookup for one file line — the Ctrl+B
    "no users? double-check" probe. The background index can hold a symbol in
    a stale no-callers state; before the editor flashes the red
    nothing-to-jump-to emphasis it calls this to recompute usage data for
    just the caret's line via the jedi-free refs index (full cross-file
    caller walk over the loaded-module map, same pipeline as the background
    pass). Runs on the CALLER'S thread — deliberately, including the render
    thread, so the real cost is measurable at the call site. Returns a fresh
    {symbol: SymbolUsage}; {} when the file isn't a loaded src module, the
    buffer doesn't parse, or the line holds no resolvable symbols. The result
    is NOT written into the span cache or the tree — it's a verification
    probe for the jump decision, not a heal of the stale graph (that fix is
    separate)."""
    try:
        resolved = _Path(file_path).resolve()
    except (OSError, ValueError):
        return {}
    # PENDING text, not disk: the editor displays the in-memory pending file
    # (deferred saves), so the recheck must resolve against the same text -
    # the same context _compute_symbol_usages uses. None (read error) lets
    # _symbol_refs_index fall back to its own read. Other files in the
    # cross-file caller scan are disk-aware inside _file_symbol_refs.
    from meltygui.editor.pending_save import PendingSave
    raw = _symbol_refs_index(str(resolved), line, line,
                             text=PendingSave.current_file_text(resolved))
    if raw is _PARSE_FAILED or not raw:
        return {}
    return _rebuild_symbol_usages(raw)


def _distribute_by_name(gp, flat: dict, _matched=None) -> None:
    """Attach each symbol's usage to the GeneralParse node that DIRECTLY contains
    it (its immediate parent), recursing into nested GeneralParse children. A
    symbol never lands on a grandparent — each node owns only its own keys.
    Symbols matching NO node key — the reverse references ("Mode.WINDOW" used in
    this span but defined elsewhere) — attach to the TOP node so the editor's
    site walk still finds them."""
    top = _matched is None
    if top:
        _matched = set()
    own = {}
    for k, v in list(gp.items()):
        if k == "__cst__":
            continue
        if isinstance(v, GeneralParse):
            _distribute_by_name(v, flat, _matched)
        if isinstance(k, str) and k in flat:
            own[k] = flat[k]
            _matched.add(k)
    if top:
        for k, v in flat.items():
            if k not in _matched:
                own.setdefault(k, v)
    if own:
        gp["__symbol_usages__"] = own


# Returned by the fast_only probe when a refresh would need the full/incremental
# recompute (i.e. it is NOT a cheap position-only offset or an exact cache hit).
# The caller (the editor's auto-index) uses it to decide whether to refresh inline
# on the render thread or defer to the cooperative-yielded path.
_NEEDS_RECOMPUTE = object()


class InterimUsages:
    """_NEEDS_RECOMPUTE with a consolation prize: the fast_only probe returns
    this instead of the bare sentinel when a real recompute is owed BUT a prior
    (possibly stale/invalid) result exists. `flat` is that prior
    {sym: SymbolUsage} — a first-paint stopgap so a freshly opened editor shows
    last-known usages instead of nothing while the deferred recompute runs. It
    is safe to display as-is: the consumer (_collect_usage_spans/_site_span)
    verify-recovers each site against the live buffer (±4 lines, name-anchored)
    and DROPS mismatches, so drifted sites degrade to absent, never wrong.
    Schedulers must treat this exactly like _NEEDS_RECOMPUTE — the recompute is
    still pending; only the attach path may read `flat` (and attach it as
    NON-fresh, so the retry nudge keeps firing)."""
    __slots__ = ("flat",)

    def __init__(self, flat):
        self.flat = flat

# Returned by _symbol_refs_index when the CURRENT buffer doesn't parse (an
# in-progress edit with a syntax error). Distinct from an empty {} result (a the
# file genuinely has no symbols): on a parse failure _compute_symbol_usages HOLDS
# the last-good graph rather than discarding it - the references stay washed as
# the user fixes the code, and the next valid parse recomputes incrementally from
# the held result instead of cold. (A real empty span just caches {} normally.)
_PARSE_FAILED = object()


@lag_traced("symbol usages (in-proc jedi)", 50)
def compute_symbol_usages_for_address(address, fast_only=False):
    """Build {symbol: SymbolUsage} (callers + definition) for an address's source
    span, via in-process jedi. The entry point for the editor's manual trigger;
    run it on a background thread. Cached per file mtime, so a re-trigger on an
    unchanged file is free. Works for a module, class, or function span.

    fast_only=True returns the result ONLY when it is cheap (an exact cache hit,
    a debounce-served held base, or a blank-line position offset — the offset
    materializes at most once per _SHIFT_MAT_MIN_S) and `_NEEDS_RECOMPUTE`
    otherwise — letting the caller run the cheap case inline (UI stays current)
    and defer the expensive recompute behind the cooperative yield."""
    if DISABLE_JEDI or address is None or getattr(address, "path", None) is None:
        return _NEEDS_RECOMPUTE if fast_only else {}
    from meltygui.editor.pending_save import PendingSave
    resolved = _Path(address.path).resolve()
    start = (getattr(address, "start", 0) or 0) + 1  # address.start is a 0-indexed slice bound
    end = getattr(address, "end", None)
    pending_gen = PendingSave.pending_gen_for(address.path)
    if end is None:  # whole-file span
        text = PendingSave.current_file_text(resolved)
        if text is None:
            return _NEEDS_RECOMPUTE if fast_only else {}
        end = text.count("\n") + 1
    # The "Compute usage" message fires inside _compute_symbol_usages, on the
    # recompute path only - the cheap fast paths (exact cache hit, content-hash
    # rescue, position offset) stay silent.
    return _compute_symbol_usages(resolved, start, end, pending_gen, fast_only=fast_only)


def usages_fresh_for_address(address) -> bool:
    """True when the span cache holds a result computed against the CURRENT
    signature (mtime, pending gen, resolver, index gen) — i.e. the last
    compute really landed for the live buffer. False when it served a hold
    (typing quiet-gate / inflight dedup / parse failure), which never caches.
    Attach paths consult this before stamping _symbol_gen: stamping a held
    result marked stale symbols as current-generation, so the ensure pass
    never retried and a freshly-typed symbol (e.g. a newly imported class)
    stayed unindexed — and untinted — until an unrelated index-gen bump."""
    if DISABLE_JEDI or address is None or getattr(address, "path", None) is None:
        return True   # nothing will ever recompute - don't keep the nudge locked
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.editor.pending_save import PendingSave
    resolved = _Path(address.path).resolve()
    start = (getattr(address, "start", 0) or 0) + 1
    end = getattr(address, "end", None)
    if end is None:
        text = PendingSave.current_file_text(resolved)
        if text is None:
            return True
        end = text.count("\n") + 1
    accurate = Toggles.jedi_correctness
    try:
        mtime = resolved.stat().st_mtime
    except OSError:
        mtime = None
    gen = _index_generation if not accurate else None
    cached = _symbol_usage_cache.get((resolved, start, end))
    return cached is not None and cached[0] == (
        mtime, PendingSave.pending_gen_for(address.path), accurate, gen)


def _compute_symbol_usages(resolved, start, end, pending_gen=0, fast_only=False) -> dict:
    """Cache + A/B branch core: {symbol: SymbolUsage} for a [start, end] span.
    Toggles.jedi_correctness picks the resolver — jedi (accurate, slow:
    re-exports / dotted access / locals) vs the import index (fast, direct
    imports of module-level symbols).

    Cache key is (disk mtime, pending-edit generation, resolver, index gen) —
    NEVER a hash/compare of file content (an O(file) digest on a hot path; see
    CLAUDE.md). `mtime` catches external/disk writes; `pending_gen`
    (PendingSave.pending_gen_for) catches deferred edits that never touch disk;
    `gen` folds in the index warmer's generation (a caller added in ANOTHER file);
    `accurate` re-computes when the resolver toggle flips. The CURRENT file
    content (disk + unsaved edits) is built only on a MISS — to recompute, never
    to detect the miss.

    A MISS that moved only `mtime`/`pending_gen` (same resolver, same `gen`) is a
    pure live-edit of THIS file: every other file's content and every live object
    are unchanged, so the prior result's cross-file callers + symbol definitions
    (~80% of the cost) still hold. We hand that prior result to the index path as
    `prev` so it rescans only the edited file + newly-typed names. The prior result
    is the exact-span entry when present, else the same file's best-overlapping
    span (an edit that adds/removes lines shifts the (start,end) key, but defs +
    callers are keyed by symbol NAME and valid across spans at one generation — so
    a line-break edit reuses the expensive half instead of cold-recomputing it).
    Gated by Toggles.TextEditor.SymbolUsages.incremental_symbol_index for A/B
    against the full recompute.

    stale_gen_incremental extends the reuse to STALE-generation entries (a
    cross-session pickle restore, or a gen bump because another file changed):
    instead of throwing the entry out and recomputing cold, it seeds the same
    incremental pass and the result is marked in _stale_seeded_spans — its
    reused cross-file callers may lag, so the next miss where the generation
    moved again skips the rescue/reuse and runs one full healing pass.

    fast_only=True returns ONLY the cheap outcomes — an exact cache hit or a
    blank-line position offset — and `_NEEDS_RECOMPUTE` the moment a real
    recompute would be needed, doing none of it. The caller runs this inline on
    the render thread (UI stays current) and falls back to the deferred path on
    the sentinel. A within-line edit is rejected by a cheap line-count check
    before the O(file) offset map even runs. The offset materialization itself
    is debounced to _SHIFT_MAT_MIN_S: mid-burst probes serve the unshifted base
    uncached (the editor splice-remaps its display independently) and the
    composite shift lands at most one window later."""
    from meltygui.core.runtime.toggles import Toggles  # lazy: avoid import cycle
    from meltygui.editor.pending_save import PendingSave
    _t_probe = _time.monotonic()
    accurate = Toggles.jedi_correctness
    try:
        mtime = resolved.stat().st_mtime
    except OSError:
        mtime = None
    key = (resolved, start, end)
    gen = _index_generation if not accurate else None
    sig = (mtime, pending_gen, accurate, gen)
    cached = _symbol_usage_cache.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    text = PendingSave.current_file_text(resolved)  # disk + pending overlay (MISS only)
    if text is None:
        return _NEEDS_RECOMPUTE if fast_only else {}
    # LONGEVITY RESCUE: the sig missed (the GLOBAL index gen bumped because SOME
    # other file changed, an mtime touch, or a cross-session restore) but THIS
    # file's content is bit-identical to when the result was computed - sites +
    # definitions are valid and cross-file callers are (acceptably) reused. Serve
    # the cached result and re-stamp the sig instead of recomputing. Only the
    # exact-key entry with a matching resolver qualifies (a span-shift / content
    # edit changes the hash anyway). [content edit not allowed for this cache.]
    # Attempted only when THIS file's pending_gen is unchanged: rescue exists
    # for gen-bump/mtime/restore Misses, and a moved pending_gen means this very
    # file was edited - content is all but guaranteed different, so the O(file)
    # digest is pure per-keystroke render-thread overhead. `chash` stays None on
    # the edit path and is computed lazily by the store sites that need it.
    # HEAL: this span was seeded from a STALE-generation prev (cross-session
    # restore / gen-bump reuse - see the prev block below), so its cross-file
    # callers may lag. The next miss where the generation MOVED again is the
    # heal point: skip the hash rescue and the prev reuse and run one full
    # pass. A same-gen miss (a pure edit of this file) keeps the fast
    # incremental and stays marked - chaining from a marked entry inherits
    # the mark, so the debt is never laundered, only repaid entirely.
    heal = (key in _stale_seeded_spans
            and cached is not None and cached[0][3] != gen)
    chash = None
    if not heal and cached is not None and cached[0][1] == pending_gen:
        chash = _content_hash(text)
        if cached[0][2] == accurate and _span_hashes.get(key) == chash:
            _store_usages(key, sig, cached[1], text, chash=chash)
            _ptrace(f"usage hash-rescue in {(_time.monotonic() - _t_probe) * 1000:.1f}ms "
                    f"(sig lapsed, content identical)", file=resolved.name, span=f"{start}-{end}")
            return cached[1]

    # DEBUG timeline: WHY the cache missed - which sig component moved.
    if cached is None:
        _why = "cold(no-entry)"
    else:
        _os = cached[0]
        _why = "changed:" + ",".join(
            n for n, o, nw in (("mtime", _os[0], mtime),
                               ("pending_gen", _os[1], pending_gen),
                               ("resolver", _os[2], accurate),
                               ("index_gen", _os[3], gen)) if o != nw)
    # Find a reusable prior result (same resolver + index generation): the exact-
    # span entry, else the best-overlapping sibling (the span key shifts as lines
    # are added). `src_key` is tracked so we can read its buffer-text snapshot for
    # the position-offset fast path and evict it when the view shifts off it.
    prev = src = src_key = None
    stale_seed = False
    if (not accurate and not heal
            and Toggles.TextEditor.SymbolUsages.incremental_symbol_index):
        # stale_gen_incremental: a prev whose generation lapsed (another file
        # changed / cross-session restore) still contains a valid expensive
        # half for THIS file; reuse it instead of re-recomputing and mark the
        # result (see `heal` above) so it trues up at the next gen bump.
        any_gen = Toggles.TextEditor.SymbolUsages.stale_gen_incremental
        if cached is not None and cached[0][2] is False \
                and (cached[0][3] == gen or any_gen):
            src, src_key = cached, key
        else:
            src_key, src = _best_same_file_prev(resolved, start, end, gen, key,
                                                any_gen=any_gen)
        if src is not None:
            stale_seed = (src[0][3] != gen
                          or src_key in _stale_seeded_spans)
            # Cheapest path: a position-only edit (blank lines added/removed, no
            # non-blank content change) needs no recompute - remap the prior
            # result's buffer positions by a count delta. _line_offset_map returns
            # None on any substantial change, falling through to the full
            # recompute below.
            if Toggles.TextEditor.SymbolUsages.offset_symbol_positions:
                old_text = _span_text.get(src_key)
                # fast_only render-thread gate: only a line-COUNT change can be a
                # position-only offset, so a within-line edit skips the O(file)
                # map and defers immediately (cheap path). The bg path (not
                # fast_only) always tries the offset for line-or-zero-blank edits.
                if not (fast_only and (old_text is None
                                       or old_text.count("\n") == text.count("\n"))):
                    offset = None
                    if old_text is not None:
                        # Contiguous blank insert/delete (single Enter /
                        # line delete): (pivot, delta) shift with full
                        # reuse of unchanged symbols - the common keystroke
                        # case, far cheaper than the per-line dict remap.
                        _shift = _line_shift(old_text, text)
                        if _shift is not None:
                            # Mid-burst debounce (render thread only): the
                            # remap is correct but not the fastest sub-ms -
                            # at ~700ms it outweighs most of the map
                            # (~19ms measured), per keystroke. Between
                            # materializations serve the UNSHIFTED graph
                            # UNCACHED (no sig restamp + base snapshot kept,
                            # so the next probe re-detects the COMPOSITE
                            # shift from the same base). Display stays exact:
                            # re-serving the identical map is an identity
                            # no-op for the attach (see _post_symbol_attach),
                            # and the editor's held spans splice-remap per
                            # edit on their own. Only jump-target lines drift,
                            # by at least _SHIFT_MAT_MIN_S of typing; the
                            # ensure pass retries per frame while the sig is
                            # stale, so positions true up one frame window
                            # after the burst pauses.
                            if fast_only:
                                _lm = _shift_last_mat.get(resolved)
                                if (_lm is not None and _time.monotonic() - _lm
                                        < _SHIFT_MAT_MIN_S):
                                    _ptrace_rl(("shift-defer", resolved),
                                               "usage shift deferred (mid-burst) — serving unshifted base",
                                               file=resolved.name, span=f"{start}-{end}")
                                    return src[1]
                            offset = _offset_usages_shift(
                                src[1], _shift[0], _shift[1], resolved)
                        else:
                            line_map = _line_offset_map(old_text, text)
                            offset = (_offset_usages(src[1], line_map, resolved)
                                      if line_map is not None else None)
                    if offset is not None:
                        if len(_shift_last_mat) > 256:
                            _shift_last_mat.clear()
                        _shift_last_mat[resolved] = _time.monotonic()
                        if chash is None:
                            chash = _content_hash(text)
                        # Position-only remap: provenance passes along unchanged
                        # (the data is the seed's, just shifted).
                        if src_key != key:
                            _usage_graph_source[key] = _usage_graph_source.get(
                                src_key, (_symbol_store.get("origin", "disk"), 0))
                        _store_usages(key, sig, offset, text, chash=chash,
                                      evict=src_key if src_key != key else None)
                        if stale_seed:
                            _stale_seeded_spans.add(key)
                        _ptrace(f"usage offset-remap in {(_time.monotonic() - _t_probe) * 1000:.1f}ms",
                                file=resolved.name, span=f"{start}-{end}")
                        return offset
            prev = _raw_from_usages(src[1])
    if fast_only:
        # Per-frame render-thread probe that missed - the probe itself already
        # costs O(file) (pending-overlay text build + content hash), so surface
        # what it burns per frame while the deferred recompute is pending.
        _ptrace_rl(("probe-miss", key),
                   f"usage probe miss, deferring (probe cost {(_time.monotonic() - _t_probe) * 1000:.1f}ms/frame)",
                   file=resolved.name, span=f"{start}-{end}")
        # Any prior result - even a truly empty span - beats an empty first
        # paint: hand it to the caller as an INTERIM stopgap (the editor's
        # usage-site verify-recover aligns small drift and drops mismatched
        # sites, so it never washes wrong tokens) while the real recompute is
        # deferred. Wrapped so schedulers still treat it as needs-recompute.
        _stopgap = (src[1] if src is not None
                    else cached[1] if cached is not None else None)
        if _stopgap:
            return InterimUsages(_stopgap)
        return _NEEDS_RECOMPUTE  # only exact-hit + offset are cheap; defer the rest
    # Typing quiet-gate (the cst-merge lesson): a recompute launched mid-burst
    # holds the GIL for 150ms-4s against the render thread and is obsolete by
    # the next keystroke anyway. Serve the held result uncached - the editor's
    # nudge retries every frame, but the real refresh lands the moment typing
    # goes quiet. A cold miss (nothing held) returns {} uncached and seeds at
    # quiet instead.
    _held_prior = (src[1] if src is not None
                   else cached[1] if cached is not None else None)
    _li = getattr(Melty, "_last_input_time", 0.0)
    # Small files recompute in a few ms, so they take the short debounce and
    # stay near-live while typing; big ones keep the long coalescing window.
    _cap = Toggles.TextEditor.small_file_max_chars
    _small = bool(_cap) and isinstance(text, str) and len(text) <= _cap
    _quiet_s = (Toggles.TextEditor.small_file_debounce_ms if _small
                else Toggles.TextEditor.parse_debounce_ms) / 1000.0
    if _li and _quiet_s > 0 and _time.monotonic() - _li < _quiet_s:
        _ptrace_rl(("usage-typing-hold", key),
                   "usage recompute deferred (typing) — holding last-good",
                   file=resolved.name, span=f"{start}-{end}")
        return _held_prior if _held_prior is not None else {}
    # In-flight dedup: concurrent triggers for the same span stacked multi-
    # second recomputes back-to-back (observed 4.0+2.8+4.3s). Later callers
    # defer; the winner stores the fresh result for everyone.
    _ifl = _usage_inflight.get(key)
    if _ifl is not None and _time.monotonic() - _ifl < 10.0:
        return _held_prior if _held_prior is not None else {}
    _usage_inflight[key] = _time.monotonic()   # self-expires after a second
    # Past every fast path — this is a real incremental/full recompute. Time and
    # notify only here, so cache hits / offsets stay silent.
    recompute_start = _time.monotonic()
    _mode = ("jedi" if accurate
             else "incremental" if prev is not None
             else "heal-full" if heal else "cold")
    if prev is None:
        # Conspicuous: a FULL recompute is the expensive path (~0.4-2s) the
        # stale-seed reuse exists to avoid - make every one visible on stdout.
        print(f"\033[1;33;45m[symbol-usage] FULL recompute ({_mode}) "
              f"{resolved.name}:{start}-{end} miss={_why}\033[0m")
    _ptrace(f"usage recompute start ({_mode}, miss={_why})",
            file=resolved.name, span=f"{start}-{end}", pending_gen=pending_gen)
    try:
        raw = (_symbol_refs_worker(str(resolved), start, end, text) if accurate
               else _symbol_refs_index(str(resolved), start, end, text, prev=prev))
    except Exception as _e:
        raw = {}
        _ptrace(f"usage recompute RAISED {type(_e).__name__}: {_e}",
                file=resolved.name, span=f"{start}-{end}")
    # In-progress edit with a syntax error: hold the last-good graph instead of
    # discarding it. Return the prior result (so the editor keeps the references
    # live) WITHOUT caching over the good entry - leaving it intact means the
    # next valid parse recomputes and builds from it rather than cold, and a
    # never-evicted broken {} snapshot can't trip the offset/reuse paths. A
    # genuinely empty span (raw == {}) still caches cleanly below.
    if raw is _PARSE_FAILED:
        _usage_inflight.pop(key, None)
        _ptrace(f"usage recompute: buffer parse failed after "
                f"{(_time.monotonic() - recompute_start) * 1000:.0f}ms — holding last-good",
                file=resolved.name, span=f"{start}-{end}")
        held = (src[1] if src is not None
                else cached[1] if cached is not None else {})
        return held
    usages = _rebuild_symbol_usages(raw)
    _ptrace(f"usage recompute done ({_mode}) in "
            f"{(_time.monotonic() - recompute_start) * 1000:.0f}ms",
            file=resolved.name, span=f"{start}-{end}", names=len(usages))
    notify(f"Symbol usage compute for {resolved.name}:{start}-{end} took "
           f"{_time.monotonic() - recompute_start:.2f}s", tag="Compute usage")
    # The view moved to `key`; the old sibling we reused is now dead weight.
    if chash is None:
        chash = _content_hash(text)
    # Provenance stamp for the usage-source badge: a full pass (cold / heal /
    # jedi) resets the base to "fresh"; an incremental reuse keeps the seed's
    # base and bumps its refresh count.
    if prev is None:
        _usage_graph_source[key] = ("fresh", 0)
    else:
        _b, _n = _usage_graph_source.get(
            src_key, (_symbol_store.get("origin", "disk"), 0))
        _usage_graph_source[key] = (_b, _n + 1)
    _store_usages(key, sig, usages, text, chash=chash,
                  evict=src_key if (src_key is not None and src_key != key) else None)
    # Marker lifecycle: a result built on a stale-gen seed owes a full pass
    # (repaid via `heal` at the next gen bump); a full/incremental-at-gen
    # result is debt-free and clears any prior mark.
    if stale_seed:
        _stale_seeded_spans.add(key)
    else:
        _stale_seeded_spans.discard(key)
    _usage_inflight.pop(key, None)
    return usages


# Spans with a real recompute currently running - later concurrent triggers
# return the held result instead of stacking another multi-second pass.
_usage_inflight = globals().get("_usage_inflight") or {}

# Span keys whose result was seeded from a STALE-generation prev (their
# cross-file callers might reflect other files' edits). The mark forces one full
# refresh at the next generation-moved miss (see `heal` in
# _compute_symbol_usages), then clears. Transient - never pickled; a restored
# stale entry simply re-marks itself when it's next used as a seed.
_stale_seeded_spans = globals().get("_stale_seeded_spans") or set()

# resolved-path -> monotonic time of the last position-offset materialization.
# The render-thread (fast_only) shift path serves the UNSHIFTED base between
# materializations (see the offset switch in _compute_symbol_usages), so a
# typing burst pays the O(lines) remap cost only once per _SHIFT_MAT_MIN_S
# instead of per keystroke. Bounded; timestamps are harmless to leak.
_SHIFT_MAT_MIN_S = 0.25   # matches text_editor._TINT_RECOMPUTE_MIN_S
_shift_last_mat = globals().get("_shift_last_mat") or {}


def _best_same_file_prev(resolved, start, end, gen, exclude_key, any_gen=False):
    """Pick the cached entry for the SAME file at the SAME index generation whose
    span best overlaps [start, end] — seeds an incremental refresh when the exact
    (start, end) key shifted (the span grew/shrank as the user edited). Defs +
    callers are keyed by symbol NAME and valid across spans at one generation, so a
    shifted sibling reuses cleanly (names it lacks just recompute). Returns
    (key, entry) or (None, None). The cache is small (≈one entry per open editor
    span), so the linear scan is negligible.

    any_gen=True (stale_gen_incremental) also admits STALE-generation entries —
    a cross-session pickle restore or a gen bump from another file's edit — as
    seeds; a same-gen entry still wins over a stale one at any overlap, since
    only stale seeds incur the heal debt."""
    best = None  # ((gen_match, overlap), key, entry)
    for k, entry in _symbol_usage_cache.items():
        if k == exclude_key or k[0] != resolved:
            continue
        s = entry[0]
        if s[2] is not False:  # different resolver
            continue
        if s[3] != gen and not any_gen:  # different generation
            continue
        ov = min(end, k[2]) - max(start, k[1])
        rank = (s[3] == gen, ov)
        if ov > 0 and (best is None or rank > best[0]):
            best = (rank, k, entry)
    return (best[1], best[2]) if best else (None, None)


def _raw_from_usages(usages: dict) -> dict:
    """Reconstruct the index path's raw {sym: {definition, callers}} shape from a
    cached {sym: SymbolUsage}. Lets the incremental refresh reuse a prior compute
    WITHOUT widening the (persisted) cache entry — the SymbolUsage already carries
    every definition + caller, so the expensive half is recovered from it rather
    than stored twice. Only the two keys _symbol_refs_index reads are rebuilt."""
    out = {}
    for nm, su in usages.items():
        d = su.definition
        out[nm] = {
            "definition": ((str(d.path) if d.path else None, d.line, d.column,
                            d.module_name) if d is not None else (None, 0, 0, "")),
            "callers": [(str(c.path) if c.path else None, c.line, c.column,
                         c.scope, c.module_name) for c in su.callers],
        }
    return out


def _line_offset_map(old_text: str, new_text: str) -> dict | None:
    """If `old_text` and `new_text` differ ONLY in blank (whitespace-only) lines —
    a pure newline / blank-line insert-or-delete — return {old_line: new_line}
    (1-based) for every non-blank line. Otherwise None: any change to a non-blank
    line (new code, edited text, even reindentation) is "substantial" and must
    recompute. One linear lockstep walk over the two line lists builds the map and
    detects substantiality at once. This is recompute-on-MISS work (the miss was
    already detected cheaply via pending_gen), and it's far cheaper than the ast
    parse + walks it avoids — so it doesn't run afoul of the no-content-hashing
    rule, which is about DETECTING misses on the hot path."""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    nO, nN = len(old_lines), len(new_lines)
    oi = ni = 0
    mapping = {}
    while True:
        while oi < nO and not old_lines[oi].strip():  # skip blanks in old
            oi += 1
        while ni < nN and not new_lines[ni].strip():  # skip blanks in new
            ni += 1
        if oi >= nO and ni >= nN:
            return mapping  # both exhausted: match
        if oi >= nO or ni >= nN:
            return None  # non-blank counts differ
        if old_lines[oi] != new_lines[ni]:
            return None  # non-blank content changed
        mapping[oi + 1] = ni + 1  # 1-based line numbers
        oi += 1
        ni += 1


def _line_shift(old_text: str, new_text: str):
    """(pivot_line, delta) when the two texts differ only by ONE contiguous
    run of inserted/deleted BLANK lines — the single-keystroke Enter/delete
    case: lines before `pivot` (1-based) are identical, lines at/after shift
    by `delta`. None otherwise (caller falls back to the lockstep map /
    recompute). This is the fast shape for _offset_usages_shift: no per-line
    dict, and untouched symbols get reused without allocation."""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    nO, nN = len(old_lines), len(new_lines)
    if nO == nN:
        return None
    m = min(nO, nN)
    p = 0
    while p < m and old_lines[p] == new_lines[p]:
        p += 1
    s = 0
    while s < m - p and old_lines[nO - 1 - s] == new_lines[nN - 1 - s]:
        s += 1
    if (any(l.strip() for l in old_lines[p:nO - s])
            or any(l.strip() for l in new_lines[p:nN - s])):
        return None      # middle isn't purely blank - substantial change
    return p + 1, nN - nO


def _offset_usages_shift(usages: dict, pivot: int, delta: int,
                         resolved: _Path) -> dict:
    """_offset_usages for the (pivot, delta) shift shape: every in-file
    position < pivot is untouched, >= pivot moves by delta. A SymbolUsage
    with nothing past the pivot is REUSED as-is — for an edit low in the
    file that's most of the map, which is what turns the per-keystroke
    remap from tens of ms of allocation into a scan. Sites can't land
    inside the changed region (it's blank on both sides — symbols live on
    non-blank lines), so no absent-line fallback is needed."""
    out = {}
    for nm, su in usages.items():
        needs = any(l >= pivot for l, _ in su.sites)
        if not needs:
            needs = any(r.path == resolved and r.line >= pivot
                        for r in su.callers)
        d = su.definition
        if (not needs and d is not None and d.path == resolved
                and d.line >= pivot):
            needs = True
        if not needs:
            out[nm] = su
            continue
        new_sites = [(l + delta if l >= pivot else l, c) for l, c in su.sites]
        new_callers = [UsageRef(path=r.path, line=r.line + delta,
                                column=r.column, scope=r.scope,
                                module_name=r.module_name)
                       if r.path == resolved and r.line >= pivot else r
                       for r in su.callers]
        if d is not None and d.path == resolved and d.line >= pivot:
            d = UsageRef(path=d.path, line=d.line + delta, column=d.column,
                         scope=d.scope, module_name=d.module_name)
        out[nm] = SymbolUsage(name=su.name, definition=d,
                              callers=new_callers, sites=new_sites)
    return out


def _offset_usages(usages: dict, line_map: dict, resolved: _Path) -> dict | None:
    """Return a NEW {sym: SymbolUsage} with this-file BUFFER positions remapped via
    line_map (old_line -> new_line) — exactly what a recompute would produce for a
    position-only edit, without the recompute:
      • sites and IN-FILE callers are buffer positions → remapped (col unchanged,
        the line's content is identical),
      • cross-file callers are reused as-is (their files didn't move),
      • an IN-FILE definition is a buffer line too → remapped. Local variables
        define in THIS file, and the declaration's line drives the per-site
        `at_def` direction (text_editor._collect_usage_spans): if the def line
        stayed stale while the sites shifted, the declaration occurrence stopped
        matching it and the jump direction broke for everything after an inserted
        line. A cross-file def (a module symbol resolved via inspect against the
        live object) is KEPT — its file didn't move.
    Returns None if any in-file SITE/caller is absent from the map so the caller
    falls back to a recompute; a def line that's absent (it became blank — can't
    happen for a real declaration) keeps its old value rather than force one."""
    out = {}
    for nm, su in usages.items():
        new_sites = []
        for (l, c) in su.sites:
            nl = line_map.get(l)
            if nl is None:
                return None
            new_sites.append((nl, c))
        new_callers = []
        for ref in su.callers:
            if ref.path == resolved:  # in-file caller: buffer pos
                nl = line_map.get(ref.line)
                if nl is None:
                    return None
                new_callers.append(UsageRef(path=ref.path, line=nl, column=ref.column,
                                            scope=ref.scope, module_name=ref.module_name))
            else:
                new_callers.append(ref)  # other file: unchanged, kee
        d = su.definition
        if d is not None and d.path == resolved:  # in-file def: buffer line, remap
            ndl = line_map.get(d.line)
            if ndl is not None:
                d = UsageRef(path=d.path, line=ndl, column=d.column,
                             scope=d.scope, module_name=d.module_name)
        out[nm] = SymbolUsage(name=su.name, definition=d,
                              callers=new_callers, sites=new_sites)
    return out


def _store_usages(key, sig, usages, text, chash=None, evict=None) -> None:
    """Write a span result + the buffer-text snapshot it was computed from + the
    content hash (for the longevity rescue), and drop a superseded sibling key
    (and its snapshot/hash) the view shifted off of."""
    _symbol_usage_cache[key] = (sig, usages)
    _span_text[key] = text
    if chash is not None:
        _span_hashes[key] = chash
    if evict is not None:
        _symbol_usage_cache.pop(evict, None)
        _span_text.pop(evict, None)
        _span_hashes.pop(evict, None)
        _stale_seeded_spans.discard(evict)
        _usage_graph_source.pop(evict, None)


def invalidate_usage_cache(path: _Path | str | None = None,
                           drop_spans: bool = False) -> None:
    """Invalidate cross-file reference caches for a path, or all if None.

    Span RESULTS are KEPT by default: their sig (mtime / pending_gen / index
    gen) already detects any staleness this invalidation could signal, and a
    kept entry is worth a lot — an instant hash rescue when the content is
    unchanged (the common case: a save writing the buffer verbatim to disk
    used to wipe the spans here and force a cold recompute of a file whose
    content didn't change), or the incremental seed (stale_gen_incremental)
    when it did. drop_spans=True is the scorched-earth path for an EXPLICIT
    force-refresh (the manual Index button): the next compute is a true cold
    pass with no reuse."""
    print(f"Invalidating usage cache for {path if path else 'ALL PATHS'}"
          f"{' (dropping spans)' if drop_spans else ''}")
    if path is None:
        _xref_cache.clear()
        _file_parse_cache.clear()
        if drop_spans:
            _symbol_usage_cache.clear()
            _span_text.clear()
            _span_hashes.clear()
            _stale_seeded_spans.clear()
            _usage_graph_source.clear()
    else:
        resolved = _Path(path).resolve()
        _xref_cache.pop(resolved, None)
        _file_parse_cache.pop(resolved, None)
        if drop_spans:
            for k in [k for k in _symbol_usage_cache if k[0] == resolved]:
                _symbol_usage_cache.pop(k, None)
            for k in [k for k in _span_text if k[0] == resolved]:
                _span_text.pop(k, None)
            for k in [k for k in _span_hashes if k[0] == resolved]:
                _span_hashes.pop(k, None)
            for k in [k for k in _stale_seeded_spans if k[0] == resolved]:
                _stale_seeded_spans.discard(k)
            for k in [k for k in _usage_graph_source if k[0] == resolved]:
                _usage_graph_source.pop(k, None)


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


# def populate_usages(gp: GeneralParse) -> None:
#     """Populate cross-file UsageRefs on a GeneralParse and its children.
#
#     Intra-module usages are already populated during construction
#     (cst_module_to_dict / cst_classdef_to_dict).  This adds cross-file
#     references via jedi (cached per-file by mtime).
#
#     Safe to call from a background thread - does not touch imgui
#     or Melty state.  Call this OUTSIDE the stateful converter chain:
#
#         Background.run(populate_usages,
#                        func_kwargs={"gp": result},
#                        stateful=False)
#     """
#     print("Populating cross-file usages for", gp.file_path)
#     file_path = gp.file_path
#     if file_path is not None:
#         _populate_xrefs(gp, file_path)
#     # Per-symbol caller/definition index for the symbols referenced in this view
#     # (the data behind caller lists). Cached per file mtime → once per view.
#     populate_symbol_usages(gp)


# # Keep old name as alias
# populate_cross_file_usages = populate_usages


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
    Memoized by node identity while the incremental span reconvert is active
    (_inc_memo, see _cst_to_python_or_raw): the output is position-independent
    text, so nodes shared across the body splice reuse it verbatim — the
    per-statement source strings _extract_block_assignments builds were the
    dominant reconvert cost (~1600 codegens per pass on a big function).
    _inc_memo is defined later in the module; module-level calls at import
    time see no pair and take the plain path."""
    pair = getattr(globals().get("_inc_memo"), "pair", None) if "_inc_memo" in globals() else None
    if pair is not None:
        old_m, new_m = pair
        key = ("c", id(node))
        hit = old_m.get(key)
        if hit is not None and hit[0] is node:
            new_m[key] = hit
            return hit[1]
        state = _CodegenState(default_indent="    ", default_newline="\n")
        node._codegen(state)
        code = "".join(state.tokens)
        new_m[key] = (node, code)
        return code
    state = _CodegenState(default_indent="    ", default_newline="\n")
    node._codegen(state)
    return "".join(state.tokens)


def _is_dunder(key):
    """True if key is a __dunder__ string — safe on non-string keys."""
    return isinstance(key, str) and key.startswith("__") and key.endswith("__")


def parse_def_name(node):
    """The class/def name of a ClassParse / FunctionParse from EITHER parser:
    `def_name` (stamped by cst_classdef_to_dict / cst_funcdef_to_dict and by
    core_syntax) or, for a parse pickled before the stamp existed, the libcst
    node's name. None for anything else."""
    name = getattr(node, "def_name", None)
    if name is not None:
        return name
    cst_node = node.get("__cst__") if isinstance(node, dict) else None
    return getattr(getattr(cst_node, "name", None), "value", None)


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


def _ensure_float_str(s, min_dp=2):
    """Ensure a numeric string is a valid CST float with at least `min_dp`
    decimal places.

    Adds a decimal point if one is missing and pads trailing zeros so the
    result always shows at least `min_dp` decimals (default 2). Strings with
    more precision than `min_dp` are left untouched. Scientific notation
    ('1e10') is returned unchanged.

    '3' → '3.00', '100' → '100.00', '0.5' → '0.50', '3.14159' → '3.14159'
    """
    if "e" in s.lower():
        return s
    if "." not in s:
        s += "."
    decimals = len(s.split(".", 1)[1])
    if decimals < min_dp:
        s += "0" * (min_dp - decimals)
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
        return _ensure_float_str(repr(value))

    # First check: does this value survive float32 round-trip?
    f32_bytes = _F32_PACK.pack(value)
    f32 = _F32_PACK.unpack(f32_bytes)[0]
    if f32 != value:
        return _ensure_float_str(repr(value))  # pure float64 - no cleaning

    # Float32-representable: find shortest string that preserves it
    full = repr(value)
    for sig in range(2, 8):
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
    # A core_syntax reverse (dict_to_cst_module on an __origin__ node) is already text.
    return value if isinstance(value, str) else value.code


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

import re as _re_ws
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


# ────────── UI yield ───────────────────────────────────────────────────────
# cst_module_to_dict is pure-Python and GIL-bound; even after the ast position
# optimization (~90ms on a big buffer) it stutters interaction when it runs in a
# background parse worker concurrently with the render loop. While the user is
# actively interacting - typing (incl. arrow keys), clicking/dragging the mouse, or
# scrolling (NOT bare hover - that doesn't defer, else the parse stalls while the
# mouse merely wanders, the slow initial load) - _yield_to_ui pauses the parse at
# statement boundaries:
# time.sleep fully releases the GIL, so the render thread gets uncontended frames.
# The parse resumes once input goes quiet. Gated on Toggles.yield_to_ui. It NEVER
# sleeps the render/GL or main thread (that would freeze the very UI we're
# protecting) - only the background worker the code actually runs on.
_YIELD_QUIET_S = 0.5  # resume once keyboard input has been quiet this long
_YIELD_SLICE_S = 0.1  # GIL-releasing sleep granularity while backing off (~1 frame)

# Per-thread accumulation of time _yield_to_ui actually slept, so a conversion
# (cst_module_to_dict) can report how much of its wall time was deliberate
# back-off versus real work. Reset by the top-level convert, added to here.
_yield_slept = threading.local()


_FRAME_PARK_SLICE_S = 0.002   # sleep granularity while the render thread draws
_FRAME_PARK_MAX_S = 0.5       # per-call cap so conversions always make progress
_FRAME_STAMP_STALE_S = 2.0    # a frame stamp this old = frame aborted mid-draw


def _park_while_frame(max_park_s=_FRAME_PARK_MAX_S):
    """Sleep a background thread while the render thread is INSIDE a frame
    (Melty._frame_draw_start set at frame start, cleared at post_frame end).

    Why: post_frame's capture pass is hundreds of ctypes GL calls, each
    releasing and re-acquiring the GIL. Under a CPU-bound background
    conversion every re-acquisition loses the convoy race, and a 30ms frame
    was measured at 300-680ms — the capture-pass wall time matching the
    background thread's CPU time to the millisecond. Input recency is the
    wrong gate for that starvation (the heavy reparse fires AFTER the typing
    debounce, exactly when input is stale), so heavy loops park on the frame
    flag itself at their natural chunk boundaries.

    Returns seconds actually slept. No-op on the main/render/GL threads
    (never sleep the thread being protected), on a stale frame stamp (an
    aborted frame must not park conversions forever), and after max_park_s
    (progress guarantee when frames are back-to-back)."""
    fs = getattr(Melty, "_frame_draw_start", 0.0)
    if not fs:
        return 0.0
    cur = threading.current_thread()
    if cur is threading.main_thread():
        return 0.0
    import meltygui.core.graphics.gl_state as gl_state
    glt = getattr(gl_state, "_gl_thread", None)  # read, don't claim
    if glt is None or cur is glt:
        return 0.0
    t0 = time.monotonic()
    if t0 - fs >= _FRAME_STAMP_STALE_S:
        return 0.0
    while True:
        time.sleep(_FRAME_PARK_SLICE_S)
        fs = getattr(Melty, "_frame_draw_start", 0.0)
        now = time.monotonic()
        if not fs or now - fs >= _FRAME_STAMP_STALE_S or now - t0 >= max_park_s:
            slept = now - t0
            _yield_slept.t = getattr(_yield_slept, "t", 0.0) + slept
            return slept


def _yield_to_ui():
    from meltygui.core.runtime.toggles import Toggles  # lazy: avoid import cycle
    if not Toggles.yield_to_ui:
        return
    # The incremental span reconvert is the LIGHT path built to run during
    # typing (~60-90ms memo-assisted) - parking it at statement boundaries
    # multiplied its wall time up to 40x (observed 4.1s) for no GIL benefit
    # worth the latency. Instead of parking, hand the GIL off: sleep(0)
    # forces a GIL yield so the render thread interleaves between statements -
    # the merge stops paying one solid 100ms+ GIL hold (for 200ms frames) and
    # costs multiple per-statement slices. Full parses keep parking as before.
    _im = globals().get("_inc_memo")
    if _im is not None and getattr(_im, "pair", None) is not None:
        # Throttled: a handoff every 8th statement keeps the max contiguous
        # hold at a few ms, avoiding ~400 GIL round-trips per merge.
        _n = getattr(_im, "yield_n", 0) + 1
        _im.yield_n = _n
        if _n % 8 == 0:
            # sleep(0) drops the GIL for an instant but the CPU-bound merge
            # wins the reacquisition convoy against the render thread's
            # per-GL-call round trips — measured 250-530ms capture passes
            # with yielded=0 while this path ran. When a frame is actively
            # drawing, park properly; between frames it stays a quick handoff.
            if not _park_while_frame():
                time.sleep(0)
        return
    if Melty.frame_count < 4:
        return  # app startup: never back off the initial parse, just run it
    # Mid-frame park FIRST, independent of input recency: the full reparse
    # fires after the typing debounce (input already stale), while the render
    # thread is still repainting the invalidating state - the measured
    # 300-680ms capture-pass starvation. Thread guards live in the background.
    _park_while_frame()
    last = getattr(Melty, "_last_input_time", 0.0)
    if not last or time.monotonic() - last >= _YIELD_QUIET_S:
        return  # no recent input - fast path, no back-off
    # Recent input. Only the background worker may sleep here; sleeping the
    # render/GL thread (or main) would freeze the very UI we mean to protect.
    cur = threading.current_thread()
    if cur is threading.main_thread():
        return
    import meltygui.core.graphics.gl_state as gl_state
    glt = getattr(gl_state, "_gl_thread", None)  # read, don't claim (is_current_thread claims)
    if glt is None or cur is glt:
        return
    _t0 = time.monotonic()
    while Toggles.yield_to_ui:
        if time.monotonic() - getattr(Melty, "_last_input_time", 0.0) >= _YIELD_QUIET_S:
            break
        time.sleep(_YIELD_SLICE_S)
    _yield_slept.t = getattr(_yield_slept, "t", 0.0) + (time.monotonic() - _t0)


def _build_ast_span_map(module, source=None):
    """`{libcst node: Span}` for the nodes `cst_module_to_dict` stamps, derived
    from Python's `ast` (native lineno/col_offset from the C parser) instead of
    libcst's whole-tree `PositionProvider` codegen (~64% of the cst→dict cost).

    `source` is the module's already-rendered code (the caller computes it once
    for the GeneralParse.source). Pass it in: `module.code` is itself a full
    libcst codegen, so recomputing it here would give back much of what we saved.

    Walks the libcst and ast trees in parallel: both visit statements in source
    order, a `SimpleStatementLine` expands to its small statements 1:1, and every
    compound statement is exactly one ast statement — so positional pairing stays
    aligned (any unrecognized statement still consumes one ast slot, preserving
    alignment for its siblings). Best-effort: a node we can't place simply gets
    no span, and `_span_of`/`_record_child`/`_stamp_span` already treat a missing
    span as 'skip'."""
    out = {}
    if source is None:
        source = module.code
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return out

    def sp(n):
        el = getattr(n, "end_lineno", None) or n.lineno
        ec = getattr(n, "end_col_offset", None)
        return Span(n.lineno, n.col_offset, el, ec if ec is not None else n.col_offset)

    def list_sp(stmts):
        if not stmts:
            return None
        a, b = stmts[0], stmts[-1]
        return Span(a.lineno, a.col_offset,
                    getattr(b, "end_lineno", None) or b.lineno,
                    getattr(b, "end_col_offset", 0))

    def params_sp(fn):
        a = fn.args
        nodes = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
        if a.vararg: nodes.append(a.vararg)
        if a.kwarg: nodes.append(a.kwarg)
        nodes += [d for d in (a.defaults + a.kw_defaults) if d is not None]
        if not nodes:
            return None
        first = min(nodes, key=lambda n: (n.lineno, n.col_offset))
        last = max(nodes, key=lambda n: (getattr(n, "end_lineno", None) or n.lineno,
                                         getattr(n, "end_col_offset", 0)))
        return Span(first.lineno, first.col_offset,
                    getattr(last, "end_lineno", None) or last.lineno,
                    getattr(last, "end_col_offset", 0))

    def body_list(node):
        body = getattr(node, "body", None)
        if isinstance(body, (cst.IndentedBlock, cst.SimpleStatementSuite)):
            return list(body.body), body
        return [], body

    def pair(cst_stmts, ast_stmts):
        ai, n = 0, len(ast_stmts)
        for cs in cst_stmts:
            if ai >= n:
                break
            if isinstance(cs, (cst.SimpleStatementLine, cst.SimpleStatementSuite)):
                spans = []
                for small in cs.body:
                    if ai >= n:
                        break
                    an = ast_stmts[ai];
                    ai += 1
                    s = sp(an)
                    out[small] = s
                    spans.append(s)
                    # Map the statement's RHS value node too: a dict-valued leaf
                    # (CallParse, collection literal) is skipped by _record_child
                    # and gets its .span stamped on the value node instead
                    # (cst_call_to_dict → _stamp_span). Without it, live_view's
                    # _key_pressed / token_box read child.span == None and the
                    # box falls back to a whole-line highlight.
                    sval = getattr(small, "value", None)
                    aval = getattr(an, "value", None)
                    if sval is not None and aval is not None:
                        out[sval] = sp(aval)
                if spans:
                    out[cs] = Span(spans[0].start_line, spans[0].start_col,
                                   spans[-1].end_line, spans[-1].end_col)
            elif isinstance(cs, (cst.FunctionDef, cst.ClassDef)):
                an = ast_stmts[ai];
                ai += 1
                out[cs] = sp(an)
                # Decorator-inclusive lineno (ast lineno points at def/class,
                # decorators sit ABOVE it) so the incremental cst merge with
                # the true logical start of the statement node.
                _decs = getattr(an, "decorator_list", None)
                if _decs:
                    out[("dec_start", cs)] = _decs[0].lineno
                bl, bnode = body_list(cs)
                if isinstance(cs, cst.FunctionDef):
                    out[cs.params] = params_sp(an) or sp(an)
                out[bnode] = list_sp(getattr(an, "body", []))
                pair(bl, getattr(an, "body", []))
            elif isinstance(cs, cst.If):
                pair_if(cs, ast_stmts[ai]);
                ai += 1
            elif isinstance(cs, (cst.For, cst.While)):
                an = ast_stmts[ai];
                ai += 1
                out[cs] = sp(an)
                if isinstance(cs, cst.For) and getattr(an, "iter", None) is not None:
                    out[cs.iter] = sp(an.iter)
                bl, bnode = body_list(cs)
                out[bnode] = list_sp(getattr(an, "body", []))
                pair(bl, getattr(an, "body", []))
                pair_else(cs.orelse, getattr(an, "orelse", []))
            elif isinstance(cs, cst.Try):
                pair_try(cs, ast_stmts[ai]);
                ai += 1
            elif isinstance(cs, cst.With):
                an = ast_stmts[ai];
                ai += 1
                out[cs] = sp(an)
                bl, bnode = body_list(cs)
                out[bnode] = list_sp(getattr(an, "body", []))
                pair(bl, getattr(an, "body", []))
            else:
                out[cs] = sp(ast_stmts[ai]);
                ai += 1

    def pair_if(cs_if, ast_if):
        if not isinstance(ast_if, ast.If):
            return
        out[cs_if] = sp(ast_if)  # whole-statement span (incremental merge)
        out[cs_if.test] = sp(ast_if.test)
        out[cs_if.body] = list_sp(ast_if.body)
        pair(list(cs_if.body.body), ast_if.body)
        orelse, ao = cs_if.orelse, ast_if.orelse
        if isinstance(orelse, cst.If):
            if ao and isinstance(ao[0], ast.If):
                pair_if(orelse, ao[0])
        elif isinstance(orelse, cst.Else):
            out[orelse] = list_sp(ao)
            pair(list(orelse.body.body), ao)

    def pair_else(cs_orelse, ast_orelse):
        if isinstance(cs_orelse, cst.Else) and ast_orelse:
            out[cs_orelse] = list_sp(ast_orelse)
            pair(list(cs_orelse.body.body), ast_orelse)

    def pair_try(cs_try, ast_try):
        if not isinstance(ast_try, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            return
        out[cs_try] = sp(ast_try)  # whole-statement span (incremental merge)
        out[cs_try.body] = list_sp(ast_try.body)
        pair(list(cs_try.body.body), ast_try.body)
        for h_cs, h_ast in zip(cs_try.handlers, ast_try.handlers):
            out[h_cs] = sp(h_ast)
            hbl, _ = body_list(h_cs)
            pair(hbl, h_ast.body)
        pair_else(cs_try.orelse, getattr(ast_try, "orelse", []))
        fb = getattr(cs_try, "finalbody", None)
        if fb is not None and getattr(ast_try, "finalbody", None):
            fbl, _ = body_list(fb)
            pair(fbl, ast_try.finalbody)

    pair(list(module.body), tree.body)
    out[module] = Span(1, 0, source.count("\n") + 1, len(source.rsplit("\n", 1)[-1]))
    # The nested helpers are recursive closures (function -> __closure__ cell
    # -> function), a reference cycle that also includes `out` -- and `out` is
    # keyed by EVERY libcst node. Left alone, each conversion parked the whole
    # CST (~37k objects) as cyclic garbage until the next gen2 pass. Releasing
    # the locals empties the cells so the tree dies by refcount instead.
    pair = pair_if = pair_else = pair_try = body_list = sp = list_sp = params_sp = None
    return out


class _position_map:
    """Publish a node→span provider on the thread-local for the duration of a
    conversion, so nested extractors can stamp spans via `_span_of`. Source is
    either Python's `ast` (Toggles.new_position_map, the fast C-parser path) or
    libcst's `PositionProvider` (whole-tree codegen). Failed/absent resolution
    degrades to no spans (None)."""

    def __init__(self, module, source=None):
        self._module = module
        self._source = source

    def __enter__(self):
        self._prev = getattr(_span_scope, "positions", None)
        try:
            from meltygui.core.runtime.toggles import Toggles  # lazy to avoid import cycle
            if Toggles.new_position_map:
                _span_scope.positions = _build_ast_span_map(self._module, self._source)
            else:
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
    # The ast-backed map (new_position_map) stores Spans directly, keyed by the
    # libcst node; libcst's PositionProvider stores CodeRanges. Accept either.
    if isinstance(cr, Span):
        return cr
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
    the container's `_child_spans` map. Skipped for dict-valued children that
    carry their own `.span` — those are found by tree walk. A dict value
    WITHOUT one (e.g. a local assigned a plain dict literal) is recorded here
    too, or it would have no position at all — the completion filter treats a
    span-less local as always-visible. LineMap ignores `_child_spans` entries
    for dict values, so this only feeds the by-key span lookups."""
    if isinstance(value, dict) and getattr(value, "span", None) is not None:
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
                    # A dict VALUE (`x = {...}` parses to a plain dict child)
                    # carries no .span of its own - index it via the parent's
                    # _child_spans like any other leaf, or line→node lookups
                    # skip straight over the enclosing container and the
                    # assignment to its statement key (live Editor's line:N
                    # highlighting). Scope dicts (FunctionParse etc.) keep their
                    # own span entry from the top of _build.
                    if not isinstance(getattr(v, "span", None), Span):
                        cspan = child_spans.get(k)
                        if isinstance(cspan, Span):
                            self._entries.append(
                                (cspan, depth + 1,
                                 NodeRef(v, k, node, cspan, cpath)))
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
# ║   Code completion - scope-aware candidates from the parsed dict + spans      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
# Drives the editor's suggestion popup (draw_text). Uses the SAME data the editor
# already holds - the digest of scope dicts (names as keys, nested function/class
# scopes, `parameters`/`locals` sub-dicts), the `.span`/LineMap line→node index,
# the raw libcst tree under `__cst__` (for imports the dict doesn't surface), and,
# when jedi has run, the cross-project `symbol_usage` data. No reparse, no network.

import keyword as _keyword

_KW = frozenset(_keyword.kwlist)
# Structural keys in scope dict children that are NOT user symbols.
_NON_SYMBOL_KEYS = frozenset({"decorators", "parameters", "locals"})


def _is_symbol_key(k):
    """A dict key that names a real user symbol (not a dunder, structural key,
    control-flow heading like 'if x', comment, or keyword)."""
    return (isinstance(k, str) and k.isidentifier()
            and not k.startswith("__") and k not in _NON_SYMBOL_KEYS and k not in _KW)


def _classify(value):
    """Completion `kind` for a module/class member by its dict value."""
    if isinstance(value, dict):
        node = value.get("__cst__")
        if isinstance(node, cst.FunctionDef):
            return "func"
        if isinstance(node, cst.ClassDef):
            return "class"
        return "member"
    return "var"


def _is_scope_node(d):
    """True for the dict nodes that introduce a Python scope (module / def /
    class) — NOT the intermediate `parameters`/`locals`/control-flow sub-dicts."""
    return isinstance(d, dict) and isinstance(
        d.get("__cst__"), (cst.FunctionDef, cst.ClassDef, cst.Module))


def _flatten_local_names(locals_dict, before_line=None):
    """(name, 'local') for every assignment in a function's `locals` sub-dict,
    descending through control-flow blocks (keyed by non-identifier headings like
    'if cond:') but never into a nested data value or scope. With `before_line`
    (1-indexed, relative to the parse source) locals whose recorded span starts
    AFTER that line are skipped — not yet defined at the caret. A local without
    a recorded span is kept: never over-filter on missing position data."""
    child_spans = getattr(locals_dict, "_child_spans", None) or {}
    for k, v in locals_dict.items():
        if _is_symbol_key(k):
            if before_line is not None:
                span = child_spans.get(k)
                if span is None and isinstance(v, dict):
                    span = getattr(v, "span", None)
                if isinstance(span, Span) and span.start_line > before_line:
                    continue
            yield k, "local"
        elif (isinstance(k, str) and not k.isidentifier()
              and isinstance(v, dict) and "__cst__" not in v):
            if before_line is not None:
                span = getattr(v, "span", None)
                if isinstance(span, Span) and span.start_line > before_line:
                    continue  # the whole control-flow block starts below the caret
            yield from _flatten_local_names(v, before_line)


def _direct_member_names(scope):
    """(name, kind) for the symbols a module or class scope defines directly."""
    return [(k, _classify(v)) for k, v in scope.items() if _is_symbol_key(k)]


def _scope_local_names(scope, before_line=None):
    """(name, kind) the given scope dict introduces. Functions expose their
    `parameters` + `locals`; module/class scopes expose their direct members.
    `before_line` (1-indexed, parse-relative) filters locals — and class/module
    direct members — to those defined at or above that line; params are always
    in scope and never filtered. A member without a recorded span is kept."""
    params, locs = scope.get("parameters"), scope.get("locals")
    if isinstance(params, dict) or isinstance(locs, dict):  # function scope
        out = []
        if isinstance(params, dict):
            out += [(k, "param") for k in params if _is_symbol_key(k)]
        if isinstance(locs, dict):
            out += list(_flatten_local_names(locs, before_line))
        return out
    if before_line is None:
        return _direct_member_names(scope)
    cs = getattr(scope, "_child_spans", None) or {}
    out = []
    for k, v in scope.items():
        if not _is_symbol_key(k):
            continue
        span = cs.get(k) or (getattr(v, "span", None) if isinstance(v, dict) else None)
        if isinstance(span, Span) and span.start_line > before_line:
            continue  # class-body member sits below the caret
        out.append((k, _classify(v)))
    return out


def _scope_chain_for_line(root, rel_line):
    """The scope dicts enclosing relative (1-indexed) `rel_line`, outermost
    first: [module, …, innermost def/class]. Falls back to [root] if the line
    can't be located (e.g. unsaved edits shifted it past the parsed spans)."""
    chain = [root]
    try:
        # LineMap build is O(tree), and completions run per keystroke: cache
        # it on the root. A reparse makes a NEW root (attr gone with it);
        # in-place mutations (usage stats, live edits) never move spans.
        # Set after the pool path only - parses are pickled into the per-dict
        # cache right after conversion, before any completion runs, so the
        # cached LineMap never rides in a pickle.
        lm = getattr(root, "_completion_line_map", None)
        if lm is None or lm.root is not root:
            lm = LineMap(root)
            try:
                root._completion_line_map = lm
            except Exception:
                pass  # plain dict - rebuild per call
        ref = lm.node_at_line(rel_line)
    except Exception:
        return chain
    if ref is None:
        return chain
    node = root
    for k in ref.path:
        try:
            node = node[k]
        except (KeyError, IndexError, TypeError):
            break
        if _is_scope_node(node):
            chain.append(node)
    return chain


class _ImportNameCollector(cst.CSTVisitor):
    """Bound names introduced by import statements anywhere in the tree:
    `import a.b as c` → c; `import a.b` → a; `from x import y, z` → y, z."""

    def __init__(self):
        self.names = []

    @staticmethod
    def _bound(alias):
        if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
            return alias.asname.name.value
        node = alias.name
        while isinstance(node, cst.Attribute):
            node = node.value
        return node.value if isinstance(node, cst.Name) else None

    def visit_Import(self, node):
        for alias in node.names:
            self.names.append(self._bound(alias))

    def visit_ImportFrom(self, node):
        if isinstance(node.names, cst.ImportStar):
            return
        for alias in node.names:
            self.names.append(self._bound(alias))


# Import parsing, source-side: one C-speed pass instead of a full libcst
# visitor. `from x import (a, b as c)` groups may span lines (the [^)]* eats
# newlines); plain `import`/`from` bodies stop at end of line.
_IMPORT_STMT_RE = re.compile(
    r'(?m)^[ \t]*(?:from[ \t]+[\w.]+[ \t]+import[ \t]+(\([^)]*\)|[^\n#]+)'
    r'|import[ \t]+([^\n#]+))')


def _import_names_from_source(text):
    """Bound names introduced by import statements in `text` — the regex
    equivalent of _imported_names for when the raw source is at hand:
    `import a.b as c` → c; `import a.b` → a; `from x import y, z as w` → y, w.
    A `from x import *` contributes nothing (same as the visitor)."""
    out = []
    for m in _IMPORT_STMT_RE.finditer(text):
        body, is_from = (m.group(1), True) if m.group(1) is not None \
            else (m.group(2), False)
        for part in body.strip("()").split(","):
            part = part.strip()
            if not part or part == "*":
                continue
            if " as " in part:
                name = part.rsplit(" as ", 1)[1].strip()
            elif is_from:
                name = part
            else:
                name = part.split(".", 1)[0].strip()
            if name.isidentifier():
                out.append(name)
    return out


def _imported_names(module_cst):
    if not isinstance(module_cst, cst.CSTNode):
        return []
    try:
        v = _ImportNameCollector()
        module_cst.visit(v)
        return [n for n in v.names if n]
    except Exception:
        return []


def completions_at(code_tree, line):
    """Ranked completion candidates for a caret on 0-indexed `line` within
    `code_tree.source`. Returns an ordered list of (name, kind), best first:

        enclosing scope (params/locals, innermost out) → class members →
        module-level names → imports → jedi-resolved cross-project symbols

    The caret's own scope only offers locals already defined at `line` (by
    recorded span); enclosing/module scopes stay unfiltered (late binding).

    `kind` ∈ {param, local, member, func, class, var, import, symbol}. Pure read
    over the digestible dict tree + line/span index + `__cst__` imports + (when
    present) `symbol_usage`; cheap enough to call per keystroke. Returns [] for a
    non-dict tree so the caller can fall back to a plain identifier scan."""
    if not isinstance(code_tree, dict):
        return []
    out, seen = [], set()

    def add(name, kind):
        if isinstance(name, str) and name and name not in seen and _is_symbol_key(name):
            seen.add(name)
            out.append((name, kind))

    chain = _scope_chain_for_line(code_tree, line + 1)  # spans are 1-indexed
    # The caret's own scope filters its members by position - a name bound
    # BELOW the caret isn't exist yet there (function locals AND class-body
    # members alike). Enclosing FUNCTION scopes don't: closures bind late, so
    # their later locals exist by the time inner code runs (module members are
    # offered unfiltered below for the same reason). Enclosing CLASS scopes are
    # skipped ignored - Python's name lookup never reaches a class scope from
    # code nested inside it (bare `Member` in a method throws a NameError).
    innermost = chain[-1]
    for scope in reversed(chain[1:]):  # innermost scope first
        if scope is not innermost and isinstance(scope.get("__cst__"), cst.ClassDef):
            continue
        before = line + 1 if scope is innermost else None
        for name, kind in _scope_local_names(scope, before_line=before):
            add(name, kind)
    for name, kind in _direct_member_names(code_tree):  # module level
        add(name, kind)
    # Import names come from a regex scan of the parse SOURCE, cached on the
    # tree (a reparse makes a new tree object). Never a libcst visitor here
    # (_imported_names): that walks the whole Module - hundreds of ms on a big
    # file - and this path runs per keystroke on the render thread; even
    # cached, its cold cost would explode once per parse landing. The visitor
    # stays as the fallback when the tree carries no source.
    imports = getattr(code_tree, "_completion_import_names", None)
    if imports is None:
        src_txt = getattr(code_tree, "source", None)
        imports = (_import_names_from_source(src_txt) if isinstance(src_txt, str)
                   else _imported_names(code_tree.get("__cst__")))
        try:
            code_tree._completion_import_names = imports
        except Exception:
            pass  # plain dict - recompute per call
    for name in imports:
        add(name, "import")
    symbols = getattr(code_tree, "symbol_usage", None)  # jedi, only if indexed
    if isinstance(symbols, dict):
        # Two exclusions keep the index from undoing the scope/position rules
        # above, which already offer every in-buffer name correctly:
        #   - local-variable entries (key is scope\x1fname\x1fdefpos) cover
        #     every function's locals at any position - bare spellings of
        #     other functions' / not-yet-defined names;
        #   - any entry whose DEFINITION sits inside this buffer (a class
        #     member like `WindowSettings` defined below the caret would
        #     re-enter position-blind lookup as 'symbol').
        # What survives is the index's real value set: names defined OUTSIDE
        # the buffer (elsewhere in the file or cross-project). Dotted member
        # spellings pass _is_symbol_key.
        # Buffer extent in file lines: line_offset (0-indexed first file line,
        # stamped with file_path) + the parse source's line count. NOT
        # `code_tree.address` - in some parses that attribute resolves to a
        # class-body ANNOTATION (typing.Any | ...), not an Address.
        lo = getattr(code_tree, "line_offset", None)
        src_txt = getattr(code_tree, "source", None)
        hi = lo + src_txt.count("\n") if (lo is not None
                                          and isinstance(src_txt, str)) else None
        fp = getattr(code_tree, "file_path", None)
        try:
            fp_res = str(_Path(fp).resolve()) if fp is not None else None
        except Exception:
            fp_res = None
        for k, su in symbols.items():
            if isinstance(k, str) and "\x1f" in k:
                continue
            if fp_res is not None and lo is not None and hi is not None:
                d = getattr(su, "definition", None)
                dp = getattr(d, "path", None)
                dl = getattr(d, "line", None)
                if (dp is not None and dl is not None and str(dp) == fp_res
                        and lo <= dl - 1 <= hi):
                    continue  # defined in this buffer - scope walk owns it
            add(getattr(su, "name", k), "symbol")
    return out


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  cst.Module ↔ dict                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@register
def cst_module_to_dict(input_value: cst.Module, run_jedi=False, **kwargs) -> dict:
    """Top-level statements become readable dict keys.

    Handles: assignments, annotated assignments, class definitions,
    function definitions (default args), and decorator kwargs.
    """
    # A str input is the core_syntax path (Toggles.TextEditor.melty_syntax):
    # string_to_cst.Module hands the converter the TEXT itself and this node
    # parses it to core_syntax.parse_to_dict - same dict, a text residual
    # (gp["__origin__"]) in place of __cst__. The address / symbol tail below
    # is shared by both parsers.
    text_input = isinstance(input_value, str)
    if not text_input and not isinstance(input_value, cst.Module):
        print("Expected cst.Module, got", type(input_value).__name__, file=sys.stderr)
        return input_value
    _t_start = time.monotonic()
    _yield_slept.t = 0.0  # this conversion's cumulative yield-to-UI sleep
    # _source: the module's full code, when the caller already holds it (the
    # incremental span reconvert verified the splice against it) - skips a
    # whole-module codegen.
    source_code = input_value if text_input else (kwargs.get("_source") or input_value.code)
    _t_codegen = time.monotonic()
    if text_input:
        from meltygui.code.core_syntax import parse_to_dict
        # SAME src name scope as the libcst branch, so enum members / callables
        # resolve to the same live objects either way.
        with _module_scope(_build_src_scope()):
            readable = parse_to_dict(source_code)
    else:
        readable = GeneralParse(source=source_code)
        _stamp_span(readable, input_value)

        # Publish the src symbol scope so every nested name/callable resolution
        # below (values, classdef/funcdef defaults) resolves against the module
        # only - no per-usage sys.modules scan. Built once here; nested classdef /
        # funcdef conversions inherit it. The _position_map publishes a
        # PositionProvider for the same span so the child can stamp source spans
        # (.address / _child_spans) for the line ↔ node lookup.
        with _position_map(input_value, source=source_code), _module_scope(_build_src_scope()):
            # Retain this conversion's position map for the incremental span
            # merge (line statement + dec_start lookups against the PREVIOUS
            # parse). Held off-gp (id-keyed, weakref-finalized) so gp pickling
            # (the cst-dict cache) never sees it.
            _retain_pos_map(readable, _active_positions())
            # Module header comments (top-of-file, before first statement)
            _extract_comment_lines(input_value.header, readable)

            _classdef_to_dict = Melty._converters.get((cst.ClassDef, dict))
            _funcdef_to_dict = Melty._converters.get((cst.FunctionDef, dict))

            # Sibling keys let a bare top-level caller map its positional args to
            # parameter names; call_seen keys repeat calls (func()#1, ...).
            local_sigs = _collect_local_signatures(input_value.body)
            call_seen: dict[str, int] = {}

            # Incremental-merge bookkeeping (cst_dict_incremental_update): per
            # top-level statement: its source line span and how many gp keys
            # existed BEFORE it ran - so a later merge can identify exactly which
            # statements/keys a splice region owns. Each has len(body)+1
            # entries (final total appended after the loop).
            _stmt_lines = []
            _stmt_key_counts = []
            _positions_now = _active_positions() or {}

            for stmt in input_value.body:
                _yield_to_ui()  # back off mid-parse while the user can typing
                _stmt_key_counts.append(len(readable))
                _sp_stmt = _positions_now.get(stmt)
                if _sp_stmt is not None:
                    _dstart = _positions_now.get(("dec_start", stmt))
                    _stmt_lines.append((min(_sp_stmt.start_line, _dstart)
                                        if _dstart else _sp_stmt.start_line,
                                        _sp_stmt.end_line))
                else:
                    _stmt_lines.append((None, None))
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
                        # Surface a lone call so its args are visible/editable: a bare
                        # call (print(debug=True) OR a call assigned to a NON-Name
                        # target (changed, new_dict = check_collection(...)). The latter
                        # used to hit the Assign branch, fail the `isinstance Name` check,
                        # and surface NOTHING - the gap that made a lone call line parse
                        # to an empty dict. Mirrors _extract_block_assignments so a call
                        # statement surfaces the same at module level as in a method body.
                        call_node = _stmt_call_node(node)
                        if call_node is not None:
                            ck = _surface_call(call_node, readable, call_seen, local_sigs)
                            if ck is not None:
                                last_key = ck

                    # Trailing inline comment
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

            _stmt_key_counts.append(len(readable))
            readable._stmt_lines = _stmt_lines
            readable._stmt_key_counts = _stmt_key_counts
            readable["__cst__"] = input_value
            # readable.usages = _collect_usages(input_value, top_scope="<module>")
    _t_converted = time.monotonic()

    # `jump_to` (a resolved source address: file + line span) rides in via
    # **extra - the chain route forwards it from the host's child_kwargs. It
    # stamps the gp's on-disk identity so downstream passes (the editor's
    # site mapping, populate_symbol_usages) know where this parse lives.
    address = kwargs.get("jump_to")
    if address is not None and getattr(address, "path", None) is not None:
        readable.file_path = address.path
        readable.line_offset = getattr(address, "start", 0) or 0

    # Symbol-usage indexing: flat {name: SymbolUsage} for the whole span
    # (module-level symbols AND class members), distributed down the gp tree -
    # each GeneralParse node's __symbol_usages__ for its OWN keys, so a
    # member lands on its own node, not the class/module wrapper. Because the
    # gp IS what the editor sees, nothing downstream wires.
    #
    # It runs AUTOMATICALLY per parse (we're already in the chain's background
    # thread) when the fast jediless resolver is enabled and the cache warmer
    # (SymbolIndexCache) has completed a build - then a clean file is a dict
    # lookup and a changed file ~0.2s, cached per (mtime, index generation).
    # The accurate jedi resolver is far too slow to run per parse; it stays
    # behind the Refresh Index button (run_jedi), which also force-drops this
    # file's cached spans so a re-click is a true refresh.
    _sym_note = None
    if address is not None:
        from meltygui.core.runtime.toggles import Toggles  # lazy to avoid import cycle
        # The drag probe (max_wait=0) drops the auto pass while the user is
        # mid-gesture (a structured tint drag echoes through chain_in, which
        # would run this compute DURING the drag): the gp ships unstamped, and
        # the editor-side nudge re-indexes it the moment the drag ends.
        wants_auto = (Toggles.enable_jedi
                      and Toggles.TextEditor.SymbolUsages.auto_index
                      and not Toggles.jedi_correctness)
        auto = (wants_auto
                and _index_generation > 0
                # Never inside an incremental reconvert: the merge carries the
                # previous symbols (_carry_symbols), and one refresh lands us
                # input-quiet — running the pass here added 170-650ms to every
                # ~50-100ms merge.
                and getattr(_inc_memo, "pair", None) is None
                and _wait_for_no_drag(max_wait=0.0))
        if wants_auto and not auto:
            # WHY the parse ships unstamped: the two cases read very
            # differently in the timeline (no-gen: warmer hasn't built yet, the
            # editor-side nudge fires after the first bump; mid-drag: deferred).
            _sym_note = "skipped:no-gen" if _index_generation < 1 else "skipped:mid-drag"
        if run_jedi:
            print("Index refresh (manual) for", address.path)
            # Explicit force-refresh: the only caller that really wants a cold
            # pass with no seed/rescue reuse.
            invalidate_usage_cache(address.path, drop_spans=True)
        if run_jedi or auto:
            try:
                _t_sym0 = time.monotonic()
                flat = compute_symbol_usages_for_address(address)
                # Generation stamp even when flat is empty: marks "indexed
                # against the current generation" so the editor-side auto-index
                # nudge doesn't re-trigger on a span with no visible symbols.
                # Only when the index really landed - a typing-hold /
                # inflight-dedup fallback returns the stale prior uncached,
                # and stamping that froze the nudge on pre-edit symbols.
                if usages_fresh_for_address(address):
                    readable._symbol_gen = _index_generation
                if flat:
                    readable.symbol_usage = flat  # whole-module flat (debug access)
                    _distribute_by_name(readable, flat)
                _sym_note = f"{(time.monotonic() - _t_sym0) * 1000:.0f}ms/{len(flat)}syms"
            except Exception as _e:
                _sym_note = f"FAILED:{type(_e).__name__}"
    _ap = getattr(address, "path", None)
    _total_ms = (time.monotonic() - _t_start) * 1000
    _ptrace(f"cst→dict done in {_total_ms:.0f}ms",
            file=_Path(_ap).name if _ap else "<no address>",
            lines=source_code.count("\n") + 1,
            codegen=f"{(_t_codegen - _t_start) * 1000:.0f}ms",
            convert=f"{(_t_converted - _t_codegen) * 1000:.0f}ms",
            yielded=f"{getattr(_yield_slept, 't', 0.0) * 1000:.0f}ms",
            symbols=_sym_note or "off")
    _slept_ms = getattr(_yield_slept, 't', 0.0) * 1000
    _im = globals().get("_inc_memo")
    _inc_mark = (" meltygui" if text_input else
                 " inc" if (_im is not None
                            and getattr(_im, "pair", None) is not None) else "")
    _work_ms = _total_ms - _slept_ms
    notify(f"cst→dict{_inc_mark} {_total_ms:.0f}ms"
           + (f" (parked {_slept_ms:.0f}ms)" if _slept_ms >= 1 else "")
           + f"  {source_code.count(chr(10)) + 1} lines"
             f"  {_Path(_ap).name if _ap else 'no-address'}",
           tint=((1.0, 0.3, 0.2) if _work_ms >= 300
                 else (1.0, 0.65, 0.2) if _work_ms >= 60
           else (0.6, 0.75, 0.6)),
           tag="cst")
    return readable


# Largest changed region (chars) an incremental cst merge will re-convert;
# past it the full parse is cheaper-per-value and the fallback runs.
_INC_CST_MAX_REGION = 64 * 1024
# Force a full reconvert after this many incremental merges as a cheap backstop
# against deep metadata drift that per-merge text verification can't see.
_INC_CST_MAX_DEPTH = 200


def _carry_symbols(prev_gp, gp):
    """Hold-last-good for the SYMBOL layer, mirroring the cst-dict hold: a
    successful incremental merge builds a fresh gp, which would otherwise
    ship symbol-less — washes/links vanish per merge until a full re-index.
    Carry the previous parse's flat {symbol: SymbolUsage} map so flat
    consumers keep working instantly. _symbol_gen deliberately NOT carried
    (a current-looking stamp told the ensure pass "indexed and covered by
    the chain's reparse" — but merges skip the auto pass, so sites drifted
    and the span collector's verification dropped every wash). The per-node
    __symbol_usages__ maps are NOT redistributed here either: distribution
    into shared sub-dicts from the worker is the resize-during-render-
    iteration hazard — the flag below makes the editor's ensure pass run
    the frame-boundary attach instead."""
    flat = getattr(prev_gp, "symbol_usage", None)
    if isinstance(flat, dict) and flat:
        gp.symbol_usage = flat
        gp._needs_distribute = True


def _funcdef_span_incremental(prev_gp, old_mod, a, b, pre, suf, new_src):
    """Single-FunctionDef span buffers (a function edited in its own host):
    module-level splicing has nothing to split, so splice INSIDE the def —
    swap the changed body statements (region re-parsed under a throwaway
    `def` wrapper so its 4-space indentation and continuation whitespace stay
    byte-exact), then re-run the REAL cst_module_to_dict over the spliced
    module with the node-identity value memo primed from the previous
    conversion (_inc_memo / _cst_to_python_or_raw). Correctness is the full
    converter's own — it IS a full conversion of the true new module, so key
    numbering (`x#1`), ordering and spans come out exact; the memo only skips
    re-deriving values whose nodes survive the splice. The first reconvert
    after a plain full parse finds an empty memo and just seeds it."""
    from meltygui.core.runtime.toggles import Toggles
    try:
        fd = old_mod.body[0]
        body = list(getattr(fd.body, "body", None) or ())
        if not body:
            return _inc_fallback("span-no-body")
        pos = _pos_map_for(prev_gp)
        if pos is None:
            # A gp served from the pickle cache has no retained pos - one
            # ~60ms rebuild here beats the ~250ms full parse it forced, and
            # every later edit merges from the retained copy.
            pos = _build_ast_span_map(old_mod, "\n".join(a))
            if not pos:
                return _inc_fallback("span-no-pos-map")
            _retain_pos_map(prev_gp, pos)
        na, nb = len(a), len(b)
        delta = nb - na
        first_row, last_row = pre + 1, na - suf
        fd_span = pos.get(fd)
        if fd_span is None:
            return _inc_fallback("span-unplaced-def")

        def text_start(idx):
            st = pos.get(body[idx])
            if st is None:
                return None
            s0 = st.start_line
            d0 = pos.get(("dec_start", body[idx]))
            if d0:
                s0 = min(s0, d0)
            return s0 - len(body[idx].leading_lines)  # 1-based

        j0 = None
        for j in range(len(body)):
            sp = pos.get(body[j])
            if sp is None:
                return _inc_fallback("span-unplaced-stmt")
            if sp.end_line >= first_row:
                j0 = j
                break
        if j0 is None:
            # Edit entirely BELOW the last statement: trailing blank lines /
            # comments - MODULE FOOTER territory. Rebuild just the footer;
            # every span and key is untouched, and the merged gp is a cheap
            # re-wrap of the previous one with a fresh identity.
            tail_start = fd_span.end_line          # 0-based first tail row
            if first_row <= tail_start:
                return _inc_fallback("span-after-last-stmt")
            tail_text = "\n".join(b[tail_start:])
            try:
                tail_mod = cst.parse_module(tail_text)
            except cst.ParserSyntaxError:
                notify("cst merge: held last-good (region unparseable)",
                       tint=(1.0, 0.65, 0.2), tag="cst")
                return prev_gp
            if tail_mod.body:
                return _inc_fallback("span-tail-not-blank")
            new_mod = old_mod.with_changes(footer=tail_mod.header)
            if Toggles.TextEditor.verify_incremental_cst:
                if new_mod.code != new_src:
                    return _inc_fallback("span-verify-mismatch")
            merged = GeneralParse(source=new_src)
            merged.update(prev_gp)
            merged["__cst__"] = new_mod
            for _attr in ("file_path", "line_offset", "_child_spans",
                          "_stmt_lines", "_stmt_key_counts", "_value_memo"):
                if hasattr(prev_gp, _attr):
                    setattr(merged, _attr, getattr(prev_gp, _attr))
            _sp_prev = getattr(prev_gp, "span", None)
            if isinstance(_sp_prev, Span):
                merged.span = Span(_sp_prev.start_line, _sp_prev.start_col,
                                   _sp_prev.end_line + delta, _sp_prev.end_col)
            _retain_pos_map(merged, pos)
            _carry_symbols(prev_gp, merged)
            return merged
        ts0 = text_start(j0)
        if ts0 is None:
            return _inc_fallback("span-unplaced-stmt")
        if first_row < ts0:
            return _inc_fallback("span-signature-edit")
        j1 = j0
        while j1 < len(body):
            spj = pos.get(body[j1])
            if spj is None:
                return _inc_fallback("span-unplaced-stmt")
            if spj.start_line > last_row:
                break
            j1 += 1
        if j1 == j0:
            j1 = j0 + 1
        if j1 < len(body):
            ts1 = text_start(j1)
            if ts1 is None:
                return _inc_fallback("span-unplaced-next")
            hi_excl = ts1 - 1  # 0-based exclusive
        else:
            hi_excl = fd_span.end_line  # def's last line (1b) == 0bexcl
        lo = ts0 - 1  # 0-based inclusive
        eof_region = False
        if j1 >= len(body) and last_row > hi_excl:
            # The edit reaches past the last statement's text (Enter at the
            # bottom, trailing blanks): take the region to end-of-text; the
            # wrapper's footer becomes the module footer below.
            hi_excl = na
            eof_region = True
        if last_row > hi_excl or lo >= hi_excl:
            return _inc_fallback("span-bounds")
        new_hi = hi_excl + delta
        if new_hi <= lo or new_hi > nb:
            return _inc_fallback("span-bounds")
        region_text = "\n".join(b[lo:new_hi])
        if len(region_text) > _INC_CST_MAX_REGION:
            return _inc_fallback("span-region-too-big")
        # Rows are newline-terminated by EOF: when the region stops short of
        # EOF the separator newline to the next row must ALWAYS be appended -
        # an endswith guard missed it for blank-tailed regions and dropped
        # one newline per splice (span-verify-mismatch on Enter-at-bottom).
        if new_hi < nb:
            region_text += "\n"
        wrap_text = "def __melty_inc_wrap__():\n" + region_text
        try:
            wrap_mod = cst.parse_module(wrap_text)
        except cst.ParserSyntaxError:
            # Mid-p-ping broken region: a full reparse of the buffer would
            # fail identically, so there's nothing to (re)convert - keep the
            # previous gp intact as this run's result. The caller's compile
            # check still reports the error (accurate line, and src_good
            # stays None so the baseline doesn't advance); the first update
            # that parses again runs correctly against the last-good
            # baseline, covering the whole accumulated region.
            notify("cst merge: held last-good (region unparseable)",
                   tint=(1.0, 0.65, 0.2), tag="cst")
            return prev_gp
        if wrap_mod.code != wrap_text:
            return _inc_fallback("span-region-not-lossless")
        if (len(wrap_mod.body) != 1
                or not isinstance(wrap_mod.body[0], cst.FunctionDef)):
            return _inc_fallback("span-wrap-shape")
        region_stmts = list(wrap_mod.body[0].body.body)
        if not region_stmts:
            return _inc_fallback("span-empty-region")
        new_inner = list(body[:j0]) + region_stmts + list(body[j1:])
        new_fd = fd.with_changes(body=fd.body.with_changes(body=new_inner))
        if eof_region:
            new_mod = old_mod.with_changes(body=[new_fd],
                                           footer=wrap_mod.footer)
        elif wrap_mod.footer and j1 >= len(body):
            # Region ends at the def's last statement but its text carried
            # trailing blank lines (Enter at the bottom of the function) -
            # those parse into the wrapper's footer and are module-footer
            # territory here: prepend to the kept footer or they vanish.
            new_mod = old_mod.with_changes(
                body=[new_fd],
                footer=list(wrap_mod.footer) + list(old_mod.footer))
        else:
            new_mod = old_mod.with_changes(body=[new_fd])
        if Toggles.TextEditor.verify_incremental_cst:
            if new_mod.code != new_src:
                return _inc_fallback("span-verify-mismatch")
        old_memo = getattr(prev_gp, "_value_memo", None) or {}
        new_memo = {}
        # Seed the codegen memo for the WHOLE spliced FunctionDef: it's a
        # fresh node every merge, so cst_funcdef_to_dict's
        # FunctionParse.source codegen (~27ms on draw_text) missed every
        # time. Its code is just the (verified) new source minus the
        # module header/footer renderings.
        try:
            _blank = cst.Module(body=[])
            _hdr = "".join(_blank.code_for_node(l) for l in new_mod.header)
            _ftr = "".join(_blank.code_for_node(l) for l in new_mod.footer)
            _fd_code = new_src[len(_hdr): (len(new_src) - len(_ftr)) or None]
            new_memo[("c", id(new_fd))] = (new_fd, _fd_code)
        except Exception:
            pass
        _inc_memo.pair = (old_memo, new_memo)
        _inc_memo.pending = []
        try:
            gp = cst_module_to_dict(new_mod, _source=new_src)
        finally:
            _inc_memo.pair = None
            _pending_shifts = getattr(_inc_memo, "pending", None) or []
            _inc_memo.pending = None
        if not isinstance(gp, GeneralParse):
            return _inc_fallback("span-convert-failed")
        gp._value_memo = new_memo
        # Apply the pending memo re-anchors now that the conversion is a
        # confirmed success - one tight loop of integer span writes instead
        # of scattered mutation across the whole (interleavable) walk.
        for _val, _fresh in _pending_shifts:
            _stale = getattr(_val, "span", None)
            if isinstance(_stale, Span):
                _d = _fresh.start_line - _stale.start_line
                if _d:
                    _shift_gp_spans([_val], [], _d)
            else:
                _val.span = _fresh
        for attr in ("file_path", "line_offset"):
            if hasattr(prev_gp, attr):
                setattr(gp, attr, getattr(prev_gp, attr))
        _carry_symbols(prev_gp, gp)
        return gp
    except Exception as _e:
        return _inc_fallback(f"span-exception:{type(_e).__name__}")


def _shift_gp_spans(roots, extra_spans, delta):
    """Shift every Span reachable from `roots` (gp subtrees) by `delta` lines,
    in place, each span once (spans are shared between .span attrs and
    _child_spans maps — the visited set covers both). Only dict/list/tuple
    containers are descended; __cst__ values are skipped (libcst nodes carry
    no absolute positions)."""
    if not delta:
        return
    seen = set()

    def bump(sp):
        if isinstance(sp, Span) and id(sp) not in seen:
            seen.add(id(sp))
            sp.start_line += delta
            sp.end_line += delta

    for sp in extra_spans:
        bump(sp)
    stack = list(roots)
    while stack:
        o = stack.pop()
        oid = id(o)
        if oid in seen:
            continue
        seen.add(oid)
        bump(getattr(o, "span", None))
        cs = getattr(o, "_child_spans", None)
        if isinstance(cs, dict):
            for v in cs.values():
                bump(v)
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "__cst__":
                    continue
                bump(getattr(k, "span", None))  # Comment keys carry spans
                if isinstance(v, (dict, list, tuple)) or hasattr(v, "span") \
                        or hasattr(v, "_child_spans"):
                    stack.append(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                if isinstance(v, (dict, list, tuple)) or hasattr(v, "span"):
                    stack.append(v)


_gp_pos_maps = {}


def _retain_pos_map(gp, pos):
    """{cst node: Span} of a finished conversion, kept per gp id. Weakref
    finalize evicts with the gp; the size cap is a backstop for anything
    non-weakref-able."""
    if pos is None:
        return
    if len(_gp_pos_maps) > 64:
        _gp_pos_maps.clear()
    key = id(gp)
    _gp_pos_maps[key] = pos
    try:
        import weakref
        weakref.finalize(gp, _gp_pos_maps.pop, key, None)
    except TypeError:
        pass


def _pos_map_for(gp):
    return _gp_pos_maps.get(id(gp))


_WS_ONLY_LINE = _re_ws.compile(r"(?m)^[ \t]+$")


def _norm_blank_lines(text):
    """textwrap.dedent's blank-line normalization, alone: whitespace-only
    lines become empty. string_to_cst_module dedents every buffer, so the
    parsed module's code differs from the RAW input in exactly this way —
    the incremental merge must diff/splice/verify in the same normalized
    space, or any buffer containing a trailing-space blank line hard-fails
    verification on every edit (observed live as span-verify-mismatch on
    draw_text). Line count is unchanged, so spans are unaffected."""
    return _WS_ONLY_LINE.sub("", text)


def _inc_fallback(reason):
    """The incremental cst merge bailed — say why in the "cst" column so a
    stream of full parses is diagnosable at a glance. Returns None (the
    caller's fallback-to-full-parse sentinel)."""
    notify(f"cst merge fallback: {reason}", tint=(0.75, 0.6, 0.35), tag="cst")
    return None


def cst_dict_incremental_update(prev_gp, old_src, new_src):
    """O(edited statements) replacement for a full cst→dict reconvert.

    Diffs old→new source by lines, maps the changed rows onto whole top-level
    STATEMENTS via the tables the full parse stamped (_stmt_lines /
    _stmt_key_counts), re-parses and re-converts just those statements'
    text, and splices the result into the previous GeneralParse:

      * gp keys: [keys of stmts before] + [fresh region keys] + [keys after],
        preserving statement order (dict order IS statement order downstream);
      * module __cst__: old module with the region's body statements swapped
        in (libcst is lossless, so dict_to_cst_module round-trips the REAL
        new text — verified below);
      * spans: fresh region entries shift region→module coordinates; kept
        entries below the edit shift by the line delta, in place.

    Returns the merged gp, or None → caller runs the normal full conversion.
    Fidelity gate: when Toggles.TextEditor.verify_incremental_cst is on, the
    spliced module must regenerate EXACTLY the new source (one O(file)
    codegen, ~a sixth of the full-parse cost) — any header/footer/comment
    attribution drift falls back to the full parse instead of corrupting the
    round-trip. All validation happens before any in-place mutation."""
    from meltygui.core.runtime.toggles import Toggles
    try:
        # Normalize FIRST (see _norm_blank_lines): the held module was parsed
        # from dedent-normalized text, so raw-space diffs would misplace the
        # region and the byte-exact verification could never pass.
        old_src = _norm_blank_lines(old_src)
        new_src = _norm_blank_lines(new_src)
        table = getattr(prev_gp, "_stmt_lines", None)
        counts = getattr(prev_gp, "_stmt_key_counts", None)
        old_mod = prev_gp.get("__cst__")
        if (not table or not counts or old_mod is None
                or not isinstance(old_src, str) or not isinstance(new_src, str)
                or len(counts) != len(table) + 1
                or len(table) != len(old_mod.body)):
            return _inc_fallback("no-tables")
        depth = getattr(prev_gp, "_inc_depth", 0)
        if depth >= _INC_CST_MAX_DEPTH:
            return _inc_fallback("depth-cap")
        a = old_src.split("\n")
        b = new_src.split("\n")
        na, nb = len(a), len(b)
        pre = 0
        m = min(na, nb)
        while pre < m and a[pre] == b[pre]:
            pre += 1
        if pre == na and pre == nb:
            # Normalized-identical: the RAW texts differ only in whitespace-
            # only lines, which the parser blanks anyway - the held gp IS the
            # correct result. Serving it (rather than falling through to a
            # ~250ms full parse of identical content) also advances the
            # caller's src_good baseline past the ws-only churn.
            return prev_gp  # identical - the safe-skip handle
        suf = 0
        while suf < (na - pre) and suf < (nb - pre) and a[na - 1 - suf] == b[nb - 1 - suf]:
            suf += 1
        delta = nb - na
        first_row = pre + 1  # 1-based first changed old row
        last_row = na - suf  # 1-based last changed old row
        # Statement range [i0, i1) covers the changed rows - every span in
        # the region must be known, and the edit must not reach into the
        # module header (i0 == 0 file-start) or past the last statement.
        i0 = None
        for i, (s0, e0) in enumerate(table):
            if s0 is None:
                continue
            if e0 >= first_row:
                i0 = i
                break
        if not i0:  # None or 0: header-adjacent
            if (len(table) == 1
                    and isinstance(old_mod.body[0], cst.FunctionDef)):
                # A function edited in its own span host: one top-level
                # statement - splice INSIDE the def instead.
                return _funcdef_span_incremental(
                    prev_gp, old_mod, a, b, pre, suf, new_src)
            return _inc_fallback("first-stmt-or-header")
        i1 = i0
        while i1 < len(table):
            s1, e1 = table[i1]
            if s1 is None:
                return _inc_fallback("unplaced-stmt")
            if s1 > last_row:
                break
            i1 += 1
        if i1 == i0:
            i1 = i0 + 1  # gap-only edit: the gap is the next stmt's leading lines

        def _stmt_text_start(idx):
            # 0-based line index where statement idx's LIBCST text begins. The ast
            # span starts on the code line, but libcst attaches the preceding
            # blank/comment lines to the node as leading_lines - the true
            # inter-statement boundary lies above them (an ast-end boundary
            # would leave those lines in BOTH the region and the kept node,
            # duplicating them on merge).
            s0 = table[idx][0]
            if s0 is None:
                return None
            return s0 - 1 - len(old_mod.body[idx].leading_lines)

        old_lo = _stmt_text_start(i0)
        if old_lo is None or old_lo < 0:
            return _inc_fallback("bad-region-start")
        # i1 was picked by CODE start (s1 > last_row), but the region boundary
        # sits ABOVE stmt i1's leading blank/comment lines. An edit that touches
        # a statement's tail AND the gap below it (delete/paste across a
        # blank line, a coalesced burst of keystrokes) has last_row inside
        # the leading lines - widen the region to take stmt i1 too, else
        # the changed rows fall outside it (was: footer-or-degenerate).
        while i1 < len(table):
            hi = _stmt_text_start(i1)
            if hi is None:
                return _inc_fallback("unplaced-next-stmt")
            if last_row <= hi:
                break
            i1 += 1
        if i1 < len(table):
            old_hi_excl = _stmt_text_start(i1)  # 0-based exclusive old end
            if old_hi_excl is None:
                return _inc_fallback("unplaced-next-stmt")
        else:
            old_hi_excl = na  # region runs to end of file
        if last_row > old_hi_excl or old_lo >= old_hi_excl:
            return _inc_fallback("footer-or-degenerate")
        new_hi = old_hi_excl + delta  # exclusive 0-based end of new text
        if new_hi <= old_lo or new_hi > nb:
            return _inc_fallback("bounds")
        region_lines = b[old_lo:new_hi]
        region_text = "\n".join(region_lines)
        if len(region_text) > _INC_CST_MAX_REGION:
            return _inc_fallback("region-too-big")
        if new_hi < nb:
            region_text += "\n"
        try:
            region_mod = cst.parse_module(region_text)
        except cst.ParserSyntaxError:
            # Same hold as the span path: broken region → nothing derivable
            # from a full parse either; serve the previous gp and let the
            # integrity check report the failure.
            notify("cst merge: held last-good (region unparseable)",
                   tint=(1.0, 0.65, 0.2), tag="cst")
            return prev_gp
        # Basic fidelity gate: the region itself must be lossless standalone
        # (a region whose text leaks into module body/footer would drift).
        if region_mod.code != region_text:
            return _inc_fallback("region-not-lossless")
        region_body = list(region_mod.body)
        if not region_body:
            return _inc_fallback("empty-region")
        # The region's leading gap lines (blanks/comments before its first
        # statement) parse into Module.header - a body-only splice would lose
        # them. Fold them into the first statement's leading_lines (both are
        # EmptyLineNodes). Symmetrically, region tail lines that parse
        # into Module.footer fold into the NEXT kept statement's leading
        # lines (or the module footer when the region runs to end of file).
        # Any mis-attribution these folds could cause fails the full-text
        # verification below and falls back to the full parse.
        if region_mod.header:
            region_body[0] = region_body[0].with_changes(
                leading_lines=list(region_mod.header)
                              + list(region_body[0].leading_lines))
        tail_body = list(old_mod.body[i1:])
        if region_mod.footer and i1 < len(table):
            if not tail_body:
                return _inc_fallback("footer-no-home")
            tail_body[0] = tail_body[0].with_changes(
                leading_lines=list(region_mod.footer)
                              + list(tail_body[0].leading_lines))
        new_body = list(old_mod.body[:i0]) + region_body + tail_body
        if i1 < len(table):
            new_mod = old_mod.with_changes(body=new_body)
        else:
            new_mod = old_mod.with_changes(body=new_body,
                                           footer=region_mod.footer)
        if Toggles.TextEditor.verify_incremental_cst:
            if new_mod.code != new_src:
                return _inc_fallback("verify-mismatch")
        # Convert the FOLDED region, not the raw parse: standalone, a statement's
        # leading comment lines sit in region_mod.header, and the header
        # extractor keys every comment as a module-level Comment and treats a
        # `# [...]` line as a module override - while the full parse skipped
        # that header line (a def's/class's leading override routes via the
        # CHILD's __overrides__). Left raw, the region minted a stray Comment +
        # `__overrides__` key at module level (draw_any's tint override): the
        # first merge silently dropped the child override and the next edit's
        # region minted `__overrides__` again → incremental-collision fallback.
        # Same code (header + leading_lines render identically), so spans and
        # the verified merge are unaffected.
        region_gp = cst_module_to_dict(
            region_mod.with_changes(header=[], body=region_body)
            if region_mod.header else region_mod)
        if not isinstance(region_gp, GeneralParse):
            return _inc_fallback("region-convert-failed")
        rc = getattr(region_gp, "_stmt_key_counts", None)
        rt = getattr(region_gp, "_stmt_lines", None)
        if rc is None or rt is None:
            return _inc_fallback("region-tables-missing")
        k0, k1, total_old = counts[i0], counts[i1], counts[-1]
        old_keys = list(prev_gp.keys())
        region_total = rc[-1]
        region_keys = [k for k in list(region_gp.keys())[:region_total]]
        # Assemble the merge plan first; any collision → fall back (dict
        # re-assignment would silently keep the FIRST position and scramble
        # statement order downstream).
        before_keys = old_keys[:k0]
        after_keys = old_keys[k1:total_old]
        tail_keys = old_keys[total_old:]
        plan = before_keys + region_keys + after_keys + tail_keys
        if len(set(map(id, plan))) != len(plan):
            return _inc_fallback("key-collision-id")
        seen_names = set()
        for k in plan:
            if isinstance(k, str):
                if k in seen_names:
                    return _inc_fallback(f"key-collision-name {str(k)[:40]!r}")
                seen_names.add(k)
        # ── build (no fallback past this point mutates shared data yet) ──
        merged = GeneralParse(source=new_src)
        for attr in ("file_path", "line_offset"):
            if hasattr(prev_gp, attr):
                setattr(merged, attr, getattr(prev_gp, attr))
        merged.span = Span(1, 0, nb, 0)
        merged._inc_depth = depth + 1
        for k in before_keys:
            merged[k] = prev_gp[k]
        for k in region_keys:
            merged[k] = region_gp[k]
        for k in after_keys:
            merged[k] = prev_gp[k]
        for k in tail_keys:
            merged[k] = new_mod if k == "__cst__" else prev_gp[k]
        if "__cst__" not in merged:
            merged["__cst__"] = new_mod
        # child-span map recombined from both sources (before/after entries
        # share Span objects with the previous subtrees - the shift walk's
        # visited set keeps each only once)
        prev_cs = getattr(prev_gp, "_child_spans", None) or {}
        region_cs = getattr(region_gp, "_child_spans", None) or {}
        merged_cs = {}
        for k in before_keys + after_keys:
            if k in prev_cs:
                merged_cs[k] = prev_cs[k]
        for k in region_keys:
            if k in region_cs:
                merged_cs[k] = region_cs[k]
        if merged_cs:
            merged._child_spans = merged_cs
        # statement tables for the NEXT merge
        len_r = len(rt)
        merged._stmt_lines = (
                table[:i0]
                + [((s0 + old_lo, e0 + old_lo) if s0 is not None else (None, None))
                   for (s0, e0) in rt]
                + [((s0 + delta, e0 + delta) if s0 is not None else (None, None))
                   for (s0, e0) in table[i1:]])
        merged._stmt_key_counts = (
                counts[:i0]
                + [k0 + c for c in rc[:-1]]
                + [k0 + region_total + (c - k1) for c in counts[i1:]])
        # ── span shifts (in place; shared state - visited-set guarded) ──
        _shift_gp_spans([region_gp[k] for k in region_keys if k in region_gp],
                        [region_cs[k] for k in region_keys if k in region_cs],
                        old_lo)
        _shift_gp_spans([prev_gp[k] for k in after_keys if k in prev_gp],
                        [prev_cs[k] for k in after_keys if k in prev_cs],
                        delta)
        return merged
    except Exception as _e:
        return _inc_fallback(f"exception:{type(_e).__name__}")


@register
def dict_to_cst_module(input_value: dict) -> cst.Module:
    """Rebuild from __cst__, patching in any edited values.

    Handles assignments, ClassDef __init__ self-assignments, and
    decorator keyword arguments.
    """
    if input_value.get("__origin__") is not None:
        # core_syntax parse: the fallback does text surgery on the existing
        # source (see core_syntax.general_parse_to_str) - returns a STR,
        # which cst_module_to_str / cst_module_to_string pass through.
        from meltygui.code.core_syntax import general_parse_to_str
        from meltygui.code.core_syntax import CoreSyntaxError
        try:
            return general_parse_to_str(input_value)
        except CoreSyntaxError as e:
            return Pending(wrapped=ParseError(
                source=e.text, error=str(e), line=e.lineno, column=e.offset,
            ), originated=dict_to_cst_module, state=PendingState.ERROR, status=str(e))
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

def _cst_key_to_python(node):
    """Dict-key conversion: like _cst_to_python, but an unresolvable key
    expression falls back to its raw source as a CodeLine instead of
    _UNREADABLE. An out-of-scope name key (`Any:` in a Mode entry — typing
    isn't in the src scope) used to drop the whole element, collapsing a
    single-entry dict to all-dunder and thus a raw CodeLine, hiding the
    value from every parse consumer (e.g. the mode-kwargs matrix). Shared
    by BOTH directions so the read key and the patch key always agree —
    a mismatch would re-append the element as a duplicate on save."""
    key = _cst_to_python(node)
    if key is _UNREADABLE:
        return CodeLine(_cst_node_to_code(node))
    return key


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
        key = _cst_key_to_python(el.key)
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
            key = _cst_key_to_python(el.key)
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

def _base_last_name(node) -> str:
    """The trailing identifier of a base/metaclass expression: `Enum` for both a
    bare `Enum` Name and a dotted `enum.Enum` Attribute. "" for anything else."""
    while isinstance(node, cst.Attribute):
        node = node.attr
    return node.value if isinstance(node, cst.Name) else ""


def _classdef_is_enum(value: cst.ClassDef) -> bool:
    """True if a ClassDef is (syntactically) an enum.

    Purely name-based — at parse time we only have the source, no live MRO — so a
    base or metaclass whose trailing name ends in 'Enum'/'Flag' (Enum, IntEnum,
    StrEnum, IntFlag, Flag, the project's RelaxedEnum, …) counts, plus the
    EnumMeta/EnumType metaclasses. A mixed-in base (`class C(str, Enum)`) still
    matches on its Enum base. Good enough for routing/styling — false positives are
    only cosmetic, and the codebase's enums all subclass Enum/RelaxedEnum."""

    def _enumish(name: str) -> bool:
        return name.endswith("Enum") or name.endswith("Flag")

    for base in value.bases:
        if _enumish(_base_last_name(base.value)):
            return True
    for kw in value.keywords:
        if isinstance(kw.keyword, cst.Name) and kw.keyword.value == "metaclass":
            n = _base_last_name(kw.value)
            if _enumish(n) or n in ("EnumMeta", "EnumType"):
                return True
    return False


@register
def cst_classdef_to_dict(value: cst.ClassDef) -> dict:
    """Extract readable fields from a class definition.

    Handles three patterns:
      1. body-level Assign:     invalidate_stack_trace = False
      2. body-level AnnAssign:  debug: bool = False  (dataclass fields)
      3. __init__ self.X = literal  (traditional classes)

    Comments are extracted via Comment keys.
    Decorators go in a "decorators" sub-dict.

    An enum ClassDef produces an EnumParse (a ClassParse subclass) so enums are
    distinguishable by type; everything else produces a ClassParse.
    """
    parse_type = EnumParse if _classdef_is_enum(value) else ClassParse
    readable = parse_type(source=_cst_node_to_code(value))
    readable.def_name = value.name.value
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
        _yield_to_ui()  # back off mid-class while the user is typing
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

    # Dict key order drives the body order: a popped-and-reinserted key moves its
    # statement. Runs after injection so a brand-new field also lands at its
    # dict position instead of the end-of-body slot _inject_class_fields used.
    result = _reorder_class_body(result, value)

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


def _statement_member_key(stmt):
    """Dict key cst_classdef_to_dict surfaces a class-body statement under, or
    None for statements with no own dict entry (docstring, bare annotations,
    __init__, bare call statements). Mirrors the forward pass's extraction so
    reordering only ever touches statements the dict actually represents."""
    if isinstance(stmt, cst.SimpleStatementLine):
        for node in stmt.body:
            if isinstance(node, cst.AnnAssign) and node.value is None:
                continue  # bare annotation - passes through as __cst__
            nm = _assign_field_name(node, in_init=False)
            if nm is not None:
                return nm
        return None
    if isinstance(stmt, cst.ClassDef):
        return stmt.name.value
    if isinstance(stmt, cst.FunctionDef) and stmt.name.value != "__init__":
        return stmt.name.value
    return None


def _reorder_class_body(classdef: cst.ClassDef, value: dict) -> cst.ClassDef:
    """Permute keyed class-body statements to match the dict's key order.

    Each statement with a dict key (field assignment, method, nested class) is
    a movable unit; its leading_lines (comments, blank lines) and trailing
    override comment travel with it. Unkeyed statements keep their original
    slots, so the docstring stays first and __init__ stays put. Keys with no
    body statement (e.g. __init__ self.X fields) are ignored."""
    order = {k: i for i, k in enumerate(value)
             if isinstance(k, str) and not isinstance(k, Comment)
             and not _is_dunder(k) and k != "decorators"}
    body = list(classdef.body.body)
    slots = [i for i, stmt in enumerate(body)
             if _statement_member_key(stmt) in order]
    if len(slots) < 2:
        return classdef
    stmts = sorted((body[i] for i in slots),
                   key=lambda s: order[_statement_member_key(s)])
    if all(body[i] is s for i, s in zip(slots, stmts)):
        return classdef
    for i, s in zip(slots, stmts):
        body[i] = s
    return classdef.with_changes(
        body=classdef.body.with_changes(body=tuple(body)))


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
    readable = FunctionParse(source=_cst_node_to_code(value))
    readable.def_name = value.name.value
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
        _yield_to_ui()  # back off mid-parse while the user is typing
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


# ─── Multi-line comment blocks ────────────────────────────────────────────────
# Consecutive, directly-adjacent comment lines (no blank line, no code between
# them) surface as a SINGLE Comment whose text is the '\n'-joined lines. The
# reverse (_rebuild_comment_lines) splits on '\n' back into one `#` EmptyLine
# per line. A blank line or a code statement breaks the run, so distinct
# paragraphs stay separate. An override `# [...]` comment is always its own
# group - including one split across several `#` lines for readability
# (`# [a=1,` / `# b=2]`), which only parses as an override when joined - so
# paragraphs and overrides never merge.


def _override_run_end(lines, i, end):
    """The exclusive end of a (possibly multi-line) override comment starting
    at lines[i], or None if lines[i] doesn't start one. Tries the shortest
    extent first, so a complete single-line override never absorbs the line
    below it. Cheap gates (`[` opener / `]` closer) bound the ast parsing."""
    if not lines[i].comment.value.lstrip("#").strip().startswith("["):
        return None
    for j in range(i, end):
        if lines[j].comment.value.rstrip().endswith("]"):
            joined = "\n".join(ll.comment.value for ll in lines[i:j + 1])
            if _parse_override_comment(joined) is not None:
                return j + 1
    return None


def _comment_line_groups(lines):
    """Yield (start, end, run) for each group of consecutive comment
    EmptyLines in `lines` (end exclusive, run == lines[start:end]).

    Blank lines and non-comment lines act as separators and never belong to a
    group. Within a run of comment lines, each override comment (single- or
    multi-line, see _override_run_end) is its own group; the plain lines
    around it group into comment blocks. A lone comment line is a group of
    one, so single comments round-trip exactly as before.
    """

    def _is_comment(ll):
        return isinstance(ll, cst.EmptyLine) and ll.comment is not None

    i, n = 0, len(lines)
    while i < n:
        if not _is_comment(lines[i]):
            i += 1
            continue
        j = i + 1
        while j < n and _is_comment(lines[j]):
            j += 1
        k = plain = i
        while k < j:
            ov_end = _override_run_end(lines, k, j)
            if ov_end is None:
                k += 1
                continue
            if plain < k:
                yield plain, k, lines[plain:k]
            yield k, ov_end, lines[k:ov_end]
            k = plain = ov_end
        if plain < j:
            yield plain, j, lines[plain:j]
        i = j


def _extract_comment_lines(lines, result, skip_overrides=False):
    """Surface comments from a sequence of leading/header lines into `result`.

    Adjacent plain comment lines collapse into one multi-line Comment (see
    _comment_line_groups); a lone comment stays a single-line Comment. An
    override `# [...]` comment — single-line or split across several lines —
    is its own Comment and is routed into __overrides__ via
    _merge_override_comment (unless skip_overrides drops it).
    """
    for _start, _end, run in _comment_line_groups(lines):
        text = "\n".join(ll.comment.value for ll in run)
        if skip_overrides and _parse_override_comment(text) is not None:
            continue
        c = Comment(text)
        result[c] = c
        _merge_override_comment(c, result)


def _extract_leading_comments(stmt, result, skip_overrides=False):
    """Extract standalone comment lines from a statement's leading_lines.

    skip_overrides leaves '# [...]' comments out of `result` — used when the
    statement is a nested class/function, whose leading override comment is
    routed to the child's own __overrides__ via _attach_leading_override.
    """
    _extract_comment_lines(getattr(stmt, "leading_lines", ()), result,
                           skip_overrides=skip_overrides)


def _attach_leading_override(stmt, child_dict):
    """Route a leading '# [...]' comment (single- or multi-line) above a nested
    class/function into that child's __overrides__ (the first one wins).

    The child's __overrides__ may ALREADY exist — its body conversion creates
    it for field-slot comments (__<field>__ entries). Bailing on that (the old
    guard) silently dropped the class's OWN leading comment on any class with
    commented fields, so merge instead: field slots are namespaced (`__…__`)
    and never collide with the leading comment's plain keys; setdefault keeps
    body-side entries authoritative on the impossible overlap."""
    if not isinstance(child_dict, dict):
        return
    existing = child_dict.get("__overrides__")
    for _s, _e, run in _comment_line_groups(getattr(stmt, "leading_lines", ())):
        parsed = _parse_override_comment("\n".join(ll.comment.value for ll in run))
        if parsed:
            if isinstance(existing, dict):
                # First leading comment wins: bail if a body run (or an
                # earlier attach) already placed plain keys.
                if any(not (isinstance(k, str) and k.startswith("__"))
                       for k in existing):
                    return
                for k, v in parsed.items():
                    existing.setdefault(k, v)
            else:
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
    for start, end, run in _comment_line_groups(lines):
        joined = "\n".join(ll.comment.value for ll in run)
        original = _parse_override_comment(joined)
        if original is None:
            continue
        if not current:
            # All overrides deleted → drop the comment line(s) entirely
            # rather than leave an empty `# []`.
            del lines[start:end]
            return node.with_changes(leading_lines=lines)
        if _override_changed(current, original):
            lines[start:end] = _rebuild_comment_block(
                _reformat_override_comment(joined, current), run)
            return node.with_changes(leading_lines=lines)
        return node
    if current:
        # No override comment exists yet (e.g. the input tab's + creates a
        # comment-less site) - so create one on a fresh leading line just
        # above the statement, after any plain comments.
        lines.append(cst.EmptyLine(
            comment=cst.Comment(_format_override_comment(current))))
        return node.with_changes(leading_lines=lines)
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
    for _s, _e, run in _comment_line_groups(getattr(stmt, "leading_lines", ())):
        parsed = _parse_override_comment("\n".join(ll.comment.value for ll in run))
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
# stored under result["__overrides__"]. The comment can be split across
# several `#` lines for readability (each continuation line is a literal `#`
# line; a line break is only valid where whitespace could go, so end lines at
# a comma). Parsing is best-effort: anything that doesn't fit the shape is
# left as an ordinary comment and never raises.


def _parse_override_comment(text):
    """Parse a '# [k=v, ...]' override comment into a dict, or None.

    `text` may span multiple '#' lines ('\\n'-joined, the grouped-Comment
    shape): each line's leading '#' is stripped and the bodies joined, so an
    override split across lines for readability parses like one long line.
    The bracketed body is read as keyword arguments (commas inside tuples,
    lists, etc. are respected) and each value is literal-eval'd. Returns None
    on any malformed input — callers treat None as "not an override comment".
    """
    if not isinstance(text, str):
        return None
    body = " ".join(ln.strip().lstrip("#").strip() for ln in text.split("\n")).strip()
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
            if kw.arg == "view_func" and isinstance(kw.value, (ast.Name, ast.Attribute)):
                reference = ast.unparse(kw.value)
                if not all(part.isidentifier() for part in reference.split(".")):
                    return None
                parsed[kw.arg] = reference
            else:
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
    if isinstance(value, int) and type(value) is not int:
        # int SUBCLASSES (TensorDim, IntEnum): repr would render the wrapper
        # ("TensorDim(1)"), which literal_eval can't read back - the whole
        # override comment would stop working. Store the plain number; the
        # reading edge re-specializes (ParamProxy) where the type matters.
        return repr(int(value))
    if isinstance(value, str) and type(value) is not str:
        # str SUBCLASSES (Lut): same deal - repr renders the wrapper
        # ("Lut('viridis')"), which literal_eval can't read back. Store the
        # plain string; the reading edge re-specializes (ParamProxy).
        return repr(str(value))
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
    """Render an overrides dict back into a '# [k=v, ...]' comment string,
    pairs in dict order — the comment IS that order."""
    parts = [f"{k}={_format_override_value(v)}" for k, v in overrides.items()
             if not _is_dunder(k)]
    return "# [" + ", ".join(parts) + "]"


def _override_changed(current, original):
    """Whether `current` (the edited pairs) differs from `original` (the pairs
    parsed from the comment) — by VALUE or by ORDER. An override comment is an
    ordered set of pairs, so a pure reorder is an edit that must rewrite it;
    dict `!=` alone is order-blind and let reorders evaporate on save."""
    return current != original or list(current) != list(original)


def _reformat_override_comment(original_text, overrides):
    """Render an updated overrides dict in ITS key order, preserving
    `original_text`'s line structure by slot: the comment's key positions are
    slots pinned to their lines, the surviving keys fill those slots in the
    dict's order (so a reorder moves keys between lines while each line keeps
    its pair count), a dropped key vacates its slot (a line losing every key
    disappears), and a brand-new key lands beside its predecessor in the dict
    (a key appended to the end rides the last line, an insert mid-dict its
    neighbour's line). A single-line comment stays single-line."""
    pairs = {k: v for k, v in overrides.items() if not _is_dunder(k)}
    lines = original_text.split("\n")
    original = _parse_override_comment(original_text)
    if len(lines) == 1 or not pairs or not original:
        return _format_override_comment(pairs)
    # Assign each original key to the line its `k=` sits on. Keys parse in
    # source order, so a forward-moving cursor keeps repeated text in a
    # key value from pushing a later key onto an earlier line; a key that
    # can't be matched falls to the last line. Mis-attribution only ever
    # shifts formatting - values always regenerate from `overrides`.
    slots = []      # line of each surviving key's slot, in SOURCE order
    li = pos = 0
    for key in original:
        pat = re.compile(rf"(?<!\w){re.escape(key)}\s*=")
        while li < len(lines):
            m = pat.search(lines[li], pos)
            if m is not None:
                pos = m.end()
                break
            li, pos = li + 1, 0
        if key in pairs:
            slots.append(min(li, len(lines) - 1))
    # Walk the DICT order: the i-th surviving key takes the i-th slot's line
    # (slots are non-decreasing, so lines stay monotone on the walk and
    # within-line order is the dict's), a new key its predecessor's line.
    per_line = [[] for _ in lines]
    slot_iter = iter(slots)
    line = 0
    for key in pairs:
        if key in original:
            line = next(slot_iter)
        per_line[line].append(key)
    rendered = [", ".join(f"{k}={_format_override_value(pairs[k])}" for k in keys)
                for keys in per_line if keys]
    if len(rendered) == 1:
        return "# [" + rendered[0] + "]"
    return "\n".join(("# [" if i == 0 else "# ") + part
                     + ("]" if i == len(rendered) - 1 else ",")
                     for i, part in enumerate(rendered))


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
    Leading/header lines yield per GROUP ('\\n'-joined, matching
    _extract_comment_lines), so a multi-line override reads as one text.
    """

    def _group_texts(lines):
        for _s, _e, run in _comment_line_groups(lines):
            yield "\n".join(ll.comment.value for ll in run)

    if isinstance(node, cst.Module):
        yield from _group_texts(node.header)
        stmts = node.body
    elif isinstance(node, (cst.ClassDef, cst.FunctionDef)) and isinstance(node.body, cst.IndentedBlock):
        # The node's own leading lines count too: a leading-line comment
        # above this class/function means body insertion should duplicate it.
        yield from _group_texts(getattr(node, "leading_lines", ()))
        stmts = node.body.body
    else:
        stmts = ()
    for stmt in stmts:
        yield from _group_texts(getattr(stmt, "leading_lines", ()))
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
            if _override_changed(current, original):
                # {} → remove the comment line(s) entirely (not `# []`).
                text_map[str(k)] = (_reformat_override_comment(str(k), current)
                                    if current else _REMOVE_COMMENT)
            break
    return text_map


def _patch_module_comments(module, comment_edits):
    """Patch comments on a cst.Module (header + body) by direct walk."""
    text_map = _collect_comment_edits(comment_edits)
    if not text_map:
        return module

    result = module

    # Patch header comments (a _REMOVE_COMMENT mapping drops the line)
    new_header, header_changed = _patch_comment_lines(list(module.header), text_map)
    if header_changed:
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
    if_idx = counters["if"];
    counters["if"] += 1
    key = f"if##{if_idx}"
    body = _extract_block_assignments(if_node.body.body)
    branch = Conditional(condition=key)
    cond_key = _condition_key(if_node.test, "if")
    branch[cond_key] = _condition_to_editable(if_node.test)
    branch.update(body)
    _merge_child_spans(branch, body)  # update() copies items, not _child_spans
    _record_child(branch, cond_key, branch[cond_key], if_node.test)
    _stamp_span(branch, _union_span([if_node.test, if_node.body]))
    result[key] = branch

    # Walk the orelse chain
    orelse = if_node.orelse
    while orelse is not None:
        if isinstance(orelse, cst.If):
            # elif
            elif_idx = counters["elif"];
            counters["elif"] += 1
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
            else_idx = counters["else"];
            counters["else"] += 1
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
def dict_to_cst_funcdef(value: dict, dangerous_reorder=True) -> cst.FunctionDef:
    """Patch decorators, parameter defaults, and body assignments.

    "decorators" sub-dict patches decorator kwargs.
    "parameters" sub-dict patches param defaults; its key order is the
    signature order (see _reorder_params). Positional reorders change what
    positional call sites mean; they are honoured by default — the user
    asked for the order — and dangerous_reorder=False drops them.
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
        # Runs outside the `if edits:` test: a pure reorder of no-default
        # params leaves `edits` empty but still has to move them.
        new_params = _reorder_params(result.params, param_edits, dangerous_reorder)
        if new_params is not result.params:
            result = result.with_changes(params=new_params)

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

    # This block's overrides override comments: a nested block dict (Loop/
    # Conditional/try branch) carries its statements' `# [...]` overrides in
    # its own __overrides__, exactly like the funcdef's locals do at the top
    # level (where dict_to_cst_funcdef applies overrides via
    # _apply_field_overrides). Without this, an edit that landed in a
    # loop-body site's comment text - the debug-view value windows for
    # it - silently never reached the source.
    _ov_stmts = _patch_field_overrides(new_stmts, edits.get("__overrides__")
                                       if isinstance(edits, dict) else None)
    if _ov_stmts is not None:
        new_stmts = _ov_stmts
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


def _rebuild_comment_block(new_text, run):
    """Split an edited multi-line comment string into one EmptyLine comment per
    line — the inverse of the join in _extract_comment_lines. The original run's
    first line is reused as a formatting template (indent/whitespace/newline) and
    each line is normalized to a valid comment value (leading whitespace stripped,
    a `# ` prefix added when missing; a blank line becomes a bare `#`)."""
    template = run[0]
    pieces = []
    for raw in new_text.split("\n"):
        line = raw.rstrip("\r\n").lstrip()
        if not line.startswith("#"):
            line = "# " + line if line else "#"
        pieces.append(template.with_changes(comment=cst.Comment(value=line)))
    return pieces


def _patch_comment_lines(lines, text_map):
    """Apply `text_map` (old_text -> new_text | _REMOVE_COMMENT) to a sequence of
    leading/header lines, mirroring _extract_comment_lines' grouping: a changed
    multi-line block (or multi-line override) is rebuilt into one `#` line per
    text line; a removed block drops every line in the run. Every comment line
    belongs to a group (a lone comment is a run of one), so only blank/other
    lines pass through ungrouped. Returns (new_lines, changed)."""
    groups = {start: (end, run) for start, end, run in _comment_line_groups(lines)}
    new_lines, changed, i, n = [], False, 0, len(lines)
    while i < n:
        if i in groups:
            end, run = groups[i]
            joined = "\n".join(ll.comment.value for ll in run)
            new_text = text_map.get(joined)
            if new_text is _REMOVE_COMMENT:
                changed = True
            elif new_text is not None:
                new_lines.extend(_rebuild_comment_block(new_text, run))
                changed = True
            else:
                new_lines.extend(run)
            i = end
            continue
        new_lines.append(lines[i])
        i += 1
    return new_lines, changed


def _patch_stmt_comments(stmt, text_map):
    """Patch leading and trailing comments on a statement by direct access."""
    result = stmt
    changed = False

    # Leading comments (EmptyLine nodes); a _REMOVE_COMMENT mapping drops the line
    if hasattr(result, "leading_lines") and result.leading_lines:
        new_lines, lead_changed = _patch_comment_lines(list(result.leading_lines), text_map)
        if lead_changed:
            result = result.with_changes(leading_lines=new_lines)
            changed = True

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
    if_idx = counters["if"];
    counters["if"] += 1
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
        elif_idx = counters["elif"];
        counters["elif"] += 1
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
        else_idx = counters["else"];
        counters["else"] += 1
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


def _reorder_params(params_node: cst.Parameters, value: dict,
                    dangerous_reorder: bool) -> cst.Parameters:
    """Permute parameters to match the parameters dict's key order.

    Reorders within each group (posonly / regular / kwonly) only — a param
    never crosses a `/` or `*` boundary. Keyword-only params are always safe
    to move (call sites must pass them by name) and reorder in both modes.
    Reordering positional params changes what positional call sites mean;
    dangerous_reorder (on by default at the registry dispatch) lets them
    move — flag off, the reorder is silently dropped and the next forward
    parse snaps the dict back to source order.
    Even in dangerous mode an order that would put a no-default param after a
    defaulted one (a SyntaxError) is refused. Params not in the dict
    (self/cls, *args/**kwargs) keep their slots."""
    order = {k: i for i, k in enumerate(value)
             if isinstance(k, str) and not isinstance(k, Comment)
             and not _is_dunder(k)}

    def permuted(param_list, keyword_only):
        slots = [i for i, p in enumerate(param_list) if p.name.value in order]
        if len(slots) < 2:
            return None
        moved = sorted((param_list[i] for i in slots),
                       key=lambda p: order[p.name.value])
        if all(param_list[i] is p for i, p in zip(slots, moved)):
            return None
        if not keyword_only and not dangerous_reorder:
            return None  # positional reorder - needs the dangerous flag
        out = list(param_list)
        for i, p in zip(slots, moved):
            # Keep the slot's separator/whitespace - a multi-line signature's
            # line structure and a comma-less final param survive the move -
            # only the param itself (name/annotation/default) travels.
            out[i] = p.with_changes(
                comma=param_list[i].comma,
                whitespace_after_param=param_list[i].whitespace_after_param)
        if not keyword_only:
            seen_default = False
            for p in out:
                if p.default is not None:
                    seen_default = True
                elif seen_default:
                    return None  # no-default after defaulted - SyntaxError
        return out

    changes = {}
    for field, kw_only in (("posonly_params", False), ("params", False),
                           ("kwonly_params", True)):
        plist = getattr(params_node, field)
        if plist:
            new = permuted(list(plist), kw_only)
            if new is not None:
                changes[field] = new
    return params_node.with_changes(**changes) if changes else params_node


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
                    key = el.key.evaluated_value  # libcst-native; ast.literal_eval leaves closure cycles
                    if key is None:
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
            key = el.key.evaluated_value  # libcst-native; ast.literal_eval leaves closure cycles
            if key is None:
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

    if value.get("__callee__") is not None:
        old_node = old_node.with_changes(func=cst.parse_expression(str(value["__callee__"])))

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

    # Reorder keyword args to the dict's key order (decorated kwargs in surfaced
    # calls). Kwargs bind by name, so moving them never changes the call's
    # meaning - no dangerous flag needed. Positional args keep their slots:
    # their position IS their binding, so honoring a dictionary key move would
    # rebind values to different parameters. Runs before Pass 3 so comma
    # normalization sees the final order.
    surviving = _reorder_call_kwargs(surviving, value)

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


def _reorder_call_kwargs(args, value):
    """Permute keyword args to match the edited dict's key order.

    Slot permutation, same as _reorder_class_body / _reorder_params: keyword
    args sort into dict-key order but only occupy the slots keyword args
    already held, so positional args and */** splats never move. A newly
    appended kwarg's end slot participates too, letting it land mid-call when
    the dict says so. Each moved arg takes its destination slot's comma so a
    multi-line call keeps its line structure."""
    order = {k: i for i, k in enumerate(value)
             if isinstance(k, str) and not isinstance(k, Comment)
             and not _is_dunder(k)}
    slots = [i for i, a in enumerate(args)
             if a.keyword is not None and a.keyword.value in order]
    if len(slots) < 2:
        return args
    moved = sorted((args[i] for i in slots),
                   key=lambda a: order[a.keyword.value])
    if all(args[i] is a for i, a in zip(slots, moved)):
        return args
    out = list(args)
    for i, a in zip(slots, moved):
        out[i] = a.with_changes(comma=args[i].comma)
    return out


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


@functools.lru_cache(maxsize=1024)
def _cached_signature(obj):
    """inspect.signature per callable, memoized. On builtins it runs
    _signature_fromstr, which defines a local class + closures (a gc cycle)
    on EVERY call -- ~50 per conversion. Hotswapped functions are new objects
    and simply miss; unhashable callables fall through uncached."""
    try:
        return inspect.signature(obj)
    except (TypeError, ValueError):
        return None


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
        sig = _cached_signature(obj)
    except TypeError:  # unhashable callable: lru_cache can't key it
        sig = None
    if sig is None:
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


_inc_memo = threading.local()


def _cst_to_python_or_raw(node):
    """Memo shim over _cst_to_python_or_raw_impl — active only during the
    incremental span reconvert (_funcdef_span_incremental). Statements kept
    across the body splice are the SAME node objects, so their converted
    values are reused by identity instead of re-derived (the dominant cost of
    re-extracting a big function). On a hit the value's spans re-anchor to
    the node's CURRENT position from the fresh position map, so line shifts
    below an edit come out right without a separate pass."""
    pair = getattr(_inc_memo, "pair", None)
    if pair is None:
        return _cst_to_python_or_raw_impl(node)
    old_m, new_m = pair
    hit = old_m.get(id(node))
    if hit is not None and hit[0] is node:
        val = hit[1]
        new_m[id(node)] = hit
        if isinstance(val, dict):
            fresh = _span_of(node)
            if fresh is not None:
                # DEFERRED re-anchor: these value objects are SHARED with the
                # live gp the render thread is drawing right now, and this
                # run may yet fail verification or be superseded by the next
                # edit. Record the fresh position; _funcdef_span_incremental
                # applies every shift in one batch only after the conversion
                # succeeded - failed/aborted runs never touch shared state.
                pending = getattr(_inc_memo, "pending", None)
                if pending is not None:
                    pending.append((val, fresh))
        return val
    val = _cst_to_python_or_raw_impl(node)
    new_m[id(node)] = (node, val)
    return val


def _cst_to_python_or_raw_impl(node):
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
    if isinstance(obj, type(sys)):  # a module
        d = getattr(obj, "__dict__", None)
        return d.get(attr) if d is not None else None
    return getattr(obj, attr, None)





from meltygui.core.runtime.paths import application_root
_SRC_PREFIX = str(application_root()) + "/"


def _build_src_scope():
    """name -> live object for every top-level symbol defined in latent-descent
    src. THIS is the resolution scope: a name resolves only if it names a src
    symbol (function / class / enum / src module); stdlib and third-party are
    out of scope and intentionally left as raw source. Built once per analysis —
    its size is bounded by the project's symbol count, not the file size, and it
    replaces the per-usage sys.modules scans entirely."""
    from meltygui.code.fileref import is_editable_source
    src_mods = {}
    for modname, mod in list(sys.modules.items()):
        if mod is None:
            continue
        f = getattr(mod, "__file__", None)
        if f and is_editable_source(f):
            src_mods[modname] = mod
    src_names = set(src_mods)

    scope = {}
    for modname, mod in src_mods.items():
        # The module itself, keyed by its import leaf (a.b.melty -> "meltygui"), so
        # dotted names like `meltygui.Melty` resolve through it.
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
                scope[name] = obj  # src definition wins
            else:
                scope.setdefault(name, obj)  # src re-export fills gaps
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
    for attr_name in parts[1:-1]:  # walk to the class (not the MEMBER)
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

      ["meltygui", "Melty", "draw"] → src module meltygui -> Melty -> draw
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
                if quote_char not in "'\"":
                    # Prefixed literal (r'...', b'...') - value[0] is the
                    # prefix, not a quote; re-rendering under the prefix
                    # can't represent an arbitrary edited value (r'' has no
                    # escapes at all), so fall back to a plain repr literal.
                    return cst.SimpleString(repr(py_value))
                # Escape control chars too - the old backslash+quote-only
                # escape wrote an edited '\n' as a RAW newline inside the
                # literal (unterminated string in the source). Unicode text
                # (icons) lands as is; only the chars that break or restyle
                # a literal are escaped.
                escaped = (py_value.replace("\\", "\\\\")
                           .replace("\n", "\\n").replace("\r", "\\r")
                           .replace("\t", "\\t")
                           .replace(quote_char, f"\\{quote_char}"))
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
from meltygui.core.rendering.window_decoration import window as _window


def build_index_cache() -> tuple:
    """(Re)resolve every loaded src file's references into _index_refs_cache so
    the fast Index is warm before it's clicked. Only files whose mtime changed
    are re-parsed (the rest hit the cache). Returns (n_src_files, n_reparsed,
    n_real_changes) — `n_real_changes` counts files whose mtime moved past the
    snapshot (an actual content change, vs a cold re-warm where every file
    re-parses but nothing really changed). Pure index work — safe on a background
    thread (no imgui / Melty)."""
    global _index_generation
    _t_build0 = _time.monotonic()
    mod_map = _src_mod_map()
    _ptrace("warmer: build pass start", files=len(mod_map), gen=_index_generation)
    _build_slept = 0.0
    reparsed = 0
    real_changes = 0
    for path, mod in mod_map.items():
        # Pause at file boundaries while the user is mid-drag - even the
        # 2ms-sliced build below competes for the GIL, and a gesture is when
        # dropped frames are most visible. Resumes where it left off.
        _wait_for_no_drag(max_wait=10.0, label="warmer-build")
        prev = _index_refs_cache.get(path)
        _file_index_refs(path, mod)
        entry = _index_refs_cache.get(path)
        if entry is not prev:
            reparsed += 1
            # A REAL content change happens when the mtime moved past the
            # snapshot: a refs rebuild after a restart-in-place re-parses every
            # file (ids are fresh) but does not lapse adopted span results.
            # entry[0] is (mtime, cache_gen) - only the MTIME half counts
            # here: streaming edits already sig the edited file's own spans,
            # and bumping the generation per queued keystroke would suck.
            if entry is not None and _mtime_snapshot.get(path) != entry[0][0]:
                real_changes += 1
                _mtime_snapshot[path] = entry[0][0]
            # GIL yield after each ACTUAL re-parse: this is CPU-bound pure
            # Python (ast.parse + ref walk, 5-10ms/file) that cannot move to
            # the jedi subprocess pool (it works against live objects), so
            # the first build - every loaded src file at once - would
            # otherwise hold the GIL ~1s contiguous and visibly hang the
            # render thread. The sleep slices it into file-sized chunks
            # (+~0.3s wall on a 15s-cadence daemon: irrelevant); cache-hit
            # files skip immediately), keeping steady-state passes at ~10ms.
            # Frame-gate: during launch (first frames) nobody is interacting
            # yet - sprint through the build so symbols are ready the moment
            # the user is; politeness only buys anything once the UI is live.
            if Melty.frame_count > 60:
                _s0 = _time.monotonic()
                _time.sleep(0.002)
                _build_slept += _time.monotonic() - _s0
    _ptrace(f"warmer: build pass done in {(_time.monotonic() - _t_build0) * 1000:.0f}ms",
            files=len(mod_map), reparsed=reparsed, real_changes=real_changes,
            slept=f"{_build_slept * 1000:.0f}ms")
    if real_changes:
        _bump_generation()
    elif _index_generation == 0 and reparsed:
        # Pathological fallback: nothing counted as a real change (snapshot
        # already current) but we've never served results this launch - make
        # the gate open anyway.
        _index_generation = 1
        _symbol_store["gen"] = 1
        _ptrace("warmer: generation gate force-opened (0 -> 1, no counted changes)")
    return len(mod_map), reparsed, real_changes


def _bump_generation():
    """Source really changed somewhere — span-level usage results may have
    gained or lost callers. Bumping the generation lapses them (see
    _compute_symbol_usages); the callbacks wake idle consumers (cached editor
    hosts replay their blit until something re-runs them, so a change they
    can't observe must push the re-index trigger); the store write-back
    records the new generation and stale spans are pruned. Shared tail of both change detectors: the
    file-watch path below (primary) and the warmer's reconcile pass."""
    global _index_generation
    _index_generation += 1
    _symbol_store["gen"] = _index_generation
    _ptrace(f"index generation -> {_index_generation}",
            callbacks=len(_index_bump_callbacks))
    for cb in list(_index_bump_callbacks):
        try:
            # Callbacks run INLINE on this (daemon / watch-timer) thread -
            # _wake_stale_code_hosts can take seconds (0.25s sleep per host),
            # which delays the store save and the next watch batch.
            with _pspan(f"gen callback {getattr(cb, '__name__', cb)}", min_ms=1.0):
                cb(_index_generation)
        except Exception as _e:
            _ptrace(f"gen callback {getattr(cb, '__name__', cb)} RAISED "
                    f"{type(_e).__name__}: {_e}")
    _prune_symbol_store()


# ── File-watch driven index updates ───────────────────────────
# The PRIMARY change detector: FileWatch (meltygui.py) raises events for every
# .py under the src tree (the recursive watch scheduled in
# _register_index_watch below), so we re-index exactly the files that
# changed - no scanning. The warmer daemon's periodic pass remains only as a
# slow safety reconcile (missed/overflowing watchdog events, modules whose
# files changed before they were first imported) plus the initial cold load.

_watch_pending: set = set()
_watch_timer = None
_watch_lock = _threading_spans.Lock()
_WATCH_DEBOUNCE_S = 0.6  # a save arrives as a truncate+touch event burst


def _on_watch_event(src_path):
    """FileWatch global listener (runs on the watchdog OBSERVER thread — only
    collect + re-arm here). Debounce-batches changed src .py paths; the timer
    thread does the actual re-index, so one save burst costs one pass."""
    if not (isinstance(src_path, str) and src_path.endswith(".py")
            and src_path.startswith(_SRC_PREFIX)):
        return
    global _watch_timer
    with _watch_lock:
        _watch_pending.add(src_path)
        if _watch_timer is not None:
            _watch_timer.cancel()
        t = _threading_spans.Timer(_WATCH_DEBOUNCE_S, _process_watch_events)
        t.daemon = True
        _watch_timer = t
        t.start()


def _process_watch_events():
    """Debounced batch: re-parse refs for JUST the changed files, then bump
    the generation when any really moved past the snapshot (same real-change
    accounting as the warmer pass — a touch with identical mtime, or a file
    outside the loaded module map, bumps nothing). Runs on the debounce timer
    thread, with the usual drag deferral."""
    global _watch_timer
    with _watch_lock:
        paths = list(_watch_pending)
        _watch_pending.clear()
        _watch_timer = None
    if not paths:
        return
    _wait_for_no_drag(max_wait=10.0, label="watch-batch")
    _t_watch0 = _time.monotonic()
    mod_map = _src_mod_map()
    changed = 0
    for p in paths:
        try:
            rp = _Path(p).resolve()
        except (OSError, ValueError):
            continue
        mod = mod_map.get(rp)
        if mod is None:
            continue  # not a loaded module - outside the index
        # Per-file frame park: this sweep re-parses a burst of files right
        # after a save (30-170ms of GIL-bound work each) while the render
        # thread repaints the same save's invalidations - the same convoy
        # starvation as the conversion path (see _park_while_frame).
        _park_while_frame()
        prev = _index_refs_cache.get(rp)
        _file_index_refs(rp, mod)
        entry = _index_refs_cache.get(rp)
        if (entry is not prev and entry is not None
                and _mtime_snapshot.get(rp) != entry[0][0]):  # mtime half of (mtime, pgen)
            changed += 1
            _mtime_snapshot[rp] = entry[0][0]
    _ptrace(f"watch batch re-index done in {(_time.monotonic() - _t_watch0) * 1000:.0f}ms",
            paths=len(paths), changed=changed)
    if changed:
        _bump_generation()


def _register_index_watch():
    """Subscribe the index to FileWatch and put one recursive watch on the
    src tree, so EVERY src .py raises events (the per-editor watches only
    cover directories with open views). Re-exec safe: the listener dedupes by
    __name__, the recursive watch by a marker in _watched_dirs (fresh sets on
    a restart-in-place re-create both against the new Observer)."""
    try:
        from meltygui.core.melty import FileWatch
        listeners = getattr(FileWatch, "global_listeners", None)
        if listeners is None:
            return  # older meltygui.py still running - reconcile pass covers us
        listeners[:] = [f for f in listeners
                        if getattr(f, "__name__", "") != "_on_watch_event"]
        listeners.append(_on_watch_event)
        marker = _SRC_PREFIX + "::recursive"
        if hasattr(FileWatch, "watch_recursive"):
            # One inotify instance for the whole src tree; per-dir emitters
            # under it are retired (FileWatch.watch_recursive).
            FileWatch.watch_recursive(_SRC_PREFIX)
        elif marker not in FileWatch._watched_dirs:   # older meltygui.py loaded
            FileWatch.observer.schedule(FileWatch.handler, _SRC_PREFIX,
                                        recursive=True)
            FileWatch._watched_dirs.add(marker)
    except Exception as e:
        print(f"[symbol-index] watch registration failed: {e}")


_register_index_watch()


@_window
class SymbolIndexCache:
    """Keeps the fast caller-index cache warm on a background thread, so the
    editor's Index button is instant. Flip `auto` off to stop the periodic
    refresh; call rebuild() for a one-shot. Status fields below are live."""
    auto = True  # keep the cache warm in the background
    interval_s = 300.0  # SLOW safety reconcile only - the FileWatch
    # listener (_on_watch_event) is the primary
    # change detector now, re-indexing exactly the
    # files that moved within ~0.6s of a save
    startup_delay_s = 10.00  # build immediately; the loop's immediate second
    src_files = 0
    # pass catches modules that import after us
    # ── status (written by the worker) ──
    building = False
    last_reparsed = 0
    builds = 0
    last_secs = 0.0

    last_real_changes = 0

    @classmethod
    def _build_once(cls):
        if cls.building:
            return
        cls.building = True
        real = 0
        t0 = _time.perf_counter()
        try:
            cls.src_files, cls.last_reparsed, real = build_index_cache()
        except Exception as _e:
            # A silently-dead build leaves the generation gate open and NO
            # symbols ever attach - make that failure visible in the timeline.
            _ptrace(f"warmer: build RAISED {type(_e).__name__}: {_e}")
        finally:
            cls.last_real_changes = real
            cls.last_secs = round(_time.perf_counter() - t0, 3)
            cls.builds += 1
            cls.building = False
        # Notify ONLY when the safety reconcile caught a genuine content change -
        # FileWatch is the primary detector now so this is rare. Steadyy no-op
        # passes and cold re-warms (after a hotswap) reparse files but move no
        # mtimes; they stay silent rather than reading as "building...".
        if real:
            notify(f"SymbolIndexCache: reconciled {real} changed file(s) "
                   f"in {cls.last_secs:.2f}s", tint=(1, 0, 0.2))

    @classmethod
    def rebuild(cls):
        """Kick a one-off cache build on a background thread."""
        _threading.Thread(target=cls._build_once, daemon=True,
                          name="symbol-index-rebuild").start()


def _symbol_index_daemon(stop):
    # Re-fetch the class from sys.modules each pass rather than closing over the
    # module's global SymbolIndexCache: if this daemon ever outlives a
    # restart-in-place (crash path where shutdown didn't run), it must target
    # whatever class is live now, building into the live module's
    # _symbol_refs_cache (its method's __globals__), not the orphaned one.
    # `stop` is THIS daemon's own event (passed at start, not re-read from sys)
    # so a successor launch's fresh event can't mask the pending stop.
    modname = __name__

    def live_cls():
        mod = sys.modules.get(modname)
        return getattr(mod, "SymbolIndexCache", None) or SymbolIndexCache

    _delay = max(0.0, getattr(live_cls(), "startup_delay_s", 0.0))
    _ptrace(f"warmer daemon: started, first build in {_delay:.0f}s",
            gen=_index_generation)
    stop.wait(_delay)
    while not stop.is_set():
        cls = live_cls()
        if getattr(cls, "auto", True):
            cls._build_once()
        stop.wait(max(1.0, getattr(cls, "interval_s", 15.0)))


def shutdown_symbol_index_daemon():
    """Stop the symbol-index daemon and clear its process guard, so the next
    exec of this module (a restart-in-place launch) starts a FRESH daemon
    instead of inheriting a survivor mid-interval-sleep. Also flushes the
    portable span results to disk while they're at their freshest. Called from
    Melty.cleanup → FileWatch.shutdown, next to shutdown_jedi_pool.

    NOTE: a daemon started before this stop-event code landed runs the old
    loop bytecode and cannot observe the event — it dies only with the
    process. It's harmless meanwhile (its passes hit warm caches and the
    `building` flag prevents overlap with a successor)."""
    ev = getattr(sys, "_symbol_index_stop", None)
    if ev is not None:
        ev.set()
    sys._symbol_index_daemon_started = False
    _prune_symbol_store()

#
# # Guard to `sys` (shared across the src/lsd.py module dupes) so the daemon runs
# # exactly once even though this file can be imported under two names.
# # shutdown_symbol_index_daemon clears the guard on teardown, so a clean
# # restart-in-place comes through the True branch with a fresh daemon + event.
# if not getattr(sys, "_symbol_index_daemon_started", False):
#     sys._symbol_index_daemon_started = True
#     _stop_event = _threading.Event()
#     sys._symbol_index_stop = _stop_event
#     _threading.Thread(target=_symbol_index_daemon, args=(_stop_event,),
#                       daemon=True, name="symbol-index-daemon").start()
# else:
#     # Re-exec with the thread already started (a hotswap of THIS file, or a
#     # restart-in-place where shutdown didn't clear the guard). The exec wiped this
#     # module's _index_refs_cache, but a LIVE daemon re-warms it on its next pass
#     # (and subsequent re-warm the cache lazily thereafter) - so we no longer kick a
#     # full rebuild on every hotswap. Only force one when NO daemon thread is alive
#     # (a true crash), so indexing isn't left cold indefinitely. Name-check rather
#     # than a direct ref so it also catches a daemon started by another file.
#     _daemon_alive = any(t.name == "symbol-index-daemon" and t.is_alive()
#                         for t in _threading.enumerate())
#     if not _daemon_alive:
#         try:
#             SymbolIndexCache.rebuild()
#         except Exception:
#             pass