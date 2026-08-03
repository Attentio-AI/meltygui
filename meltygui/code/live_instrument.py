"""Transparent function instrumentation: run an in-memory TWIN of a function
whose every assignment also publishes its value through live_view. The source
file never changes — the editor keeps showing the original code — and the
captured values key and attach exactly as hand-written live_view() calls
would, so the whole marker/window/watcher stack works unchanged.

How the rendezvous holds with NO live_view tokens in the file: every injected
call is stamped with the ORIGINAL line number of the assignment it follows
(ast.copy_location), and the twin compiles against the real co_filename. At
publish time, capture-side site resolution for a line that has no live_view
call falls back to the statement's own CST key — ('x',), ('if##0', 'z'), or a
('line:N',) key for statements the dict conversion doesn't surface (aug-
assigns, while/with bodies — which therefore get coverage here that manual
live_view() calls can't anchor). The store object resolves by co_filename +
lineno to the ORIGINAL function, not the twin.

The twin is rebuilt per code version: the cache keys on the original
function's CURRENT __code__ identity, and hotswap replaces __code__ in place,
so an edit + recompile transparently re-instruments from the new source.

Out of scope (the twin falls back to running the ORIGINAL uninstrumented
function): closures (free variables can't be rebuilt standalone), generators
and async functions. Nested defs/classes/lambdas inside an instrumented
function run uninstrumented, and recursive calls re-enter the ORIGINAL
function — only the top frame snapshots.
"""

import ast
import inspect
import linecache
import sys
import textwrap
import weakref
from pathlib import Path

from src.lsd.gl_gui.view.core_conversion.live_view import live_view

# id(original __code__) -> ((source mtime, pending gen), twin function |
# original on fallback). Identity-keyed for the same reason as
# live_view._sites: code objects compare equal across files; weakref.finalize
# evicts when hotswap drops the lastref. The signature is the SEAMLESS-EDIT
# half: the twin builds from the file's IN-MEMORY text (disk + PendingSave
# splices - deferred saves never reach disk before shutdown), so both a plain
# disk save (mtime) and a queued editor edit (pending gen) rebuild the twin
# even though fn.__code__ hasn't been hotswapped - otherwise Run keeps
# replaying the pre-edit code and "nothing updates".
_twins = {}

_SNAP_NAME = "__lv_view__"


def run_instrumented(fn, *args, **kwargs):
    """Call `fn` with assignment snapshots publishing to its live_view store.
    Equivalent to fn(*args, **kwargs) — same return value, same exceptions —
    with the original function running un-instrumented when the source can't
    be transformed (closure/generator/async/unparseable).

    A successful instrumented run also PRUNES the store (run_capture): the
    twin republishes every assignment the function still contains, so keys
    it didn't touch belong to removed lines and are dropped — values,
    markers, and their orphaned value windows. The uninstrumented fallback
    republishes nothing and must not prune."""
    try:
        target = inspect.unwrap(fn)
    except Exception:
        target = fn
    twin = instrumented_twin(fn)
    try:
        if twin is target:
            return fn(*args, **kwargs)
        from src.lsd.gl_gui.view.core_conversion.live_view import run_capture
        with run_capture(target):
            return twin(*args, **kwargs)
    finally:
        # Retire the PREVIOUS run's generation: the studio's gc_manager keeps
        # gen2 out of auto-reach, so the cycle-trapped graphs each run
        # replaces (deepcopied components, the old ForwardPassResult) pin
        # their CUDA tensors until an explicit collect. This runs on the
        # run's main thread, win or lose - the problem exists either way.
        try:
            from src.lsd.gl_gui.gc_manager import collect_after_run
            collect_after_run(getattr(target, "__name__", "run"))
        except Exception:
            pass


