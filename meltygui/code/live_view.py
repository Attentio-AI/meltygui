"""live_view() — capture a running local and key it by its CST-dict address.

User code drops a bare `live_view()` after an assignment (or `live_view(expr)`
anywhere) in a function body. At runtime the call resolves its own site to the
SAME key the libcst→dict conversion gives that statement — `("if##0",
"live_view()")`, not a line number — reads the preceding local for the bare
form, and stores the value RAW on the OWNING FUNCTION object under
`__live_values__`. The editor reads the store off the object it is editing
(`live_values_for`) and renders each value as a nested window anchored to the
call's inline token, so the value and the code that produced it stay linked.

Why CST keys, not line numbers: the keys (`x`, `x#1`, `live_view()#N`,
`if##0`) are structural — they survive the line drift between code that is
RUNNING (an old frame in a training loop) and source that is being edited.
Keys are relative to the STORE OBJECT's scope (the segments after the owning
def's own "locals"), so a nested def's sites stay distinct on the outer
function they attach to, and the editor's span parse sees the same paths.
Statements the dict conversion doesn't surface (while/with/match bodies,
nested defs) fall back to a `line:N`-qualified key — still distinct per site,
just line-stable instead of edit-stable.

Why the function object: hotswap mutates `func.__code__` in place
(file_converters._recompile) and never rebinds the module var, so the function
object — and this store — keeps its identity across recompiles. The capture
side and the editor side converge on the same object through the same
resolver (`_enclosing_function`). Class-body and module-level sites attach to
the module instead (a class-body frame is not CO_OPTIMIZED — see
_resolve_site).

Two parses, both bounded: a C-speed `ast` parse of the file finds the
enclosing top-level statement and the bare form's preceding assignment (ast
sees aug-assigns, tuple unpacks, while/with bodies — everything the display
dict elides), then the libcst→dict pass runs on JUST that statement's span, so
a save of a large file costs O(function), not the whole-file position pass
that chain_converters measured at ~1s of GIL stall.

Capture must be hot-loop cheap: everything line-dependent is resolved ONCE per
(code object, line) and cached BY CODE OBJECT IDENTITY — the steady-state path
is two dict lookups, no stat / parse / sys.modules walk. A hotswap installs a
new code object, whose first live_view call re-resolves against the file; the
old code object's sites evict via weakref.finalize.

Known limit: a frame still executing OLD code after a hotswap-without-save
reports linenos that may not match the file on disk, so a site first resolved
after that point can mis-key until the function re-enters. Sites resolved
before the swap are cached and stay correct.
"""

import ast
import inspect
import sys
import threading
import time
import weakref
from pathlib import Path

_MISSING = object()

# Guards _sites entry creation only. Parses run OUTSIDE any lock - two threads
# racing a first call may duplicate a parse (benign, identical results) instead
# of one file's parse blocking every other file's resolution.
_lock = threading.Lock()

# id(code object) -> {lineno: _Site or None}. Identity-keyed, NOT a
# WeakKeyDictionary - code objects compare equal across files (co_filename is
# excluded from code equality), so identity keying aliases same-source
# functions in different files onto one site. A weakref.finalize evicts the
# entry when the code object dies - hotswap gives the function a new __code__,
# the old one's sites evict themselves. (A None site marks a failed
# resolution, so it never re-parses per call.)
_sites = {}

# str(path) -> (mtime, ast.Module, source text). One C-speed parse per file
# version, shared by span lookup and previous-assignment resolution.
_asts = {}

# (str(path), span start line) -> (mtime, LineMap). One libcst→dict pass per
# top-level statement (def/class) version - function-size, latest mtime wins.
_linemaps = {}


