"""core_syntax — the libcst-free cst_dict: a syntax dict whose round-trip is
text surgery on the ORIGINAL source, not code generation from the dict.

    text ──parse_to_dict──► GeneralParse (same types as libcst_conversion)
                              + gp["__origin__"] = Origin(text, items, seqs)
    user mutates the gp in place (draw_collection, focus, live_apply, …)
    general_parse_to_str(gp) = apply_edits(origin.text, diff(gp, origin))

The dict alone can't rebuild the code (it drops everything it doesn't surface),
so it never tries to. The reverse path diffs the LIVE dict against the flat
`Origin` tables — one `Item` per surfaced site (its value span, the statement
extent that moves/deletes with it, its indent, the value it was parsed as) and
one `Seq` per ordered container (a body, a parameter list, a call's arguments,
a literal's elements) — and emits `TextEdit`s into the original string:

  * a changed leaf      → replace its value span (`render`, styled on the old text)
  * an added key        → a synthesized statement / `k=v` / element at its dict position
  * a removed key       → delete its extent (leading comments included, like libcst)
  * a reordered Seq     → ONE region edit that re-concatenates the members' source
                          slices verbatim in the new order (gaps stay in their slots)

Everything not touched is copied byte-for-byte, so "unchanged code comes back
exactly" holds by construction.

FRONT ENDS (the forward half lives in `melty_scan`, stdlib-only on purpose):
  "scan"   the tokenize-based cst-lite parser, in-process — small files
           (Toggles.TextEditor.melty_scanner)
  "worker" the same scanner + extractor run in a 3.12 SUBINTERPRETER with its
           own GIL, so a big file's parse never stalls the render thread; the
           result crosses back as pickled neutral data that `materialize_parse`
           turns into the studio's parse classes, resolving names against the
           src scope and binding positional args to runtime signatures here
           (files ≥ Toggles.TextEditor.melty_async_min_chars)
  "ast"    Python's parser — the oracle the other two are tested against

`reparse_reusing` is the live path: a fresh parse whose result reuses every
unchanged value object of the previous parse by identity, so draw_states stay
stable — the previous tree is never mutated (the studio's held tree is
bubbling-wrapped: a dict mutation there reads as a user edit).
"""

from __future__ import annotations

import ast
import atexit
import enum
import os
import pickle
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meltygui.code.libcst_conversion import GeneralParse
from meltygui.code.libcst_conversion import ClassParse
from meltygui.code.libcst_conversion import EnumParse
from meltygui.code.libcst_conversion import FunctionParse
from meltygui.code.libcst_conversion import CallParse
from meltygui.code.libcst_conversion import DecorationParse
from meltygui.code.libcst_conversion import Comment
from meltygui.code.libcst_conversion import CodeLine
from meltygui.code.libcst_conversion import Conditional
from meltygui.code.libcst_conversion import Loop
from meltygui.code.libcst_conversion import Try
from meltygui.code.libcst_conversion import Except
from meltygui.code.libcst_conversion import NO_DEFAULT
from meltygui.code.libcst_conversion import NoDefault
from meltygui.code.libcst_conversion import Span
from meltygui.code.libcst_conversion import _SKIP_PARAMS
from meltygui.code.libcst_conversion import _UNREADABLE
from meltygui.code.libcst_conversion import _float_to_str
from meltygui.code.libcst_conversion import _floats_match
from meltygui.code.libcst_conversion import _is_dunder
from meltygui.code.libcst_conversion import _override_changed
from meltygui.code.libcst_conversion import _format_override_comment
from meltygui.code.libcst_conversion import _reformat_override_comment
from meltygui.code.libcst_conversion import _resolve_as_enum
from meltygui.code.libcst_conversion import _resolve_callable_by_name
from meltygui.code.libcst_conversion import _resolve_callable_by_parts
from meltygui.code.libcst_conversion import _cached_signature
from meltygui.code.melty_scan import Item
from meltygui.code.melty_scan import Seq
from meltygui.code.melty_scan import Origin
from meltygui.code.melty_scan import Base
from meltygui.code.melty_scan import ZERO_BASE
from meltygui.code.melty_scan import Types
from meltygui.code.melty_scan import UNRESOLVED
from meltygui.code.melty_scan import _Src
from meltygui.code.melty_scan import extract as _extract
from meltygui.code.melty_scan import scan_comments as _scan_comments
from meltygui.code.melty_scan import NGeneralParse
from meltygui.code.melty_scan import NClassParse
from meltygui.code.melty_scan import NEnumParse
from meltygui.code.melty_scan import NFunctionParse
from meltygui.code.melty_scan import NCallParse
from meltygui.code.melty_scan import NDecorationParse
from meltygui.code.melty_scan import NConditional
from meltygui.code.melty_scan import NLoop
from meltygui.code.melty_scan import NTry
from meltygui.code.melty_scan import NExcept
from meltygui.code.melty_scan import NComment
from meltygui.code.melty_scan import NCodeLine
from meltygui.code.melty_scan import NameRef
from meltygui.code.melty_scan import NNoDefault
from meltygui.code.melty_scan import NSpan

ORIGIN_KEY = "__origin__"


class CoreSyntaxError(ValueError):
    """The edited text no longer parses. `.text` is the produced text so a
    caller can still show it (the libcst path wraps this as a ParseError)."""

    def __init__(self, message, text, lineno=0, offset=0):
        super().__init__(message)
        self.text = text
        self.lineno = lineno
        self.offset = offset


@dataclass
class TextEdit:
    start: int
    end: int
    replacement: str


@dataclass
class RegionEdit:
    """A rebuilt region: `pieces` are literal strings or (start, end) source
    slices copied verbatim (with any nested edits inside them applied)."""
    start: int
    end: int
    pieces: list


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                      Forward: text → GeneralParse + Origin                             ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _resolve_parts(parts):
    """A dotted name → the live callable / enum member it names in the src scope
    (builtins for a bare name), else UNRESOLVED. Same resolvers as libcst."""
    if len(parts) == 1:
        resolved = _resolve_callable_by_name(parts[0])
    else:
        resolved = _resolve_as_enum(parts)
        if resolved is _UNREADABLE:
            resolved = _resolve_callable_by_parts(parts)
    return UNRESOLVED if resolved is _UNREADABLE else resolved


def _positional_names_for(parts):
    """Ordered positional parameter names of the callee `parts` names (leading
    self/cls dropped, stops at *args), or None when it can't be resolved or
    inspected — mirrors libcst_conversion._call_positional_param_names."""
    obj = _resolve_callable_by_name(parts[0]) if len(parts) == 1 else _resolve_callable_by_parts(parts)
    if obj is _UNREADABLE or not callable(obj):
        return None
    try:
        sig = _cached_signature(obj)
    except TypeError:
        sig = None
    if sig is None:
        return None
    names = []
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            if not names and p.name in _SKIP_PARAMS:
                continue
            names.append(p.name)
        elif p.kind == p.VAR_POSITIONAL:
            break
    return names


