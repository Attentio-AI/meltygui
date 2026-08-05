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
import types
import weakref
from contextlib import contextmanager
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


def twin_snap(value, name=None):
    """The instrumented twin's injected snapshot hook (`__lv_view__`) — the
    FAST publish path. Synthesizes the same ``line:N#name`` keys the
    frame-snapshot publisher uses (the overlay anchors line keys by line and
    boxes by label), so a twin run does NO site resolution: the classic path
    pays a libcst linemap per enclosing span (~240ms for a big function,
    the whole CLASS for a method) on every fresh twin code object — i.e.
    after every edit. Store resolution is the cached _enclosing_function
    walk. Sharing the frame-snapshot key scheme also means a twin run and a
    context-menu snapshot UPDATE THE SAME MARKERS instead of doubling them.
    Manual live_view() calls in the body keep the structural-key path (their
    markers anchor to the call token). Falls back to the classic resolved
    path when the store can't be resolved."""
    frame = sys._getframe(1)
    try:
        code, lineno = frame.f_code, frame.f_lineno
    finally:
        del frame
    try:
        from src.lsd.gl_gui.view.core_conversion.chain_converters import (
            _enclosing_function)
        fn = _enclosing_function(code.co_filename, lineno)
        if (isinstance(fn, types.FunctionType)
                and isinstance(name, str) and name.isidentifier()):
            site = _Site((f"line:{lineno}#{name}",), None, None, fn, lineno)
            _publish(site, value, name, bare=False)
            return value
    except Exception:
        pass
    site = _site_for(code, lineno)
    if site is not None and site.store_obj is not None:
        _publish(site, value, name, bare=False)
    return value


def twin_ret(value=None):
    """The instrumented twin's return hook (`__lv_ret__`): every `return X`
    in the twin compiles as `return __lv_ret__(X)` (bare `return` passes
    None), so the calling frame's f_lineno IS the return statement's
    original file line. Stamps `__live_return_line__` (absolute 1-indexed
    file line) on the resolved store function and wakes the store-level
    watchers, so the editor's snapshot overlay can wash that line green —
    the success twin of the red error-line wash — the moment the run comes
    out. run_instrumented clears the stamp at run start, so a raise (or an
    edit that removes the return) never leaves a stale green line. Returns
    the value unchanged."""
    frame = sys._getframe(1)
    try:
        code, lineno = frame.f_code, frame.f_lineno
    finally:
        del frame
    try:
        from src.lsd.gl_gui.view.core_conversion.chain_converters import (
            _enclosing_function)
        fn = _enclosing_function(code.co_filename, lineno)
        if isinstance(fn, types.FunctionType):
            stamp_run_marker(fn, "__live_return_line__", lineno)
    except Exception:
        pass
    return value


def stamp_run_marker(fn, attr, value):
    """Stamp a per-run marker attribute (`__live_return_line__`,
    `__live_error_line__`) on a store function and wake its store-level
    watchers (the snapshot editors) with the same throttled render wake the
    publish path uses, so the editor washes appear without an unrelated
    repaint. Safe from any thread; swallows everything — markers are
    decoration, never worth breaking a run over."""
    try:
        vars(fn)[attr] = value
        notified = False
        try:
            targets = tuple(
                getattr(fn, "__live_store_watchers__", None) or ())
        except RuntimeError:
            targets = ()
        for ds in targets:
            try:
                ds.invalidate()
                notified = True
            except Exception:
                pass
        if notified:
            global _last_wake
            now = time.time()
            if now - _last_wake > 0.033:
                _last_wake = now
                from src.lsd.gl_gui.utils.glfw_utils import request_render
                request_render()
    except Exception:
        pass