class _Site:
    """Everything line-dependent about one live_view call site, resolved once.
    `key_path` is relative to `store_obj`'s scope; `var_name` labels (and for
    the bare form, selects) the captured local; `arg_label` is the explicit
    form's argument source text."""
    __slots__ = ("key_path", "var_name", "arg_label", "store_obj", "lineno")

    def __init__(self, key_path, var_name, arg_label, store_obj, lineno):
        self.key_path = key_path
        self.var_name = var_name
        self.arg_label = arg_label
        self.store_obj = store_obj
        self.lineno = lineno


def live_view(value=_MISSING, name=None):
    """Publish a live value for the editor, keyed by this call's CST address.

    Bare form — `live_view()` — captures the variable assigned by the
    PRECEDING statement (aug-assigns and block bodies included; resolved from
    the ast, not the display dict). Explicit form — `live_view(expr)` —
    captures expr and passes it through, so `y = live_view(f(x))` works
    inline. Never raises into the caller; an unresolvable site is recorded
    once and skipped thereafter."""
    frame = sys._getframe(1)
    try:
        site = _site_for(frame.f_code, frame.f_lineno)
        if site is None or site.store_obj is None:
            return None if value is _MISSING else value
        bare = value is _MISSING
        if bare:
            if site.var_name is None:
                return None
            resolved = frame.f_locals.get(site.var_name, _MISSING)
            if resolved is _MISSING:
                return None
        else:
            resolved = value
        _publish(site, resolved, name, bare)
        return resolved
    finally:
        del frame


def live_values_for(obj):
    """A SNAPSHOT {key_path: value} of a function/module's store, or {}. The
    store holds the captured values RAW — no record wrapper — so a reader
    hands them straight to draw_any and the framework routes by type. Copied
    so a render-thread iteration can't race a training-thread insert. Looks
    through decorator wrappers — capture attaches to the UNWRAPPED function,
    the object whose __code__ hotswap mutates."""
    try:
        obj = inspect.unwrap(obj)
    except Exception:
        pass
    store = getattr(obj, "__live_values__", None)
    return dict(store) if store else {}


def label_for(obj, key_path):
    """The display label published for a key (the explicit name= kwarg, else
    the variable the bare form read, else the argument's source text), or
    None. Labels ride the store object beside the values (the same
    attach-to-object pattern as the watcher sets)."""
    try:
        obj = inspect.unwrap(obj)
    except Exception:
        pass
    labels = getattr(obj, "__live_labels__", None)
    return labels.get(key_path) if labels else None