class RelSpan(Span):
    """A `Span` whose LINE numbers are relative to a `Base` cell (the enclosing
    top-level statement), so an edit above the statement moves every span in
    it by touching the cell — the incremental reparse never walks the tree to
    renumber. Reads exactly like a Span (`start_line`, `end_line`, …)."""
    __slots__ = ("base", "rel_start_line", "rel_end_line")

    def __init__(self, base, rel_start_line, start_col, rel_end_line, end_col):
        self.base = base
        self.rel_start_line = rel_start_line
        self.start_col = start_col
        self.rel_end_line = rel_end_line
        self.end_col = end_col

    @property
    def start_line(self):
        return self.rel_start_line + self.base.line

    @property
    def end_line(self):
        return self.rel_end_line + self.base.line

    def __reduce__(self):
        return (RelSpan, (self.base, self.rel_start_line, self.start_col, self.rel_end_line, self.end_col))


REAL_TYPES = Types(
    GeneralParse=GeneralParse, ClassParse=ClassParse, EnumParse=EnumParse, FunctionParse=FunctionParse,
    CallParse=CallParse, DecorationParse=DecorationParse, Comment=Comment, CodeLine=CodeLine,
    Conditional=Conditional, Loop=Loop, Try=Try, Except=Except, NO_DEFAULT=NO_DEFAULT, Span=RelSpan,
    resolve=_resolve_parts, positional_names=_positional_names_for)


def _default_frontend(n_chars):
    from meltygui.core.runtime.toggles import Toggles      # lazy: avoid an import cycle
    if not Toggles.TextEditor.melty_scanner:
        return "ast"
    if n_chars >= Toggles.TextEditor.melty_async_min_chars and _worker.available():
        return "worker"
    return "scan"


def parse_to_dict(text, *, file_path=None, line_offset=0, frontend=None) -> GeneralParse:
    """Parse `text` into a GeneralParse with `gp["__origin__"]` attached.
    `frontend`: "scan" | "worker" | "ast" (None = by Toggles and size).
    Raises SyntaxError when the text doesn't parse."""
    if frontend is None:
        frontend = _default_frontend(len(text))
    gp = origin = None
    if frontend == "worker":
        result = _worker.scan_extract(text)
        if result is None:
            frontend = "scan"                       # worker unavailable: same parser, in-process
        elif result[0] == "error":
            _, msg, lineno, offset = result
            raise SyntaxError(msg, (str(file_path or "<text>"), lineno, offset, ""))
        else:
            gp, origin = materialize_parse(result[1], result[2])
    if gp is None:
        # No ast.parse fallback pass here (Lukas 09-01): a second full parse
        # of a buffer per chain? - the scanner's ScanError covers what the
        # tokenizer / syntax parser doesn't take, the rest is on the reverse
        # path's converter (general_parse_to_str) and the hotswap compile.
        gp, origin = _extract(text, frontend=frontend, types=REAL_TYPES,
                              file_path=file_path, line_offset=line_offset)
    origin.file_path = file_path
    origin.line_offset = line_offset
    gp.file_path = file_path
    gp.line_offset = line_offset
    gp[ORIGIN_KEY] = origin
    return gp


# ─── materialize: the worker's neutral tree → the studio's parse classes ─────────

def _walk_paths(node, path, fn):
    """fn(path, node) over every dict node of a parse tree (list/tuple elements
    included, indexed like origin paths). Bookkeeping keys are skipped by NAME
    — a dunder-named def (`__missing__`) is a real node and is walked."""
    if isinstance(node, dict):
        fn(path, node)
        for k, v in node.items():
            if k in ("__origin__", "__pos_names__", "__symbol_usages__", "__cst__"):
                continue
            if isinstance(v, (dict, list, tuple)):
                _walk_paths(v, path + (k,), fn)
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _walk_paths(v, path + (i,), fn)


def _bind_pending_positionals(ngp, origin):
    """The worker keys positional args `argN`; here the callee's runtime
    signature names them (`live_view(x)` → `value`) exactly as the in-process
    extractor would have, renaming the dict keys and every origin path under
    the call in one pass. Positions past the signature are dropped, like the
    in-process path never surfaced them."""
    renames = {}

    def visit(path, node):
        parts = node.__dict__.pop("_pos_pending", None) if hasattr(node, "__dict__") else None
        if parts is None:
            return
        names = _positional_names_for(parts)
        if names is None:
            return
        pos_keys = [k for k in node if isinstance(k, str) and k.startswith("arg") and k[3:].isdigit()]
        mapping = {k: (names[i] if i < len(names) else None) for i, k in enumerate(pos_keys)}
        items = []
        for k, v in node.items():
            if k in mapping:
                if mapping[k] is None:
                    continue
                items.append((mapping[k], v))
            elif k == "__pos_names__":
                continue
            else:
                items.append((k, v))
        node.clear()
        node.update(items)
        if names:
            node["__pos_names__"] = list(names)
        renames[path] = mapping

    _walk_paths(ngp, (), visit)
    if not renames:
        return

    def rename_path(path):
        for L in range(len(path)):
            m = renames.get(path[:L])
            if m is not None and L < len(path) and path[L] in m:
                new = m[path[L]]
                if new is None:
                    return None
                path = path[:L] + (new,) + path[L + 1:]
        return path

    items = {}
    dropped = set()
    for path, item in origin.items.items():
        np = rename_path(path)
        if np is None:
            dropped.add(id(item))
            continue
        item.path = np
        item.key = np[-1]
        items[np] = item
    origin.items = items
    text = origin.text
    for seq in origin.seqs.values():
        seq.owner = rename_path(seq.owner) or seq.owner
        kept = [it for it in seq.items if id(it) not in dropped]
        if len(kept) != len(seq.items):
            seq.items = kept                    # `region` follows the items; only the separator is re-read
            if len(kept) >= 2:
                seq.sep = text[kept[0].extent[1]:kept[1].extent[0]]
    origin.default_seq = {rename_path(p) or p: v for p, v in origin.default_seq.items()}
    origin.owned = {rename_path(p) or p: v for p, v in origin.owned.items()}
    origin.loose = {rename_path(p) or p: [it for it in v if id(it) not in dropped]
                    for p, v in origin.loose.items()}


_NODE_ATTR_DEFAULTS = {"file_path": None, "line_offset": 0, "usages": None, "_bg_hash_cache": None,
                       "source_ref": None, "symbol_usage": None}


