"""
Source/file round-trip for editable views — load, edit, save, recompile, all in
one @render_func whose state lives on its own draw_state.

The whole flow is `code_file_io`. It reads top to bottom; nothing crosses a node
boundary, so there is no chain executor and no shared cache tree to thread:

    resolve address  →  load  →  edit (nested view)  →  save / recompile

Three pieces carry the design:

CODECS (`new_codecs.py`) own how a given input becomes editable text and how an
edit goes back. A codec is picked by the input's TYPE (`type_to_codec`, e.g.
class / function / module → source span) or, for paths, by EXTENSION
(`extension_to_codec`, e.g. .png → texture). `code_file_io` only ever calls
`codec.resolve_address`, `codec.load`, `codec.save` — it knows nothing about
spans, files, or formats. Adding a filetype is adding a codec, not touching this
file.

VIEWS are codec-agnostic and reusable. `code_file_io` hands the loaded value to
an injected `view_func` (default `draw_text`). `draw_modes` is the interesting
one: it shows a tab per repr (text | structured), running a `chain_in` (e.g.
str → cst → dict for `draw_collection`) and `chain_out` back on edit. BOTH
chains run on a background worker (`_run_chain_in` / `_run_chain_out` via
`run_in_background`) — they are O(buffer) cst rebuilds and would otherwise stall
the render loop on every keystroke / drag. `_run_convert` is the inline executor
the worker calls (bare functions, no threads, no imgui). A parse failure is
treated as a VALUE — a cst error over half-typed source is a normal editor
state, surfaced as UI (and as a red line highlight, see `text_editor`), with
structured views falling back to the last good parse (`ModesState`).

ASYNC + DEBOUNCE. load / save / recompile / chain_in / chain_out each run through
`run_in_background`,
a one-shot worker keyed by a distinct `name=` so they never clobber each other.
Load is effectively cached (re-offered only on disk change); save auto-fires on
edit but is debounced (`save_debounce_ms`) so a burst of keystrokes collapses
into one write — a one-shot timer wakes the loop at the deadline instead of
spinning `request_render`. An explicit Save / Ctrl+S bypasses the debounce. The
auto-save also passes `wait_for_drag` so the debounce additionally holds the
launch while a mouse button is down — a slow/paused drag (a tint slider) can
outlast the time deadline, and we don't want the O(buffer) write firing mid-
gesture; it lands once the button releases.

ASYNC NEVER LAGS THE UI — the one design rule everything above serves. The live
buffer (text_cache / a host's held value) is ALWAYS the newest state and is what
the UI renders; background work only ever consumes SNAPSHOTS of it and may not
push results back over it ungated. Concretely:
  * The save is a trailing, write-only side channel. While a save is queued or
    in flight, edits keep landing in the buffer, the UI keeps rendering them,
    and every edit refreshes the queued save's snapshot (run_in_background's
    one-slot queue). Nothing about the save's lifecycle — debounce, drag hold,
    in-flight write, completion, even its own mtime bump reading back as a
    stale file — may suppress edits, freeze the snapshot, or substitute an
    older value for what the UI shows. A save-vintage value re-entering the
    display path (e.g. a false "changed on disk" conflict from our own write
    starving save_start, so the queue wrote an old buffer that file-syncing
    views then displayed) is THE classic bug here; see the INVARIANT comment at
    the save site in code_file_io and run_in_background's docstring.
  * Background results that legitimately flow back (a load, a chain_in parse)
    go through ordering gates — frame-precedence / generation tags in
    render_host, the pending-save gate on reloads — so an older result can
    never clobber a newer local edit.

Syntax-error feedback belongs to the editor, not this file. chain_in parses the
buffer on its background thread every time the text changes; the route hands that
result to draw_text — `code_tree` on success, the parse exception on failure —
which highlights the offending line. code_file_io does no parsing of its own and
displays no error, so the highlight clears as soon as a fresh background parse
succeeds. Recompile (hotswap, no disk write) is a separate concern: it just
hotswaps the live object and flashes a checkmark.
"""

import inspect
import linecache
import textwrap
import threading
import time
import tokenize
import traceback
import types
from collections import defaultdict
from datetime import datetime
from enum import Enum
from pathlib import Path

import imgui
import libcst as cst

from src.lsd.gl_gui import toggles
from src.lsd.gl_gui.melty import FileWatch, Melty
from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.model.core_model.draw_state import TabState
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.notifications import notify
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace, get_exception_frames
from src.lsd.gl_gui.view.core_conversion import hotswap_guard
from src.lsd.gl_gui.view.core_conversion.address import (
    Address, _evict_linecache,
)
from src.lsd.gl_gui.view.core_conversion.chain_converters import (
    record_compile, _enclosing_function, live_apply_edits,
)
from src.lsd.gl_gui.view.core_conversion.code_checks import check_source
from src.lsd.gl_gui.view.core_conversion.file_converters import (
    _recompile, _recompile_class, _recompile_module,
)
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    cst_module_to_dict, dict_to_cst_module,
)
from src.lsd.gl_gui.view.core_conversion.new_codecs import Codec, CallSite, Decorations, SaveConflict, \
    type_to_codec, extension_to_codec, codec_for_path
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save_exclude, no_save
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
from src.lsd.gl_gui.view.invalidation_tracker import Note
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Core helpers - load / write / recompile (plain synchronous, no chain magic)  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


def save_file(address, code_str, codec=None, ensure_import=None, parent_ds=None, force=False):
    """Write the edited value back through the resolved codec (span splice for
    code, whole-file for images, etc.). Returns the codec's result — a
    SaveConflict when the codec refused the splice because the on-disk span
    changed under us (force=True, the user's explicit Keep-mine, bypasses)."""
    current_time = datetime.now().strftime("%H:%M:%S")
    print(f"{current_time} Saved {address.path} from {parent_ds.name}")
    file_name = address.path.name if address.path is not None else "unknown"
    notify(f"Saved {file_name} from {parent_ds.name}", tint=(0.5, 1.0, 0.5))
    PendingSave.queue_save(address=address, codec=codec, data=code_str, ensure_import=ensure_import, force=force)
    return True
    # return codec.save(address=address, data=code_str, ensure_import=ensure_import, force=force)


def load_file(input_value: Address, codec: Codec = None, **kwargs) -> str:
    """Read the value through the resolved codec (span for code, whole file for
    images, etc.)."""
    file_name = input_value.path.name if input_value.path is not None else "unknown"
    notify(f"Loading {file_name}...", tint=(0.5, 1.0, 0.5))
    data = codec.load(input_value)
    PendingSave.mark_load(address=input_value, codec=codec, data=data)
    return data


def recompile_source(source, code_str, file_path, address=None):
    """Hotswap the edited code in place (no disk write) — do_recompile's dispatch.

    type / function / module: code_str IS the whole object's source, so it
    recompiles directly. A CallSite is different — code_str is a single statement
    inside a function body, which redefines nothing on its own — so we recompile
    the ENCLOSING function instead (see _recompile_caller). Decorations is the same
    shape: code_str is just the `@...` block, which redefines nothing alone, so we
    recompile the WHOLE decorated object (see _recompile_decorations)."""
    result = None
    notify(f"Recompiling {getattr(source, '__name__', str(source))}...", tag="recompile", tint=(0.5, 1.0, 0.5))
    if isinstance(source, Decorations):
        result = _recompile_decorations(source, code_str, file_path, address)
    elif isinstance(source, type):
        result = _recompile_class(source, code_str, str(file_path))
    elif isinstance(source, types.FunctionType):
        result = _recompile(source, code_str, str(file_path))
    elif isinstance(source, types.ModuleType):
        result = _recompile_module(source, code_str, str(file_path))
    elif isinstance(source, CallSite):
        result = _recompile_caller(source, code_str, file_path, address)
    return result


def _recompile_decorations(decorations, deco_str, file_path, address):
    """Hotswap a class/function after its DECORATOR block was edited.

    The codec hands code_file_io just the `@...` lines (DecorationsCodec edits the
    decorator block, not the def/class), so recompiling `deco_str` alone redefines
    nothing. The decorated OBJECT is the unit that recompiles: we read its current
    source, splice the edited decorator lines over the decorator span, then re-run
    the whole object through the normal class/function recompile — which re-executes
    the decorators (already handled by _recompile / _recompile_class).

    Splicing the live buffer over the on-disk object source (rather than reading the
    object back from disk) makes the hotswap reflect the edit before the debounced
    save lands — same trick as _recompile_caller."""
    target = decorations.target
    if not isinstance(target, (type, types.FunctionType)):
        return None
    unwrapped = inspect.unwrap(target) if isinstance(target, types.FunctionType) else target
    _evict_linecache(str(file_path))
    try:
        # getsourcelines starts at the first decorator (1-based obj_start) - so its
        # line list is shifted with the address's decorator span at the top.
        obj_lines, obj_start = inspect.getsourcelines(unwrapped)
    except (OSError, TypeError, tokenize.TokenError, SyntaxError) as e:
        print(f"_recompile_decorations: could not read source for "
              f"{getattr(unwrapped, '__name__', unwrapped)}: {e}")
        return None

    new_source = "".join(obj_lines)
    if address is not None and address.start is not None and address.end is not None:
        rel_start = address.start - (obj_start - 1)
        rel_end = address.end - (obj_start - 1)
        if 0 <= rel_start <= rel_end <= len(obj_lines):
            deco = deco_str
            if deco.endswith("\r\n"):
                deco = deco[:-2]
            elif deco.endswith("\n"):
                deco = deco[:-1]
            deco_lines = [line + "\n" for line in deco.splitlines()] if deco else []
            new_source = "".join(obj_lines[:rel_start] + deco_lines + obj_lines[rel_end:])

    if isinstance(target, type):
        return _recompile_class(target, new_source, str(file_path))
    return _recompile(unwrapped, new_source, str(file_path))


def _recompile_caller(call_site, stmt_str, file_path, address):
    """Hotswap the function ENCLOSING a call site, with the edited statement
    spliced into its live source.

    The codec hands code_file_io just the call EXPRESSION (CallerCodec edits the
    bare `foo(...)`, not the statement around it), so recompiling `stmt_str` alone
    would redefine nothing — and splicing it raw would drop the statement's
    indentation/prefix (`if `, `x = `) and suffix (`[0]:`), landing the call at
    col 0 → IndentationError. The enclosing function is the unit that recompiles —
    already resolved onto `address.source` by CallerCodec (via _enclosing_function),
    with a fallback re-resolve from the site if that's missing.

    We read the function's current source and splice the FULL reconstructed
    statement (prefix + edited call + suffix, the same reattachment CallerCodec.save
    does) over its span, then recompile the whole def. Splicing (rather than just
    reading the def back from disk, the old recompile_caller_fn approach) makes the
    hotswap reflect the LIVE buffer even before the debounced disk save lands: the
    unedited lines come from disk, the edited statement from the buffer."""
    fn = getattr(address, "source", None) if address is not None else None
    if not isinstance(fn, types.FunctionType):
        fn = _enclosing_function(call_site.filename, call_site.lineno)
    if not isinstance(fn, types.FunctionType):
        return None

    unwrapped = inspect.unwrap(fn)
    _evict_linecache(str(file_path))
    try:
        fn_lines, fn_start = inspect.getsourcelines(unwrapped)  # fn_start is 1-based
    except (OSError, TypeError, tokenize.TokenError, SyntaxError) as e:
        print(f"_recompile_caller: could not read source for {unwrapped.__name__}: {e}")
        return None

    # Splice the edited statement over its span in the function. Address spans
    # are file-absolute 0-based (load convention); shift them to the function's
    # own line list. Guard the bounds - a stale/odd span falls back to recompiling
    # the function's unedited disk source rather than blanking it.
    new_source = "".join(fn_lines)
    if address is not None and address.start is not None and address.end is not None:
        rel_start = address.start - (fn_start - 1)
        rel_end = address.end - (fn_start - 1)
        if 0 <= rel_start <= rel_end <= len(fn_lines):
            # Reattach the prefix/suffix CallerCodec stripped (the `if `/=` and
            # `[0]:` around the call), then splice - exactly like CallerCodec.save.
            prefix = getattr(address, "_call_prefix", "")
            suffix = getattr(address, "_call_suffix", "")
            stmt = stmt_str
            if stmt.endswith("\r\n"):
                stmt = stmt[:-2]
            elif stmt.endswith("\n"):
                stmt = stmt[:-1]
            full_stmt = prefix + stmt + suffix
            stmt_lines = [line + "\n" for line in full_stmt.splitlines()]
            new_source = "".join(fn_lines[:rel_start] + stmt_lines + fn_lines[rel_end:])

    return _recompile(unwrapped, new_source, str(file_path))