def site_for_line(filename, lineno):
    """(store_obj, key_path) of the live_view call at a 1-indexed absolute
    file line — the EDITOR-side mirror of capture resolution. Same caches,
    same span parse, same relativization, so the widget anchoring a value and
    the publisher writing it converge on one key by construction. (None, None)
    when no live_view call is surfaced at that line — e.g. while/with bodies,
    whose line-keyed fallback sites have no token node to anchor anyway."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _enclosing_function, _module_for_file)
    try:
        path = Path(filename).resolve()
        mtime = path.stat().st_mtime
        tree, text = _ast_for(path, mtime)
        span = _top_level_span(tree, lineno)
        lm = _linemap_for(path, mtime, span, text)
        ref = _live_view_ref(lm, lineno)
        if ref is None:
            return None, None
        # Function-frame detection, editor flavor: capture reads CO_OPTIMIZED
        # off the frame; here the parse path crossing a `<name>, "def"`
        # segment says the call sits in a def body.
        store_obj = None
        store_is_module = True
        if _owning_def_name(ref.path) is not None:
            store_obj = _enclosing_function(str(path), lineno)
            store_is_module = store_obj is None
        if store_obj is None:
            store_obj = _module_for_file(path)
        if store_obj is None:
            return None, None
        key_path = _store_relative(ref.path, store_obj, store_is_module)
        return store_obj, (key_path or (f"line:{lineno}",))
    except Exception as e:
        print(f"live_view: site_for_line failed for {filename}:{lineno}: "
              f"{e!r}", file=sys.stderr)
        return None, None


def install_builtin(name="live_view"):
    """Make live_view() callable from ANY module with no import — the seamless
    instrumentation path: type the call, hotswap, run. Mirrors Python's own
    breakpoint(): a debugging entry point that shouldn't demand source changes
    beyond the call itself. No-op if the name is already bound to something
    else in builtins. Also taught to the static lint: code_checks freezes
    dir(builtins) at ITS import, so patch its set when it loaded first."""
    import builtins
    existing = getattr(builtins, name, None)
    if existing is not None and existing is not live_view:
        return
    setattr(builtins, name, live_view)
    checks = sys.modules.get("src.lsd.gl_gui.view.core_conversion.code_checks")
    if checks is not None and hasattr(checks, "_BUILTIN_NAMES"):
        checks._BUILTIN_NAMES = frozenset(checks._BUILTIN_NAMES) | {name}


def watch(store_obj, key_path, draw_state, first_only=False):
    """Register a draw_state to invalidate when `key_path` publishes on
    `store_obj`. Watchers ride the store object (attach-to-object, like the
    values) in a per-key WeakSet — a closed window's draw_state just drops
    out. Re-registering every render is the idempotent norm.

    first_only=True fires ONLY on a key's FIRST value: the
    marker dot registers this way, so a freshly-typed live_view() flips green
    and auto-opens the moment its code first runs — one editor re-render per
    new key — without paying a full editor recomposite on every publish.

    key_path=None registers a STORE-LEVEL watcher, fired when ANY brand-new
    key appears on the object. The snapshot overlay registers the editor this
    way: before a function's first instrumented run there are no keys, hence
    no markers, hence no per-key watchers — without this the first run's
    values would sit invisible until an unrelated editor repaint."""
    try:
        if key_path is None:
            vars(store_obj).setdefault(
                "__live_store_watchers__", weakref.WeakSet()).add(draw_state)
            return
        attr = "__live_first_watchers__" if first_only else "__live_watchers__"
        watchers = vars(store_obj).setdefault(attr, {})
        watchers.setdefault(key_path, weakref.WeakSet()).add(draw_state)
    except (AttributeError, TypeError):
        pass


_last_wake = 0.0


def _notify_watchers(store_obj, key_path, first):
    """Invalidate every draw_state watching this key (the terminal-reader
    pattern: invalidate from the publishing thread, render loop repaints) and
    wake the render loop at most ~30/s so a hot training loop can't spin it.
    First publishes additionally fire the first-only watcher set."""
    attrs = ("__live_watchers__", "__live_first_watchers__") if first else (
        "__live_watchers__",)
    notified = False
    for attr in attrs:
        watchers = getattr(store_obj, attr, None)
        if not watchers:
            continue
        try:
            targets = tuple(watchers.get(key_path) or ())
        except RuntimeError:  # concurrent registration resized the set
            continue
        for ds in targets:
            try:
                ds.invalidate()
                notified = True
            except Exception:
                pass
    if first:
        # A brand-new key: wake the store-level watchers (usually editors)
        # so the overlay re-runs and materializes this key's marker.
        try:
            store_targets = tuple(
                getattr(store_obj, "__live_store_watchers__", None) or ())
        except RuntimeError:
            store_targets = ()
        for ds in store_targets:
            try:
                ds.invalidate()
                notified = True
            except Exception:
                pass
    if not notified:
        return
    global _last_wake
    now = time.time()
    if now - _last_wake > 0.033:
        _last_wake = now
        try:
            from src.lsd.gl_gui.utils.glfw_utils import request_render
            request_render()
        except Exception:
            pass  # headless (tests) - nothing to wake


# ── capture internals ─────────────────────────────────────────────────────────

def _site_for(code, lineno):
    per_code = _sites.get(id(code))
    if per_code is not None and lineno in per_code:
        return per_code[lineno]
    with _lock:
        per_code = _sites.get(id(code))
        if per_code is None:
            per_code = {}
            _sites[id(code)] = per_code
            weakref.finalize(code, _sites.pop, id(code), None)
    # Resolve under the lock; a racing duplicate resolution writes the same
    # result twice.
    if lineno not in per_code:
        try:
            site = _resolve_site(code, lineno)
        except Exception as e:
            print(f"live_view: site resolution failed for "
                  f"{code.co_filename}:{lineno}: {e!r}", file=sys.stderr)
            site = None
        per_code[lineno] = site
    return per_code[lineno]


def _publish(site, value, name, bare):
    try:
        # setdefault on the object's __dict is atomic under the GIL, so two
        # threads first-publishing to a function can't drop a store.
        store = vars(site.store_obj).setdefault("__live_values__", {})
    except (AttributeError, TypeError):
        return
    first = site.key_path not in store
    label = name or (site.var_name if bare else site.arg_label)
    if label:
        vars(site.store_obj).setdefault("__live_labels__", {})[
            site.key_path] = label
    # The value is stored RAW - a single object assignment, atomic under the
    # GIL, so the rendering thread always reads either the old or new value.
    store[site.key_path] = value
    _notify_watchers(site.store_obj, site.key_path, first=first)


def _resolve_site(code, lineno):
    """The slow once-per-(code, line) path: locate the enclosing top-level
    statement via ast, run the dict conversion on just that span, and derive
    this call's key, the preceding assignment's name, and the store object."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _enclosing_function, _module_for_file)

    path = Path(code.co_filename).resolve()
    mtime = path.stat().st_mtime
    tree, text = _ast_for(path, mtime)
    var_name = _previous_assign_name(tree, lineno)

    # A class body or method frame is not CO_OPTIMIZED; _enclosing_function
    # would wrongly pick the nearest def ABOVE such a line (it has the end
    # check), so only function frames attach to a function.
    store_obj = None
    store_is_module = True
    if code.co_flags & inspect.CO_OPTIMIZED:
        store_obj = _enclosing_function(code.co_filename, lineno)
        store_is_module = store_obj is None
    if store_obj is None:
        store_obj = _module_for_file(path)

    span = _top_level_span(tree, lineno)
    lm = _linemap_for(path, mtime, span, text)
    arg_label = None

    ref = _live_view_ref(lm, lineno)
    if ref is not None:
        key_path = _store_relative(ref.path, store_obj, store_is_module)
        arg_label = _arg_source(ref.value)
    else:
        # The line isn't surfaced by a bare call: either it's an
        # assignment form (y = live_view(x)`, keyed by its target - a real
        # statement key, keep it) or it sits in a body the dict conversion
        # doesn't extract (while/try/catch, nested def). The latter resolves
        # to a CONTAINER, which would conflict across sites - qualify by line.
        fallback = lm.node_at_line(lineno, absolute=True)
        if fallback is not None:
            full_path, call_node = _truncate_into_call(lm.root, fallback.path)
            key_path = _store_relative(full_path, store_obj, store_is_module)
            arg_label = _arg_source(call_node)
            if call_node is None and isinstance(fallback.value, dict):
                # The line landed on a CONTAINER (a while/with body the dict
                # conversion doesn't surface) - this key would collide across
                # every site in the block, so qualify by line. A LEAF hit is
                # the call's own key: statement-level, keep it is.
                key_path = key_path + (f"line:{lineno}",)
        else:
            key_path = (f"line:{lineno}",)
    if not key_path:
        key_path = (f"line:{lineno}",)
    if arg_label is None:
        # No CallParse to read the arg from (un-surfaced body) - use the ast.
        arg_label = _arg_source_ast(tree, lineno)
    return _Site(key_path, var_name, arg_label, store_obj, lineno)