def call_with_body_capture(func, kwargs):
    """Call ``func(**kwargs)`` and capture its body frame's locals at return,
    handing them to the async frame-snapshot publisher.

    This is the context menu's one-shot answer to "what are the TARGET's
    mid-body locals": the wrapper's stack capture runs before the body
    executes, so entry kwargs were all it could publish and the func tab
    showed markers only on the signature. A per-thread profile hook watches
    for the target code's outermost 'return' (depth-tracked, so a
    self-recursive view captures the WIDGET's frame, not an inner one);
    profiling covers only this one call's subtree, armed on menu-open only —
    never steady-state. An exception unwind still fires the profile return
    event, so a crashed body publishes its state at the raise."""
    inner = inspect.unwrap(func)
    target_code = getattr(inner, "__code__", None)
    if target_code is None:
        return func(**kwargs)
    captured = {}
    exit_line = [None]
    depth = 0

    prev = sys.getprofile()

    def prof(frame, event, arg):
        nonlocal depth
        if frame.f_code is not target_code:
            return
        if event == "call":
            depth += 1
        elif event == "return":
            depth -= 1
            if depth <= 0 and not captured:
                try:
                    captured.update(frame.f_locals)
                except Exception:
                    pass
                # The frame's lineno AT the return event is the return
                # statement's (or the raise, on an exception unwind) -
                # the same __live_return_line__ the instrumented twin
                # stamps, so the func tab's editor gets the green exit-line
                # wash from a plain body capture too.
                exit_line[0] = frame.f_lineno

    sys.setprofile(prof)
    try:
        return func(**kwargs)
    finally:
        sys.setprofile(prev)
        if captured:
            if exit_line[0] is not None:
                stamp_run_marker(inner, "__live_return_line__", exit_line[0])
            publish_stack_locals((), extra_snapshots=[(inner, captured, None)])


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
        tree, text, sig = _ast_for(path, mtime)
        # Same stamp to line bridge as _resolve_site, anchored at the
        # enclosing def's disk start when one resolves (module-level stamps
        # anchor at the stamp - no own-span growth to mis-count there).
        _fn = _enclosing_function(str(path), lineno)
        _fc = getattr(_fn, "__code__", None)
        line_p = lineno + _stamp_delta(
            path, _fc.co_firstlineno if _fc is not None else lineno)
        span = _top_level_span(tree, line_p)
        lm = _linemap_for(path, sig, span, text)
        ref = _live_view_ref(lm, line_p)
        if ref is None:
            return None, None
        # Function-frame detection, editor flavor: capture reads CO_OPTIMIZED
        # off the frame; here the parse path crossing a `<name>, "def"`
        # segment says the call sits in a def body.
        store_obj = None
        store_is_module = True
        if _owning_def_name(ref.path) is not None:
            store_obj = _fn          # resolved above for the line bridge
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
            # The open value WINDOW is a nested root - the marker's
            # invalidate stops at its own tile + ancestors and never
            # reaches the window tile, - a rerun's publish leaves the
            # window blitting the stale value. Invalidate FORCE-down its
            # subtree via the marker's window handle (the draw_function
            # completion pattern - safe from the publishing thread).
            win = getattr(ds, "_lv_window_ds", None)
            if win is not None and not getattr(win, "closed", False):
                try:
                    from src.lsd.gl_gui.melty import Melty
                    Melty.cache.invalidate_up(win._tile_id, force=True,
                                              max_depth=8)
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
    # Run-scope liveness: while a run_capture is active for this store,
    # every published key is recorded so the run's exit can prune the rest
    # (set.add - atomic under the GIL).
    touched = vars(site.store_obj).get("__live_touched__")
    if touched is not None:
        touched.add(site.key_path)
    _record_scope_type(site, value, name, bare)
    _notify_watchers(site.store_obj, site.key_path, first=first)