class TestClass:
    some_val = -2
    some_other_val = 73
    tint=(0.52, 0.80, 0.688)
    tint = (0.52, 0.80, 0.688)

    # [tint=(0.7722222, 0.5336913466453552, 0.17589502036571503)]
    def some_func(a=84, b=-153):
        imgui.set_cursor_pos()
    some_line = 87
    myflot = 5

    aomw_list= 62

    list_new = [1,-1,12]
    # [tint=(0.31290125846862793, 0.6864094, 0.7611111402511597)]
    class NestedClass:
        so = 31

    some_nested = NestedClass()

    new_bool = True


def slow_task(**kwargs):
    import time
    print("Starting slow task...")
    time.sleep(1);
    print("Slow task completed.")
    return {"result": "This is the result of the slow task", "kwargs": kwargs}
    print("Slow task completed.")
    return {"result": "This is the result of the slow task", "kwargs": kwargs}
    print("Slow task completed.")
    return {"result": "This is the result of the slow task", "kwargs": kwargs}


# @window()
# @render_func(use_cache=True)
# def editor_window():
#     code_file_io(
#         TestClass,
#         mode=Modes.NEW_CODE
#     )
#     return False, None
# #
#
# @window()
# @render_func(use_cache=True)
# def editor_window_2():
#     code_file_io(
#         TestClass,
#         view_func=convert_in_and_out,
#         auto_load_edits=False,
#         auto_load=False,
#         auto_save=False,
#         child_kwargs={
#             # convert_in_and_out runs the chains, draw_with_view_funcs draws the
#             # tabs/columns. string_to_cst_module's output is named "code_tree" -
#             # draw_text uses it to highlight parse errors (a failed parse arrives
#             # as the exception value). cst_module_to_dict's output is "code_dict",
#             # which draw_collection consumes. draw_text gets the raw string as its
#             # input_value (no route entry).
#             "view_func": draw_with_view_funcs,
#             "chain_in": [string_to_cst_module, cst_module_to_dict],
#             "chain_out": [dict_to_cst_module, cst_module_to_string],
#             "route": {
#                 string_to_cst_module: "code_tree",
#                 cst_module_to_dict: "code_dict",
#                 RenderFuncs.draw_collection: "code_dict",
#             },
#             "child_kwargs": {
#                 "view_funcs": [RenderFuncs.draw_text, RenderFuncs.draw_collection],
#             },
#         },
#     )
#     return False, None


# @window()
# @render_func(use_cache=True, disable_scroll=True)
# def draw_collection_code():
#     # view_func=draw_modes: text | dict tabs over one shared file-IO layer.
#     code_file_io(
#         RenderFuncs.draw_collection,
#         mode=Modes.NEW_CODE
#     )
#     return False, None


# @window()
# @render_func(use_cache=True)
# def editor_window_3():
#     code_file_io(slow_task, auto_load_edits=False, auto_load=False)
#     return False, None
#
#
# @window()
# @render_func(use_cache=True, selectable=False, disable_scroll=True)
# def test_toggles():
#     code_file_io(Toggles, mode=Modes.NEW_CODE)
#     return False, None


class LoadingState:
    def __init__(self):
        self._loading = False
        self.cached_result = UNSET
        self._run_next = None
        self._pending_change = False
        self.error = None
        self._loading_start_frame = None
        # Wall-clock deadline (time.time()) the queued task must wait until before
        # it launches. None = no debounce. Each fresh `start` pushes it out.
        self._debounce_deadline = None
        # One-shot timer that wakes the render loop once at the deadline, so we
        # don't busy-spin request_render every frame during the quiet window.
        self._debounce_timer = None



UNSET = object()
LOADING = object()


@render_func(use_cache=True, selectable=False, temp=True)
def run_in_background(input_value, loading_state: LoadingState, unique,
                      draw_state, child_kwargs, start=False, timeout=20,
                      debounce_ms=50, wait_for_drag=False, main_thread=False, **kwargs):
    """One-shot background runner: call it every frame; `start=True` is the
    trigger edge that snapshots (input_value, child_kwargs) into the queue.

    Returns (changed, value):
      (False, LOADING)        — BUSY: a run is in flight, OR a run finished but a
                                newer one is already queued (see below).
      (True, result)          — a run completed AND nothing newer is queued.
                                Reported exactly once.
      (False, cached_result)  — idle; last completed result (UNSET before any).

    Queue semantics — latest-only, coalesced:
      * The queue is ONE slot (`_run_next`). Every `start` overwrites it, so the
        eventual run always uses the LATEST snapshot. Callers must therefore keep
        re-triggering `start` while their input keeps changing — anything that
        gates the trigger (e.g. code_file_io's `not conflict`) freezes the queued
        snapshot at the last armed value, and the run will execute OLD data.
      * Completion is reported only when the queue is empty, so consumers only
        ever see the final result of a burst (render_host's materialize gate
        relies on a reported result never being superseded).
      * A completed-but-superseded run is BUSY, not idle: its completion edge is
        deliberately swallowed (coalescing), so the runner must not present an
        idle return — consumers attach side effects to the busy state
        (code_file_io's mark_file_current absorbs the save's own mtime bump on
        LOADING frames; an idle return there once latched a false "changed on
        disk" conflict that starved the auto-save snapshot — the
        old-value-written-during-save bug).

    Debounce: `debounce_ms` defers the launch until the trigger goes quiet (a
    one-shot timer wakes the loop at the deadline — never per-frame polling).
    `wait_for_drag` additionally holds the launch while a mouse button is down,
    so an O(buffer) run never fires mid-gesture; the snapshot keeps tracking the
    latest input the whole time."""
    if Melty.frame_count < 10 or main_thread:
        debounce_ms = 0
    if start:
        loading_state._run_next = input_value, child_kwargs
        if debounce_ms:
            # Debounce: defer the launch until the input goes quiet. Re-start on
            # every start (a burst of typing keeps pushing it out), and wake the
            # loop ONCE at the deadline via a one-shot timer - never busy-spin
            # request_render per frame, or we peg the whole render thread. The
            # run_next field above always holds the LATEST value, so the
            # eventual single run uses the final value.
            loading_state._debounce_deadline = time.time() + debounce_ms / 1000.0
            if loading_state._debounce_timer is not None:
                loading_state._debounce_timer.cancel()
            timer = threading.Timer(debounce_ms / 1000.0, request_render)
            timer.daemon = True
            loading_state._debounce_timer = timer
            timer.start()
            note = Note(name="Run in background, start debounce", tint=(1, 0.5, 0))
            draw_state.invalidate(note=note)
        else:
            loading_state._debounce_deadline = None
            note = Note(name="Run in background, no debounce", tint=(1, 0.5, 0))
            draw_state.invalidate(note=note)
            request_render()

    if loading_state._run_next is not None:
        deadline = loading_state._debounce_deadline
        if deadline is not None and time.time() < deadline:
            # Inside the quiet window - keep this draw_state dirty so the deadline
            # render re-runs this frame, but DON'T request_render: the one-shot
            # timer above wakes the loop exactly once when the deadline lands.
            note = Note(name="new converters, Deadline", tint=(1, 0.5, 1.0), draw_state=draw_state)

            draw_state.invalidate(note=note)
        elif wait_for_drag and (imgui.is_mouse_down(0) or imgui.is_mouse_dragging(1) or imgui.is_mouse_dragging(2)):
            # Past the time deadline, but a mouse button is still held - the user
            # is mid-drag (a tint slider, a value drag). The time debounce only
            # collapses a BURST of edits; it can still elapse during a slow or
            # paused drag, firing the O(n) save in the middle of the gesture.
            # Hold the launch until the button releases. No one-shot timer can wake
            # us on mouse-up, so re-check every frame via request_render is cheap,
            # because an active drag is already generating frames. The _run_next
            # snapshot keeps tracking the latest input, so the eventual single run
            # still uses the final dragged value.
            # note = Note(name="new converters, wait for drag", tint=(1, 0.7, 0.2), draw_state=draw_state)
            # draw_state.invalidate(note=note)
            request_render()
        else:
            loading_state._debounce_deadline = None
            if loading_state._debounce_timer is not None:
                loading_state._debounce_timer.cancel()
                loading_state._debounce_timer = None

            def run(run_next_inner):
                loading_state._loading = True
                value, background_kwargs = run_next_inner
                # Never run a @render_func WRAPPER on this worker thread - the wrapper
                # mutates process-global Melty state (depth, unique_id, ...) on
                # entry/exit, which races the main render thread. Grab the bare inner
                # function: plain functions pass through unchanged.
                value = getattr(value, '__wrapped__', value)
                try:
                    loading_state.cached_result = value(**background_kwargs)
                except Exception as exc:
                    loading_state.error = exc
                    print_stack_trace(exception=exc)
                finally:
                    loading_state._loading = False
                    loading_state._pending_change = True
                    if not main_thread:
                        note= Note(name="Run in background complete", tint=(0.5, 1.0, 0.5), draw_state=draw_state)
                        Melty.cache.invalidate(draw_state._tile_id, note=note)
                        request_render()

            if Melty.frame_count < 3 or main_thread:
                run(run_next_inner=loading_state._run_next)
                loading_state._run_next = None
            else:
                run_next = loading_state._run_next
                if not loading_state._loading:
                    loading_state._run_next = None
                    threading.Thread(target=run, kwargs={"run_next_inner": run_next}).start()
                    loading_state._loading_start_frame = Melty.frame_count
                    if loading_state._run_next is run_next:
                        loading_state._run_next = None

    # BUSY includes "completed, but a newer run is already queued". Completion is
    # only ever REPORTED once the queue is empty (coalesced to latest-only - see
    # the docstring), so a swallowed completion must surface as LOADING, never
    # as idle `(False, cached_result)`: the caller can't tell idle from
    # completed-and-superseded, and must attach a meaning to the busy
    # state (code_file_io absorbs its own write's mtime bump on LOADING frames).
    # Dropping into idle could let a save's mtime bump read back as an EXTERNAL
    # change → false "changes on disk" conflict → auto-save stopped re-arming →
    # the queued save wrote a stale snapshot (the old-value-during-save bug).
    if loading_state._loading or (loading_state._pending_change
                                  and loading_state._run_next is not None):
        return False, LOADING

    if loading_state._pending_change and loading_state._run_next is None:
        loading_state._pending_change = False
        note = Note(name="run in background complete, new conv", tint=(0.5, 0.5, 1.0))

        # draw_state.invalidate_up(max_depth=4, note=note)
        request_render()
        return True, loading_state.cached_result
    else:
        return False, loading_state.cached_result


@no_save_exclude()
@no_save("text_cache", "code_tree_cache", "address")
class CodeState(DictConversion):
    def __init__(self):
        super().__init__()
        self.text_cache = UNSET
        self.code_tree_cache = None
        self.address = None
        self.file_mtime = None
        self.file_size = None
        self._pending_save = False
        # Set when the codec REFUSED a save (SaveConflict: the on-disk span
        # changed during the debounced write). While set, a stale file is a
        # GENUINE conflict even if the disk content is an in-process write -
        # it gates the auto-write absorb in code_file_io so the conflict UI
        # actually surfaces. Cleared on a successful save or a (re)load.
        self._save_refused = False
        # Set the frame an edit changes the buffer; consumed next frame to force a
        # reconvert (so chain_in re-parses the new text and surfaces syntax errors)
        # even when nothing external changed.
        self._reconvert = False
        self._recompiled_on_frame = None
        self.recompile_result = None
        # External-change indication: _loaded_externally marks an in-flight load
        # that was TRIGGERED by a disk change (vs the initial fill); when it
        # completes, the frame/time stamps trigger the fading "loaded from disk"
        # status next to the buttons (see external_load_status).
        self._loaded_externally = False
        self._external_load_frame = None
        self._external_load_time = None

    def is_file_stale(self):
        if self.address is None:
            return False
        try:
            s = self.address.path.stat()
            return s.st_mtime != self.file_mtime or s.st_size != self.file_size
        except OSError:
            return True

    def mark_file_current(self):

        if self.address is None:
            return
        try:
            s = self.address.path.stat()
            self.file_mtime = s.st_mtime
            self.file_size = s.st_size
        except OSError:
            pass

    def mark_file_stale(self):
        self.file_mtime = None
        self.file_size = None


def _common_indent(text):
    """The leading whitespace `textwrap.dedent` would strip — i.e. the indent
    shared by every non-blank line, returned as the actual chars (tab-safe), or
    "" if there is none. The inverse prefix for re-indenting after a round trip."""
    if not isinstance(text, str):
        return ""
    dedented = textwrap.dedent(text)
    for orig, ded in zip(text.splitlines(), dedented.splitlines()):
        if orig.strip():
            return orig[:len(orig) - len(ded)]
    return ""