def _ast_for(path, mtime):
    cached = _asts.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1], cached[2]
    text = path.read_text()
    tree = ast.parse(text)
    _asts[str(path)] = (mtime, tree, text)
    return tree, text


def _top_level_span(tree, lineno):
    """(start, end) 1-indexed file lines of the top-level statement containing
    `lineno` — the def/class (decorators included) whose span the dict
    conversion runs on. None when no statement contains the line."""
    for stmt in tree.body:
        end = getattr(stmt, "end_lineno", stmt.lineno)
        start = min([stmt.lineno] + [d.lineno for d in
                                     getattr(stmt, "decorator_list", [])])
        if start <= lineno <= end:
            return start, end
    return None


def _linemap_for(path, mtime, span, text):
    """The LineMap of one top-level statement's span (whole file when span is
    None), rebuilt when mtime changes. Span-bounded so a save re-parses one
    function, not the file — the whole-file position pass holds the GIL for
    ~1s on big modules (see chain_converters' measurement) and this runs on
    the calling thread."""
    import libcst as cst
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
        LineMap, cst_module_to_dict)

    start = span[0] if span else 1
    key = (str(path), start)
    cached = _linemaps.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    if span is None:
        snippet = text
    else:
        lines = text.splitlines(keepends=True)
        snippet = "".join(lines[span[0] - 1:span[1]])
    parse = cst_module_to_dict(cst.parse_module(snippet))
    lm = LineMap(parse, line_offset=start - 1)
    _linemaps[key] = (mtime, lm)
    return lm