def materialize_parse(gp, origin):
    """Finish a worker parse that `_ParseUnpickler` already loaded as the
    studio's classes: give nodes the attributes their `__init__` would have set
    (pickle bypasses it), resolve `NameRef`s against the live src scope, and
    bind positional args to runtime signatures (renaming dict keys and origin
    paths). `Item.orig` identity with the dict values survives the pickle
    round trip on its own (one dumps → shared references)."""
    from meltygui.code.libcst_conversion import _yield_to_ui
    _yield_to_ui()
    _bind_pending_positionals(gp, origin)

    visited = 0
    def fix(path, node):
        nonlocal visited
        visited += 1
        if visited % 128 == 0:
            _yield_to_ui()
        d = getattr(node, "__dict__", None)
        if d is not None:
            if isinstance(node, GeneralParse):
                for attr, default in _NODE_ATTR_DEFAULTS.items():
                    d.setdefault(attr, default)
                if "address" not in d:
                    d["address"] = None
                if "usages" not in d or d["usages"] is None:
                    d["usages"] = {}
                if "symbol_usage" not in d or d["symbol_usage"] is None:
                    d["symbol_usage"] = [None]
            elif isinstance(node, Loop):
                d.setdefault("_bg_hash_cache", None)
        for k in list(node.keys()):
            v = node[k]
            if isinstance(v, (NameRef, list, tuple)):
                node[k] = _resolve_refs(v, path + (k,), origin)

    _walk_paths(gp, (), fix)
    return gp, origin


def _resolve_refs(v, path, origin):
    """`v` with every NameRef (at any container depth) resolved against the src
    scope or turned into a CodeLine; `Item.orig` at each touched path follows,
    so the residual holds what the dict holds. Tuples are rebuilt, lists
    patched in place."""
    if isinstance(v, NameRef):
        r = _resolve_parts(v.parts)
        new = r if r is not UNRESOLVED else CodeLine(str(v))
    elif isinstance(v, (list, tuple)):
        if not any(isinstance(x, (NameRef, list, tuple)) for x in v):
            return v
        fixed = [_resolve_refs(x, path + (i,), origin) for i, x in enumerate(v)]
        if isinstance(v, tuple):
            new = tuple(fixed)
        else:
            v[:] = fixed
            return v
    else:
        return v
    item = origin.items.get(path)
    if item is not None:
        item.orig = new
    return new




class _ParseUnpickler(pickle.Unpickler):
    """Loads the worker's neutral classes AS the studio's classes: no second
    tree, no per-node conversion — `Comment(text, inline)`, `CodeLine`,
    `Span(...)`, the NO_DEFAULT singleton, and the parse dict subclasses come
    out of `loads` directly (their `__init__` is bypassed; materialize_parse
    fills the attributes it would have set)."""
    _MAP = {"NGeneralParse": GeneralParse, "NClassParse": ClassParse, "NEnumParse": EnumParse,
            "NFunctionParse": FunctionParse, "NCallParse": CallParse, "NDecorationParse": DecorationParse,
            "NConditional": Conditional, "NLoop": Loop, "NTry": Try, "NExcept": Except,
            "NComment": Comment, "NCodeLine": CodeLine, "NSpan": RelSpan, "NNoDefault": lambda: NO_DEFAULT}

    def find_class(self, module, name):
        if module == "_melty_scan" or module.endswith(".melty_scan"):
            real = self._MAP.get(name)
            if real is not None:
                return real
            if module == "_melty_scan":
                from meltygui.code import melty_scan
                return getattr(melty_scan, name)
        return super().find_class(module, name)


# ── the parse worker: a subinterpreter with its own GIL ───────────────────────────

_WORKER_SCRIPT = r"""
import sys
import importlib.util
import pickle
import _xxinterpchannels as _ch
# Importing the package also initializes app/threading state in this
# interpreter. It can then hang at shutdown waiting for the original
# caller thread. The scanner is stdlib-only: load it without the GUI.
if '_melty_scan' not in sys.modules:
    _spec = importlib.util.spec_from_file_location('_melty_scan', SCANNER)
    _ms = importlib.util.module_from_spec(_spec)
    sys.modules['_melty_scan'] = _ms
    _spec.loader.exec_module(_ms)
else:
    _ms = sys.modules['_melty_scan']
import gc as _gc
_gc.disable()                    # ~20% of the scan was gen-2 collections over the fresh tree
try:
    _res = _ms.scan_extract(TEXT, validate=False)   # no ast validation pass (09-01)
except Exception as _e:          # never raise across the boundary: report as data
    _res = ("error", f"{type(_e).__name__}: {_e}", 0, 0)
_blob = pickle.dumps(_res, protocol=pickle.HIGHEST_PROTOCOL)
del _res
_gc.enable()
_gc.collect()
_ch.send(CID, _blob)
"""


class _ScanWorker:
    """One 3.12 subinterpreter (`_xxsubinterpreters`, per-interpreter GIL) that
    runs melty_scan.scan_extract. A dedicated owner thread executes inside the
    subinterpreter, under ITS GIL — the main interpreter's GIL is free for the
    render thread the whole time (measured: a 130 ms parse and 130 ms of main-
    thread Python overlap to 130 ms wall). One run at a time; a second caller
    waits on the lock. Any failure to boot marks the worker unavailable and
    parses fall back to the in-process scanner."""

    def __init__(self):
        self._lock = threading.Lock()
        self._interp = None
        self._cid = None
        self._broken = False
        self._requests = queue.Queue()
        self._thread = None

    def _run(self):
        try:
            while True:
                request = self._requests.get()
                if request is None:
                    return
                text, reply = request
                try:
                    reply.put(self._execute(text))
                except BaseException as error:
                    reply.put(error)
        finally:
            if self._interp is not None:
                import _xxsubinterpreters as si
                import _xxinterpchannels as ch
                si.destroy(self._interp)
                ch.destroy(self._cid)
                self._interp = self._cid = None

    def close(self):
        with self._lock:
            thread = self._thread
            if thread is not None:
                self._requests.put(None)
        if thread is not None:
            thread.join()
            self._thread = None

    def available(self):
        if self._broken:
            return False
        try:
            import _xxsubinterpreters  # noqa: F401
            import _xxinterpchannels  # noqa: F401
        except ImportError:
            self._broken = True
            return False
        return True

    def scan_extract(self, text):
        """("ok", gp, origin) | ("error", msg, lineno, offset) — or None when the
        worker can't run (the caller parses in-process)."""
        if not self.available():
            return None
        reply = queue.Queue(maxsize=1)
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='syntax-scanner', daemon=True)
                self._thread.start()
            self._requests.put((text, reply))
        blob = reply.get()
        if isinstance(blob, BaseException):
            raise blob
        if blob is None:
            return None
        import io
        from meltygui.code.libcst_conversion import _yield_to_ui
        _yield_to_ui()  # deserialization is back under the application's GIL
        return _ParseUnpickler(io.BytesIO(blob)).load()

    def _execute(self, text):
        import _xxsubinterpreters as si
        import _xxinterpchannels as ch
        scanner = str(Path(__file__).with_name('melty_scan.py'))
        with self._lock:
            try:
                if self._interp is None:
                    self._interp = si.create()
                    self._cid = ch.create()
                si.run_string(self._interp, _WORKER_SCRIPT,
                              shared={"TEXT": str(text), "SCANNER": scanner, "CID": int(self._cid)})
                blob = ch.recv(self._cid)
            except Exception as e:
                print(f"core_syntax: scan worker failed ({type(e).__name__}: {str(e)[:200]}); "
                      "parsing in-process from now on")
                self._broken = True
                return None
        return blob