# A call site is a statement lifted from INSIDE a function body, and a
# `return` / `yield` / `await` is a syntax error at module scope even though the
# code is fine where it lives. Wrap it in a throwaway function so the parser
# accepts it: `def` for return/yield/yield from, `async def` for await. The
# synthetic def is self-marking (by name), so the reverse strips it without any
# flag to remember. Shared by string_to_cst_module (libcst) and _compile_check
# (compile) so both treat the in-function case identically.
_CALL_WRAP_NAME = "__melty_call_wrap__"
_CALL_WRAP_PREFIXES = (f"def {_CALL_WRAP_NAME}():\n", f"async def {_CALL_WRAP_NAME}():\n")

# A Decorations codec span is a bare `@deco` block, which needs a def/class after
# it to parse. Append a throwaway def so the parser accepts it. the decorators
# attach to it and the reverse (cst_module_to_string) lifts them back off, so no
# flag has to thread through. Self-marking via the synthetic def.
_DECO_WRAP_NAME = "__melty_deco_wrap__"
_DECO_WRAP_SUFFIX = f"\ndef {_DECO_WRAP_NAME}(): pass\n"


def _unwrap_call_module(module):
    """Strip the synthetic def string_to_cst_module added to parse an isolated
    snippet, recovering the original source at module column. A no-op for a normal
    (unwrapped) module.

    Two shapes: `def __melty_call_wrap__()` wraps an in-function STATEMENT (recover
    its body); `def __melty_deco_wrap__()` carries a DECORATOR block (recover the
    `@...` lines off its decorators)."""
    body = getattr(module, "body", None)
    if body and len(body) == 1 and isinstance(body[0], cst.FunctionDef):
        name = body[0].name.value
        if name == _CALL_WRAP_NAME:
            return cst.Module(body=list(body[0].body.body)).code
        if name == _DECO_WRAP_NAME:
            blank = cst.Module(body=[])
            return "".join(blank.code_for_node(d) for d in body[0].decorators)
    return module.code


@render_func()
def string_to_cst_module(input_value, **kwargs):
    # A targeted codec (a call site, a nested def/class) hands in a snippet that
    # carries its original leading indentation, which cst.parse_module rejects - a
    # module can't start indented ("expected an indented block"/"unexpected
    # indent"). Strip the common indent before parsing; cst_module_to_string
    # re-applies it on the way out so the edit splices back at its real column.
    # No-op for top-level source (common indent is ""), so unchanged for the
    # class/function/module codecs.
    text = textwrap.dedent(input_value) if isinstance(input_value, str) else input_value
    if not isinstance(text, str):
        return True, cst.parse_module(text)
    try:
        return True, cst.parse_module(text)
    except cst.ParserSyntaxError as bare_exc:
        # Bare parse failed - retry wrapped in a function (then async function), the
        # same fallback _compile_check uses, so an in-function statement parses.
        # cst_module_to_string strips the wrapper back off via _unwrap_call_module.
        indented = textwrap.indent(text, "    ")
        for prefix in _CALL_WRAP_PREFIXES:
            try:
                return True, cst.parse_module(prefix + indented)
            except cst.ParserSyntaxError:
                continue
        # Decorator-only snippet (a Decorations codec span): a bare `@deco` needs a
        # def after it. Append a throwaway one; _unwrap_call_module strips the
        # decorators back off on the way out.
        try:
            return True, cst.parse_module(text.rstrip() + _DECO_WRAP_SUFFIX)
        except cst.ParserSyntaxError:
            pass
        raise bare_exc  # genuinely unparseable - surface the original error


@render_func()
def cst_module_to_string(input_value, indent="", **kwargs):
    # Mirror of string_to_cst_module: strip any synthetic wrapper def, then re-apply
    # the snippet's original indent (computed once from the whole buffer by
    # convert_in_to_out and threaded down through chain_out). Empty indent and an
    # unwrapped module both make this a no-op for top-level source.
    code_str = _unwrap_call_module(input_value)
    if indent:
        code_str = textwrap.indent(code_str, indent)
    return True, code_str


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  draw_modes - the general version of the old hardcoded code_file_io_wrapped   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _run_convert(chain, value, route=None, routed=None, **extra):
    """Run a stateless converter chain inline — no run_chain, no threads.

    Each node is called as a BARE function: render_func nodes via `__wrapped__`
    (so we skip the wrapper's global-stack mutation and don't spawn draw_states),
    plain @register converters directly. A node returns either (changed, value)
    or a bare value; we keep only the value.

    `route` mirrors run_chain's routing: it maps a node to a name, and that
    node's output is stashed under the name in `routed` so downstream nodes — and
    the view functions in draw_modes — can pull it by name.

    EXCEPTIONS ARE VALUES. A cst parse over half-typed source raises — that is a
    normal editor state, not a bug in our code — so we CATCH it and return the
    exception itself in the value slot, stopping the chain, rather than letting it
    propagate. The caller renders it as UI and falls back to the last good
    conversion. `routed` only ever holds clean values: the failing node returns
    before its route entry is written, so an Exception never lands in `routed`."""
    if routed is None:
        routed = {}
    for node in chain:
        if isinstance(node, tuple):
            node, node_kwargs = node
        else:
            node_kwargs = {}
        inner = getattr(node, '__wrapped__', node)
        if not hasattr(node, "__params__"):
            node.__params__ = dict(inspect.signature(inner).parameters)

        accepts_var_kw = "kwargs" in node.__params__
        call = {'input_value': value, **routed, **extra, **node_kwargs}

        if not accepts_var_kw:
            call = {k: v for k, v in call.items() if k in node.__params__}
        try:
            result = inner(**call)
        except Exception as e:

            return e, routed
        if isinstance(result, tuple) and len(result) == 2:
            _, value = result
        else:
            value = result
        if route and node in route:
            # A route entry is either a bare output NAME, or a tuple whose first
            # element is the output name and whose remaining elements are the
            # extra-input kwargs this node consumes (handled by its caller, not
            # here). Either way the node's output is stashed under the name only -
            # NOT under every tuple element (that used to alias jump_to/run_jedi to
            # the dict and clobber them).
            target = route[node]
            out_name = target[0] if isinstance(target, tuple) else target
            if out_name:
                routed[out_name] = value

    return value, routed


def _compile_check(text):
    """Second-pass syntax check, catching errors libcst's lenient parser lets
    through but Python's own compiler rejects — duplicate args (`def f(x, x)`),
    repeated kwargs (`foo(a=1, a=1)`), etc.

    Returns the SyntaxError (carrying a real `lineno` for the red highlight) or
    None if it compiles clean. Runs ONLY after libcst already parsed the buffer,
    so it never double-reports a plain syntax error — it only *adds* the class of
    mistakes cst misses.

    Dedented first because the editor can hold an indented span (a nested class
    as getsourcelines returns it); `compile` rejects a leading indent the same
    way `_recompile_class` handles it.

    Call sites complicate this: a caller snippet is a statement lifted from INSIDE
    a function body, so a `return` / `yield` / `await` / bare continuation is a
    SyntaxError at module scope ("'return' outside function") even though the code
    is perfectly valid where it lives. When the bare compile fails, retry the
    snippet wrapped in a throwaway `def`; if THAT compiles clean the error was only
    the missing function context, so report nothing. A real mistake survives the
    wrap and is reported, with its line mapped back by 1 (the synthetic `def` adds
    a line on top). NOTE: pure syntax/compile only — it does NOT catch undefined
    names / typos (`print(myvarr)`), which are runtime NameErrors needing scope
    analysis (pyflakes)."""
    if not isinstance(text, str):
        return None
    import textwrap
    dedented = textwrap.dedent(text)
    try:
        compile(dedented, "<editor>", "exec")
        return None
    except SyntaxError as e:
        # Retry inside a function - then an async function - so a statement valid
        # only inside a function body isn't flagged just for missing that context:
        # `return` / `yield` / `yield from` need a `def`, `await` needs `async def`.
        # Clean under EITHER wrapper → not a bug, report nothing. Both wrappers are
        # one line, so map a surviving error's line back by 1.
        indented = textwrap.indent(dedented, "    ")
        wrapped_e = None
        for prefix in _CALL_WRAP_PREFIXES:
            try:
                compile(prefix + indented, "<editor>", "exec")
                return None
            except SyntaxError as we:
                wrapped_e = we
            except Exception:
                return e
        if wrapped_e is not None and wrapped_e.lineno is not None:
            wrapped_e.lineno = max(1, wrapped_e.lineno - 1)
        return wrapped_e if wrapped_e is not None else e
    except Exception:
        # Any non-SyntaxError exception (e.g. ValueError on null bytes) isn't the
        # user's code being wrong in a way we can pin to a line - ignore it.
        return None


def _run_chain_in(input_value, chain=None, _src_gen=None, lint_path=None, **extra):
    """Background entry point for the forward (chain_in) conversion.

    A plain module-level function (NOT a @render_func) so run_in_background can
    call it directly on its worker thread without touching any imgui/Melty global
    state. Runs the whole chain via _run_convert and returns the full `routed`
    dict — every column's input in one shared payload. The result/exception is
    folded back into ModesState on the main thread when the worker completes.

    `_src_gen` (the origin-edit generation of the source being parsed) is pulled out
    of the kwargs so it isn't forwarded to the chain nodes, then echoed back in the
    payload — bound to THIS worker's input snapshot, so the caller learns which
    generation the finished parse actually reflects (not whatever the source is by the
    time the worker returns)."""
    notify(f"_run_chain_in: start", tag="chain_in")
    result, routed = _run_convert(chain, input_value, **extra)
    error = result if isinstance(result, Exception) else None
    # cst parsed clean - run the compiler check, to surface the syntax errors libcst
    # is too lenient to flag (duplicate args/kwargs, ...). Same red-highlight path.
    if error is None and isinstance(input_value, str):
        error = _compile_check(input_value)
    # Compiled clean - run the static "will this RUN" pass too: undefined names +
    # call-signature mismatches (code_checks.check_source). Only when the host
    # declared a lint_path (a WHOLE-FILE buffer - a span buffer would flag every
    # module-level import it can't see). Same background thread, [(line, msg)].
    lint = []
    if error is None and lint_path is not None and isinstance(input_value, str):
        try:
            lint = check_source(input_value, path=lint_path)
        except Exception:
            lint = []
    return {"routed": routed, "error": error, "lint": lint, "_src_gen": _src_gen}


def _run_chain_out(input_value, chain=None, _out_gen=None, **extra):
    """Background entry point for the reverse (chain_out) conversion.

    The mirror of _run_chain_in: a plain module-level function (NOT a
    @render_func) so run_in_background can call it on its worker thread.
    chain_out is `dict → cst → str` — it REBUILDS and re-serializes the whole
    module (O(buffer)), which is exactly the work that pegged the render loop
    when it ran inline on every structured edit. Returns {"value": text} on
    success or {"error": exc} (a half-typed structured edit can fail to round-
    trip; we surface it as a value rather than letting it propagate, same as
    chain_in).

    `**extra` (e.g. `indent`) is forwarded to every node so cst_module_to_string
    can re-apply the snippet's leading indent stripped by string_to_cst_module —
    nodes without a matching param ignore it (_run_convert filters by signature).

    `_out_gen` (the origin-edit generation of the structured edit being serialized) is
    pulled out so it isn't forwarded to the nodes, then echoed back in the payload —
    bound to this worker's snapshot, so the produced source string can be tagged with the
    edit frame that made it (used to recognize and order its chain_in echo)."""
    notify(f"_run_chain_out: start", tag="chain_out")

    result, _ = _run_convert(chain, input_value, **extra)
    if isinstance(result, Exception):
        return {"error": result, "_out_gen": _out_gen}
    return {"value": result, "_out_gen": _out_gen}


class ModesState:
    """Per-window scratch for draw_modes.

    chain_in (str → cst → dict, ...) is O(buffer) and runs on a BACKGROUND thread
    via run_in_background, so it can't block the render loop. This holds the
    cross-frame state that makes that work while keeping every column in sync:

    last_good — the routed outputs of the last conversion that SUCCEEDED. Every
      selected column reads the SAME last_good, so they never drift apart. While a
      fresh conversion is in flight (or one throws on half-typed source) the views
      keep rendering off this snapshot instead of blanking out.
    last_error — the parse/compile error from the last run, or None when clean.
    last_lint — [(line, msg)] from the static name/signature pass over the same
      run (code_checks.check_source); [] when clean or when the buffer isn't a
      lintable whole file (no lint_path on the host)."""

    def __init__(self):
        self.last_good = {}
        self.last_error = None
        self.last_lint = []
        # Round-trip generation tracking (kills the value-flicker). Every conversion
        # carries the Melty.frame_count of the LOCAL EDIT that originated it, so a
        # chain-in result can be ordered against the host's latest edit and a stale parse
        # rejected. `echo_str` is the exact string object our LOCAL chain_out produced;
        # when it comes back as the_in's input (by identity) we know the parse reflects
        # `echo_gen` (that edit's frame) - anything else is an external change (as of now).
        self.echo_str = None
        self.echo_gen = 0