def _live_view_ref(lm, lineno):
    """The NodeRef of the live_view CALL whose span contains `lineno` — matched
    by key, not node_at_line, because the deepest node at the line is usually
    an ARG inside the CallParse. Deepest match wins. (Reads LineMap's _entries
    index directly — in-package machinery, same data node_at_line scans.)"""
    rel_line = lineno - lm.line_offset
    best = None
    for span, depth, ref in lm._entries:
        if not (span.start_line <= rel_line <= span.end_line):
            continue
        if not isinstance(ref.key, str):
            continue
        if ref.key.split("#", 1)[0] != "live_view()":
            continue
        if best is None or depth > best[0]:
            best = (depth, ref)
    return best[1] if best else None


def _truncate_into_call(root, path):
    """Cut `path` segments that descend INSIDE a call's argument dict — the
    statement key is the site's address; which argument the line landed on is
    not. Returns (path, call_parse) where call_parse is the CallParse the path
    was cut at (None if the path never enters one)."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import CallParse
    node = root
    for i, seg in enumerate(path):
        node = node.get(seg) if isinstance(node, dict) else None
        if isinstance(node, CallParse):
            return tuple(path[:i + 1]), node
        if node is None:
            break
    return tuple(path), None


def _owning_def_name(path):
    """The name of the innermost def a key path crosses (`..., <name>,
    "locals", ...`), or None for module/class-level paths."""
    for i in range(len(path) - 1, 0, -1):
        if path[i] == "locals":
            return path[i - 1]
    return None


def _store_relative(path, store_obj, store_is_module):
    """Key path relative to the STORE OBJECT's scope. For a function store,
    cut at the first `<func name>, "locals"` pair — nested-def segments stay,
    so two closures' sites on the same outer function can't collide, and the
    editor parsing that function's span sees the same paths. Module stores
    keep the full path."""
    if store_is_module:
        return tuple(path)
    fn_name = getattr(store_obj, "__name__", None)
    for i in range(len(path) - 1):
        if path[i] == fn_name and path[i + 1] == "locals":
            return tuple(path[i + 2:])
    # Renamed wrapper / span mismatch: fall back to the innermost scope cut.
    for i in range(len(path) - 1, -1, -1):
        if path[i] == "locals":
            return tuple(path[i + 1:])
    return tuple(path)


def _arg_source(call_parse):
    """The source text of an explicit `live_view(expr)` argument, for display.
    The positional arg binds to live_view's `value` parameter when the
    signature resolves, else surfaces under a synthetic `arg0` key — take the
    first real entry either way."""
    if not isinstance(call_parse, dict):
        return None
    arg = call_parse.get("value")
    if arg is None:
        for k, v in call_parse.items():
            if isinstance(k, str) and not k.startswith("__"):
                arg = v
                break
    return str(arg) if arg is not None else None


# ── previous-assignment resolution (ast) ──────────────────────────────────────
# The display dict drops statements it can't surface (aug-assigns, tuple
# unpacks, while/with/match bodies), so walking its keys silently skips them
# and captures an OLDER variable. The ast sees every statement; resolve the
# bare form against it instead.

def _previous_assign_name(tree, lineno):
    """The variable assigned by the statement preceding the live_view call at
    `lineno`: locate the deepest body list holding the call's statement, walk
    its preceding siblings backwards, and take the first assignment target —
    descending into a preceding block's last assignment, skipping defs."""
    found = _stmt_in_body(tree.body, lineno)
    if found is None:
        return None
    body, idx = found
    for stmt in reversed(body[:idx]):
        name = _direct_target(stmt)
        if name is _STOP:
            # The preceding statement DOES bind something we can't name (tuple
            # unpack, obj.attr / a[i] target) - publishing an older variable
            # instead would be silently losing data. Capture nothing.
            return None
        if name is not None:
            return name
        name = _last_target(stmt)  # blocks capture their last assignment
        if name is not None:
            return name
    return None