_worker = _ScanWorker()
atexit.register(_worker.close)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Value codec: equality + rendering styled on the old text                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _parse_kind(obj):
    """The parse class of a node, seen through a bubbling subclass (the studio's
    held tree is reclassed in place to `Bubbling_<Base>`)."""
    t = type(obj)
    # Generated `Bubbling_<Base>` reclasses AND the static `_BubblingDict` /
    # `_BubblingList` copies that replace plain container containers.
    if t.__module__.endswith(".bubbling") or t.__name__.startswith("Bubbling_"):
        from meltygui.core.conversion.bubbling import base_of_bubbling
        return base_of_bubbling(t)
    return t


def _same_kind(a, b):
    """Same container family (a bubbling list IS a list), else same type."""
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list)
    if isinstance(a, tuple) or isinstance(b, tuple):
        return isinstance(a, tuple) and isinstance(b, tuple)
    if isinstance(a, dict) or isinstance(b, dict):
        return isinstance(a, dict) and isinstance(b, dict)
    return type(a) is type(b)


def values_equal(a, b):
    """Semantic equality for leaf values: the float32-noise rule for floats,
    type-strict for bools/ints/strs (1 vs 1.0 vs True are different code),
    identity for enum members and callables."""
    if a is b:
        return True
    if isinstance(a, NoDefault) or isinstance(b, NoDefault):
        return isinstance(a, NoDefault) and isinstance(b, NoDefault)   # TODO: any instance (pickle)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, float) and isinstance(b, float):
        return _floats_match(a, b)
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        return type(a) is type(b) and a == b
    if isinstance(a, CodeLine) or isinstance(b, CodeLine):
        return type(a) is type(b) and str(a) == str(b)
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, (list, tuple)) and _same_kind(a, b):
        return len(a) == len(b) and all(values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        ka = [k for k in a if not _is_dunder(k)]
        kb = [k for k in b if not _is_dunder(k)]
        return ka == kb and all(values_equal(a[k], b[k]) for k in ka)
    if isinstance(a, set) and isinstance(b, set):
        return a == b
    if isinstance(a, enum.Enum) or callable(a) or isinstance(b, enum.Enum) or callable(b):
        return False
    try:
        return bool(a == b)
    except Exception:
        return False


def _qualname_text(obj):
    qualname = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
    if not qualname:
        return None
    parts = [p for p in qualname.split(".") if not p.startswith("<")]
    return ".".join(parts) if parts else None


def _render_str(value, old_text):
    if (old_text and len(old_text) >= 2 and old_text[0] in "'\"" and old_text[-1] == old_text[0]
            and not old_text.startswith(("'''", '"""'))):
        q = old_text[0]
        escaped = (value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
                   .replace("\t", "\\t").replace(q, "\\" + q))
        return f"{q}{escaped}{q}"
    return repr(value)


def _container_style(old_text):
    """(open, close, sep, trailing_comma) reusing the old literal's layout: a
    multi-line literal keeps its line breaks and continuation indent."""
    if not old_text:
        return None
    opener, closer = old_text[0], old_text[-1]
    if opener not in "([{" or closer not in ")]}":
        return None
    inner = old_text[1:-1]
    trailing = inner.rstrip().endswith(",")
    sep = ", "
    if "\n" in inner:
        after = inner.split("\n", 1)[1]
        indent = after[:len(after) - len(after.lstrip())]
        sep = ",\n" + indent
    return opener, closer, sep, trailing


def render(value, old_text=None, orig=None):
    """Source text for `value`, styled on `old_text` (the text it replaces)."""
    if isinstance(value, CodeLine):
        return str(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, float):
        return _float_to_str(value, old_text if isinstance(orig, float) else None)
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, Comment):
        return str(value)
    if isinstance(value, str):
        return _render_str(value, old_text if isinstance(orig, str) and not isinstance(orig, CodeLine) else None)
    if isinstance(value, enum.Enum):
        if isinstance(orig, enum.Enum) and type(orig) is type(value) and old_text and "." in old_text:
            return old_text.rsplit(".", 1)[0] + "." + value.name
        return f"{type(value).__qualname__}.{value.name}"
    if isinstance(value, CallParse):
        args = ", ".join(f"{k}={render(v)}" for k, v in value.items() if not _is_dunder(k))
        return f"{value.func_name or 'call'}({args})"
    if isinstance(value, dict):
        style = _container_style(old_text) if isinstance(orig, dict) else None
        opener, closer, sep, trailing = style or ("{", "}", ", ", False)
        parts = [f"{render(k)}: {render(v)}" for k, v in value.items() if not _is_dunder(k)]
        return opener + sep.join(parts) + ("," if trailing and parts else "") + closer
    if isinstance(value, (list, tuple)):
        is_tuple = isinstance(value, tuple)
        style = _container_style(old_text) if _same_kind(orig, value) else None
        if style is None:
            if is_tuple and old_text and isinstance(orig, tuple) and old_text[0] != "(":
                opener, closer = "", ""        # a bare `x = 1, 2` tuple keeps its bareness
            else:
                opener, closer = ("(", ")") if is_tuple else ("[", "]")
            sep, trailing = ", ", False
        else:
            opener, closer, sep, trailing = style
        parts = [render(v) for v in value]
        if is_tuple and len(parts) == 1:
            return opener + parts[0] + "," + closer
        return opener + sep.join(parts) + ("," if trailing and parts else "") + closer
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(render(v) for v in sorted(value, key=repr)) + "}" if value else "set()"
    if callable(value):
        name = _qualname_text(value)
        if name is not None:
            return name
    return repr(value)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Reverse: diff the live dict against the Origin → text edits                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _managed_keys(node):
    if isinstance(node, dict):
        return [k for k in node if not _is_dunder(k)]
    if isinstance(node, (list, tuple)):
        return list(range(len(node)))
    return []


def diff(gp, origin=None):
    """All edits that turn `origin.text` into the text `gp` now describes."""
    if origin is None:
        origin = gp[ORIGIN_KEY]
    edits = []
    _walk(gp, (), origin, edits)
    return edits