def compute_height(draw_state):
    return None
    # return min(draw_state., 400)


@render_func(use_cache=True, show_bg=False, selectable=False, disable_scroll=True,
             shadow=False, indent_size=0, with_footer=None, fill_height=False)
def draw_with_view_funcs(input_value, view_funcs, route, routed, route_to_kwargs,
                         tab_state: TabState, unique, column_widths=None,
                         draw=False, draw_state=None, **kwargs):
    """Tab strip + columns half of the old draw_modes.

    Shows a tab per entry in `view_funcs` and renders the selected ones side by
    side. The shared `routed` payload (built by convert_in_and_out) decides each
    column's input: a view with a `route` entry is fed routed[route[view]] (e.g.
    draw_collection <- the parsed dict); a view with no entry edits the raw text
    (draw_text <- input_value). Every routed value also rides in as a kwarg, so a
    view picks up the extras it wants (draw_text <- jump_to / error / code_tree)
    WITHOUT convert_in_and_out hand-threading them — that's the routing.

    Two edit channels flow back to convert_in_and_out:
      * a RAW text edit is returned directly as (changed, value);
      * a CONVERTED edit (a routed/structured view) is stashed on the shared
        `routed` dict under 'converted_edit', because it must go back through
        chain_out before it becomes text. It's written AFTER the loop so the
        columns in this same frame never see it as an input kwarg."""
    # Drop entries that didn't survive (de)serialization, then default to the
    # first two views (text | structured) like the old draw_modes did.
    tab_state.selected_tabs = [t for t in tab_state.selected_tabs if t is not None]
    if not tab_state.selected_tabs:
        tab_state.selected_tabs = view_funcs[:2] or view_funcs[:1]

    imgui.dummy(0, 5)
    names = [getattr(vf, '__name__', str(vf)) for vf in view_funcs]
    tab_changed, new_tabs = RenderFuncs.draw_tab_bar(input_value=tab_state.selected_tabs,
                                                     tab_height=30, show_bg=False, bg_offset=1,
                                                     name=f"tab_bar{unique}", names=names,
                                                     collection=view_funcs, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    imgui.dummy(0, 2)
    raw_changed, raw_value = False, input_value
    converted_edit = UNSET
    for idx, view_func in enumerate(tab_state.selected_tabs):
        if column_widths is not None and len(column_widths) > idx:
            column_width = column_widths[idx]
        else:
            column_width = None

        # A view with a route entry consumes a routed (converted) value; one
        # without edits the raw text. Falls back to the last-good routed value
        # (already merged into `routed`), or UNSET if it never parsed.
        uses_converted = route is not None and view_func in route
        if uses_converted:
            view_input = routed.get(route[view_func], UNSET)
        else:
            view_input = input_value

        # route_to_kwargs_this = route_to_kwargs.get(view_func, {})
        # for k in route_to_kwargs_this:
        #     arg_name = route_to_kwargs_this[k]
        #     if arg_name in routed:
        #         routed[k] = routed[arg_name]

        # routed = {**routed, **{k: routed.get(k) for k in route_to_kwargs_this}}

        # `draw=` (the external trigger flag) bypasses the view's cache for one
        # redraw WITHOUT setting any sticky edit flags - never pass it as
        # `changed=`, or a forced redraw comes back reported as an edit and, with
        # auto_save, spins an endless reload -> redraw -> save.
        if len(tab_state.selected_tabs) == 1:
            column = None
        m_changed, m_out = view_func(input_value=view_input, excluded=["__cst__"],
                                     show_system=True, draw=draw, max_width=draw_state.content_width - 10,
                                     disable_scroll=False, show_header=False,
                                     column=idx, column_width=column_width,
                                     show_add_delete=False, name=f"{view_func.__name__}##{unique}",
                                     selectable=False, **routed)
        if not m_changed:
            continue
        if draw_state is not None:
            draw_state.invalidate_up(max_depth=3)
        if uses_converted:
            # Keep only the latest converted edit so a continuous drag collapses
            # into one chain_out call when it settles.
            converted_edit = m_out
        else:
            raw_changed, raw_value = True, m_out

    if converted_edit is not UNSET:
        routed['converted_edit'] = converted_edit
    return raw_changed, raw_value


@render_func(use_cache=True, show_bg=False, selectable=False, disable_scroll=True,
             shadow=False, indent_size=0, with_footer=None, fill_height=False, temp=True)
def convert_in_and_out(input_value, draw_state, view_func=None, chain_in=None, chain_out=None,
                       run_chain_kwargs=None, route=None, modes_state: ModesState = None,
                       external_change=False, child_kwargs=None, unique=0, **kwargs):
    """Conversion half of the old draw_modes — chains only, no tabs.

    Runs `chain_in` ONCE on a background worker (str -> cst -> dict), shares its
    outputs across every column via `routed`, hands rendering off to the injected
    `view_func` (draw_with_view_funcs), then runs the reverse `chain_out` on a
    background worker when that view reports a CONVERTED (structured) edit.

    Routing is the indirection that keeps this view-agnostic — convert_in_and_out
    names NONE of the extra inputs it threads through:
      * `route[node]` (a name, or a tuple `(out_name, *input_names)`) names where a
        chain node's output lands in `routed`, and which of the caller's kwargs the
        node consumes. Those declared inputs are forwarded, by name from `route`
        alone, into the chain payload (`run_chain_kwargs`) AND into `routed` so the
        columns can pick them up.
      * `changed` is the chain_in trigger, supplied whole by code_file_io (load /
        external edit / the Index pulse) — we never diff the text or name an input.
      * draw_with_view_funcs reads the same `route`/`routed` to feed each column."""
    if child_kwargs is None:
        child_kwargs = {}
    if route is None:
        route = {}

    # Forward every kwarg a route entry DECLARES (the tuple tail after the output
    # name) from the caller's kwargs into one payload - agnostically; we look the
    # names up from `route`, never spell them out. This seeds the chain inputs and
    # the values the columns read.
    forwarded = dict(run_chain_kwargs) if run_chain_kwargs else {}
    for target in route.values():
        if isinstance(target, tuple):
            for arg_name in target[1:]:
                if arg_name in kwargs:
                    forwarded[arg_name] = kwargs[arg_name]

    # last_good only ever holds CLEAN values, so copying it here means a column
    # keeps rendering the prior good parse while a fresh chain_in is in flight or
    # fails on badly-typed source. The forwarded inputs ride alongside it so views
    # (draw_text <- draw_input) get them without being hand-fed.
    routed = dict(modes_state.last_good)
    routed['root_input'] = kwargs.get("root_input", None)
    routed.update(forwarded)

    chain_in_error = modes_state.last_error
    if chain_in:
        # Fresh dict per run (run_in_background snapshots it as _run_kwargs): never
        # reuse it for chain_out below, or a deferred chain_in run reads back
        # chain_out's mutated values.
        chain_in_kwargs = {**forwarded, "input_value": input_value,
                           "chain": chain_in, "route": route}
        # `changed` is the only trigger - code_file_io rolls load / external edit /
        # the Index pulse into it, so we never diff the text or sniff inputs here.
        finished, payload = run_in_background(
            _run_chain_in,
            child_kwargs=chain_in_kwargs,
            name=f"chain_in{unique}", start=external_change)
        if finished and isinstance(payload, dict):
            # Fold the completed outputs into the shared snapshot AND this
            # frame's routed (so the columns see the good values immediately).
            modes_state.last_error = payload.get("error")
            modes_state.last_lint = payload.get("lint") or []
            for name, val in payload["routed"].items():
                modes_state.last_good[name] = val
                routed[name] = val
            chain_in_error = modes_state.last_error

    out_changed, out_value = False, input_value

    recompile_error = kwargs.get('error')
    if isinstance(recompile_error, SyntaxError) and chain_in_error is None:
        recompile_error = None  # buffer parses again → the recompile syntax error is fixe
    routed['error'] = recompile_error or chain_in_error

    # Fresh dict - don't mutate the shared global child_kwargs. A raw text edit
    # comes straight back as the new text; a converted (structured) edit is
    # stashed on `routed` for chain_out below.
    # view_kwargs = {**child_kwargs, 'route': route, 'routed': routed}
    child_kwargs['routed'] = routed

    raw_changed, raw_value = view_func(input_value=input_value, **child_kwargs)
    if raw_changed:
        out_changed, out_value = True, raw_value
        # draw_state.invalidate_up(max_depth=3)

    converted_edit = routed.pop('converted_edit', UNSET)

    # chain_out in a BACKGROUND thread - the mirror of chain_in. Started ONLY by a
    # real structured edit from above (converted_edit set), never by chain_in's
    # output, so there's no ping-pong. Called every frame so a conversion queued
    # by a just-finished edit still drains; `start` is still the trigger flag.
    if chain_out:
        co_start = converted_edit is not UNSET
        # Re-indent the edited snippet to the buffer's original column. The buffer
        # (input_value) is the source of truth for indentation; string_to_cst_module
        # dedented to parse, so chain_out must restore it. Computed here (not inside
        # the chain) because only the live buffer knows the indent; "" for top-level
        # source, so a no indent for all class/function/module codecs.
        indent = _common_indent(input_value)
        co_changed, co_payload = run_in_background(
            _run_chain_out,
            child_kwargs={"input_value": converted_edit if co_start else None,
                          "chain": chain_out, "indent": indent},
            name=f"chain_out{unique}", start=co_start)
        if co_changed and isinstance(co_payload, dict):
            if co_payload.get("error") is not None:
                imgui.text_colored(f" chain_out: {co_payload['error']}", 1.0, 0.5, 0.0)
            elif "value" in co_payload:
                out_changed, out_value = True, co_payload["value"]

    return out_changed, out_value


@render_func(use_cache=True, show_bg=False, selectable=False, disable_scroll=True,
             shadow=False, indent_size=0, with_footer=None, temp=True)
def convert_in_and_out_value(input_value, draw_state, view_func=None, chain_in=None, chain_out=None,
                             run_chain_kwargs=None, route=None, modes_state: ModesState = None, temp=True,
                             external_change=False, child_kwargs=None, unique=0, **kwargs):
    """Like `convert_in_and_out`, but hands the view_func the chain_in OUTPUT directly.

    IDENTICAL background processing to `convert_in_and_out` — chain_in and chain_out
    both run on the `run_in_background` worker, same `modes_state.last_good` snapshot,
    same error handling. The ONE difference is the view_func contract:

      convert_in_and_out        view_func(input_value=<source TEXT>, routed={code_dict: tree})
                                a structured edit comes BACK via routed['converted_edit'].
      convert_in_and_out_value  view_func(input_value=<chain_in's output, the TREE>)
                                the view_func's RETURN is the structured edit → chain_out.

    This is the shape a RenderHost proxy wants: it gets the parsed value as its own
    `input_value` (already a dict — no `routed` side-channel, no `materialize_from`)
    and returns the edited value, which goes straight to chain_out. The legacy
    `convert_in_and_out` + `draw_with_view_funcs` (text|tree tabs) is untouched, so the
    NEW_CODE views keep working."""
    if child_kwargs is None:
        child_kwargs = {}
    if route is None:
        route = {}

    # ── (identical to convert_in_and_out) seed routed + run chain_in in background ──
    forwarded = dict(run_chain_kwargs) if run_chain_kwargs else {}
    for target in route.values():
        if isinstance(target, tuple):
            for arg_name in target[1:]:
                if arg_name in kwargs:
                    forwarded[arg_name] = kwargs[arg_name]

    routed = dict(modes_state.last_good)
    routed['root_input'] = kwargs.get("root_input", None)
    routed.update(forwarded)

    chain_in_error = modes_state.last_error
    inbound_gen = None
    if chain_in:
        # Tag this parse with the generation of the source it consumes. If the source is
        # the echo of our OWN last chain_out (same string object), it reflects that edit's
        # frame (echo_gen); otherwise it's an external change as of now. Threaded through
        # the worker snapshot so the result is tagged with the gen actually parsed.
        src_gen = modes_state.echo_gen if (input_value is modes_state.echo_str) else Melty.frame_count
        chain_in_kwargs = {**forwarded, "input_value": input_value,
                           "chain": chain_in, "route": route, "_src_gen": src_gen}
        finished, payload = run_in_background(
            _run_chain_in,
            child_kwargs=chain_in_kwargs,
            name=f"chain_in{unique}", start=external_change)
        if external_change:
            note = Note(name="convert_in_out, chain in start", tint=(1, 0.5, 0))
            draw_state._parent.invalidate(note=note)
        external_change = False
        if finished and isinstance(payload, dict):
            # The generation this finished parse reflects (origin edit frame, or "now" for
            # an external change) - pass to the view_func so its accept/reject ordering
            # compares against the user's latest LOCAL edit and drops a stale parse.
            inbound_gen = payload.get("_src_gen")
            modes_state.last_error = payload.get("error")
            modes_state.last_lint = payload.get("lint") or []
            for name, val in payload["routed"].items():
                modes_state.last_good[name] = val
                routed[name] = val

            chain_in_error = modes_state.last_error
            external_change = True
            # chain_in just finished on the worker - the fresh parse is now in `routed` /
            # `last_good`, but the view_func is a SEPARATE cached subtree that
            # run_in_background's completion never reached: its tile invalidation only
            # climbs to shared ANCESTORS (Blit->screen.invalidate), not across into the
            # view_func's descendant tiles. So invalidate our OWN subtree (up) - that
            # dirties the view_func, so next frame it re-runs with the latest value
            # instead of replaying a stale blit until some unrelated manual invalidation.
            note = Note(name="Convert in and out, chain in finished", tint=(1, 0.5, 1.0), draw_state=draw_state)
            Melty.cache.invalidate_up(draw_state._tile_id, force=True, note=note)
            notify(f"chain_in finished", tag="chain_in")

    out_changed, out_value = False, input_value

    recompile_error = kwargs.get('error')
    if isinstance(recompile_error, SyntaxError) and chain_in_error is None:
        recompile_error = None
    routed['error'] = recompile_error or chain_in_error

    # ── THE DIFFERENCE: hand the chain_in output (by name) to the view_func ────────
    # The parsed value is the routed entry the last chain_in node maps to (e.g.
    # cst_module_to_dict -> "code_dict"). That's what the view_func edits; its return
    # is the structured edit (vs convert_in_and_out's routed['converted_edit']).
    primary_key = None
    if chain_in:
        tgt = route.get(chain_in[-1])
        primary_key = tgt[0] if isinstance(tgt, tuple) else tgt
    primary = routed.get(primary_key) if primary_key is not None else None

    child_kwargs['routed'] = routed
    edited, edited_value = view_func(input_value=primary, external_change=external_change,
                                     inbound_gen=inbound_gen, **child_kwargs)
    converted_edit = edited_value if (edited and edited_value is not None) else UNSET
    if edited:
        draw_state.invalidate(note=Note(name="convert_in_out, view func edit", tint=(1.0, 0.5, 0), draw_state=draw_state))

    # ── (identical) chain_out in background ───────────────────────────────────────
    if chain_out:
        co_start = converted_edit is not UNSET
        indent = _common_indent(input_value)
        # The edit that produced converted_edit is this frame's (the view_func reported it
        # now, same frame bubbling stamped the held value), so its generation is the
        # current frame. Threaded through the worker snapshot so the output string is
        # tagged with the edit frame - its chain_in echo is then recognized + ordered.
        out_gen = Melty.frame_count
        co_changed, co_payload = run_in_background(
            _run_chain_out,
            child_kwargs={"input_value": converted_edit if co_start else None,
                          "chain": chain_out, "indent": indent, "_out_gen": out_gen},
            name=f"chain_out{unique}", start=co_start)
        if co_changed and isinstance(co_payload, dict):
            if co_payload.get("error") is not None:
                imgui.text_colored(f" chain_out: {co_payload['error']}", 1.0, 0.5, 0.0)
            elif "value" in co_payload:
                out_changed, out_value = True, co_payload["value"]
                # Remember our own output by ID, + the edit gen it carries, so when it
                # round-trips back as chain_in's source we recognize the echo and tag the
                # re-parse with that gen (above) instead of treating it as "new now".
                modes_state.echo_str = co_payload["value"]
                modes_state.echo_gen = co_payload.get("_out_gen", out_gen)

    return out_changed, out_value


def code_file_footer(input_value, code_state, **kwargs):
    if code_state.address is not None:
        imgui.text(str(code_state.address.path))
    return False, None


# ── Recompile controls - shared by code_file_io and the context menu's input
# tab (draw_input_tab reuses them against a code-host's CodeState, so its Run
# button rides the exact same path as a file leaf's). Three pieces because they
# render at three different order in code_file_io's body: the button in the top
# line, the checkmark beside it, the runner at the end (after this frame's
# edits have landed in text_cache).

def recompile_button(code_state, unique=None, height=30):
    """The Run (hotswap) button. Hidden until the buffer is loaded. Returns
    whether it was clicked."""
    if code_state.text_cache is UNSET or code_state.text_cache is None:
        return False
    play_icon = "\uf04b"
    return RenderFuncs.button(f"{play_icon} Run",
                              tint=(0.05678745, 0.5, 0.2, 0.5),
                              height=height,
                              name=f"recompile_btn{unique}")[0]


def recompile_status(code_state, draw_state):
    """The fading checkmark after a successful hotswap (same_line, so call it
    right after the button row)."""
    if code_state._recompiled_on_frame is None:
        return
    duration = 10.0

    recompiled_on = float(Melty.frame_count - code_state._recompiled_on_frame)
    fade_out = min(1.0, max(0.0, 2.0 - (max(0.0, recompiled_on) / duration)))

    if fade_out >= 0:
        imgui.same_line()
        checkmark_icon_fa = "\uf00c"
        imgui.text_colored(f"{checkmark_icon_fa}", 0.0, 1.0, 0.0, fade_out)
    if fade_out > 0.01:
        draw_state.invalidate()
        request_render()
        code_state._recompile_on_frame = None


def external_load_status(code_state, draw_state):
    """Fading "loaded from disk" indication after an external write was picked
    up and reloaded \u2014 the disk-change counterpart of recompile_status, so an
    outside program (Claude, git, another editor) saving the file is visible
    instead of the buffer just silently changing."""
    if code_state._external_load_frame is None:
        return
    duration = 120.0  # linger 2x recompile's checkmark \u2014 easy to miss otherwise

    loaded_for = float(Melty.frame_count - code_state._external_load_frame)
    fade_out = min(1.0, max(0.0, 2.0 - (max(0.0, loaded_for) / duration)))

    if fade_out >= 0:
        imgui.same_line(spacing=8)
        imgui.align_text_to_frame_padding()
        sync_icon_fa = "\uf021"
        imgui.text_colored(f"{sync_icon_fa} loaded from disk {code_state._external_load_time}",
                           1.0, 0.75, 0.25, fade_out)
    if fade_out > 0.01:
        draw_state.invalidate()
        request_render()
    else:
        code_state._external_load_frame = None


def run_recompile(source, code_state, draw_state, start=False, name="recompile"):
    """The background hotswap runner (recompile_source via run_in_background —
    no disk write). Call it unconditionally every frame so the runner can spawn
    its thread and surface completion; `start` is just the trigger edge. The
    buffer/address are snapshotted from code_state at trigger time."""
    changed, result = run_in_background(recompile_source,
                                        child_kwargs={"source": source,
                                                      "code_str": code_state.text_cache,
                                                      "file_path": code_state.address.path,
                                                      "address": code_state.address},
                                        name=name, start=start)
    if result == LOADING:
        print("Starting recompile...")
        code_state._recompiled_on_frame = None
    elif result == UNSET:
        pass
    else:
        if changed:
            print("Recompile successful-------------------------------")
            code_state._recompiled_on_frame = Melty.frame_count
            record_compile(code_state.address)
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10)
            request_render()
        code_state.recompile_result = result


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  editable_source - the whole round-trip, one function                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@render_func(use_cache=True, selectable=False, with_header=draw_header, searchable=False, disable_scroll=True)
def code_file_io(input_value, code_state: CodeState, codec=None, view_func=RenderFuncs.draw_text, auto_load=True,
                 auto_load_edits=False, min_height=20, shadow=False, show_add_delete=False,
                 child_kwargs=None, draw_state=None, auto_save=True, auto_recompile_edits=False, save=False, load=False,
                 recompile=False, run_jedi=False, save_debounce_ms=600, 
                 ensure_import=None, s_key_pressed=None, enter_key_pressed=None, unique=None, **kwargs):
    edited = False

    try:
        imgui.dummy(0, 0)

        if child_kwargs is None:
            child_kwargs = {}
        # ── 1. Resolve the source's line span ─────────────────────────────────────
        if codec is None:
            # Class lookup: match the value's type against registered types,
            # walking the MRO (like Mode.get_config_for) so a subclass of a
            # registered type still matches -- e.g. a class object whose
            # metaclass subclasses `type`, or an extended primitive instance.
            for klass in type(input_value).__mro__:
                if klass in type_to_codec:
                    codec = type_to_codec[klass]
                    break
            # File lookup fallback -- only for a GENUINE path or a bare str.
            # Hard-type the str check so an extended primitive like
            # CodeLine(str) (whose type is SOURCE, not a FILE) isn't whacked
            # into being interpreted as a path. Path uses isinstance so real
            # path objects (PosixPath, a Path subclass) still match.
            # codec_for_path = registered codec, else content sniff: text
            # files edit via TextFileCodec, anything else gets the read-only
            # binary summary - a real path never lands on "No codec".
            if codec is None and (isinstance(input_value, Path) or type(input_value) is str):
                codec = codec_for_path(Path(str(input_value)))

        if codec is None:
            imgui.text(f"No codec for type: {type(input_value).__name__}")
            return False, None

        # A codec whose output isn't editor text (ImageCodec → UITexture,
        # BinaryFileCodec → plain summary) names its own view; it wins over the
        # mode-pinned text view (FILE_TREE pins draw_text_from_code_cache, which
        # would try to PARSE the loaded value as Python).
        if getattr(codec, "view_func", None) is not None:
            view_func = codec.view_func

        address = codec.resolve_address(input_value, draw_state, code_state=code_state)
        code_state.address = address
        top_line_height = 30
        external_change = False
        imgui.same_line(spacing=0)

        if address is None:
            return False, None

        # Run (hotkey) and Index (jedi) only make sense on Python code \u2014 the
        # codec decides (TypeCodec family: yes; TextFileCodec: .py paths only;
        # images/binaries: no). Gates the buttons AND the Ctrl+Enter hotkey.
        code_buttons = codec.show_code_buttons(address)

        if auto_load:
            if draw_state.frame_count < 1:
                load = True
                code_state.text_cache = None
                code_state.mark_file_current()

        # str gate on top: even a code codec can briefly hold non-text data.
        if (code_buttons and not auto_recompile_edits
                and code_state.text_cache is not UNSET
                and isinstance(code_state.text_cache, str)):
            recompile = recompile_button(code_state, unique=unique, height=top_line_height)

        if Toggles.enable_jedi and code_buttons:
            imgui.same_line(spacing=0)
            search_icon = "\uf002"
            run_jedi = RenderFuncs.button(f"{search_icon} Index",
                                          tint=(0.8, 0.54, 0.2),
                                          height=top_line_height,
                                          name="jedi_index_btn")[0] or run_jedi

        recompile_status(code_state, draw_state)
        external_load_status(code_state, draw_state)

        file_stale = code_state.is_file_stale()
        keep_mine = False
        # A sibling in-process editor of the same file (the cache's str_host
        # in the structured tab, another tab, a lens save) syncs through
        # this FILE: its write is not an external change. Reload quietly: no
        # "loaded from disk" stamp (which also fade-invalidates every update for
        # seconds). Only a write we did NOT produce gets the indication.
        self_write = file_stale and FileWatch.is_self_write(address.path)
        # On a pending local edit, a verified IN-PROCESS write (is_self_write
        # hash-checks the actual disk content) is absorbed, not conflicted. This
        # is usually our own write's mtime bump observed before the runner's
        # completion frame consumed it - the stat above runs BEFORE the save
        # runner below, so there's always a frame where our write reads back as
        # stale. Treating it as a conflict blocks `save_start` (below), which
        # FREEZES the queued save's snapshot while the user keeps editing - the
        # save then writes an OLD buffer and the file-syncing view displays
        # the old value. Async must never gate the live buffer or its save on
        # its own in-flight write. A refused save (_save_refused) is the one
        # exception: the buffer holds a sibling's write and splice would mangle,
        # so the conflict must surface. A genuine external program's write
        # never hash-matches and conflicts as before.
        if self_write and code_state._pending_save and not code_state._save_refused:
            code_state.mark_file_current()
            file_stale = False
            self_write = False
        conflict = file_stale and code_state._pending_save

        # ── Cross-view sync via the PendingSave cache (deferred-save model) ────────
        # A sibling view's save now only goes into PendingSave - no disk write,
        # so file_stale (mtime) won't fire for it. queue_save wakes our tile; here
        # we pull that edit from the shared cache if it diverges from our buffer.
        # Gated on no local edits: the view that PRODUCED the entry matches it (no-op),
        # and an in-flight edit (_pending_save) must not be clobbered by an older
        # snapshot. Mirrors the verified self-write in-process sync below.
        if (not file_stale and auto_load_edits and not code_state._pending_save
                and not code_state._save_refused and isinstance(code_state.text_cache, str)):
            cache_text = PendingSave.pending_text_for(address)
            if cache_text is not None and cache_text != code_state.text_cache:
                code_state.text_cache = cache_text
                code_state.mark_file_current()
                external_change = True
                draw_state.invalidate_up(max_depth=6)
                request_render()

        if file_stale and not code_state._pending_save:
            if auto_load_edits:
                # A VERIFIED self-write (a sibling editor of the same file - the
                # code-host str_host, another window, a lens save - synced
                # through disk) is picked up IN-PROCESS from the exact text that
                # write produced: no disk reload, no "Loading..." banner, no
                # external stamp. A reparse still fires (text_cache change +
                # external_change below), so the structured pane updates exactly
                # as the auto-load path drove it - load their only fires for a
                # change we did NOT produce. get_self_write_text returns None if
                # an external write has since raced in, falling through to a disk
                # load (treated as external, with the stamp).
                mem_text = FileWatch.get_self_write_text(address.path) if self_write else None
                if mem_text is not None:
                    code_state.text_cache = codec.load(address, source_text=mem_text)
                    code_state.mark_file_current()
                    code_state._save_refused = False
                    external_change = True
                    draw_state.invalidate_up(max_depth=6)
                    request_render()
                else:
                    load = True
                    code_state._loaded_externally = not self_write
                    code_state.mark_file_current()
            else:
                imgui.same_line(spacing=0)
                if RenderFuncs.button("Load", width=100, height=top_line_height, name=f"reload{unique}")[0]:
                    load = True
                    code_state._loaded_externally = not self_write

                imgui.same_line()
                if RenderFuncs.button("Keep mine", width=100, height=top_line_height, name=f"keepmine{unique}")[0]:
                    save = True
                    keep_mine = True
        elif conflict:
            # External write + local unsaved edits: a write conflict. Auto-save
            # is blocked below until the user picks a side - splicing a buffer
            # that came from the OLD file into the rewritten one is exactly the
            # file-mangling path. Keep mine writes with force (skips the codec's
            # span-failure guard) through the freshly re-resolved span.
            imgui.same_line(spacing=8)
            imgui.align_text_to_frame_padding()
            imgui.text_colored("\uf071 changed on disk", 1.0, 0.55, 0.15, 1.0)
            imgui.same_line(spacing=4)
            if RenderFuncs.button("Load theirs", width=110, height=top_line_height, name=f"reload{unique}")[0]:
                load = True
                code_state._loaded_externally = True
            imgui.same_line()
            if RenderFuncs.button("Keep mine", width=100, height=top_line_height, name=f"keepmine{unique}")[0]:
                save = True
                keep_mine = True

        if not auto_save and code_state._pending_save:
            imgui.same_line(spacing=0)
            if RenderFuncs.button("Save", width=100, height=top_line_height, name=f"save{unique}")[0]:
                save = True

        if auto_save:
            imgui.same_line(spacing=16)
            imgui.align_text_to_frame_padding()
            imgui.text_colored(str(f" Auto"), *(1.0, 1.0, 1.0, 0.2))

        changed, new_text = run_in_background(load_file, main_thread=True,
                                              child_kwargs={"input_value": address, 'codec': codec},
                                              name=f"load{unique}", start=load)
        if new_text is LOADING:
            code_state.mark_file_current()

        elif changed:
            code_state.text_cache = new_text
            code_state.mark_file_current()
            draw_state.invalidate_up(max_depth=6)
            code_state._pending_save = False
            code_state._save_refused = False
            external_change = True
            if code_state._loaded_externally:
                # This load was triggered by a disk change (not the initial
                # load) - stamp the fading "loaded from disk" label.
                code_state._loaded_externally = False
                code_state._external_load_frame = Melty.frame_count
                code_state._external_load_time = datetime.now().strftime("%H:%M:%S")
            request_render()

        # ── 3. Edit - the actual call ─────────────────────────────────────────────
        if code_state.text_cache is not UNSET and code_state.text_cache is not None:

            child_kwargs['jump_to'] = address
            # The Index button's click rides through to the chain: code_module_to_gp
            # runs jedi and attaches the index straight to the gp it builds.
            child_kwargs['run_jedi'] = run_jedi
            # Error signals reach the editor's red-line highlight by this route -
            # code_text_io does NO parse of its own:
            #   • SYNTAX errors: chain_in parses the buffer on its background thread
            #     and routes the result to draw_text (`code_tree` on success, the
            #     parse exception on failure). Clears the instant a fresh parse OKs.
            #   - RECOMPILE errors: the hotswap can fail on a SyntaxError (Python's
            #     compiler pins a better line than libcst). Route it down as `error`.
            #   - RUNTIME errors: a hotswap that compiled clean can throw when its
            #     new code RUNS during a later render - the hotswap guard catches the
            #     live object back and records the error here. It carries an
            #     editor-relative `editor_line`, so it highlights the offending line.
            _runtime_error = hotswap_guard.get_runtime_error(input_value)
            _recompile_error = (code_state.recompile_result
                                if isinstance(code_state.recompile_result, BaseException)
                                else None)
            # Error checking rides the same codec switch as Run/Index: a None
            # root_input makes draw_text_from_code_cache skip the code-host
            # cache entirely - no Python parse of a .txt buffer, no syntax-error
            # highlight on plain text, and no background reparse per keystroke.
            child_kwargs['error'] = (_runtime_error or _recompile_error) if code_buttons else None
            child_kwargs['root_input'] = input_value if code_buttons else None
            # Same switch again: a .txt buffer gets plain 'default'-colored
            # text instead of Python-tokenized Darcula colors (draw_text builds
            # inline token widgets with it).
            child_kwargs['syntax_highlight'] = code_buttons
            # Enclosing-cell column edges ride down like jump_to: code_file_io
            # is transparent to the column system (its box IS the cell
            # content), so a host row passes its cell's edge dicts BY
            # REFERENCE and the nested view's Column host adopts them as its
            # own edges (draw_function_live's source column). Stamped
            # unconditionally - usually None - so a shared child_kwargs dict
            # never carries one window's edges into another.
            child_kwargs['left_edge'] = kwargs.get('left_edge')
            child_kwargs['right_edge'] = kwargs.get('right_edge')
            # The view's reconvert trigger: load / external edit (external_change),
            # the Index button (run_jedi), and a buffer edit from last frame
            # (_reconvert) - chain_in re-parses typed text and surfaces syntax
            # errors. The view passes it as-is to chain_in, but the whole trigger
            # lives here, not in the view.
            #
            # Pass it as BOTH external_change (the body reads it for `start=`) and
            # draw=True : the view is blit-cached, so without bypassing that cache its
            # body is skipped and the trigger never fires. draw=True is the view's
            # one-shot cache bypass (no sticky edit flags), so the body runs at
            # frame whenever we asked for a reconvert.
            reconvert = code_state._reconvert
            code_state._reconvert = False
            trigger = external_change or run_jedi or reconvert
            # When this code_file_io is a NON-scrolling pane (the func/class
            # context code tabs pass disable_scroll=True), the inner editor must
            # be the SOLE scroll container - so pin it to the visible clip. Its
            # height then matches the viewport: it overflows and scrolls for a
            # source of ANY size (not only one past the 30000px height clamp),
            # and the wrapper's scrollbar (clip_height = draw_state.height) draws
            # the right grab ratio. Without this the editor grows to its content,
            # "fits" itself (no scroll) yet is clipped to the clip - its bottom
            # cuts off and unreachable. Mirrors draw_code_tabs_from_cache's pane
            # pinning. Other code_file_io users (standalone editor windows,
            # folder leaves, offscreen code-host str_hosts) are NOT disable_scroll
            # and keep growing to content with the window/wrapper owning scroll.
            # if (kwargs.get("disable_scroll") and draw_state.abs_clip_rect is not None
            #         and "height" not in child_kwargs):
            #     cursor_top = imgui.get_cursor_screen_pos()[1]
            #     child_kwargs["height"] = max(50.0, draw_state.abs_clip_rect[3] - cursor_top)
            #     # A passed height makes the editor fixed_size (auto_resize off),
            #     # so the wrapper no longer expands its width to the available
            #     # content area - it'd collapse to min_width. Pin the width to
            #     # the code_file_io's available width too, exactly as the NEW_CODE
            #     # columns path passes width alongside height.
            #     child_kwargs.setdefault("width", draw_state.content_width)
            edited, value = view_func(input_value=code_state.text_cache,
                                      external_change=trigger, draw=trigger,
                                      **child_kwargs)

            if edited:
                code_state.text_cache = value
                code_state.mark_file_current()
                code_state._pending_save = True
                code_state._reconvert = True
        else:
            edited = False

        # ── 4. Save / recompile - on background threads ───────────────────────────
        # Both route through run_in_background, the same one-shot runner load uses.
        # Each call site has a different name=, so each gets its OWN injected
        # loading_state - save and recompile can't clobber each other (or load).
        # We call them unconditionally every frame so the runner can spawn the
        # thread and surface completion; `start=` is just the trigger edge.
        save_hotkey = bool(s_key_pressed and s_key_pressed.ctrl)
        recompile_hotkey = bool(enter_key_pressed and enter_key_pressed.ctrl) and code_buttons

        # Save: write the edited span back to disk off the main thread. The text is
        # snapshotted into child_kwargs at trigger time, so a later edit can't race
        # the in-flight write. Auto-save-on-edit is debounced so a burst of
        # keystrokes collapses into one write after typing pauses; the explicit
        # save button / Ctrl+S fires immediately (debounce 0).
        #
        # INVARIANT - the save is a write-only side channel off the live buffer:
        # it must never hold the UI back. The buffer (text_cache) and everything
        # rendered from it always run ahead; the save trails behind. Two rules
        # keep that true:
        #   1. The queued snapshot must hold the LATEST buffer until launch:
        #      run_in_background's one-slot queue is refreshed by every `start`,
        #      so every edit while a save is queued/in flight MUST re-arm
        #      save_start. Any condition that gates save_start across multiple
        #      edit frames (today: `not conflict`) must be impossible to trigger
        #      from the save's own lifecycle - otherwise the queue freezes on an
        #      old buffer and the completed write pushes the OLD version out to
        #      every view that syncs through the file.
        #   2. Our own write must never read back as an external change. The
        #      mtime bump is absorbed on busy frames (result is LOADING - which
        #      includes completed-but-superseded runs), on the reported `saved`
        #      frame, and by the verified self-write absorb above for any frame
        #      that runs between them.
        explicit_save = save_hotkey or save
        # During a conflict (external write + pending local edit) the debounced
        # auto-save is OFF - only an explicit save (Keep mine / Save / Ctrl+S)
        # writes, and it writes with force past the codec's span guard. An
        # already-in-flight debounced save is caught by that guard instead and
        # comes back as SaveConflict (handled below).
        save_start = (auto_save and edited and not conflict) or explicit_save
        force_save = keep_mine or (conflict and explicit_save)
        save_debounce = 0 if explicit_save else save_debounce_ms
        time = datetime.now().strftime("%H:%M:%S")
        if save_start:
            note = Note(name="Code_file_io save start", tint=(1, 0.5, 0))
            # Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=4, note=note)

        saved, result = run_in_background(save_file, main_thread=True,
                                          child_kwargs={"address": address,
                                                        "codec": codec,
                                                        "code_str": code_state.text_cache,
                                                        "ensure_import": ensure_import,
                                                        "parent_ds": draw_state,
                                                        "force": force_save},
                                          name=f"save{draw_state.name}", start=save_start,
                                          debounce_ms=save_debounce,
                                          wait_for_drag=not explicit_save)
        if result is LOADING:
            code_state.mark_file_current()

        elif saved and isinstance(result, SaveConflict):
            # The codec refused the splice - the on-disk span changed under the
            # in-flight write. Nothing was written: keep the edit pending but
            # mark the file stale so the conflict UI above surfaces next frame.
            # _save_refused disables the self-write absorb (the disk likely
            # holds a SIBLING editor's in-process write, which would otherwise
            # hash-match and silently re-absorb the conflict on this frame).
            code_state._save_refused = True
            code_state.mark_file_stale()
            request_render()

        elif saved:
            # Our own write bumped mtime; clear the stale flag set on edit so the
            # next frame doesn't read the disk as an external change.
            code_state.mark_file_current()
            code_state._pending_save = False
            code_state._save_refused = False
            note = Note(name="On saved, code_file_io", tint=(0.5, 1.0, 1.0), draw_state=draw_state)
            Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=4, note=note)

        # Recompile (hot reload, no disk write): button, Ctrl+Enter, or recompile=True
        # on edit. Same runner, its own loading_state.
        recompile_start = (recompile) or recompile_hotkey
        run_recompile(input_value, code_state, draw_state, start=recompile_start)


    except Exception as e:
        imgui.text_colored(f"editable_source error: {e}", 1.0, 0.4, 0.0)

        if not hasattr(code_state, "_resolve_stack"):
            def get_frames(an_e):
                """Extract live frames from a caught exception's traceback."""
                tb = an_e.__traceback__
                if tb is None:
                    return []
                results = []
                max_depth = 10
                while tb is not None or len(results) >= max_depth:
                    frame_obj = tb.tb_frame
                    results.append(frame_obj)
                    tb = tb.tb_next

                return results

            frames = get_frames(e)
            setattr(code_state, "_resolve_stack", frames)

        from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
        call_stack = getattr(code_state, "_resolve_stack", [])
        RenderFuncs.draw_collection(call_stack, name=f"resolve_stack{unique}{id(call_stack)}",
                                    mode=Modes.WINDOW, tint=(0.9, 0.4, 0.1))
        print_stack_trace(exception=e)

    # This is the root node, end of the line: code_file_io will load/edit/save
    # itself, so it returns the ORIGINAL input, never the edited text. When a
    # collection applies a changed child value back into its hosts; text
    # here would replace the held Path/class with a string. Consumers that want
    # the edited value (RenderHost) inject themselves as view_func and receive
    # it through that channel instead.
    return edited, input_value


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CodeHost cache - shared source/cst hosts per function/class/module          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# (source_ref | CallSite) -> (str_host, dict_host). Process-wide; entries are
# created lazily on first request and live for the session. Hotswap keeps
# function/class/module identities stable (the original wrapper/raw stay
# canonical), so reference keys survive recompiles; CallSite is a frozen
# value-equality key. Each host watches its own file (auto_load_edits), so a
# cached entry stays current when the file changes on disk.
_code_host_cache: dict = {}