def _record_scope_type(site, value, name, bare):
    """Feed the published value's runtime type into FuncsMetadata, keyed by the
    owning function — so an editor on that function's source autocompletes
    `x.` against the LIVE type of x, for every local a live_view / snapshot
    run has seen. This is the mid-body complement to the context menu's stack
    capture (which only sees callers' frames and the target's entry kwargs).

    The recorded NAME must be a real local: the bare form's resolved
    assignment target, or an explicit call's arg source when it is a plain
    identifier (`live_view(attn_weights, ...)`); expression args (`x[0].w`)
    and display-only `name=` labels are skipped. One exception: a snapshot
    stamp (instrumented_twin's injected `__lv_view__(x, name='x')`) has NO
    call in the SOURCE at its line, so the site carries no arg_label — there
    the injected `name` IS the assignment target by construction, and only
    then is it trusted as the local's name. Module/class-body stores are
    skipped too — module-level names already complete via the live namespace
    walk. Cheap on hot publish paths: record_value no-ops on an unchanged
    type. Best-effort; a hiccup must never break a publish."""
    store_obj = site.store_obj
    if not isinstance(store_obj, types.FunctionType):
        return
    local_name = site.var_name if bare else site.arg_label
    if not (isinstance(local_name, str) and local_name.isidentifier()):
        if not (site.arg_label is None and not bare and isinstance(name, str)):
            return
        local_name = name
        if not local_name.isidentifier():
            return
    try:
        from src.lsd.gl_gui.func_metadata import FuncsMetadata
        FuncsMetadata.record_value(store_obj, local_name, value)
    except Exception:
        pass


@contextmanager
def run_capture(store_obj):
    """Scope one instrumented run over `store_obj` (pass the UNWRAPPED
    function — the object capture attaches to). Keys published inside the
    with-block are recorded, and a SUCCESSFUL exit prunes every other key:
    an instrumented run republishes every assignment it still contains, so
    anything not touched is a REMOVED line — without this, stale keys linger
    forever as ghost markers, orphaned value windows, and line:N entries
    that jumble future resolution. An exception skips the prune: a partial
    run proves nothing about which sites still exist."""
    try:
        vars(store_obj)["__live_touched__"] = set()
    except (AttributeError, TypeError):
        yield
        return
    try:
        yield
    except BaseException:
        vars(store_obj).pop("__live_touched__", None)
        raise
    touched = vars(store_obj).pop("__live_touched__", set())
    _prune_untouched(store_obj, touched)


def _prune_untouched(store_obj, touched):
    """Drop every store key not in `touched` — run_capture's whole-store sweep.
    The per-key removal mechanics live in _prune_keys (shared with the frame-
    snapshot publisher, which prunes only ITS OWN stale keys)."""
    store = getattr(store_obj, "__live_values__", None)
    if not store:
        return
    _prune_keys(store_obj, [k for k in tuple(store) if k not in touched])


def _prune_keys(store_obj, removed):
    """Remove the given store keys: value, label, per-key watcher sets, and
    any open value window (win.closed = True — the next root_draw_states
    dispatch discards it; if the key ever republishes, the marker re-registers
    its window with closed= driven fresh). Store-level watchers (the snapshot
    editors) are invalidated so the overlay re-runs without the removed
    markers. Safe from any thread — dict pops/copies are GIL-atomic and
    ds.invalidate() is the established cross-thread completion pattern."""
    store = getattr(store_obj, "__live_values__", None)
    if not store or not removed:
        return
    labels = getattr(store_obj, "__live_labels__", None)
    for key in removed:
        store.pop(key, None)
        if labels:
            labels.pop(key, None)
        for attr in ("__live_watchers__", "__live_first_watchers__"):
            watchers = getattr(store_obj, attr, None)
            if not watchers:
                continue
            try:
                targets = tuple(watchers.pop(key, None) or ())
            except RuntimeError:
                targets = ()
            for ds in targets:
                win = getattr(ds, "_lv_window_ds", None)
                if win is not None:
                    try:
                        win.closed = True
                    except Exception:
                        pass
                try:
                    ds._lv_open = False
                    ds.invalidate()
                except Exception:
                    pass
    try:
        store_targets = tuple(
            getattr(store_obj, "__live_store_watchers__", None) or ())
    except RuntimeError:
        store_targets = ()
    for ds in store_targets:
        try:
            ds.invalidate()
        except Exception:
            pass
    try:
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()
    except Exception:
        pass  # headless (test)


# ── frame snapshots (context-menu capture -> live-value stores) ──────────────
# The context menu's stack entry holds every called frame's fscope for one
# snapshot. Publishing them through the SAME site/key pipeline the instrumented
# twin uses makes them first-class live values: markers, live value windows,
# watchers, voxel, and FuncsMetadata typing all come along for free, in
# ANY editor that shows the function - no new rendering machinery.