def _walk(node, path, origin, edits):
    keys = _managed_keys(node)
    if isinstance(node, CallParse) and node.get("__callee__") is not None:
        item = origin.items.get(path + ("__callee__",))
        if item is not None and str(node["__callee__"]) != item.orig:
            edits.append(TextEdit(*item.value_span, str(node["__callee__"])))
    if isinstance(node, dict):
        # Dunder-named DEFS are editable too - `__init__` methods, the chain's
        # synthetic `__melty_*_wrap__` snippet wrappers - just never reordered.
        keys += [k for k in node if _is_dunder(k)
                 and getattr(origin.items.get(path + (k,)), "kind", None) == "def"]
    present_by_seq: dict[int, list] = {}
    new_keys = []
    for k in keys:
        item = origin.items.get(path + (k,))
        if item is None:
            new_keys.append(k)
            continue
        if item.seq is not None and not _is_dunder(k):
            present_by_seq.setdefault(item.seq, []).append(k)
        if item.kind in ("comment", "trailing"):
            _diff_comment(node[k], item, origin, edits)
        elif item.kind == "override":
            continue
        else:
            _diff_value(node[k], item, path + (k,), origin, edits)
    if isinstance(node, dict) and type(node) is not dict:
        _diff_overrides(node, path, origin, edits)
    # Comments / trailing comments live outside the Seqs: a key that drops the dict is a delete.
    for item in origin.loose.get(path, ()):
        if (item.kind in ("comment", "trailing") and not item.shadowed
                and not (isinstance(node, dict) and item.key in node)):
            edits.append(TextEdit(*item.extent, ""))

    for seq_id in origin.owned.get(path, []):
        seq = origin.seqs[seq_id]
        present = present_by_seq.get(seq_id, [])
        present_set = set(present)
        # Source order of the keys, each at its first binding (a re-bound key
        # keeps its first slot in dict order too); dunders never take part.
        old = []
        for it in seq.items:
            if not _is_dunder(it.key) and it.key not in old:
                old.append(it.key)
        removed = [k for k in old if k not in present_set]
        adds = [k for k in new_keys if origin.default_seq.get(path) == seq_id
                and not isinstance(node[k], GeneralParse)]
        reordered = [k for k in old if k in present_set] != present
        if not (removed or adds or reordered):
            continue
        if seq.kind in ("body", "decorators") and not reordered:
            for k in removed:
                edits.append(TextEdit(*origin.items[path + (k,)].extent, ""))
            for k in adds:
                edits.append(_insert_member(node, k, path, seq, origin))
        else:
            edits.append(_rebuild_seq(node, keys, path, seq, present_set, set(adds), origin))


def _fixed(item):
    """Members the diff never touches and a rebuild keeps in their slot: dunder
    keys (`__all__`, the libcst patcher's _is_dunder rule) and bindings a later
    statement shadowed."""
    return item.shadowed or _is_dunder(item.key)


def _diff_value(new, item, path, origin, edits):
    orig = item.orig
    seq_id = origin.default_seq.get(path)
    if isinstance(new, dict) and isinstance(orig, dict) and seq_id is not None:
        _walk(new, path, origin, edits)
        return
    if isinstance(new, (list, tuple)) and _same_kind(new, orig) and seq_id is not None:
        _walk(new, path, origin, edits)
        return
    if item.kind in ("def", "block", "pseudo"):
        if isinstance(new, dict):
            _walk(new, path, origin, edits)
        return
    if item.value_span is None:
        if item.kind == "param" and not isinstance(new, NoDefault):
            edits.append(TextEdit(item.slot, item.slot, "=" + render(new)))
        return
    if item.kind == "param" and isinstance(new, NoDefault):
        edits.append(TextEdit(item.slot, item.value_span[1], ""))
        return
    if new is orig or values_equal(new, orig):
        return
    old_text = origin.text[item.value_span[0]:item.value_span[1]]
    edits.append(TextEdit(*item.value_span, render(new, old_text, orig)))


def _diff_comment(new, item, origin, edits):
    text = str(new)
    if text == item.orig:
        return
    nl = origin.src.newline
    edits.append(TextEdit(*item.value_span, (nl + item.indent).join(text.split("\n"))))


def _diff_overrides(node, path, origin, edits):
    ov = node.get("__overrides__")
    if not isinstance(ov, dict):
        ov = {}
    plain = {k: v for k, v in ov.items() if not _is_dunder(k)}
    _diff_override_comment(plain, path + ("__overrides__",), node, path, origin, edits, field_key=None)
    for k, v in ov.items():
        if _is_dunder(k) and isinstance(v, dict) and len(k) > 4:
            _diff_override_comment(v, path + ("__overrides__", k), node, path, origin, edits,
                                   field_key=k[2:-2])
    # A field override whose comment was removed from the dict entirely.
    for item in origin.loose.get(path + ("__overrides__",), ()):
        if item.kind == "override" and item.key not in ov and not item.shadowed:
            edits.append(TextEdit(*item.extent, ""))


def _diff_override_comment(pairs, ipath, node, path, origin, edits, field_key):
    item = origin.items.get(ipath)
    if item is not None and item.kind != "override":
        item = None         # a dict literal keyed "__overrides__" is not an override comment
    nl = origin.src.newline
    if item is not None:
        if item.comment_key is not None:
            current = node.get(item.comment_key)
            if current is not None and str(current) != item.orig and str(current) != str(item.comment_key):
                return          # the raw comment text was edited - it wins
        if not pairs:
            if item.comment_key is None:
                edits.append(TextEdit(*item.extent, ""))
            return
        if _override_changed(pairs, item.orig):
            old_text = origin.text[item.value_span[0]:item.value_span[1]]
            old_joined = "\n".join(ln.strip() for ln in old_text.split("\n"))
            text = _reformat_override_comment(old_joined, pairs)
            edits.append(TextEdit(*item.value_span, (nl + item.indent).join(text.split("\n"))))
        return
    if not pairs:
        return
    # No comment yet → insert one above the owner (a dict's own line, or the field's statement).
    if field_key is not None:
        target = origin.items.get(path + (field_key,))
    else:
        target = origin.items.get(path)
    if target is None:
        if path == () and field_key is None:
            edits.append(TextEdit(0, 0, _format_override_comment(pairs) + nl))
        return
    at = _owner_line_start(target, origin)
    edits.append(TextEdit(at, at, target.indent + _format_override_comment(pairs) + nl))


def _owner_line_start(item, origin):
    """Start of the line the item's own code begins on (below its leading
    comments): where a new override comment goes."""
    if item.code_end is None:
        return item.core[0]
    # Walk back from the core/code to the line start of the statement's first line.
    start = item.core[0]
    text = origin.text
    lines_start = start
    # The statement's first code line is the first non-comment, non-blank line in the core.
    pos = start
    while pos < item.core[1]:
        line_end = text.find("\n", pos)
        if line_end == -1:
            line_end = len(text)
        stripped = text[pos:line_end].strip()
        if stripped and not stripped.startswith("#"):
            return pos
        pos = line_end + 1
    return lines_start