def code_hosts_for(ref):
    """The shared (source_str_host, cst_dict_host) RenderHost pair for a
    function / class / module / CallSite — the same wiring draw_input_tab used
    to build per menu-open, now built ONCE per distinct reference and reused:

        str_host   code_file_io <- ref          (the editable source text)
        dict_host  convert_in_and_out_value     (source <-> cst dict via the
                   <- str_host                   NEW_CODE chain, "code_dict"
                                                 routed to the editor)

    Lazy: nothing loads until the first consumer draws the host. Consumers that
    read the value outside the host's own draw loop must still register via
    host.notify_on_change(draw_state), exactly as before."""
    key = ("callsite", ref.filename, ref.lineno) if isinstance(ref, CallSite) else ref
    try:
        pair = _code_host_cache.get(key)
    except TypeError:           # unhashable ref; fall back to uncached
        pair = None
        key = None
    if pair is None:
        from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
        n = len(_code_host_cache)
        label = getattr(ref, "__name__", None) or type(ref).__name__
        str_host = RenderHost(io_function=code_file_io, input_value=ref,
                              name=f"##code_cache_{label}_{n}{key}_str",
                              settings_renderer=RenderFuncs.draw_text,
                              child_kwargs={"auto_load_edits": True})
        # MODULE/FILE refs get the static name/signature lint (code_checks): the
        # buffer is self-contained, so an unresolved name really is a NameError.
        # A span ref (function/class/CallSite) sees none of its module's imports
        # and would flag every one - no lint_path, no lint.
        lint_path = None
        if isinstance(ref, Path) and ref.suffix == ".py":
            lint_path = str(ref)
        elif isinstance(ref, types.ModuleType):
            lint_path = getattr(ref, "__file__", None)
        dict_host = RenderHost(
            io_function=convert_in_and_out_value, input_value=str_host,
            name=f"##code_cache_{label}_{n}{key}_dict",
            child_kwargs={
                "chain_in": [string_to_cst_module, cst_module_to_dict],
                "chain_out": [dict_to_cst_module, cst_module_to_string],
                "route": {cst_module_to_dict: ("code_dict", "jump_to", "run_jedi", "drive")},
                **({"run_chain_kwargs": {"lint_path": lint_path}} if lint_path else {}),
            })
        pair = (str_host, dict_host)
        if key is not None:
            _code_host_cache[key] = pair
    return pair