def instrumented_twin(fn):
    """The instrumented twin of `fn` for its CURRENT SOURCE — cached, rebuilt
    after a hotswap (new __code__), a plain disk save (new mtime), OR a
    deferred in-editor edit (new PendingSave gen: the twin compiles from the
    file's in-memory text, the same text Ctrl+Enter's hotswap compiles, so
    Run executes the latest code even though deferred saves never reach
    disk). Returns `fn` itself when instrumentation isn't possible, so
    callers never need a fallback."""
    try:
        fn = inspect.unwrap(fn)
    except Exception:
        return fn
    code = getattr(fn, "__code__", None)
    if code is None:
        return fn
    try:
        mtime = Path(code.co_filename).stat().st_mtime
    except OSError:
        mtime = None
    sig = (mtime, _pending_gen(code.co_filename))
    cached = _twins.get(id(code))
    if cached is not None and cached[0] == sig:
        return cached[1]
    pending = _pending_state(code.co_filename)[1]   # O(file) - miss only
    try:
        linecache.checkcache(code.co_filename)  # getsourcelines must see the save
        twin = _build_twin(fn, pending)
    except Exception as e:
        print(f"live_instrument: falling back to uninstrumented "
              f"{getattr(fn, '__qualname__', fn)}: {e!r}", file=sys.stderr)
        twin = fn
    if cached is None:
        weakref.finalize(code, _twins.pop, id(code), None)
    _twins[id(code)] = (sig, twin)
    return twin


def _pending_gen(path):
    """Combined pending-edit generation for `path` — the CHEAP half of the
    cache signature (dict lookups only; never builds text). Sums the raw and
    resolved Path keys: queue_save keys the counter on the address's own
    path value (new_converters does the same dual lookup); only monotonicity
    matters."""
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        p = Path(path)
        gen = PendingSave.pending_gen_for(p)
        try:
            rp = p.resolve()
            if rp != p:
                gen += PendingSave.pending_gen_for(rp)
        except OSError:
            pass
        return gen
    except Exception:
        return 0


def _pending_state(path):
    """(pending edit generation, in-memory file text | None) for a
    co_filename. The text build is O(file) (current_file_text splices) — a
    MISS-only cost: callers compare a (mtime, _pending_gen) signature first
    and only then call this. gen 0 → nothing queued → (0, None) and the twin
    builds straight from disk via getsourcelines."""
    gen = _pending_gen(path)
    if not gen:
        return 0, None
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        return gen, PendingSave.current_file_text(Path(path))
    except Exception:
        return gen, None


def _delta_above(path, lineno):
    """Net line-count change of queued span edits fully ABOVE 1-indexed disk
    line `lineno` — bridges the live code's DISK coordinates (the
    co_firstlineno invariant) to positions in the pending text; the same sum
    text_editor's _pending_line_delta computes, without its buffer-cache
    plumbing."""
    from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    try:
        rp = Path(path).resolve()
    except OSError:
        return 0
    delta = 0
    for addr, (codec, kwargs) in list(PendingSave.pending_saves.items()):
        data = kwargs.get("data")
        start, end = getattr(addr, "start", None), getattr(addr, "end", None)
        if (not isinstance(data, str) or start is None or end is None
                or end > lineno - 1):
            continue
        try:
            if Path(addr.path).resolve() != rp:
                continue
        except OSError:
            continue
        d = data[:-1] if data.endswith("\n") else data
        delta += (d.count("\n") + 1) - (end - start)
    return delta