def _render_member(node, key, seq, origin, indent):
    """Source text for a NEW member of `seq` — a statement line for a body,
    `k=v` for args, `k: v` for pairs, the value for elements/params."""
    value = node[key] if isinstance(node, dict) else node[key]
    nl = origin.src.newline
    kind = seq.kind
    if kind == "body":
        if isinstance(value, Comment):
            return "".join(indent + ln + nl for ln in str(value).split("\n"))
        if isinstance(value, CallParse):
            return indent + render(value) + nl
        return f"{indent}{key} = {render(value)}{nl}"
    if kind == "decorators":
        if isinstance(value, str) and not isinstance(value, CodeLine) and value == key:
            return f"{indent}@{value}{nl}"
        if isinstance(value, CallParse):
            return f"{indent}@{render(value)}{nl}"
        return f"{indent}@{value}{nl}"
    if kind == "params":
        return str(key) if isinstance(value, NoDefault) else f"{key}={render(value)}"
    if kind == "args":
        pos_names = node.get("__pos_names__") if isinstance(node, dict) else None
        if pos_names and key in pos_names:
            return render(value)
        return f"{key}={render(value)}"
    if kind == "pairs":
        return f"{render(key)}: {render(value)}"
    return render(value)


def _insert_member(node, key, path, seq, origin):
    """A body/decorator insert as its own zero-width edit at the dict position."""
    keys = _managed_keys(node)
    i = keys.index(key)
    prev = None
    for k in reversed(keys[:i]):
        it = origin.items.get(path + (k,))
        if it is not None and it.seq == seq.id:
            prev = it
            break
    value = node[key]
    if isinstance(value, Comment) and value.inline is not None:
        owner = origin.items.get(path + (value.inline,))
        if owner is not None and owner.code_end is not None:
            return TextEdit(owner.code_end, owner.code_end, "  " + str(value))
    if prev is not None:
        at, indent = prev.extent[1], prev.indent
    else:
        nxt = None
        for k in keys[i + 1:]:
            it = origin.items.get(path + (k,))
            if it is not None and it.seq == seq.id:
                nxt = it
                break
        at = nxt.core[0] if nxt is not None else seq.insert_at
        indent = nxt.indent if nxt is not None else seq.indent
    text = _render_member(node, key, seq, origin, indent)
    if at >= len(origin.text) and origin.text and not origin.text.endswith("\n"):
        text = origin.src.newline + text
    return TextEdit(at, at, text)


def _rebuild_seq(node, keys, path, seq, present_set, adds_set, origin):
    """One RegionEdit re-concatenating the members in the dict's order. The old
    members are SLOTS: a fixed slot (dunder / shadowed binding) keeps its own
    text; the movable slots are filled with the movable keys in the dict's new
    order — kept members as verbatim source slices (nested edits inside them
    still apply), new ones rendered. Surplus movable keys append; surplus slots
    (removed keys) vanish. The gaps between slots stay where they were, so the
    blank-line rhythm / separators survive a reorder."""
    items_by_key = {it.key: it for it in seq.items if not _fixed(it)}
    cores = [it.core for it in seq.items]
    gaps = [(cores[i][1], cores[i + 1][0]) for i in range(len(cores) - 1)]
    movable = [k for k in keys if k in present_set or k in adds_set]
    default_gap = "" if seq.kind in ("body", "decorators") else seq.sep
    pieces = []
    emitted = False
    mi = 0
    last_indent = seq.indent

    def member(k):
        it = items_by_key.get(k)
        if it is not None:
            return it.core, it.indent
        return _render_member(node, k, seq, origin, last_indent), last_indent

    for idx, it in enumerate(seq.items):
        gap = gaps[idx - 1] if idx > 0 else None
        if _fixed(it):
            piece, indent = it.core, it.indent
        elif mi < len(movable):
            piece, indent = member(movable[mi])
            mi += 1
        else:
            continue            # a removed key's slot (and the gap before it)
        if emitted and gap is not None:
            pieces.append(gap)
        pieces.append(piece)
        last_indent = indent
        emitted = True
    while mi < len(movable):
        if emitted:
            pieces.append(default_gap)
        piece, indent = member(movable[mi])
        pieces.append(piece)
        last_indent = indent
        emitted = True
        mi += 1
    start, end = seq.region
    if not pieces and seq.kind in ("elements", "args", "pairs", "params"):
        # Everything removed: take a trailing comma / whitespace with it.
        tail = end
        text = origin.text
        while tail < len(text) and text[tail] in " \t\r\n":
            tail += 1
        if tail < len(text) and text[tail] == ",":
            end = tail + 1
    return RegionEdit(start, end, pieces)


# ── materialize + apply ──────────────────────────────────────────────────────

def apply_edits(text, edits):
    """Apply non-overlapping plain edits (any order) to `text`."""
    out, pos = [], 0
    for e in sorted(edits, key=lambda e: (e.start, e.end)):
        if e.start < pos:
            raise ValueError(f"overlapping edits at {e.start} (previous ended at {pos})")
        out.append(text[pos:e.start])
        out.append(e.replacement)
        pos = e.end
    out.append(text[pos:])
    return "".join(out)


def materialize(text, edits):
    """Flatten RegionEdits into plain TextEdits (absolute offsets), applying the
    edits nested inside each region's source slices."""
    ordered = sorted(edits, key=lambda e: (e.start, -(e.end - e.start)))
    out = []
    i = 0
    while i < len(ordered):
        e = ordered[i]
        if isinstance(e, RegionEdit):
            j = i + 1
            children = []
            while j < len(ordered) and ordered[j].start < e.end:
                if ordered[j].end > e.end:
                    raise ValueError("edit crosses a region boundary")
                children.append(ordered[j])
                j += 1
            out.append(TextEdit(e.start, e.end, _render_region(text, e, children)))
            i = j
        else:
            out.append(e)
            i += 1
    return out


def _render_region(text, region, children):
    flat = materialize(text, children) if children else []
    parts = []
    n = len(region.pieces)
    for idx, p in enumerate(region.pieces):
        if isinstance(p, str):
            parts.append(p)
            continue
        s, e = p
        inner = [TextEdit(c.start - s, c.end - s, c.replacement) for c in flat
                 if s <= c.start and c.end <= e and (c.start < e or (s == e) or idx == n - 1)]
        parts.append(apply_edits(text[s:e], inner) if inner else text[s:e])
    return "".join(parts)


def general_parse_to_str(gp, *, check=True) -> str:
    """The text `gp` now describes. With `check`, the result must parse."""
    origin = gp[ORIGIN_KEY]
    edits = materialize(origin.text, diff(gp, origin))
    new_text = apply_edits(origin.text, edits)
    if check and edits:
        try:
            ast.parse(new_text)
        except SyntaxError as e:
            raise CoreSyntaxError(str(e), new_text, e.lineno or 0, e.offset or 0) from e
    return new_text


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Live path: re-extract, reuse unchanged value objects                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_MISSING = object()

_ROOT_CARRY_ATTRS = ("file_path", "line_offset", "address", "symbol_usage", "_symbol_gen",
                     "usages", "source_ref")