def host_code_state(host):
    """The CodeState living inside a code str_host's wrapper — code_file_io's
    injected state, holding the LIVE buffer (text_cache) and resolved address.
    Same lookup shape as draw_text_from_code_cache's ModesState scan: injected
    states sit in the wrapper draw_state's misc, keyed by param name. None until
    the host has drawn at least once (hosts are lazy)."""
    wds = getattr(host, "_wrapper_draw_state", None)
    for v in (getattr(wds, "misc", None) or {}).values():
        if isinstance(v, CodeState):
            return v
    return None


# Monotonic time of the last auto-index nudge. One host per window: at
# session start every open editor's parse predates the warmer's first build,
# and letting them all index at once stacks several ~0.2s GIL-holding passes
# against the render thread. Time-based, NOT frame-based - the render loop is
# event-driven, so N frames at idle can be unbounded wall-clock time. Skipped
# hosts simply retry on a later frame.
_last_auto_index_time = 0.0
_AUTO_INDEX_STAGGER_S = 0.25


def _ensure_symbol_index(dict_host, str_host, code_dict, jump_to=None):
    """Auto-index trigger: re-run the cache's chain_in when the held parse
    predates the current symbol index — either it never got a symbol pass
    (fresh session: the host's first parse ran before any editor stamped
    jump_to into its child_kwargs) or the background cache warmer
    (SymbolIndexCache) advanced a generation because src files changed, so
    cross-file callers may have moved. The chain itself attaches the symbols
    (cst_module_to_dict's auto pass); this only supplies `jump_to` and the
    re-run edge. One nudge per (parse identity, generation), so a span whose
    index is legitimately empty doesn't re-trigger every frame."""
    global _last_auto_index_time
    if dict_host is None or not isinstance(code_dict, dict):
        return
    if not (getattr(Toggles, "enable_jedi", True)
            and getattr(Toggles, "auto_index", True)
            and not getattr(Toggles, "jedi_correctness", False)):
        return
    from src.lsd.gl_gui.view.core_conversion import libcst_conversion as _lc
    gen = _lc._index_generation
    if gen < 1:
        return          # warmer hasn't built yet - we retry once it bumps
    if getattr(code_dict, "_symbol_gen", None) == gen:
        return          # parse already indexed against the current generation
    if not _lc._wait_for_no_drag(max_wait=0.0):
        return          # mid-gesture - don't even start; retried next frame
    if jump_to is None and str_host is not None:
        cs = host_code_state(str_host)
        jump_to = getattr(cs, "address", None) if cs is not None else None
    if jump_to is None:
        return          # host hasn't resolved a span yet
    if dict_host.child_kwargs.get("jump_to") is not jump_to:
        dict_host.child_kwargs["jump_to"] = jump_to
    key = (id(code_dict), gen)
    if getattr(dict_host, "_auto_index_key", None) == key:
        return          # nudge already issued for this parse / generation
    if time.monotonic() - _last_auto_index_time < _AUTO_INDEX_STAGGER_S:
        return          # another host nudged recently - stagger a retry later
    _last_auto_index_time = time.monotonic()
    dict_host._auto_index_key = key
    threading.Thread(target=_index_host_in_place, args=(str_host, dict_host, gen),
                     daemon=True, name="symbol-index-attach").start()