def publish_frame_snapshot(fn, scope, upto_lineno=None):
    """Publish a ``{name: value}`` scope snapshot into ``fn``'s live-value
    store, anchored at EVERY occurrence of each name in the def's own scope:
    parameters at their signature lines, and every Name reference — Load and
    Store alike (assignments in all forms, loop/with targets, walrus, and
    plain reads) — so any mention of a local in the source is a live view,
    not just its binding. One key per (name, line); all of a name's markers
    show the same captured value. ``upto_lineno`` is accepted for API
    stability but no longer gates anchors: the snapshot IS the frame's state
    at capture, and every reference line is an equally valid place to
    inspect it.

    Deliberately NO site resolution: keys are synthesized ``line:N#name``
    tails, which the overlay anchors by line and boxes by label. The
    structural key `_resolve_site` derives costs a libcst parse of the whole
    enclosing span (~240ms for draw_collection, the whole CLASS for a
    method) and produces a prefix the overlay ignores for line-keyed
    entries. Total cost here: one cached whole-file ast + one walk of the
    def + N dict writes.

    Keys this publisher created in a PREVIOUS snapshot that this one didn't
    re-touch are pruned (``__frame_snapshot_keys__`` on ``fn``) so edits
    between menu-opens can't leave ghost markers; keys owned by other
    writers (manual live_view calls, twin runs) are never touched.

    Accepts a render_func WRAPPER too — unwrapped here, since anchors and
    the store must live on the real body function (the wrapper's __code__
    points at core_render)."""
    try:
        fn = inspect.unwrap(fn)
    except Exception:
        pass
    code = getattr(fn, "__code__", None)
    if code is None or not isinstance(fn, types.FunctionType) or not scope:
        return
    path = Path(code.co_filename).resolve()
    try:
        tree, _text, _sig = _ast_for(path, path.stat().st_mtime)
    except (OSError, SyntaxError, ValueError):
        return
    delta = _stamp_delta(path, code.co_firstlineno)   # pending = disk + delta
    fdef = _def_node_for(tree, fn, code.co_firstlineno + delta)
    if fdef is None:
        return
    anchors = _occurrence_lines(fdef)
    new_keys = set()
    for n, lns in anchors.items():
        if "." in n:
            # Attribute chain (`draw_state.some_val`, each segment of
            # `a.b.c` anchors separately): resolve the value through the
            # captured base object - instance __dict__ / plain class attrs
            # only, never through properties or descriptors (a DrawState
            # geometry getter must be run on the main worker).
            ok, val = _resolve_dotted(scope, n)
            if not ok:
                continue
        elif n in scope:
            val = scope[n]
        else:
            continue
        for ln in lns:
            disk = ln - delta
            site = _Site((f"line:{disk}#{n}",), None, None, fn, disk)
            _publish(site, val, n, bare=False)
            new_keys.add(site.key_path)
    try:
        prev = vars(fn).get("__frame_snapshot_keys__") or set()
        _prune_keys(fn, [k for k in prev - new_keys])
        vars(fn)["__frame_snapshot_keys__"] = new_keys
    except (AttributeError, TypeError):
        pass


def _def_node_for(tree, fn, target_lineno):
    """``fn``'s FunctionDef in the (pending-text) ast: name match, def line
    nearest ``target_lineno`` — the co_firstlineno proximity trick
    live_instrument uses, tolerant of the decorator-line offset. Scans
    module and class bodies only (a full ast.walk is O(file nodes) per
    call, and _enclosing_function can't resolve deeper functions anyway)."""
    best, best_d = None, None
    want = getattr(fn, "__name__", None)

    def consider(node):
        nonlocal best, best_d
        d = abs(node.lineno - target_lineno)
        if best_d is None or d < best_d:
            best, best_d = node, d

    for stmt in tree.body:
        if (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                and stmt.name == want):
            consider(stmt)
        elif isinstance(stmt, ast.ClassDef):
            for sub in stmt.body:
                if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name == want):
                    consider(sub)
    return best