def reparse_reusing(gp, new_text) -> GeneralParse:
    """A fresh parse of `new_text` in which every value object that is
    semantically unchanged from `gp` is REUSED by identity — a nested node
    whose whole subtree is unchanged is the old object itself (its spans
    refreshed) — so draw_states keyed on those objects survive the edit.

    `gp` is NEVER mutated: the studio's held tree is bubbling-wrapped, where a
    dict mutation notifies the code host as a user edit — the libcst
    incremental builds a new root the same way. The root is `gp` itself only
    when nothing surfaced changed (then just its residual moves on). Root
    bookkeeping (address, file_path, symbol usages, …) carries over. Raises
    SyntaxError like parse_to_dict."""
    fresh = parse_to_dict(new_text, file_path=getattr(gp, "file_path", None),
                          line_offset=getattr(gp, "line_offset", 0))
    origin = fresh[ORIGIN_KEY]
    from meltygui.code.libcst_conversion import _yield_to_ui
    _yield_to_ui()
    merged = _merge_node(gp, fresh, (), origin)
    if merged is gp:
        gp[ORIGIN_KEY] = origin            # internal key: a raw write OK on a bubbling node
        _copy_node_attrs(gp, fresh)
        return gp
    for attr in _ROOT_CARRY_ATTRS:
        if hasattr(gp, attr):
            setattr(merged, attr, getattr(gp, attr))
    return merged


def reparse_incremental(gp, new_text) -> GeneralParse:
    """The keystroke path: re-parse only the TOP-LEVEL statements the edit
    touched and splice them into a new root; everything else is the previous
    parse's objects with their offsets / line numbers shifted. Falls back to
    `reparse_reusing` (a full parse) when the edit lands outside every
    statement at the module head, touches more than half the file, or the
    previous parse carries no statement table. Raises SyntaxError like
    parse_to_dict (region line numbers mapped back to the file).

    The previous parse is CONSUMED: nodes it shares with the result get their
    spans moved to the new coordinates and its residual tables are re-based —
    use the returned gp from then on (the libcst incremental had the same
    contract; the code host always chains)."""
    origin = gp.get(ORIGIN_KEY)
    if origin is None or not getattr(origin, "top_stmts", None):
        return reparse_reusing(gp, new_text)
    old_text = origin.text
    if new_text == old_text:
        return gp

    # ── 1. the changed char range (memcmp-style prefix / suffix), snapped to lines ──
    pre = _common_prefix(old_text, new_text)
    suf = _common_suffix(old_text, new_text, pre)
    old_lo = old_text.rfind("\n", 0, pre) + 1
    old_hi = old_text.find("\n", len(old_text) - suf)
    old_hi = len(old_text) if old_hi == -1 else old_hi + 1
    delta = len(new_text) - len(old_text)
    top = origin.top_extents()
    i0 = i1 = None
    for i, (s0, e0, _k) in enumerate(top):
        if e0 > old_lo and s0 < max(old_hi, old_lo + 1):
            if i0 is None:
                i0 = i
            i1 = i
    # Appending AFTER the last statement still belongs to a small region:
    # include the statement plus the trailing gap. Including the statement
    # preserves indented body extensions and comments/decorators at its head.
    tail_edit = old_lo >= top[-1][0] and old_hi >= top[-1][1]
    if i0 is None and tail_edit:
        i0 = i1 = len(top) - 1
    if i0 is None or old_lo < top[0][0]:
        return reparse_reusing(gp, new_text)
    if tail_edit:
        # Semicolon-separated statements share a physical line. Include
        # all preceding them so the regional parser preserves their columns.
        line_start = old_text.rfind('\n', 0, top[i0][0]) + 1
        while i0 > 0 and top[i0 - 1][0] >= line_start:
            i0 -= 1
    rs, re_ = top[i0][0], top[i1][1]
    if tail_edit and i1 == len(top) - 1:
        re_ = len(old_text)
    if old_hi > re_ or (re_ - rs) * 2 > len(old_text):
        return reparse_reusing(gp, new_text)
    region_old = old_text[rs:re_]
    region_new = new_text[rs:re_ + delta]
    if rs > pre or len(old_text) - re_ > suf:
        return reparse_reusing(gp, new_text)
    dl = region_new.count("\n") - region_old.count("\n")

    # ── 2. parse the region on its own (column 0, statement boundaries) ──
    from meltygui.core.runtime.toggles import Toggles
    frontend = "scan" if Toggles.TextEditor.melty_scanner else "ast"
    first_line = origin.src.linecol(rs)[0]
    try:
        rgp, rorigin = _extract(region_new, frontend=frontend, types=REAL_TYPES, module_header=(rs == 0))
    except SyntaxError as e:
        raise SyntaxError(e.msg, (str(getattr(gp, "file_path", None) or "<text>"),
                                  (e.lineno or 1) + first_line - 1, e.offset or 0, "")) from None

    # ── 3. root keys: before / region / after, by extent ──
    before_keys, after_keys, region_keys = [], [], set()
    for k in gp:
        if _is_dunder(k):
            continue
        item = origin.items.get((k,))
        if item is None or (item.extent[0] < re_ and item.extent[1] > rs):
            region_keys.add(k)
        elif item.extent[1] <= rs:
            before_keys.append(k)
        else:
            after_keys.append(k)
    kept = set(before_keys) | set(after_keys)
    rkeys = [k for k in rgp if not _is_dunder(k)]
    if kept.intersection(rkeys):
        # The full parser disambiguates all top-level names globally.
        # A parsed tail cannot safely choose those keys on its own.
        return reparse_reusing(gp, new_text)

    # ── 4. the new root ──
    merged = type(gp)(source=new_text)
    for attr in _ROOT_CARRY_ATTRS:
        if hasattr(gp, attr):
            setattr(merged, attr, getattr(gp, attr))
    for k in before_keys:
        merged[k] = gp[k]
    for k in rkeys:
        v = rgp[k]
        ov = gp.get(k) if k in region_keys else None
        if isinstance(ov, dict) and isinstance(v, dict) and _parse_kind(ov) is _parse_kind(v):
            v = _merge_node(ov, v, (k,), rorigin)      # unchanged subtrees keep their identity
        merged[k] = v
    for k in after_keys:
        merged[k] = gp[k]
    old_ov = gp.get("__overrides__") if isinstance(gp.get("__overrides__"), dict) else {}
    new_ov = {k: v for k, v in old_ov.items()
              if (_is_dunder(k) and len(k) > 4 and k[2:-2] in kept) or (not _is_dunder(k) and rs > 0)}
    if isinstance(rgp.get("__overrides__"), dict):
        new_ov.update(rgp["__overrides__"])
    if new_ov:
        merged["__overrides__"] = new_ov
    for k, v in gp.items():
        if _is_dunder(k) and k not in merged and k not in (ORIGIN_KEY, "__cst__", "__overrides__"):
            merged[k] = v

    # ── 5. the residual, updated IN PLACE: forget the region's entries (start by
    #       walking the OLD region subtrees), re-base the statements after it,
    #       add the region's entries ──
    body = origin.seqs[origin.default_seq[()]]
    body_before = [it for it in body.items if it.extent[1] <= rs]
    body_after = [it for it in body.items if it.extent[0] >= re_]
    for k in region_keys:
        if k in gp:
            _forget_subtree(origin, gp[k], (k,))
    old_ov = gp.get("__overrides__") if isinstance(gp.get("__overrides__"), dict) else {}
    for k in old_ov:
        if _is_dunder(k) and len(k) > 4 and k[2:-2] not in kept:
            origin.items.pop(("__overrides__", k), None)
    if rs == 0:
        origin.items.pop(("__overrides__",), None)
    if () in origin.loose:
        origin.loose[()] = [it for it in origin.loose[()]
                            if (it.path[0] in kept) or (it.path[0] == "__overrides__" and (it.path[1:2] or ("",))[0] != ""
                                                        and it.path[1][2:-2] in kept) or (it.path == ("__overrides__",) and rs > 0)]
    rl = first_line - 1
    for b, _e, _k in origin.top_stmts[i1 + 1:]:
        b.offset += delta
        b.line += dl
    for b, _e, _k in rorigin.top_stmts:
        b.offset += rs
        b.line += rl
    origin.items.update(rorigin.items)
    idmap = {}
    for sid, sq in rorigin.seqs.items():
        if sq.owner == ():
            continue
        sq.id = origin._next_seq_id
        origin._next_seq_id += 1
        origin.seqs[sq.id] = sq
        idmap[sid] = sq.id
        for it in sq.items:
            it.seq = sq.id
    for path, sid in rorigin.default_seq.items():
        if path != () and sid in idmap:
            origin.default_seq[path] = idmap[sid]
    for path, sids in rorigin.owned.items():
        if path != ():
            origin.owned[path] = [idmap[x] for x in sids if x in idmap]
    for path, its in rorigin.loose.items():
        if path == ():
            origin.loose.setdefault((), []).extend(its)
        else:
            origin.loose[path] = its
    rbody = rorigin.seqs[rorigin.default_seq[()]]
    body.items = body_before + list(rbody.items) + body_after
    for it in rbody.items:
        it.seq = body.id
    origin.top_stmts = origin.top_stmts[:i0] + rorigin.top_stmts + origin.top_stmts[i1 + 1:]
    origin.text = new_text
    origin.src = _Src.spliced(origin.src, new_text, rs, re_, delta, rorigin.src)

    # ── 6. spans: nothing to renumber - they hang off the Base cells shifted above ──
    child_spans = {k: sp for k, sp in (getattr(gp, "_child_spans", None) or {}).items() if k in kept}
    child_spans.update(getattr(rgp, "_child_spans", None) or {})
    if child_spans:
        merged._child_spans = child_spans
    merged[ORIGIN_KEY] = origin
    merged.source = new_text
    return merged