def _index_host_in_place(str_host, dict_host, gen):
    """Compute + attach symbol usages onto a host's HELD gp, on a background
    thread, WITHOUT re-running its chain. A chain re-run would libcst-reparse
    the whole buffer — at ~0.5s+ of GIL-bound parse per open editor, the
    original gen-bump kick stacked those into one big render-thread hang right
    after the warmer's first build. The index compute itself is unavoidable
    GIL work (it resolves against LIVE objects via _src_mod_map, so unlike the
    accurate-jedi path it cannot move to the subprocess pool), but it's the
    small slice — mtime/generation-cached, ~25ms warm. In-place dict writes on
    the gp are safe here: consumers only re-read after _notify_consumers
    invalidates their subtrees (the same wake a background parse uses)."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
        compute_symbol_usages_for_address, _distribute_by_name, _wait_for_no_drag)
    if not _wait_for_no_drag():
        # Gesture outlasted the wait - bail rather than steal GIL time from
        # it. Clearing the in-flight key lets the editor-side nudge (or
        # the next generation bump) retry once the user lets go.
        dict_host._auto_index_key = None
        return
    address = dict_host.child_kwargs.get("jump_to")
    if address is None:
        cs = host_code_state(str_host)
        address = getattr(cs, "address", None) if cs is not None else None
        if address is None:
            return
        # Persist for the chain: future reparses attach via cst_module_to_dict.
        dict_host.child_kwargs["jump_to"] = address
    try:
        flat = compute_symbol_usages_for_address(address)
    except Exception:
        return

    def _attach():
        # Runs on the render thread (Melty.post_to_render): the gp is LIVE -
        # several hosts (editor views, usage spans, draw_collection) iterate
        # its dicts every frame, and adding __symbol_usages__ keys from a
        # worker mid-iteration raises "dictionary changed size during
        # iteration". Between frames there is no iterator to race. Re-fetch
        # the held value here - a reparse may have replaced it mid-compute;
        # sites are file-absolute, so attaching to the newer gp is correct.
        gp = dict_host._held()
        if not isinstance(gp, dict):
            return
        gp._symbol_gen = gen
        if flat:
            gp.symbol_usage = flat
            _distribute_by_name(gp, flat)
        dict_host._notify_consumers(name="symbol index attached")

    Melty.post_to_render(_attach)


def _wake_stale_code_hosts(gen):
    """Index-generation-bump hook (runs on the warmer's daemon thread):
    refresh the symbol usages of every cached code host whose held parse
    predates `gen`, without any editor interaction. The editor-side
    _ensure_symbol_index can't cover this case — cached editor views replay
    their blit on an idle app, so a bump that happens while nothing is
    invalidating (right after startup, or an external-IDE edit) would never
    be observed. Attaches IN PLACE (no chain re-run / libcst reparse — see
    _index_host_in_place); a small sleep between hosts keeps their index
    passes from stacking into one GIL burst against the render thread."""
    if not (getattr(Toggles, "enable_jedi", True)
            and getattr(Toggles, "auto_index", True)
            and not getattr(Toggles, "jedi_correctness", False)):
        return
    for sh, dh in list(_code_host_cache.values()):
        gp = dh._held()
        if not isinstance(gp, dict) or getattr(gp, "_symbol_gen", None) == gen:
            continue
        if getattr(dh, "_auto_index_key", None) == (id(gp), gen):
            continue
        dh._auto_index_key = (id(gp), gen)
        _index_host_in_place(sh, dh, gen)
        time.sleep(0.25)


def _register_index_bump_hook():
    from src.lsd.gl_gui.view.core_conversion import libcst_conversion as _lc
    cbs = getattr(_lc, "_index_bump_callbacks", None)
    if cbs is None:
        return                  # older libcst_conversion still loaded
    cbs[:] = [cb for cb in cbs
              if getattr(cb, "__name__", "") != "_wake_stale_code_hosts"]
    cbs.append(_wake_stale_code_hosts)


_register_index_bump_hook()


def draw_text_from_code_cache(input_value=None, root_input=None, error=None,
                              run_jedi=False, **kwargs):
    """FILE_TREE's editor view: plain draw_text fed from the shared code-host
    cache instead of an inline chain. code_hosts_for(path) owns the parse —
    str_host watches the file, dict_host re-parses on change — and we pull its
    held cst_dict each frame, so usage links and syntax-error highlighting
    arrive as code_dict / code_tree exactly as in the NEW_CODE routes. Pull-
    based by design: an edit here auto-saves to disk, the cache's str_host
    reloads from the file, and the editor picks up the fresh parse a beat
    later — the leaf and the cache only ever talk through the file."""
    code_dict, cache_error, dict_host = None, None, None
    if root_input is not None:
        _str_host, dict_host = code_hosts_for(root_input)
        code_dict = dict_host._held()
        # Background auto-index: keep the held parse's symbol usages current
        # without the manual Index click (no-op when already indexed).

        _ensure_symbol_index(dict_host, _str_host, code_dict, kwargs.get("jump_to"))
        # The chain's parse error lives on the wrapper's injected ModesState.
        # Normalize it to the ParseError-dict shape _code_tree_errors reads
        # ({'__error__','__line__'}) and pass it as code_tree: the raw exception
        # could be a cst.ParserSyntaxError, whose line lives in `raw_line` - a
        # name neither _code_tree_errors nor _exception_handler knows. code_dict
        # keeps the last GOOD parse alongside, so the editor highlights the
        # offending line without losing its structure.
        wds = getattr(dict_host, "_wrapper_draw_state", None)
        for v in (getattr(wds, "misc", None) or {}).values():
            if isinstance(v, ModesState):
                err = v.last_error
                lint = getattr(v, "last_lint", None) or None
                if err is not None or lint:
                    # ONE dict per underlying (exception, lint) pair, not one
                    # per frame: draw_text's parse-error staleness check
                    # compares code_tree by IDENTITY to detect "a fresh parse
                    # landed", so a dict rebuilt every frame would re-hide a
                    # stale highlight one frame after an edit, pinned to the
                    # old line. The memo keeps identity stable until the
                    # background reparse actually replaces last_error /
                    # last_lint (both swapped per completed parse, never
                    # mutated in place).
                    memo = getattr(dict_host, "_err_view_memo", None)
                    if memo is not None and memo[0] is err and memo[1] is lint:
                        cache_error = memo[2]
                    else:
                        # The parse/compile error first (lint only runs on a
                        # clean compile, so in practice it's one or the other),
                        # then the static name/signature findings - all un
                        # __errors__, with the first mirrored into the single
                        # __error__/__line__ pair older readers use.
                        markers = []
                        if err is not None:
                            line = (getattr(err, "editor_line", None) or getattr(err, "lineno", None)
                                    or getattr(err, "raw_line", None) or 1)
                            msg = (getattr(err, "message", None) or getattr(err, "msg", None)
                                   or str(err))
                            markers.append((line, msg))
                        markers += list(lint or ())
                        cache_error = {"__error__": markers[0][1], "__line__": markers[0][0],
                                       "__errors__": markers}
                        dict_host._err_view_memo = (err, lint, cache_error)
        # The Index button's pulse rides to the cache's chain_in (one-shot:
        # cleared again on the next un-pulsed frame). cst_module_to_dict only
        # runs jedi when it ALSO has the resolved address (jump_to) - the leaf's
        # code_file_io hands us the whole-file Address, so forward it alongside.
        # _pending_external makes the host re-run chain_in (its start gate is
        # external_change); invalidating alone would just replay the blit.
        if run_jedi:
            dict_host.child_kwargs["run_jedi"] = True
            if kwargs.get("jump_to") is not None:
                dict_host.child_kwargs["jump_to"] = kwargs["jump_to"]
            dict_host._pending_external = True
            for hds in (wds, getattr(dict_host, "_draw_state", None)):
                if hds is not None:
                    hds.invalidate()
            request_render()
        elif (dict_host.child_kwargs.get("run_jedi")
                and not dict_host._pending_external):
            # One-shot clear - but only after the host actually consumed the
            # pulse (_pending_external drops when its chain_in is handed the
            # kwargs). Popping earlier loses a click whenever this editor
            # re-renders between the pulse frame and the host's next draw.
            dict_host.child_kwargs.pop("run_jedi", None)

        if len(_str_host.values()) > 0:
            changed, value, ds = RenderFuncs.draw_text(list(_str_host.values())[0], code_dict=code_dict,
                                                       code_tree=cache_error, error=error,
                                                       return_extras=True, **{**kwargs, "is_tree":False})
            if changed:
                _str_host[list(_str_host.keys())[0]] = value
                dict_host.notify_on_change(ds)
    # # Re-render this editor when a background parse lands: its cached
    # # subtree is outside the host's own draw loop, so without registering it
    # # the fresh cst_dict sits invisible until an unrelated invalidation.
    # if dict_host is not None and ds is not None:
    #     dict_host.notify_on_change(ds)
    return False, None


def _host_code_tree_error(dict_host):
    """The dict-host's parse / compile / lint error, normalized to draw_text's
    code_tree shape ({__error__, __line__, __errors__}), or None when clean.

    The same extraction draw_text_from_code_cache does, memoized on the host by
    (exception, lint) IDENTITY: draw_text's parse-error staleness check compares
    code_tree by identity to tell "a fresh parse landed", so a dict rebuilt every
    frame would un-hide a stale highlight the frame after an edit. last_error /
    last_lint are swapped per finished parse (never mutated in place), so identity
    is a safe key."""
    if dict_host is None:
        return None
    wds = getattr(dict_host, "_wrapper_draw_state", None)
    for v in (getattr(wds, "misc", None) or {}).values():
        if not isinstance(v, ModesState):
            continue
        err = v.last_error
        lint = getattr(v, "last_lint", None) or None
        if err is None and not lint:
            return None
        memo = getattr(dict_host, "_err_view_memo", None)
        if memo is not None and memo[0] is err and memo[1] is lint:
            return memo[2]
        markers = []
        if err is not None:
            line = (getattr(err, "editor_line", None) or getattr(err, "lineno", None)
                    or getattr(err, "raw_line", None) or 1)
            msg = (getattr(err, "message", None) or getattr(err, "msg", None) or str(err))
            markers.append((line, msg))
        markers += list(lint or ())
        cache_error = {"__error__": markers[0][1], "__line__": markers[0][0], "__errors__": markers}
        dict_host._err_view_memo = (err, lint, cache_error)
        return cache_error
    return None


@render_func(use_cache=True, show_bg=False, selectable=False, disable_scroll=True,
             shadow=False, indent_size=0, with_footer=None)
def draw_code_tabs_from_cache(input_value=None, root_input=None, tab_state: TabState = None,
                              unique=None, draw_state=None, column_widths=None,
                              column_edges=None, draw=False, error=None,
                              run_jedi=False, **kwargs):
    """NEW_CODE's text|structured tabs on the code-host-cache route — no inline
    chain_in/chain_out (the legacy convert_in_and_out path this replaces).

      • draw_text — delegates to draw_text_from_code_cache; a text edit returns
        to the enclosing code_file_io, which saves the span (its normal path).
      • draw_collection — renders the shared dict_host's HELD GeneralParse
        (code_hosts_for). An edit mutates the bubbling-wrapped parse in place,
        marking the host dirty; the host chain_outs to source and saves through
        the cache's own str_host, so nothing returns upward from this column.
        An edit ALSO drives the live source immediately (live_apply_edits:
        class vars, function param defaults + constant locals, module
        globals) — the same responsive preview the chain route's
        general_parse_to_address gives, ahead of any recompile.

    The two code_file_io instances (this window's and the cache's str_host)
    only ever talk through the FILE: a dict edit saves via the cache and this
    window's auto_load_edits picks it up; a text edit saves here and the
    cache's file watch re-parses."""
    view_funcs = [RenderFuncs.draw_collection, RenderFuncs.draw_text]
    # Drop entries that didn't survive (de)serialization, then default to two
    # tabs (structured | text), matching draw_with_view_funcs.
    tab_state.selected_tabs = [t for t in tab_state.selected_tabs if t is not None]
    if not tab_state.selected_tabs:
        tab_state.selected_tabs = view_funcs[:2]

    imgui.dummy(0, 5)
    names = [getattr(vf, '__name__', str(vf)) for vf in view_funcs]
    tab_changed, new_tabs = RenderFuncs.draw_tab_bar(input_value=tab_state.selected_tabs,
                                                     tab_height=30, show_bg=False, bg_offset=1,
                                                     name=f"tab_bar{unique}", names=names,
                                                     collection=view_funcs, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    imgui.dummy(0, 2)
    dict_host = None
    if root_input is not None:
        _str_host, dict_host = code_hosts_for(root_input)
        # Repaint this subtree when the background parse lands - it reads the
        # host's value from outside the host's own draw loop (same registration
        # draw_text_from_code_cache makes for its error/code_dict pull).
        dict_host.notify_on_change(draw_state)
        # Auto-index here too (idempotent with the draw_text delegate's call):
        # a structured-only window never runs draw_text_from_code_cache, and
        # its usage links should stay live all the same.
        _ensure_symbol_index(dict_host, _str_host, dict_host._held(),
                             kwargs.get("jump_to"))

        # New columnLayout (shared edge system): the panes line up with other
        # objects drawn on the root window - the divider between the
        # structured and text panes is a draggable line in the same collision
        # region as every other window edge. Lazy import (new_core_view sits
        # between this module and columns.py). left_edge/right_edge: when this
        # view renders inside another row's cell, the host passes the cell's edge
        # dicts (through code_file_io's child_kwargs) and they become this row's
        # far edges by reference - same adoption draw_columns gives nested
        # Columns. Absent (the usual standalone window), ColumnLayout falls back
        # to the window frame edges.
        from src.lsd.gl_gui.view.core_views.columns import ColumnLayout, MIN_ROW_HEIGHT
        cols = ColumnLayout(draw_state, len(tab_state.selected_tabs),
                            column_edges=column_edges, column_widths=column_widths,
                            left_edge=kwargs.get("left_edge"),
                            right_edge=kwargs.get("right_edge"))
        # Pin each pane to the visible viewport (the legacy column_max_height
        # path this replaces) - long sources scroll inside their pane.
        avail_h = None
        size_kwargs = {}
        clip = cols.clip if cols.clip is not None else draw_state.abs_clip_rect
        if clip is not None:
            # Exactly match the frame band: the pane then ends one pixel
            # above the band's bottom edge, so the black reads even all around.
            avail_h = max(MIN_ROW_HEIGHT, clip[3] - cols.top)
            # The pane content is inset by the fixed padding on every side.
            size_kwargs = {"height": avail_h - 2 * cols.padding}

        # Grab the parse + its normalized error off the MANAGED dict_host once, before
        # the tab loop. Both tabs read them: the structured tab renders `gp` directly,
        # the text tab injects code_dict/code_tree/error into its draw_text leaf via
        # child_kwargs. Computing here also drops the old order dependency (the text tab
        # read `gp` before the structured branch defined it).
        gp = dict_host._held() if dict_host is not None else None
        cache_error = _host_code_tree_error(dict_host)

        raw_changed, raw_value = False, input_value
        for idx, view_func in enumerate(tab_state.selected_tabs):
            with cols.cell(idx, height=avail_h) as col_width:
                if getattr(view_func, "__name__", "") == "draw_text":
                    # The host's parse + errors flow to the draw_text leaf through
                    # draw_collection's child_kwargs: code_dict → token views / symbol
                    # usages, code_tree → the syntax/lint error highlight, error → the
                    # recompile/runtime highlight (the same trio draw_text_from_code_cache
                    # hands draw_text, now via the parent _str_host).
                    m_changed, m_out = RenderFuncs.draw_collection(
                        input_value=_str_host, child_kwargs={"error": error, "view_func": RenderFuncs.draw_text,
                                                             "code_dict": gp, "code_tree": cache_error,
                                                             "run_jedi": run_jedi, "jump_to": kwargs.get("jump_to")},
                        show_header=False, show_name=False,
                        width=col_width, **size_kwargs,
                        name=f"draw_text##{unique}")
                    if m_changed:
                        notify("text changed", tag="save bug", tint=(1, 1, 0.5))
                        raw_changed, raw_value = True, m_out
                        draw_state.invalidate_up(max_depth=2)
                else:
                    if not isinstance(gp, dict):
                        imgui.text_colored("Parsing…" if dict_host is not None
                                           else "No parse for this source", 0.6, 0.6, 0.6, 1.0)
                        continue
                    m_changed, m_out = RenderFuncs.draw_collection(
                        gp, excluded=["__cst__"], show_system=True, draw=draw,

                        disable_scroll=False, show_header=False, show_add_delete=False,
                        width=col_width, **size_kwargs, show_parent_add_delete=False,
                        name=f"draw_collection##{unique}", selectable=False)
                    if m_changed:
                        notify("dict changed", tag="save bug", tint=(1,1,0.5))
                        # A rebuilt top-level dict (reorder / add / delete) replaces the
                        # host value; an in-place value edit already bubbled the host
                        # dirty. Either way the host chain_outs + updates on its own draw.
                        if m_out is not gp and isinstance(m_out, dict):
                            dict_host[dict_host.value_key] = m_out
                            gp = m_out
                        live_apply_edits(root_input, gp)
                        draw_state.invalidate_up(max_depth=2)

        cols.finish()
    return False, None