_BODY_FIELDS = ("body", "orelse", "finalbody")


def _stmt_in_body(body, lineno):
    """(body list, index) of the statement containing a live_view call at
    `lineno`, in the DEEPEST body list that holds it."""
    for i, stmt in enumerate(body):
        end = getattr(stmt, "end_lineno", stmt.lineno)
        if not (stmt.lineno <= lineno <= end):
            continue
        for field in _BODY_FIELDS:
            sub = getattr(stmt, field, None)
            if isinstance(sub, list) and sub:
                found = _stmt_in_body(sub, lineno)
                if found is not None:
                    return found
        for handler in getattr(stmt, "handlers", None) or []:
            found = _stmt_in_body(handler.body, lineno)
            if found is not None:
                return found
        for case in getattr(stmt, "cases", None) or []:
            found = _stmt_in_body(case.body, lineno)
            if found is not None:
                return found
        if _holds_live_view_call(stmt):
            return body, i
        return None
    return None


def _arg_source_ast(tree, lineno):
    """The explicit argument's source for a live_view call at `lineno`, read
    from the ast — the label fallback for calls in bodies the dict conversion
    doesn't surface (while/with/match, nested defs)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        end = getattr(node, "end_lineno", node.lineno)
        if not (node.lineno <= lineno <= end):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None)
        if name == "live_view":
            return ast.unparse(node.args[0])
    return None


def _holds_live_view_call(stmt):
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name == "live_view":
                return True
    return False


_STOP = object()


def _direct_target(stmt):
    """A direct assignment's target name; _STOP for an assignment whose target
    can't be named as a frame local (tuple unpack, obj.attr, a[i]); None for a
    non-binding statement (defs and classes bind names whose body locals are
    not in the caller's frame — non-binding here)."""
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:  # p = q = v → the first plain-name target
            if isinstance(target, ast.Name):
                return target.id
        return _STOP
    if isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        return stmt.target.id if isinstance(stmt.target, ast.Name) else _STOP
    return None


def _last_target(stmt):
    """The LAST direct assignment name anywhere inside a block statement, in
    source order — what a bare live_view right after an if/for/try captures."""
    last = None
    direct = _direct_target(stmt)
    if direct is not None:
        return direct if direct is not _STOP else None
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return None
    for field in _BODY_FIELDS:
        for sub in getattr(stmt, field, None) or []:
            name = _last_target(sub)
            if name is not None:
                last = name
    for handler in getattr(stmt, "handlers", None) or []:
        for sub in handler.body:
            name = _last_target(sub)
            if name is not None:
                last = name
    return last