def _fn_source(fn, pending):
    """(source text incl. decorators, 1-indexed anchor line in DISK coords)
    of the function's CURRENT code. With nothing queued (`pending` is None)
    this is exactly inspect.getsourcelines. Otherwise the def is located in
    the pending text — by name, nearest its expected (delta-shifted) line —
    and the anchor maps BACK to disk coordinates so the injected snapshot
    linenos keep matching the hotswapped code's disk-coord stamps and the
    editor overlay's line math (disk span start + pending-relative offset)."""
    if pending is None:
        src_lines, start = inspect.getsourcelines(fn)
        return "".join(src_lines), start
    code = fn.__code__
    delta = _delta_above(code.co_filename, code.co_firstlineno)
    want = code.co_firstlineno + delta
    best = None
    for node in ast.walk(ast.parse(pending)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == code.co_name):
            score = abs(node.lineno - want)
            if best is None or score < best[0]:
                best = (score, node)
    if best is None:        # def renamed/deleted in the pending text
        src_lines, start = inspect.getsourcelines(fn)
        return "".join(src_lines), start
    node = best[1]
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    lines = pending.split("\n")
    return ("\n".join(lines[start - 1:node.end_lineno]) + "\n",
            start - delta)


def _build_twin(fn, pending=None):
    code = fn.__code__
    if code.co_freevars:
        return fn  # a standalone def can't rebind another frame's cells
    if code.co_flags & (inspect.CO_GENERATOR | inspect.CO_COROUTINE
                        | inspect.CO_ASYNC_GENERATOR):
        return fn

    src, start = _fn_source(fn, pending)
    tree = ast.parse(textwrap.dedent(src))
    ast.increment_lineno(tree, start - 1)
    fdef = tree.body[0]
    if not isinstance(fdef, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return fn
    # The twin is a plain function: re-running decorators would re-register
    # wrappers (render_funcs, etc.) against the twin.
    fdef.decorator_list = []

    fdef.body = _inject_snaps(fdef.body)
    ast.fix_missing_locations(tree)
    code_obj = compile(tree, code.co_filename, "exec")

    # A copy of the function's globals: the twin's def lands here instead of
    # the real module (no name clashes). The snap hook is live_view ITSELF -
    # bound directly (no dependence on the builtin install) and barely with
    # no wrapper frame in between: live_view reads sys._getframe(1), so the
    # injected call must be the twin's own frame for the site to resolve to
    # the original assignment's file + line.
    namespace = dict(fn.__globals__)
    namespace[_SNAP_NAME] = live_view
    exec(code_obj, namespace)
    twin = namespace[fdef.name]
    twin.__qualname__ = fn.__qualname__ + ".<instrumented>"
    return twin


_BLOCK_FIELDS = ("body", "orelse", "finalbody")


def _inject_snaps(body):
    """Insert `__lv_view__(<name>, name='<name>')` after every single-Name
    assignment in `body`, recursing into control-flow blocks but NOT into
    nested defs/classes (their locals live in other frames). Each injected
    call copies the assignment's location, which IS the key rendezvous."""
    out = []
    for stmt in body:
        out.append(stmt)
        name = _snap_target(stmt)
        if name is not None:
            call = ast.Expr(value=ast.Call(
                func=ast.Name(id=_SNAP_NAME, ctx=ast.Load()),
                args=[ast.Name(id=name, ctx=ast.Load())],
                keywords=[ast.keyword(arg="name",
                                      value=ast.Constant(value=name))]))
            ast.copy_location(call, stmt)
            for child in ast.walk(call):
                ast.copy_location(child, stmt)
            out.append(call)
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        for field in _BLOCK_FIELDS:
            sub = getattr(stmt, field, None)
            if isinstance(sub, list) and sub:
                setattr(stmt, field, _inject_snaps(sub))
        for handler in getattr(stmt, "handlers", None) or []:
            handler.body = _inject_snaps(handler.body)
        for case in getattr(stmt, "cases", None) or []:
            case.body = _inject_snaps(case.body)
    return out


def _snap_target(stmt):
    """The name to snapshot after `stmt`: single-Name (or first-Name chained)
    assignment targets only — tuple unpacks and attribute/subscript targets
    have no single frame-local to read."""
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                return target.id
        return None
    if isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        if isinstance(stmt.target, ast.Name):
            # An AnnAssign without a value (`x: int`) binds rather to read.
            if isinstance(stmt, ast.AnnAssign) and stmt.value is None:
                return None
            return stmt.target.id
    return None