def _common_prefix(a, b):
    """Length of the common prefix — C-speed slice compares, log(n) of them."""
    lo, hi = 0, min(len(a), len(b))
    if hi and a[:hi] == b[:hi]:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _common_suffix(a, b, prefix):
    """Length of the common suffix past `prefix` (so the two never overlap)."""
    lo, hi = 0, min(len(a), len(b)) - prefix
    if hi > 0 and a[-hi:] == b[-hi:]:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[-mid:] == b[-mid:]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _forget_subtree(origin, node, path):
    """Drop every Origin table entry the old region subtree at `path` owns:
    its node's Items (one per key, elements by index), loose comments /
    overrides, and the Seqs it owns — O(region), no table scans."""
    stack = [(path, node)]
    while stack:
        p, n = stack.pop()
        for sid in origin.owned.pop(p, ()):
            origin.seqs.pop(sid, None)
        origin.default_seq.pop(p, None)
        origin.loose.pop(p, None)
        origin.loose.pop(p + ("__overrides__",), None)
        if isinstance(n, dict):
            # Callee provenance is virtual: it has no normal dict key.
            origin.items.pop(p + ("__callee__",), None)
            ov = n.get("__overrides__")
            if isinstance(ov, dict):
                origin.items.pop(p + ("__overrides__",), None)
                for k in ov:
                    origin.items.pop(p + ("__overrides__", k), None)
            for k, v in n.items():
                if k in (ORIGIN_KEY, "__pos_names__", "__symbol_usages__", "__overrides__"):
                    continue
                cp = p + (k,)
                origin.items.pop(cp, None)
                if isinstance(v, (dict, list, tuple)):
                    stack.append((cp, v))
        elif isinstance(n, (list, tuple)):
            for i, v in enumerate(n):
                cp = p + (i,)
                origin.items.pop(cp, None)
                if isinstance(v, (dict, list, tuple)):
                    stack.append((cp, v))
    origin.items.pop(path, None)


def _merge_node(old, fresh, path, origin):
    """The node to use in place of `fresh`: `old` itself when the whole subtree
    is unchanged (coordinates refreshed onto it), else `fresh` with every
    unchanged child swapped for the old object. Reads `old` only; writes go
    into `fresh`, which is a plain (unwrapped) parse."""
    changed = ("__callee__" in old
               or [k for k in old if not _is_dunder(k)] != [k for k in fresh if not _is_dunder(k)])
    # __overrides__ / __pos_names__ are part of the node's identity (a new
    # `# [tint=...]` above a def must not be dropped for an otherwise-equal node).
    for meta in ("__overrides__", "__pos_names__"):
        if not values_equal(old.get(meta), fresh.get(meta)):
            changed = True
    for k in list(fresh.keys()):
        if _is_dunder(k):
            continue
        v = fresh[k]
        ov = old.get(k, _MISSING)
        if ov is _MISSING:
            changed = True
            continue
        item = origin.items.get(path + (k,))
        if isinstance(ov, dict) and isinstance(v, dict) and _parse_kind(ov) is _parse_kind(v):
            m = _merge_node(ov, v, path + (k,), origin)
            if m is not v:
                fresh[k] = m
            if m is not ov:
                changed = True
            if item is not None:
                item.orig = m
        elif not isinstance(v, dict) and _same_kind(ov, v) and values_equal(ov, v):
            fresh[k] = ov
            if item is not None:
                item.orig = ov
        else:
            changed = True
    if changed:
        # Bookkeeping the fresh parse doesn't produce (`__symbol_usages__`
        # distributed by the symbol generator) rides along on the new node.
        for k, v in old.items():
            if _is_dunder(k) and k not in fresh and k not in ("__cst__", "__callee__"):
                fresh[k] = v
        return fresh
    _copy_node_attrs(old, fresh)
    return old


def _copy_node_attrs(dst, src):
    for attr in ("span", "_child_spans", "source", "condition", "target", "iter", "header",
                 "func_name", "def_name"):
        if hasattr(src, attr):
            try:
                setattr(dst, attr, getattr(src, attr))
            except AttributeError:
                pass