# Scopes a binding walk must NOT descend into: their Store names bind in a
# DIFFERENT frame (nested defs/classes, lambdas, comprehensions).
_FOREIGN_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                   ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                   ast.GeneratorExp)


def _occurrence_lines(fdef):
    """``{name: set of pending linenos}`` of every occurrence of a name in
    ``fdef``'s own scope: parameters at their signature lines, then EVERY
    Name node — Store and Load alike — so references anchor live views, not
    just bindings. (The publisher filters to names actually captured in the
    frame's scope, which is also what keeps module globals like `imgui` out:
    they're Load names here but never frame locals.) Pure ATTRIBUTE chains
    off a Name (`draw_state.some_val`, and each inner segment of `a.b.c`)
    anchor too, under their dotted spelling — single-line chains only (the
    overlay boxes the final segment by regex on its line) and never through
    calls/subscripts (`foo().x` has no frame-resolvable base). Nested
    defs/classes/lambdas/comprehensions are not descended — their names
    live in other frames."""
    anchors = {}
    a = fdef.args
    params = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    for extra in (a.vararg, a.kwarg):
        if extra is not None:
            params.append(extra)
    for arg in params:
        anchors.setdefault(arg.arg, set()).add(arg.lineno)

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FOREIGN_SCOPES):
                continue
            if isinstance(child, ast.Name):
                anchors.setdefault(child.id, set()).add(child.lineno)
            elif (isinstance(child, ast.Attribute)
                  and getattr(child, "end_lineno", child.lineno) == child.lineno):
                dotted = _dotted_name(child)
                if dotted is not None:
                    anchors.setdefault(dotted, set()).add(child.lineno)
            walk(child)

    walk(ast.Module(body=fdef.body, type_ignores=[]))
    return anchors


