"""
Hotswap rollback guard — revert a live hotswap that throws at RUNTIME.

A hotswap (edit → recompile → patch the live function/class/module in place) can
compile and validate cleanly yet still raise when the patched code actually RUNS
during render — often several frames after the swap, and in a different view than
the editor. There's no way to catch that at recompile time.

This guard closes the loop:

  • register(...)        — called right after a successful hotswap. Records the
                           NEW code objects (so a later traceback can be traced
                           back to this edit) and a `restore` closure that puts
                           the PREVIOUS good state back.
  • handle_exception(e)  — called from the render loop's central except block. If
                           any traceback frame runs through a registered hotswap,
                           that object is rolled back to its previous state, the
                           error is recorded (with an editor-relative line), and
                           True is returned.
  • get_runtime_error(source)
                         — the editor reads this for the source it's showing, to
                           surface the runtime error (red highlight + message).
                           Cleared optimistically on the next register (re-Run).

Keying is by id() of the live source object (fn/class/module). Those are
long-lived (module globals), so id reuse isn't a concern; a swap's new code
objects are unregistered on rollback / re-register, keeping that window tiny.
"""
import types

try:
    from meltygui.utils.glfw_utils import request_render
except Exception:  # pragma: no cover - keep the guard importable in isolation
    def request_render():
        pass


class _Entry:
    __slots__ = ("restore", "code_ids", "line_base")

    def __init__(self, restore, code_ids, line_base):
        self.restore = restore        # zero-arg callable → reverts to pre-swap state
        self.code_ids = code_ids      # set of id() of code objects this swap installed
        self.line_base = line_base    # subtract from a tb line → editor-buffer line


# id(source) -> _Entry for the currently-installed hotswap of that source.
_active: dict = {}
# id(code_object) -> id(source) for traceback attribution.
_code_to_source: dict = {}
# id(source) -> last runtime error (sticky until the next register() that source).
_errors: dict = {}


def collect_code_ids(code):
    """All code-object ids reachable from `code` — the body plus nested
    comprehensions / inner functions — so a traceback frame anywhere inside the
    hotswapped code attributes back to it."""
    ids = {id(code)}
    for const in getattr(code, "co_consts", ()):
        if isinstance(const, types.CodeType):
            ids |= collect_code_ids(const)
    return ids


def register(source, restore, code_ids, line_base=0):
    """Record a successful hotswap so a later runtime failure can roll it back.

    source     — the live fn/class/module the editor edits (the registry key).
    restore    — zero-arg callable that reverts source to its pre-swap state.
    code_ids   — set of id() of the code objects this swap installed.
    line_base  — subtract from a traceback line number to get the editor-buffer
                 line (function bodies are compiled padded to their file position;
                 class/module bodies compile at buffer line numbers → base 0).
    """
    sid = id(source)
    # New attempt: drop the previous error and the previous swap's code ids so a
    # stale code object can't attribute a future exception to the wrong edit.
    _errors.pop(sid, None)
    prev = _active.get(sid)
    if prev is not None:
        for cid in prev.code_ids:
            if _code_to_source.get(cid) == sid:
                _code_to_source.pop(cid, None)
    _active[sid] = _Entry(restore, set(code_ids), line_base)
    for cid in code_ids:
        _code_to_source[cid] = sid


def get_runtime_error(source):
    """The last runtime error that rolled `source` back, or None. The editor feeds
    this into its error highlight."""
    return _errors.get(id(source))


def handle_exception(exc):
    """Render-loop hook. If `exc`'s traceback runs through a hotswapped code
    object, roll that object back to its previous state and record the error.
    Returns True if a rollback happened. Never raises — a guard failure must not
    take down the render loop."""
    if not _code_to_source:
        return False
    try:
        # Walk to the INNERMOST frame whose code belongs to a hotswap - that's the
        # actual culprit (a hotswapped A calling hotswapped B that throws → roll
        # back B). Capture its line for the editor highlight.
        tb = exc.__traceback__
        hit_sid = None
        hit_line = None
        while tb is not None:
            sid = _code_to_source.get(id(tb.tb_frame.f_code))
            if sid is not None:
                hit_sid = sid
                hit_line = tb.tb_lineno
            tb = tb.tb_next
        if hit_sid is None:
            return False

        entry = _active.pop(hit_sid, None)
        if entry is None:
            return False
        for cid in entry.code_ids:
            if _code_to_source.get(cid) == hit_sid:
                _code_to_source.pop(cid, None)

        # Roll the live object back to its last-good state so the app keeps running.
        try:
            entry.restore()
        except Exception as restore_err:
            print(f"[hotswap_guard] restore failed: {restore_err}")

        # Tag the exception with an editor-buffer line so the editor highlights the
        # offending line (its _exception_errors reads `editor_line`).
        if hit_line is not None:
            try:
                exc.editor_line = max(1, hit_line - entry.line_base)
            except Exception:
                pass
        _errors[hit_sid] = exc
        request_render()
        return True
    except Exception as guard_err:
        print(f"[hotswap_guard] handle_exception error: {guard_err}")
        return False