def _dotted_name(node):
    """`a.b.c` for a pure Name-rooted attribute chain, else None (a call,
    subscript or literal anywhere in the chain has no frame-local base)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _safe_attr_value(base, attr):
    """(ok, value) of ``base.attr`` WITHOUT running any code: instance
    __dict__ first, then a plain class attribute via getattr_static —
    properties, descriptors, and class-level functions (methods) are
    refused rather than fired or published as noise."""
    d = getattr(base, "__dict__", None)
    if isinstance(d, dict) and attr in d:
        return True, d[attr]
    try:
        static = inspect.getattr_static(base, attr)
    except Exception:
        return False, None
    if isinstance(static, (staticmethod, classmethod, property)):
        return False, None
    if (inspect.isfunction(static) or inspect.ismethoddescriptor(static)
            or inspect.isdatadescriptor(static) or inspect.isbuiltin(static)):
        return False, None
    return True, static


def _resolve_dotted(scope, dotted):
    """(ok, value) of a dotted occurrence resolved from the captured frame
    scope — the base must be a captured local, every hop must pass
    _safe_attr_value."""
    parts = dotted.split(".")
    if parts[0] not in scope:
        return False, None
    obj = scope[parts[0]]
    for seg in parts[1:]:
        ok, obj = _safe_attr_value(obj, seg)
        if not ok:
            return False, None
    return True, obj


# Serializes snapshot workers: rapid menu-opens must not interleave two
# publishes' shared bookkeeping on the same function.
_snapshot_lock = threading.Lock()


def publish_stack_locals(frames, extra_snapshots=None):
    """The context-menu capture hook: publish every real caller frame's
    locals into that function's live-value store (see publish_frame_snapshot).
    ``frames`` is the raw get_live_frames output — entry[4] is the frame's
    f_locals copy. ``extra_snapshots`` is a list of extra (fn, scope,
    upto_lineno) publishes to run in the same batch (the capture site adds
    the TARGET view function's entry scope).

    Runs on a short-lived daemon worker: a first publish's site resolution
    parses the enclosing span through libcst (one linemap per function,
    cached per file-gen; a METHOD's span is its whole class) — far too heavy
    for the render thread at menu-open. The store/watcher machinery is
    worker-safe by design (the instrumented twin publishes from workers);
    markers appear a beat after the menu via the normal watcher wake."""
    threading.Thread(target=_publish_stack_locals_sync,
                     args=(frames, extra_snapshots),
                     name="lv-frame-snapshot", daemon=True).start()


def _publish_stack_locals_sync(frames, extra_snapshots=None):
    """Worker body of publish_stack_locals. Dispatch machinery and
    non-project source are skipped, and a frame whose resolved function's
    name doesn't match (lambdas, comprehensions) is dropped rather than
    mis-published. Each item ALSO records its full scope's runtime types
    into FuncsMetadata here (aliases like ds/value included — the anchored
    publishes only cover source-bound names), so the capture site pays for
    nothing but the stack grab itself. Best-effort per item — a resolution
    hiccup must never break the batch."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _is_dispatch_frame, _enclosing_function)
    from src.lsd.gl_gui.view.core_conversion.address import is_editable_source
    from src.lsd.gl_gui.func_metadata import FuncsMetadata
    with _snapshot_lock:
        for entry in frames or ():
            if len(entry) < 5 or not entry[4]:
                continue
            filename, lineno, func_name = entry[0], entry[1], entry[2]
            try:
                if (_is_dispatch_frame(filename, func_name)
                        or not is_editable_source(filename)):
                    continue
                fn = _enclosing_function(filename, lineno)
                if fn is None or getattr(fn, "__name__", None) != func_name:
                    continue
                FuncsMetadata.record(fn, entry[4])
                publish_frame_snapshot(fn, entry[4], upto_lineno=lineno)
            except Exception:
                continue
        for fn, scope, upto in extra_snapshots or ():
            try:
                FuncsMetadata.record(fn, scope)
                publish_frame_snapshot(fn, scope, upto_lineno=upto)
            except Exception:
                continue


def _stamp_delta(path, anchor_line):
    """Stamp→pending line bridge. Line stamps (twin snapshot calls, editor
    overlay lookups) are DISK-anchored — enclosing span's disk start +
    pending-relative offset, the co_firstlineno invariant — while _ast_for's
    tree is the PENDING text. The difference is the net shift of queued
    edits fully above the enclosing def, so callers anchor at the DEF start
    (co_firstlineno / the resolved function), never at the stamp itself: a
    grown function's stamps can sit past its own span's disk end, which
    would wrongly count the span's own edit into the delta."""
    from src.lsd.gl_gui.view.core_conversion.live_instrument import (
        _delta_above, _pending_gen)
    if not _pending_gen(str(path)):
        return 0
    return _delta_above(str(path), anchor_line)


def _resolve_site(code, lineno):
    """The slow once-per-(code, line) path: locate the enclosing top-level
    statement via ast, run the dict conversion on just that span, and derive
    this call's key, the preceding assignment's name, and the store object.
    Tree/LineMap lookups use pending coords (line_p); the store resolution
    and the published line:N keys keep the raw DISK-anchored stamp — live
    co_firstlineno values and the editor overlay both speak that
    convention."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _enclosing_function, _module_for_file)

    path = Path(code.co_filename).resolve()
    mtime = path.stat().st_mtime
    tree, text, sig = _ast_for(path, mtime)
    line_p = lineno + _stamp_delta(
        path, code.co_firstlineno
        if code.co_flags & inspect.CO_OPTIMIZED else lineno)
    var_name = _previous_assign_name(tree, line_p)

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

    span = _top_level_span(tree, line_p)
    lm = _linemap_for(path, sig, span, text)
    arg_label = None

    ref = _live_view_ref(lm, line_p)
    if ref is not None:
        key_path = _store_relative(ref.path, store_obj, store_is_module)
        arg_label = _arg_source(ref.value)
    else:
        # The line isn't surfaced by a bare call: either it's an
        # assignment form (y = live_view(x)`, keyed by its target - a real
        # statement key, keep it) or it sits in a body the dict conversion
        # doesn't extract (while/try/catch, nested def). The latter resolves
        # to a CONTAINER, which would conflict across sites - qualify by line.
        fallback = lm.node_at_line(line_p, absolute=True)
        if fallback is not None:
            full_path, call_node = _truncate_into_call(lm.root, fallback.path)
            key_path = _store_relative(full_path, store_obj, store_is_module)
            # arg_label means "the explicit live_view(expr) argument" - only a
            # live_view call may supply it. The truncation cuts at ANY CallParse
            # the path hits, so a snapshot stamp on `x = obj.method("lit")`
            # lands here on the RHS call - reading ITS first arg produced a
            # bogus label ('"lit"'), which also blocked _record_scope_type's
            # trust-the-injected-name rule (arg_label must be None for stamps).
            arg_label = (_arg_source(call_node)
                         if _is_live_view_callparse(call_node) else None)
            if (call_node is None and isinstance(fallback.value, dict)
                    and fallback.span.start_line != line_p - lm.line_offset):
                # The line landed in an enclosing CONTAINER (a while/with
                # body the dict conversion doesn't surface) - that key would
                # collide across every site in the block, so qualify by
                # line. A hit whose statement STARTS at this line is the
                # statement's bare entry - including a dict-VALUED assignment
                # (`x = {...}`: value is a dict instance, but it's a call) -
                # and keeps its clean statement key.
                key_path = key_path + (f"line:{lineno}",)
        else:
            key_path = (f"line:{lineno}",)
    if not key_path:
        key_path = (f"line:{lineno}",)
    if arg_label is None:
        # No CallParse to read the arg from (un-surfaced body) - use the ast.
        arg_label = _arg_source_ast(tree, line_p)
    return _Site(key_path, var_name, arg_label, store_obj, lineno)


def _ast_for(path, mtime):
    """(tree, text, sig) of the file's IN-MEMORY source — disk with every
    queued (unsaved) span edit spliced in (PendingSave.current_file_text:
    the same text the twin and Ctrl+Enter's hotswap compile). live_view must
    never parse raw disk text: deferred saves leave disk stale mid-session,
    and resolving keys against the pre-edit layout is exactly the
    adjacent-line key jumbling / bare line:N fallback bug. Cache signature =
    (mtime, pending gen) — both cheap; the O(file) splice runs on miss
    only."""
    from src.lsd.gl_gui.view.core_conversion.live_instrument import (
        _pending_gen)
    key = str(path)
    gen = _pending_gen(key)
    sig = (mtime, gen)
    cached = _asts.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1], cached[2], sig
    text = None
    if gen:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        text = PendingSave.current_file_text(path)
    if text is None:
        text = path.read_text()
    tree = ast.parse(text)
    _asts[key] = (sig, tree, text)
    return tree, text, sig


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


def _linemap_for(path, sig, span, text):
    """The LineMap of one top-level statement's span (whole file when span is
    None), rebuilt when `sig` — _ast_for's (mtime, pending gen) — changes,
    so queued edits that never touch disk still invalidate. Span-bounded so
    a save re-parses one function, not the file — the whole-file position
    pass holds the GIL for ~1s on big modules (see chain_converters'
    measurement) and this runs on the calling thread."""
    import libcst as cst
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
        LineMap, cst_module_to_dict)

    start = span[0] if span else 1
    key = (str(path), start)
    cached = _linemaps.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    if span is None:
        snippet = text
    else:
        lines = text.splitlines(keepends=True)
        snippet = "".join(lines[span[0] - 1:span[1]])
    parse = cst_module_to_dict(cst.parse_module(snippet))
    lm = LineMap(parse, line_offset=start - 1)
    _linemaps[key] = (sig, lm)
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


def _is_live_view_callparse(call_parse):
    """True when a CallParse is a `live_view(...)` call. Guards arg_label
    resolution: the statement-path truncation cuts at whatever CallParse it
    enters first, which for a snapshot-stamped assignment is the RHS's own
    call — whose arguments have nothing to do with live_view's."""
    if not isinstance(call_parse, dict):
        return False
    try:
        func = call_parse.get("__cst__").func
    except Exception:
        return False
    name = getattr(func, "value", None)          # cst.Name
    if not isinstance(name, str):
        name = getattr(getattr(func, "attr", None), "value", None)  # cst.Attribute
    return name == "live_view"


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
