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
spinning `request_render`. An explicit Save / Ctrl+S bypasses the debounce.
Saves fire during drags too (the deferred save just queues the edit in memory —
PendingSave — so it's cheap; the disk write happens once at flush).

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
from meltygui.core.runtime.extensions import get as get_service

import inspect
import linecache
import sys
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

import meltygui_imgui as imgui
import libcst as cst

import meltygui.core.runtime.toggles as toggles
from meltygui.core.melty import FileWatch
from meltygui.core.melty import Melty
from meltygui.state.core_enums import ProfileMode
from meltygui.state.new_core_model import TabState
from meltygui.state.new_core_model import DrawState
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.state.model_enums import RelaxedEnum
from meltygui.core.rendering.modes import Modes
from meltygui.core.diagnostics.notifications import notify
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.windowing.glfw_utils import print_stack_trace
from meltygui.core.windowing.glfw_utils import get_exception_frames
import meltygui.code.hotswap_guard as hotswap_guard
from meltygui.code.fileref import Address
from meltygui.code.fileref import _evict_linecache
from meltygui.code.chain_converters import record_compile
from meltygui.code.chain_converters import _enclosing_function
from meltygui.code.chain_converters import live_apply_edits
from meltygui.code.chain_converters import _blank_line_variant
from meltygui.code.chain_converters import chain_parse_cache_get
from meltygui.code.chain_converters import chain_parse_cache_put
from meltygui.code.chain_converters import chain_parse_cache_has
from meltygui.code.code_checks import check_source
from meltygui.code.code_checks import check_source_incremental
from meltygui.code.code_checks import collect_import_suggestions
from meltygui.code.file_converters import _recompile
from meltygui.code.file_converters import _recompile_class
from meltygui.code.file_converters import _recompile_module
from meltygui.code.file_converters import module_for_path
from meltygui.code.libcst_conversion import cst_module_to_dict
from meltygui.code.libcst_conversion import dict_to_cst_module
from meltygui.code.new_codecs import Codec
from meltygui.code.new_codecs import CallSite
from meltygui.code.new_codecs import Decorations
from meltygui.code.new_codecs import SaveConflict
from meltygui.code.new_codecs import type_to_codec
from meltygui.code.new_codecs import extension_to_codec
from meltygui.code.new_codecs import codec_for_path
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import no_save_exclude
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.rendering.window_decoration import window
from meltygui.core.layout.header_runtime import draw_header
from meltygui.editor.pending_save import PendingSave
from meltygui.core.cache.invalidation_tracker import Note
from meltygui.core.rendering.core_decoration import defaults
from meltygui.core.diagnostics.perf_trace import trace as _ptrace
from meltygui.core.diagnostics.perf_trace import trace_rl as _ptrace_rl
from meltygui.core.diagnostics.perf_trace import span as _pspan
from meltygui.core.diagnostics.perf_trace import once as _ponce


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Core helpers - load / write / recompile (plain synchronous, no chain magic)  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


def _focus_inside_ds(ds):
    """True when the current text focus sits inside `ds`'s subtree — i.e. the
    save being queued originates from the editor the user is typing in. Walks
    up from Melty.text_focused_ds via _parent (self-loop root) then
    parent_window hops, same shape as _preferred_source_for's ancestor walk."""
    if ds is None:
        return False
    node, hops = Melty.text_focused_ds, 0
    while node is not None and hops < 32:
        if node is ds:
            return True
        parent = getattr(node, "_parent", None)
        nxt = parent if parent is not None and parent is not node else None
        if nxt is None:
            pw = getattr(node, "parent_window", None)
            nxt = pw if pw is not None and pw is not node else None
        node = nxt
        hops += 1
    return False


def save_file(address, code_str, codec=None, ensure_import=None, parent_ds=None, force=False):
    """Write the edited value back through the resolved codec (span splice for
    code, whole-file for images, etc.). Returns the codec's result — a
    SaveConflict when the codec refused the splice because the on-disk span
    changed under us (force=True, the user's explicit Keep-mine, bypasses)."""
    current_time = datetime.now().strftime("%H:%M:%S")
    file_name = address.path.name if address.path is not None else "unknown"
    notify(f"Saved {file_name} from {parent_ds.name}", tint=(0.5, 1.0, 0.5))
    # wake=False only for the typing editor's own per-keystroke auto-save
    # (focus inside this wrapper's subtree) - waking every visible editor of
    # the file per keystroke is the storm the wake was once disabled for. A
    # programmatic save through this same runner (a side-panel/lens edit
    # flowing out of the same cache host) keeps the wake so the visible
    # editor's draw_text is invalidated promptly.
    _wake = not _focus_inside_ds(parent_ds)
    _ptrace(f"save_file wake={_wake} parent_ds={getattr(parent_ds, 'name', None)}",
            file=file_name)
    PendingSave.queue_save(address=address, codec=codec, data=code_str, ensure_import=ensure_import, force=force,
                           wake=_wake)
    return True
    # return codec.save(address=address, data=code_str, ensure_import=ensure_import, force=force)


def load_file(input_value: Address, codec: Codec = None, **kwargs) -> str:
    """Read the value through the resolved codec (span for code, whole file for
    images, etc.)."""
    file_name = input_value.path.name if input_value.path is not None else "unknown"
    notify(f"Loading {file_name}...", tint=(0.5, 1.0, 0.5))
    with _pspan("load_file", file=file_name):
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
    elif isinstance(source, (str, Path)) and str(file_path).endswith(".py"):
        # Whole-file edits resolve through TextFileCodec, whose Address
        # carries the PATH as source - resolve the live module here so the file
        # recompile patches its classes/functions the same way the span
        # recompile does. No live module is a real failure (previously this
        # fell through to result=None and reported success while swapping
        # nothing).
        module = module_for_path(file_path)
        if module is not None:
            result = _recompile_module(module, code_str, str(file_path))
        else:
            result = NameError(f"no live module loaded from {file_path} — "
                               f"nothing to hotswap")
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
    tint = (0.52, 0.80, 0.688)
    tint = (0.52, 0.80, 0.688)

    # [tint=(0.7722222, 0.5336913466453552, 0.17589502036571503)]
    def some_func(a=84, b=-153):
        imgui.set_cursor_pos()

    some_line = 87
    myflot = 5

    aomw_list = 62

    list_new = [1, -1, 12]

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
        # Perf-trace stamps (arm edge time + task label) for the timeline log
        # below run_in_background - diagnostics only, no behavior.
        self._armed_t = None
        self._armed_label = None


UNSET = object()
LOADING = object()


@render_func(use_cache=True, selectable=False, temp=True)
def run_in_background(input_value, loading_state: LoadingState, unique,
                      draw_state, child_kwargs, start=False, timeout=20,
                      debounce_ms=0, main_thread=False, inline_first=False,
                      **kwargs):
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
    It only ever applies to RE-runs: while there's no result yet (first load),
    the launch is immediate, so a debounced caller never trades first-paint
    latency for burst-coalescing."""
    if Melty.frame_count < 4 or main_thread or loading_state.cached_result is UNSET:
        debounce_ms = 0
    if start:
        # Timeline of the arm edge. _armed_t / _armed_label for the run + report
        # traces below, so the log shows arm → run (queue wait) → report (frame
        # hops) as three stamps per task instead of one opaque duration.
        loading_state._armed_t = time.perf_counter()
        loading_state._armed_label = getattr(input_value, '__name__', 'task')
        _ptrace(f"rib: armed {loading_state._armed_label}",
                debounce_ms=debounce_ms, inline_first=inline_first)
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
        else:
            loading_state._debounce_deadline = None
            if loading_state._debounce_timer is not None:
                loading_state._debounce_timer.cancel()
                loading_state._debounce_timer = None

            # Completion can arrive while another OS surface is active.
            # Wake the cache which owns this runner, captured on the UI thread.
            runner_cache = Melty.cache

            def run(run_next_inner):
                loading_state._loading = True
                value, background_kwargs = run_next_inner
                # Never run a @render_func WRAPPER on this worker thread - the wrapper
                # mutates process-global Melty state (depth, unique_id, ...) on
                # entry/exit, which races the main render thread. Grab the bare inner
                # function: plain functions pass through unchanged.
                value = getattr(value, '__wrapped__', value)
                # Queue wait = arm edge → this thread actually executing (frame
                # hops + thread spawn + GIL contention live in this gap).
                _armed = getattr(loading_state, '_armed_t', None)
                _wait_ms = (time.perf_counter() - _armed) * 1000 if _armed else -1
                try:
                    with _pspan(f"rib: run {getattr(value, '__name__', 'task')}",
                                wait_ms=round(_wait_ms, 1)):
                        loading_state.cached_result = value(**background_kwargs)
                except Exception as exc:
                    loading_state.error = exc
                    print_stack_trace(exception=exc)
                finally:
                    loading_state._loading = False
                    loading_state._pending_change = True
                    # Completion wake for every flavor, main_thread=True included.
                    # main_thread once meant "ran inline, result visible this
                    # frame" (see the commented block below) - when it moved to
                    # worker threads the invalidate was never added, so a finished
                    # load/save was unobserved until an unrelated event fired a
                    # frame (the 100–600ms idle gaps on the initial-load
                    # timeline). The invalidate dirties this runner's tile +
                    # ancestors so the caller's body actually re-runs next frame -
                    # a dirty tile would just replay its blit past the result.
                    note = Note(name="Run in background complete", tint=(0.5, 1.0, 0.5), draw_state=draw_state)
                    # A fast load can finish before this frame commits its
                    # tiles. Queue the wake after that commit on the UI thread.
                    def wake():
                        runner_cache.invalidate(draw_state._tile_id, force=True, note=note)
                        # Hidden, zero-pixel IO hosts have no tiles to dirty.
                        # Their wrapper flags are the IO pump's wake signal.
                        current = draw_state
                        seen = set()
                        while current is not None and id(current) not in seen:
                            seen.add(id(current))
                            if current._tile_id not in runner_cache._tiles:
                                current._external_change = True
                            current = current._parent
                    Melty.post_to_render(wake)

            #
            # if Melty.frame_count < 0 or main_thread:
            #     run(run_next_inner=loading_state._run_next)
            #     loading_state._run_next = None
            # else:
            run_next = loading_state._run_next
            if not loading_state._loading:
                loading_state._run_next = None
                if inline_first and loading_state.cached_result is UNSET:
                    # First-ever result for a caller that opted in (the initial
                    # file load): run synchronously so the value is visible THIS
                    # frame. Each async stage costs one whole render-hop before
                    # its result is observed; on the initial-load pipeline those
                    # hops (not the work, ~2ms here) are the latency. Only the
                    # first load is inline - reloads and every save stay async
                    # (the save UI must never interrupt the edit frame).
                    run(run_next_inner=run_next)
                else:
                    # Named after the target func so perf-trace lines from this
                    # worker read as e.g. [bg:_run_chain_in] instead of [Thread-42].
                    _bg_name = f"bg:{getattr(run_next[0], '__name__', 'task')}"
                    threading.Thread(target=run, kwargs={"run_next_inner": run_next},
                                     name=_bg_name).start()
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
        # Report edge: the caller actually OBSERVES the result. total_ms - the
        # run span's duration = frame-hop / wake latency, the historic silent
        # cost on the initial-load pipeline.
        _armed = getattr(loading_state, '_armed_t', None)
        if _armed is not None:
            loading_state._armed_t = None
            _ptrace(f"rib: report {getattr(loading_state, '_armed_label', 'task')}",
                    total_ms=round((time.perf_counter() - _armed) * 1000, 1))
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
        # What the fading external-load stamp says: None = "loaded from disk";
        # a manual load path may set its own message.
        self._external_load_label = None

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
    `@...` lines off its decorators). A str is the core_syntax path's "module"
    (the text itself, see _melty_syntax_source): the same two wrappers come off
    textually."""
    if isinstance(module, str):
        for prefix in _CALL_WRAP_PREFIXES:
            if module.startswith(prefix):
                return textwrap.dedent(module[len(prefix):])
        if module.endswith(_DECO_WRAP_SUFFIX):
            return module[:-len(_DECO_WRAP_SUFFIX)] + "\n"
        return module
    body = getattr(module, "body", None)
    if body and len(body) == 1 and isinstance(body[0], cst.FunctionDef):
        name = body[0].name.value
        if name == _CALL_WRAP_NAME:
            return cst.Module(body=list(body[0].body.body)).code
        if name == _DECO_WRAP_NAME:
            blank = cst.Module(body=[])
            return "".join(blank.code_for_node(d) for d in body[0].decorators)
    return module.code


def _melty_syntax_source(text):
    """Toggles.TextEditor.melty_syntax: the chain's "module" is the (dedented)
    TEXT itself — cst_module_to_dict parses a str through core_syntax. Same
    wrap fallbacks as the libcst path below (a function wrapper for an
    in-function statement, a throwaway def after a decorator block), checked
    with ast; _unwrap_call_module strips them textually on the way out. Raises
    the bare SyntaxError when nothing parses (a VALUE for _run_convert — it
    carries `lineno` for the red highlight like _compile_check's)."""
    import ast
    if (Toggles.TextEditor.melty_scanner
            and len(text) >= Toggles.TextEditor.melty_async_min_chars):
        # A buffer this big is a whole file (never a snippet needing a wrapper)
        # and its syntax check runs inside the scan worker, off the GIL - an
        # ast.parse here would hold the render thread for ~70ms per keystroke.
        return text
    try:
        ast.parse(text)
        return text
    except SyntaxError as bare_exc:
        indented = textwrap.indent(text, "    ")
        for prefix in _CALL_WRAP_PREFIXES:
            wrapped = prefix + indented
            try:
                ast.parse(wrapped)
                return wrapped
            except SyntaxError:
                continue
        wrapped = text.rstrip() + _DECO_WRAP_SUFFIX
        try:
            ast.parse(wrapped)
            return wrapped
        except SyntaxError:
            pass
        raise bare_exc


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
    if Toggles.TextEditor.melty_syntax:
        return True, _melty_syntax_source(text)
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
    from meltygui.code.syntax_check import check_syntax
    if isinstance(text, str) and len(text) > Toggles.TextEditor.fast_check_max_chars:
        from meltygui.code.syntax_check_worker import check_isolated
        return check_isolated(text, _CALL_WRAP_PREFIXES)
    return check_syntax(text, _CALL_WRAP_PREFIXES)


# Process-boot timestamp for the lint/suggestion boot window: within
# _LINT_BOOT_QUIET_S of the FIRST module load, chain_in skips both passes
# (lint_deferred) so app load never pays them - the editor reschedules via
# the relint path once up. globals().get keeps the stamp across hotswap
# re-execs (module registries survive; a reset clock would re-enable the
# window on every swap).
_BOOT_T = globals().get("_BOOT_T") or time.monotonic()
_LINT_BOOT_QUIET_S = 8.0


def _region_compile_check(old, new, max_chars):
    """Changed-region syntax check for buffers too big to compile whole per
    keystroke. Diffs `old` → `new` by common prefix/suffix LINES, expands the
    changed span to its enclosing top-level block(s) (nearest column-0 lines),
    and compiles just that snippet through _compile_check (which dedents and
    fake-function-wraps, so a mid-file block checks clean standalone).

    Differential by design: a region cut through a triple-quoted string or a
    bracketed continuation fails to compile for reasons that aren't the user's
    edit — so a NEW failure only counts when the SAME region from the OLD text
    compiled clean. Returns (status, err, span):
      "clean"     — new region compiles; no syntax error introduced here
      "error"     — new region fails, old was clean: err carries a REAL
                    SyntaxError with lineno mapped to buffer coordinates
      "ambiguous" — both fail (extraction artifact, or an error predating this
                    edit): err is the new failure, caller decides
      "skip"      — no line change (span None), or region over max_chars
                    (span still reported: no compile ran, but the caller can
                    keep shifting a held error around the unchecked edit)
    `span` is (start, end_old, delta): the checked block as 0-based OLD-text
    line bounds (end exclusive) plus the edit's line-count delta — what a
    caller needs to keep a held error from ANOTHER region alive across this
    edit (clear it only inside the span; shift it by delta below the span)."""
    a = old.split("\n")
    b = new.split("\n")
    na, nb = len(a), len(b)
    from meltygui.editor.text_editor import _text_splice
    edit = _text_splice(old, new)
    if edit is None:
        return "skip", None, None
    pre, end_line = edit[4], edit[5]
    # Character boundaries can sit on an unchanged empty line. Refine only
    # those two rows; the common prefix/suffix already covers the rest.
    while pre < min(na, nb) and a[pre] == b[pre]:
        pre += 1
    suf = max(0, min(na - end_line - 1, na - pre, nb - pre))
    while suf < min(na - pre, nb - pre) and a[na - 1 - suf] == b[nb - 1 - suf]:
        suf += 1
    lo, hi = pre, nb - suf
    # Expand to enclosing top-level block(s): up to the nearest column-0 line
    # at/above the first changed line, down to (exclusive) the first column-0
    # line at/after the changed span. Lines outside the changed span are
    # common to both texts (prefix/suffix aligned), so the same region slices
    # out of `old` at a suffix-shifted end index.
    start = min(lo, nb - 1)
    while start > 0 and (not b[start] or b[start][0] in " \t"):
        start -= 1
    end = hi
    while end < nb and (not b[end] or b[end][0] in " \t"):
        end += 1
    end_old = end + (na - nb)
    span = (start, max(start, end_old), nb - na)
    region_new = "\n".join(b[start:end])
    region_old = "\n".join(a[start:max(start, end_old)])
    if len(region_new) > max_chars or len(region_old) > max_chars:
        return "skip", None, span
    err_new = _compile_check(region_new)
    if err_new is None:
        return "clean", None, span
    if getattr(err_new, "lineno", None):
        err_new.lineno = start + err_new.lineno   # region → buffer line
    return (("error" if _compile_check(region_old) is None else "ambiguous"),
            err_new, span)


def _safe_newline_delta(last_good, cur):
    """True when `cur` differs from `last_good` ONLY by added/removed blank
    (whitespace-only) lines, or is byte-identical — the safe mutation class:
    it cannot change the parse structure or introduce a syntax error (blank
    lines are ignored by the grammar; inside a string literal a newline is
    still valid syntax), so chain_in can skip its whole reparse for it.

    Deliberately O(buffer): two C-speed splits + one list compare, microseconds
    against the 150-550ms GIL-held parse it avoids. This is edit CLASSIFICATION
    on a background worker replacing strictly larger work — not the render-path
    content-hash cache invalidation CLAUDE.md forbids."""
    if not last_good or not cur:
        return False
    if last_good == cur:
        return True   # byte-identical echo - nothing to reparse
    a = [l for l in last_good.split("\n") if l.strip()]
    b = [l for l in cur.split("\n") if l.strip()]
    return a == b


def _run_chain_in(input_value, chain=None, _src_gen=None, lint_path=None,
                  lint_span=False, _last_good_src=None, _last_good_routed=None,
                  **extra):
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
    # Imported once for the WHOLE body: a branch-local import would make the
    # name function-local everywhere, and the lint section's call then throws
    # UnboundLocalError whenever the incremental branch skipped the import.
    from meltygui.code.libcst_conversion import _yield_to_ui

    # ── libcst-dict cache over the chain parse ────────────────────────────────────
    # Only for a PRISTINE disk buffer: DiskCodec.load stamps the loaded text
    # with the disk mtime it reflects (DiskSpanText); any edit decays it to a
    # plain str, so provenance - not content comparison - gates the cache. Only
    # the canonical [string_to_cst_module, cst_module_to_dict] chain counts,
    # its output routed under the final node's route name. A run_jedi flag
    # (the explicit Jedi click) always runs the real pass.
    _disk_mtime = getattr(input_value, "_disk_mtime", None)
    _disk_span = getattr(input_value, "_disk_span", None)
    _tail = chain[-1] if chain else None
    if isinstance(_tail, tuple):
        _tail = _tail[0]
    _cacheable = (_disk_mtime is not None and _disk_span is not None
                  and not extra.get("run_jedi")
                  and getattr(_tail, "__name__", "") == "cst_module_to_dict")
    _route = extra.get("route") or {}
    _out_target = _route.get(_tail)
    _out_name = _out_target[0] if isinstance(_out_target, tuple) else _out_target
    if _cacheable and _out_name:
        _gp = chain_parse_cache_get(_disk_span, _disk_mtime)
        if _gp is not None:
            notify(f"cst cache hit: {Path(_disk_span[0]).name}"
                   f" [{_disk_span[1]}:{_disk_span[2]}]",
                   tag="cst_cache", tint=(0.4, 0.9, 0.4))
            # The cache only ever holds CLEAN parses, so error stays None. The
            # lint pass + the import-suggestion scan read the LIVE process,
            # so their findings aren't cacheable - but this branch runs INLINE
            # on the render thread at app load (inline_first with guaranteed
            # cache hits), so paying a whole-file ast parse + tokenize-based
            # suggestion there is just the very stall to avoid. Defer instead:
            # lint_deferred rides the payload, the fold stamps ModesState, and
            # the editor schedules a _run_relint (worker-side, input-quiet
            # parked) once the app is up.
            return {"routed": {_out_name: _gp}, "error": None, "lint": [],
                    "imports": {}, "lint_deferred": lint_path is not None,
                    "_src_gen": _src_gen, "src_good": input_value}

    # ── Safe-mutation skip ─────────────────────────────────────────────────
    # A newline-only edit vs the last successfully parsed source can't change
    # the parse structure or introduce a syntax error - rerunning the GIL-held
    # libcst parse + dict conversion + lint for it only convoys the render
    # thread. Skip the full chain: the fold sites keep the held result and the
    # src_good baseline (the good source), so the first content edit afterwards
    # diffs non-safe against it and runs the one full parse it always would
    # have. Position consumers tolerate a shifted window (the usage graph
    # pure-shift-patches per keystroke; error/lint markers sit stale-hidden
    # mid-edit; the definition's incremental fast path owns the responsive marker). A
    # run_jedi pulse (explicit Index click) always runs the real pass.
    # EXPERIMENT (Toggles.TextEditor.freeze_cst_dict): once a baseline parse
    # exists, answer EVERY reconvert with a safe-skip - the parse/conversion
    # never re-runs on edits, so the view runs on the frozen first good tree.
    # The Index pulse still forces a real pass (explicit user action, and the
    # one way to manually refresh the frozen tree while editing).
    if (Toggles.TextEditor.freeze_cst_dict
            and not extra.get("run_jedi")
            and _last_good_src is not None):
        notify("chain_in: cst dict FROZEN — reconvert skipped", tag="chain_in")
        return {"routed": {}, "error": None, "lint": [], "imports": {},
                "safe_skip": True, "lint_deferred": False,
                "_src_gen": _src_gen, "src_good": None}

    # Narrowed to the byte-identical echo ONLY: blank-line edits used to take
    # this skip too, but the skip keeps the held parse's spans UNSHIFTED - the
    # editor's washes tolerate that (they remap editor-side), while the
    # live-view markers anchor directly off gp _child_spans and stayed pinned
    # to stale lines through any amount of Enter-typing. Blank-line edits now
    # fall through to the incremental merge below, which shifts every span
    # correctly for ~50-100ms per debounced burst.
    if (Toggles.TextEditor.skip_reparse_on_blank_edits
            and not extra.get("run_jedi")
            and isinstance(input_value, str)
            and _last_good_src is not None and _last_good_src == input_value):
        notify("chain_in: identical echo — reparse skipped", tag="chain_in")
        return {"routed": {}, "error": None, "lint": [], "imports": {},
                "safe_skip": True, "lint_deferred": False,
                "_src_gen": _src_gen, "src_good": None}

    # ── Incremental cst→dict (Toggles.TextEditor.incremental_cst_parse) ─────
    # In the canonical chain with a previous good parse available, try the
    # O(edited statements) merge (cst_dict_incremental_update): re-convert
    # only the changed top-level statements and splice into the old parse -
    # skipping the 150-550ms whole-buffer libcst parse + dict conversion.
    # None (any doubt: header/footer present, over-sized region, verification
    # failure) falls through to the full conversion below.
    _inc_gp = None
    if (Toggles.TextEditor.incremental_cst_parse
            and not extra.get("run_jedi")
            and isinstance(input_value, str) and _last_good_src
            and _last_good_routed is not None and _out_name
            and getattr(_tail, "__name__", "") == "cst_module_to_dict"):
        _prev_gp = _last_good_routed.get(_out_name)
        if _prev_gp is not None:
            from meltygui.core.diagnostics.notifications import lag_span
            _prev_melty = _prev_gp.get("__origin__") is not None
            if _prev_melty and Toggles.TextEditor.melty_syntax:
                # Increment incremental parse: re-parse only the top-level statements
                # the edit touched and splice them into a NEW root that reuses
                # every unchanged value object of the held tree (draw calls
                # survive) - the entire tree is bubbling-wrapped, a mutation
                # would trigger as a user edit; it falls back to a full parse
                # (reparse_reusing) for header/tail edits. A broken keystroke
                # falls through to the full path, which is what reports the error.
                from meltygui.code.core_syntax import reparse_incremental
                try:
                    with lag_span("melty_syntax reparse", 30):
                        _inc_gp = reparse_incremental(_prev_gp, input_value)
                except SyntaxError:
                    _inc_gp = None
            elif not _prev_melty and not Toggles.TextEditor.melty_syntax:
                from meltygui.code.libcst_conversion import cst_dict_incremental_update
                with lag_span("incremental cst merge", 30):
                    _inc_gp = cst_dict_incremental_update(
                        _prev_gp, _last_good_src, input_value)
            # else: the parser toggle flipped since the held parse - full reconvert.

    if _inc_gp is not None:
        notify("chain_in: incremental cst merge", tag="chain_in")
        result, routed = _inc_gp, {_out_name: _inc_gp}
        parse_failed = False
    else:
        # Park BEFORE the libcst parse: string_to_cst_module is 150–550ms of
        # module-internal GIL-held CPU with no yield points inside - a run
        # launching just as the user resumes typing plowed through it and convoyed
        # the render thread (the residual 145–750ms frames after frame-busy
        # parking landed everywhere else). Waiting for input HERE turns that into
        # a parse that runs while the user is idle. No-op on the inline-first
        # (render-thread) path - _yield_to_ui never sleeps the render thread worker.
        _yield_to_ui()

        result, routed = _run_convert(chain, input_value, **extra)
        parse_failed = isinstance(result, Exception)
    error = result if parse_failed else None
    # cst parsed clean - run the compiler check, to surface the syntax errors libcst
    # is too lenient to flag (duplicate args/kwargs, ...). Same red-highlight path.
    if error is None and isinstance(input_value, str):
        error = _compile_check(input_value)
    # Mid-edit resiliency: a broken keystroke must not blank the structured
    # views / usage sites / definition tiling downstream. When the input differs
    # from the last successfully parsed source (`_last_good_src`, threaded from
    # ModesState) by just ONE line - the line being typed - re-run the conversion
    # with that line blanked. Line count is preserved, so every other line's
    # snippet, usage sites and washes stay position-accurate. The ORIGINAL error
    # still reports (the red-line highlight is truthful) and last_good stays
    # None (the real input never parsed, so it must not become the baseline).
    # Only for a failed PARSE - a compile-check-only error still has an exact
    # parse of the real text, which matches the blanked variant.
    if parse_failed and isinstance(input_value, str):
        patched = _blank_line_variant(_last_good_src, input_value)
        if patched is not None:
            repaired, routed_repaired = _run_convert(chain, patched, **extra)
            if not isinstance(repaired, Exception) and _compile_check(patched) is None:
                routed = routed_repaired
    # Compiled clean - run the static "will this RUN" pass too: undefined names +
    # call-signature mismatches (code_checks.check_source). Only when the host
    # declared a lint_path (a WHOLE-FILE buffer - a span buffer would flag every
    # module-level import it can't see). Same background thread, [(line, msg)].
    # Lint and import suggestions, parked behind input-quiet first (same
    # frame-busy protection as the parse above): both passes do whole-buffer
    # / whole-file work (lint-mode check_source and the suggestion scan each
    # consult _module_text_binds - a possible whole-file ast parse - and the
    # scan tokenizes the buffer), and running them mid-typing-burst
    # GIL-convoys the render thread. No-op on the inline render-thread path.
    # During APP LOAD (boot window) they're skipped outright - every host's
    # chain parse lands in one GIL-hungry burst there - and delayed through
    # the same lint_deferred → relint on the cache-hit branch above.
    lint = []
    imports = {}
    _lintable = lint_path is not None and isinstance(input_value, str)
    _defer_lint = (_lintable
                   and time.monotonic() - _BOOT_T < _LINT_BOOT_QUIET_S)
    if _lintable and not _defer_lint:
        _yield_to_ui()
        _lint_fn = (check_source_incremental
                    if Toggles.TextEditor.incremental_lint else check_source)
        # The lint stays on the input-quiet worker, including unedited files.
        if (error is None and Toggles.TextEditor.check_syntax_errors
                and Toggles.TextEditor.enable_lint
                and (Toggles.TextEditor.incremental_lint
                     or len(input_value) <= Toggles.TextEditor.lint_max_chars)):
            try:
                lint = _lint_fn(input_value, path=lint_path,
                                only_missing_imports=lint_span)
            except Exception:
                lint = []
        # Import suggestions - a SEPARATE channel from errors, computed
        # regardless of parse state (tokenize-based, so a half-typed `json.`
        # line still yields its fix - the IDE type-`json.`-press-Enter flow).
        try:
            imports = (collect_import_suggestions(input_value, path=lint_path)
                       if Toggles.TextEditor.enable_import_scan else {})
        except Exception:
            imports = {}
    # Store the finished parse for the next boot: pristine disk input (see the
    # provenance gate above) + a clean parse/compile only, so a cache hit can
    # skip the expensive pass. One dumps (~50ms for a large span) on this
    # background thread buys every editor startup a ~25ms load instead of the
    # full seconds.
    if _cacheable and _out_name and error is None:
        chain_parse_cache_put(_disk_span, _disk_mtime, routed.get(_out_name))

    return {"routed": routed, "error": error, "lint": lint, "imports": imports,
            "lint_deferred": _defer_lint, "_src_gen": _src_gen,
            "src_good": input_value if error is None else None}


def _run_relint(input_value=None, lint_path=None, lint_span=False):
    """Background lint-only pass over a span buffer — no reparse, no chain.

    What the lint and the import suggestions report depends on the FILE's
    pending text (code_checks._module_text_binds), so an import added/removed
    in another view — or a reverted pending entry — changes the right answer
    without any edit to this span. PendingSave.queue_save kicks the file's
    hosts (_kick_relint) and draw_text_from_code_cache runs this to refresh
    ModesState.last_lint / last_imports alone."""
    try:
        if not isinstance(input_value, str) or lint_path is None:
            return {"lint": [], "imports": {}}
        # Park until input goes quiet - a kicked relint must never be GIL
        # convoy an actively-typing render thread (no-op when idle).
        from meltygui.code.libcst_conversion import _yield_to_ui
        _yield_to_ui()
        # Over the lint cap the O(buffer) passes downgrade: no check_source,
        # and the import rescan runs the incremental step instead of full -
        # see Toggles.TextEditor.lint_max_chars for the trade-off.
        _small = len(input_value) <= Toggles.TextEditor.lint_max_chars
        _inc = Toggles.TextEditor.incremental_lint
        lint = []
        if (Toggles.TextEditor.check_syntax_errors
                and Toggles.TextEditor.enable_lint and (_small or _inc)):
            _lint_fn = check_source_incremental if _inc else check_source
            try:
                lint = _lint_fn(input_value, path=lint_path,
                                only_missing_imports=lint_span)
            except Exception:
                lint = []
        # Suggestions alone - tokenize-based, runs through a mid-edit
        # syntax error (check_source returns [] on those; that's fine, the
        # error marker itself comes from the parse pass, not from here).
        # full=True: a relint fires because the FILE's pending state changed
        # (import added/removed/reverted), which invalidates the incremental
        # scan's cached verdicts - rescan from scratch.
        imports = (collect_import_suggestions(input_value, path=lint_path,
                                               full=_small)
                   if Toggles.TextEditor.enable_import_scan else {})
        return {"lint": lint, "imports": imports}
    except Exception:
        return {"lint": [], "imports": {}}


def _kick_relint(path):
    """Flag every code host linting `path` to re-run its lint pass. Called
    from PendingSave.queue_save (any pending edit to the file may change what
    the lint should report) — which fires per queued keystroke save and
    redundantly during chain_out echo bursts, so this must stay CHEAP:
    setting the flag is free and idempotent; the consumer wake (which
    invalidates the cached editor bodies so the flag is actually seen) is
    rate-limited per host. An editor being typed in re-runs anyway and
    consumes the flag without the wake; the wake only matters for the
    idle-editor case (an import reverted in the pending window), where one
    wake per second is plenty."""
    target = str(path)
    woke = False
    now = time.monotonic()
    for _sh, dh in list(_code_host_cache.values()):
        lp = (dh.child_kwargs.get("run_chain_kwargs") or {}).get("lint_path")
        if lp is None:
            continue
        rp = getattr(dh, "_lint_rp", None)
        if rp is None:
            try:
                rp = str(Path(lp).resolve())
            except OSError:
                rp = lp
            dh._lint_rp = rp
        if rp != target:
            continue
        dh._relint_pending = True
        if now - getattr(dh, "_last_relint_notify", 0.0) >= 1.0:
            dh._last_relint_notify = now
            woke = True
            try:
                dh._notify_consumers(name="relint kick")
            except Exception:
                pass
    if woke:
        request_render()


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

    with _pspan("chain_out: worker", min_ms=5.0):
        result, _ = _run_convert(chain, input_value, **extra)
    if isinstance(result, Exception):
        return {"error": result, "_out_gen": _out_gen}
    return {"value": result, "_out_gen": _out_gen}


# Buffers up to this size take the inline-first parse path in
# convert_in_and_out_value (first result only - see the call site). 128KB
# covers every practical editor span (text_editor.py's draw_text is ~102KB,
# 215ms of parse+compile); beyond it the first parse runs async so a
# pathological buffer can't freeze its first frame for seconds.
_INLINE_FIRST_PARSE_MAX_CHARS = 128 * 1024
# background_load_*' inline opt-out: small enough that a window-drag
# hidden-host load stays an invisible few ms (a 16KB span parses ~10ms).
_BG_INLINE_FIRST_PARSE_MAX_CHARS = 16 * 1024

# Typing debounce for chain_in re-parses: every keystroke fires a START edge,
# and with no debounce a big buffer queues a full str→dict→convert per key -
# the workers stack up, round-robin the GIL with the render thread, and a
# normally-15ms render-thread compute measures seconds of wall time (first 3s
# frame of 2026-07-31). 300ms sits above the inter-key gap of fast typing, so
# each burst coalesces to ONE reparse when the input goes quiet; first parses
# are exempt (run_in_background zeroes debounce until a first result returns).
# Tunable live via Toggles.TextEditor.parse_debounce_ms (this is the fallback
# default) - the same knob gates the symbol-usage recompute.
_CHAIN_IN_DEBOUNCE_MS = 300


def _chain_in_debounce_ms(input_value=None):
    """Live-read the typing debounce for this buffer; falls back to the module
    default. Buffers at or under Toggles.TextEditor.small_file_max_chars take
    the shorter small_file_debounce_ms — a few-ms parse doesn't need the long
    coalescing window the big-buffer default exists for."""
    cap = Toggles.TextEditor.small_file_max_chars
    if cap and isinstance(input_value, str) and len(input_value) <= cap:
        return Toggles.TextEditor.small_file_debounce_ms
    return Toggles.TextEditor.parse_debounce_ms


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
        # {1-based line: [import statements]} from the separate suggestions
        # channel (get_import_suggestions) - the editor's Alt+Enter data.
        # Swapped per finished run, never mutated in place (identity keys the
        # editor's applied-fix reset). Read with getattr (pre-hotswap
        # instances persist on draw_states).
        self.last_imports = {}
        # True when the last chain_in SKIPPED the lint/suggestions pass (the
        # inline cst-cache-hit path at app load) - the editor turns this into
        # a deferred _run_relint once the boot delay passes.
        self._lint_deferred = False
        # Round-trip generation tracking (kills the value-flicker). Every conversion
        # carries the Melty.frame_count of the LOCAL EDIT that originated it, so a
        # chain-in result can be ordered against the host's latest edit and a stale parse
        # rejected. `echo_str` is the exact string object our LOCAL chain_out produced;
        # when it comes back as the_in's input (by identity) we know the parse reflects
        # `echo_gen` (that edit's frame) - anything else is an external change (as of now).
        self.echo_str = None
        self.echo_gen = 0
        # The last source string that parsed clean - the diff against for the
        # blank-line repair in _run_chain_in. Read with getattr (instances
        # created before a hotfix added this field persist on draw_modes).
        self.last_good_src = None


def compute_height(draw_state):
    return None
    # return min(draw_state., 400)


from meltygui.view.code_view import draw_with_view_funcs


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
                           "chain": chain_in, "route": route,
                           "_last_good_routed": modes_state.last_good,
                           "_last_good_src": getattr(modes_state, "last_good_src", None)}
        # `changed` is the only trigger - code_file_io rolls load / external edit /
        # the Index pulse into it, so we never diff the text or sniff inputs here.
        # inline_first only means a guaranteed cst cache hit (a ~26ms loads); the
        # first paint lands fully formed instead of paying the async frame-hop
        # tax. A genuine change (miss) stays on the worker as before.
        inline = (not chain_in_kwargs.get("run_jedi")
                  and chain_parse_cache_has(getattr(input_value, "_disk_span", None),
                                            getattr(input_value, "_disk_mtime", None)))
        finished, payload = run_in_background(
            _run_chain_in,
            child_kwargs=chain_in_kwargs,
            name=f"chain_in{unique}", start=external_change, inline_first=inline,
            debounce_ms=_chain_in_debounce_ms(input_value))
        if finished and isinstance(payload, dict) and payload.get("safe_skip"):
            # Newline-only edit: the worker skipped the chain because the buffer is
            # the last good source plus/minus blank lines, so it's clean. Keep
            # the held parse / lint / imports / baseline and just clear any
            # lingering error (the broken line was reverted, not reparsed).
            modes_state.last_error = None
            chain_in_error = None
        elif finished and isinstance(payload, dict):
            # Fold the completed outputs into the shared snapshot AND this
            # frame's routed (so the columns see the good values immediately).
            modes_state.last_error = payload.get("error")
            modes_state.last_lint = payload.get("lint") or []
            modes_state.last_imports = payload.get("imports") or {}
            modes_state._lint_deferred = bool(payload.get("lint_deferred"))
            if payload.get("src_good") is not None:
                modes_state.last_good_src = payload["src_good"]
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
        indent = _common_indent(input_value) if co_start else ""
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
                             external_change=False, child_kwargs=None, unique=0,
                             background_load=False, **kwargs):
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
        # "No value yet" is not a parse job: a host's first external_change edge
        # fires before its source input has loaded (input None/UNSET), the chain
        # would just AttributeError on a worker, and the loaded text raises a
        # real edge moments later anyway (real value load re-flags
        # external_change). Swallow the empty edge instead of queueing it.
        if external_change and (input_value is None or input_value is UNSET):
            external_change = False
        # Tag this parse with the generation of the source it consumes. If the source is
        # the echo of our OWN last chain_out (same string object), it reflects that edit's
        # frame (echo_gen); otherwise it's an external change as of now. Threaded through
        # the worker snapshot so the result is tagged with the gen actually parsed.
        src_gen = modes_state.echo_gen if (input_value is modes_state.echo_str) else Melty.frame_count
        # Identical-snapshot re-arm suppression: a no-op host write can re-fire
        # the same edge with the SAME string object that's already armed/run -
        # observed as back-to-back runs on one pending snapshot, the second
        # re-parsing a byte-identical buffer (~550ms pure re-burn during the
        # syntax-error hold). Identity only - any real edit is a new source -
        # and a run_jedi trigger (an Index click) always passes.
        if (external_change and not forwarded.get("run_jedi")
                and input_value is getattr(modes_state, "_last_armed_src", None)):
            external_change = False
        elif external_change:
            modes_state._last_armed_src = input_value
        chain_in_kwargs = {**forwarded, "input_value": input_value,
                           "chain": chain_in, "route": route, "_src_gen": src_gen,
                           "_last_good_routed": modes_state.last_good,
                           "_last_good_src": getattr(modes_state, "last_good_src", None)}
        # The FIRST parse of a fresh view runs INLINE, size-gated: parsing is
        # pure-Python, so a worker thread doesn't wall it under the GIL - it
        # just smears the same CPU across stretched frames, pop-in, and a second
        # render once the result drops a frame later. Synchronous-first paints
        # the view fully formed the one sooner. run_in_background inlines only
        # if its state has no result yet, so every later reparse - typing,
        # echoes, external changes - stays async/debounced exactly as before.
        # The size gate prevents a large buffer from freezing its first parse
        # indefinitely (it falls back to the async path).
        # background_load=True hidden cache hosts (input-tab feeders) opt OUT of
        # the synchronous first parse - nobody's looking at them the frame
        # they load, and a big span's inline parse puts a visible 100ms+ hitch
        # on whatever the user IS doing (dragging a window). SMALL functions
        # are carved back in: a function span parse is single-digit ms, and
        # the context-menu tabs DO look at these hosts the moment they load -
        # the async hop (worker + debounce + next-frame) was the seconds-long
        # "sources not loading" feel on every cold menu open.
        _inline_cap = (_BG_INLINE_FIRST_PARSE_MAX_CHARS if background_load
                       else _INLINE_FIRST_PARSE_MAX_CHARS)
        inline = (isinstance(input_value, str)
                  and len(input_value) <= _inline_cap)
        # A direct cst-cache hit is a ~26ms pickle.loads, not a parse - the
        # async path's frame-hop tax (~90ms+ per span at boot) costs more than
        # the work. Inline it even for background_load hosts and big spans.
        # run_jedi excluded: the Index pulse bypasses the cache and must run
        # a real (expensive) pass on the worker.
        if not inline and not chain_in_kwargs.get("run_jedi"):
            inline = chain_parse_cache_has(getattr(input_value, "_disk_span", None),
                                           getattr(input_value, "_disk_mtime", None))
        finished, payload = run_in_background(
            _run_chain_in,
            child_kwargs=chain_in_kwargs,
            name=f"chain_in{unique}", start=external_change, inline_first=inline,
            debounce_ms=_chain_in_debounce_ms(input_value))
        if external_change:
            _ptrace(f"chain_in START edge (unique={unique})", src_gen=src_gen,
                    echo=(input_value is modes_state.echo_str))
            note = Note(name="convert_in_out, chain in start", tint=(1, 0.5, 0))
            draw_state._parent.invalidate(note=note)
        external_change = False
        if finished and isinstance(payload, dict) and payload.get("safe_skip"):
            # Newline-only edit: chain skipped host-side (see the safe-
            # mutation skip in _run_chain_in). Keep the held error / lint /
            # imports / baseline and clear any lingering error; inbound_gen
            # stays None - there is no fresh parsed value to order/accept.
            modes_state.last_error = None
            chain_in_error = None
        elif finished and isinstance(payload, dict):
            # The generation this finished parse reflects (origin edit frame, or "now" for
            # an external change) - pass to the view_func so its accept/reject ordering
            # compares against the user's latest LOCAL edit and drops a stale parse.
            inbound_gen = payload.get("_src_gen")
            modes_state.last_error = payload.get("error")
            modes_state.last_lint = payload.get("lint") or []
            modes_state.last_imports = payload.get("imports") or {}
            modes_state._lint_deferred = bool(payload.get("lint_deferred"))
            if payload.get("src_good") is not None:
                modes_state.last_good_src = payload["src_good"]
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
        draw_state.invalidate(
            note=Note(name="convert_in_out, view func edit", tint=(1.0, 0.5, 0), draw_state=draw_state))

    # ── (identical) chain_out in background ───────────────────────────────────────
    if chain_out:
        co_start = converted_edit is not UNSET
        indent = _common_indent(input_value) if co_start else ""
        # The edit that produced converted_edit is this frame's (the view_func reported it
        # now, same frame bubbling stamped the held value), so its generation is the
        # current frame. Threaded through the worker snapshot so the output string is
        # tagged with the edit frame - its chain_in echo is then recognized + ordered.
        out_gen = Melty.frame_count
        if co_start:
            _ptrace(f"chain_out TRIGGERED by view_func edit (unique={unique})",
                    gen=out_gen)
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
        label = getattr(code_state, "_external_load_label", None) or "loaded from disk"
        imgui.text_colored(f"{sync_icon_fa} {label} {code_state._external_load_time}",
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

def _codec_view(codec, value, caller_view):
    """Which view renders a codec's loaded `value` inside code_file_io.

    The codec decides the data TYPE; the type decides the view — so a new
    file type is one codec whose load() returns something with a default
    renderer, and nothing else has to learn about it. In order:

      1. A RenderHost capture (render_host_view hands its
         `_internal_view_func` as view_func): ALWAYS kept. It materializes
         the value into host["value"] for the host's consumers (the code
         editor reads the dict) and draws with the host's own renderer.
         Overriding it with the codec's view skipped materialization — an
         image host never filled and the editor sat on "Loading…" forever.
      2. `codec.view_func` — the explicit override (a type without a default
         renderer, or a pinned non-default one).
      3. A str renders in whatever text view the caller wired (the mode-
         pinned draw_text_from_code_cache, RenderFuncs.draw_text, …).
      4. Anything else routes by type through draw_any (is_default_for)."""
    from meltygui.core.conversion.render_host import RenderHost
    if isinstance(getattr(caller_view, "__self__", None), RenderHost):
        return caller_view
    if getattr(codec, "view_func", None) is not None:
        return codec.view_func
    if isinstance(value, str):
        return caller_view
    from meltygui.core.rendering.render_dispatch import draw_any
    return draw_any


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  editable_source - the whole round-trip, one function                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

#tstst
@render_func(use_cache=True, selectable=False, with_header=draw_header, searchable=False, disable_scroll=True)
def code_file_io(input_value, code_state: CodeState, codec=None, view_func=RenderFuncs.draw_text, auto_load=True,
                 auto_load_edits=False, min_height=20, shadow=False, show_add_delete=False, show_bg=False,
                 show_code_buttons=False, show_name=True, is_tree=False,
                 child_kwargs=None, draw_state=None, auto_save=True, auto_recompile_edits=False, save=False, load=False,
                 recompile=False, run_jedi=False, save_debounce_ms=0, bg_offset=-0.5,
                 ensure_import=None, s_key_pressed=None, unique=None,
                 background_load=False, **kwargs):
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

        # (Which view renders the loaded value is decided at the call site in
        # _codec_view - once the value's type is known.)

        # resolve_address runs every frame; min_ms keeps the code-state cache
        # hits silent while a cold resolve (whole-file getsourcelines tokenize)
        # shows up on the console.
        with _pspan("cfio: resolve_address", min_ms=2.0,
                    codec=getattr(codec, '__name__', type(codec).__name__)):
            address = codec.resolve_address(input_value, draw_state, code_state=code_state)
        code_state.address = address
        top_line_height = 30
        external_change = False
        imgui.same_line(spacing=0)

        if address is None:
            # A refused file used to be a blank view; name the reason.
            from meltygui.code.fileref import writable_file_refusal
            why = None
            if isinstance(input_value, (Path, str)):
                why = writable_file_refusal(input_value) or (
                    None if Path(str(input_value)).is_file() else "not a file")
            imgui.text_colored(f"Not editable: {input_value}" + (f" — {why}" if why else ""),
                               0.9, 0.6, 0.5, 0.9)
            return False, None

        # Run (hotkey) and Index (jedi) only make sense on Python code \u2014 the
        # codec decides (TypeCodec family: yes; TextFileCodec: .py paths only;
        # images/binaries: no). `code_buttons` also drives error forwarding and
        # syntax highlighting below, so the codec verdict stays separate from
        # `show_code_buttons`, which only gates the button UI (hidden by
        # default; the per-view for auto-state).
        code_buttons = codec.show_code_buttons(address)

        if auto_load:
            if draw_state.frame_count < 1:
                load = True
                code_state.text_cache = None
                code_state.mark_file_current()
                _ptrace("cfio: initial load trigger",
                        file=address.path.name if address.path else "?",
                        span=(address.start, address.end))

        # str gate on top: even a code codec can briefly hold non-text data.
        if (code_buttons and show_code_buttons and not auto_recompile_edits
                and code_state.text_cache is not UNSET
                and isinstance(code_state.text_cache, str)):
            recompile = recompile_button(code_state, unique=unique, height=top_line_height)

        if Toggles.enable_jedi and code_buttons and show_code_buttons:
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

        # No automerge: pending is the current state, and an external write is
        # the INCOMING side - it is merged in manually (merge window / editor
        # banner), never silently by this code. A drift file with a live edit
        # parks on the conflict indicator below until the user resolves it.
        #
        # ── Resolved merge: reload from the pending overlay ──────────────
        # After the user resolved this file's drift (Merge / Keep pending /
        # Disc save - resolve_external arms the is_absorbed marker), the
        # pending overlay holds the file's truth: reloading is safe, and
        # codec.load answers span loads from the pending overlay, so the
        # MERGED text lands in this buffer - never a disk save; disk is only
        # written by an explicit save or the shutdown flush. is_absorbed is a
        # pure held-object identity check (no content compare); a NEW external
        # write replaces the disk object and naturally re-parks the view.
        if file_stale and not self_write and not code_state._save_refused:
            from meltygui.editor.external_changes import ExternalChanges
            _dpath = str(address.path)
            _disk_now = Melty.read_code(_dpath)
            if _disk_now is not None and ExternalChanges.is_absorbed(_dpath, _disk_now):
                load = True
                code_state._loaded_externally = True
                code_state._pending_save = False
                code_state.mark_file_current()
                file_stale = False
                conflict = False

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
                # Ancestors-only: external_change flows into the view_func's
                # draw= bypass this same body run, so the subtree that renders
                # from the hosts updates through its own data flow. The old
                # depth-6 force-sweep here rebuilt the structured pane for
                # EVERY keystroke (each body runs per key - the editor's
                # str_host queues into PendingSave every edit).
                draw_state.invalidate(note=Note(name="pending-sync", tint=(1, 0.6, 0.2)))
                request_render()

        # A read-only codec (images, binaries) has nothing local to lose: an
        # external write just reloads. no self-write check, no "changed on
        # disk" stamp, no conflict (there can be no pending edit).
        if file_stale and not codec.editable:
            load = True
            code_state._loaded_externally = not self_write
            code_state._pending_save = False
            code_state.mark_file_current()
            file_stale = False
            conflict = False

        if file_stale and not code_state._pending_save:
            if auto_load_edits and self_write:
                # A VERIFIED self-write (a sibling editor of the same file - the
                # code-host str_host, another window, a lens save - synced
                # through disk) is picked up IN-PROCESS from the exact text that
                # write produced: no disk reload, no "Loading..." banner, no
                # external stamp. A reparse still fires (text_cache change +
                # external_change below), so the structured pane updates exactly
                # as the disk-load path drove it. get_self_write_text returns
                # None if an external write has disk raced in - fall through to
                # the manual branch below rather than auto-loading stale text.
                mem_text = FileWatch.get_self_write_text(address.path)
                if mem_text is not None:
                    code_state.text_cache = codec.load(address, source_text=mem_text)
                    code_state.mark_file_current()
                    code_state._save_refused = False
                    external_change = True
                    # Ancestors-only, same reasoning as the pending-sync above:
                    # this self-write sync also lands on per keystroke save,
                    # and the depth-6 force sweep rebuilt the structured pane
                    # each time. external_change -> draw= re-runs the view.
                    draw_state.invalidate(note=Note(name="self-write mem-sync", tint=(1, 0.6, 0.2)))
                    request_render()
                else:
                    load = True
                    code_state._loaded_externally = False
                    code_state.mark_file_current()
            else:
                # A genuine external change is NEVER auto-loaded - the write is
                # the incoming side of a manual merge (merge window / editor
                # banner). Indicate and offer the merge window, and keep the
                # explicit per-span buttons as escape hatches.
                imgui.same_line(spacing=8)
                imgui.align_text_to_frame_padding()
                imgui.text_colored("\uf071 changed on disk", 1.0, 0.55, 0.15, 1.0)
                imgui.same_line(spacing=4)
                if get_service("conflicts_open") and RenderFuncs.button("Merge…", width=100, height=top_line_height,
                                      name=f"openmerge{unique}")[0]:
                    from meltygui.core.runtime.extensions import call
                    call('conflicts_open', address.path)
                imgui.same_line()
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
            # file-mangling bug. The merge window is the primary resolution;
            # Keep mine writes with force (skipping the codec's span-fingerprint
            # guard) through the freshly re-resolved span.
            imgui.same_line(spacing=8)
            imgui.align_text_to_frame_padding()
            imgui.text_colored("\uf071 changed on disk", 1.0, 0.55, 0.15, 1.0)
            imgui.same_line(spacing=4)
            if get_service("conflicts_open") and RenderFuncs.button("Merge…", width=100, height=top_line_height,
                                  name=f"openmerge{unique}")[0]:
                from meltygui.core.runtime.extensions import call
                call('conflicts_open', address.path)
            imgui.same_line()
            if RenderFuncs.button("Load theirs", width=110, height=top_line_height, name=f"reload{unique}")[0]:
                load = True
                code_state._loaded_externally = True
                code_state._pending_save = False

                # Without this the pending save keeps answering the load
                # with the edit being discarded (codec.load prefers it).
                PendingSave.discard_entry_for(address)
            imgui.same_line()
            if RenderFuncs.button("Keep mine", width=100, height=top_line_height, name=f"keepmine{unique}")[0]:
                save = True
                keep_mine = True
                # The queued entry sits in pre-external-write coordinates; the
                # forced save below re-queues the buffer under the freshly
                # resolved address, so drop the stale-span twin.
                PendingSave.discard_entry_for(address)

        if not auto_save and code_state._pending_save:
            imgui.same_line(spacing=0)
            if RenderFuncs.button("Save", width=100, height=top_line_height, name=f"save{unique}")[0]:
                save = True

        # if auto_save:
        #     imgui.same_line(spacing=16)
        #     imgui.align_text_to_frame_padding()
        #     imgui.text_colored(str(f" saving"), (1.0, 1.0, 1.0, 0.2))

        # Hidden cache hosts (background_load) never load inline - the disk
        # read + span resolve can cost ~100ms and nobody sees their first frame.
        changed, new_text = run_in_background(load_file, main_thread=True,
                                              child_kwargs={"input_value": address, 'codec': codec},
                                              name=f"load{unique}", start=load,
                                              inline_first=not background_load)
        if new_text is LOADING:
            code_state.mark_file_current()

        elif changed:
            _ptrace("cfio: load landed",
                    file=address.path.name if address.path else "?",
                    chars=len(new_text) if isinstance(new_text, str) else -1)
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
                code_state._external_load_label = None
            request_render()

        # ── 3. Edit - the actual call ─────────────────────────────────────────────
        if code_state.text_cache is UNSET or code_state.text_cache is None:
            # Buffer still loading: this frame renders nothing where the editor
            # will be - keep ancestors' persisted content_height (see
            # Melty.pending_placeholder_frame).
            Melty.pending_placeholder_frame = Melty.frame_count
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
            view = _codec_view(codec, code_state.text_cache, view_func)
            edited, value = view(input_value=code_state.text_cache,
                                 external_change=trigger, draw=trigger,
                                 **child_kwargs)

            # A read-only codec's view can surface a change (a gesture, a
            # host echo) but nothing flows back to the file - the loaded value
            # stays authoritative and no save is ever armed.
            if edited and not codec.editable:
                edited = False
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
        explicit_save = codec.editable and (save_hotkey or save)
        # During a conflict (external write + pending local edit) the debounced
        # auto-save is OFF - only an explicit save (Keep mine / Save / Ctrl+S)
        # writes, and it writes with force past the codec's span guard. An
        # already-in-flight debounced save is caught by that guard instead and
        # comes back as SaveConflict (handled below).
        save_start = (auto_save and edited and not conflict) or explicit_save
        force_save = keep_mine or (conflict and explicit_save)
        save_debounce = 0 if explicit_save else save_debounce_ms
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
                                          debounce_ms=save_debounce)
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

        # Recompile (hotswap, no disk write): the Run button. Ctrl+Enter is
        # the GLOBAL recompile-all now (draw_main's root host → PendingSave).
        # Same runner, its own loading_state. Deliberately NO edit-driven auto
        # trigger here: these hosts also back VISIBLE editor panes, so any
        # `edited`-keyed condition fires per keystroke (tried and reverted —
        # even origin-tagged edits misfire, since a synced-in keystroke
        # _materializes and reads as a value write). Programmatic writers
        # (set_anywhere) drive run_recompile themselves, writer-side.
        recompile_start = recompile
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

        from meltygui.core.rendering.render_dispatch import draw_any
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
    # Key by the ref ITSELF, not int(id(ref)). Functions/classes/modules hash by
    # identity - stable for the session, one entry each (both before and now).
    # But CallSite/Decorations are frozen dataclasses and Path is a new type: a
    # FRESH object is built per request (draw_input_tab does `CallSite(f, ln)` on
    # every menu-open), so an identity key MISSED every single time: a new host
    # pair created, registered in Melty.render_hosts, and never removed. Worse, the
    # transient key was then GC'd and its address recycled, so later id()s collided
    # and silently overwrote (or mis-returned) cache entries. Value-equality keying
    # collapses all those to one entry per distinct source.
    # Functions/classes key by (module, qualname), not object identity: a
    # whole-file hotswap re-exec can hand callers a NEW wrapper object for
    # the same def (draw_state._view_func picks up whichever object the
    # registries can resolve, and the src./bare twin mirroring makes
    # that alternate). Identity keying then missed on every recompile and
    # LEAKED a fresh host pair per swap (the draw_dropdown recreation leak) -
    # each pair registered in Melty.render_hosts and never collapsed. The
    # module name is normalized across the src./bare dual identity so both
    # spellings of the same file share one entry.
    key = ref
    if isinstance(ref, (types.FunctionType, type)):
        mod = getattr(ref, "__module__", "") or ""
        qualname = getattr(ref, "__qualname__", None)
        if qualname:
            if mod.startswith("src."):
                mod = mod[4:]
            key = ("code_host", mod, qualname)
    try:
        pair = _code_host_cache.get(key)
        cacheable = True
    except TypeError:  # genuinely unhashable ref - skip the cache
        pair, cacheable = None, False
    if pair is not None and key is not ref and pair[0].input_value is not ref:
        # Same logical def, different object (post-hotswap identity churn): point
        # the str_host at the caller's live ref so span editing see the
        # patched object instead of a stale pre-swap wrapper.
        pair[0].input_value = ref
    if pair is None:
        from meltygui.core.conversion.render_host import RenderHost
        label = getattr(ref, "__name__", None) or type(ref).__name__
        # Unique disambiguator: resolve_host_view resolves a host BY NAME, so two live
        # hosts must not collide. Every cached ref is held alive as a dict key, so
        # their id()s are all distinct - unique per concurrent host, and stable for
        # the entry's life (the value-key keeps THIS ref's id from being recycled).
        tag = id(ref)
        str_host = RenderHost(io_function=code_file_io, input_value=ref, evictable=True,
                              name=f"##code_cache_{label}{tag}_str",
                              child_kwargs={"auto_load_edits": True, "auto_save": True,
                                            "background_load": True})

        # str_proxy = RenderHost(io_function=code_file_io, input_value=draw_text, name="String Proxy test",
        #
        #                           child_kwargs={"auto_load_edits": False, "auto_load":True})   # auto-reload on external file change
        #

        # Whole-FILE refs get the full static name/signature lint (code_dict):
        # the buffer is self-contained, so an unresolved name really is a
        # NameError. A span ref (function/class/CallSite) sees none of its
        # module's imports, so it gets the SPAN pass instead (lint_span): with
        # the defining module's file as lint base, the module's current text
        # binds suppress every name the module actually binds, so names an
        # import would bind are reported - but only when that module is live in
        # sys.modules (otherwise nothing suppresses, so no lint at all) - and
        # call signatures check against the module file's PENDING text
        # (code_checks._check_call_span), so a signature edit in another view
        # flags wrong call sites before any recompile.
        lint_path, lint_span = None, False
        if isinstance(ref, Path) and ref.suffix == ".py":
            lint_path = str(ref)
        elif isinstance(ref, types.ModuleType):
            lint_path = getattr(ref, "__file__", None)
        elif isinstance(ref, (types.FunctionType, type)):
            # Only real def/class refs: an INSTANCE ref (CallSite, ...) reports
            # its CLASS's defining module via __module__, not the edited file -
            # linting against that namespace would be wrong, so those stay
            # lint-free as before.
            ref_mod = sys.modules.get(getattr(ref, "__module__", None) or "")
            lint_path = getattr(ref_mod, "__file__", None)
            lint_span = lint_path is not None
        dict_host = RenderHost(
            io_function=convert_in_and_out_value, input_value=str_host, evictable=True,
            name=f"##code_cache_{label}{tag}_dict",
            child_kwargs={
                "background_load": True,
                "chain_in": [string_to_cst_module, cst_module_to_dict],
                "chain_out": [dict_to_cst_module, cst_module_to_string],
                "route": {cst_module_to_dict: ("code_dict", "jump_to", "run_jedi", "drive")},
                **({"run_chain_kwargs": {"lint_path": lint_path,
                                         "lint_span": lint_span}} if lint_path else {}),
            })
        # The visible editor requests its first lint on the input-quiet worker.
        # A future timestamp prevents a scheduled lint stranded idle files.
        dict_host._last_relint_t = 0.0
        dict_host._relint_pending = bool(lint_path)
        pair = (str_host, dict_host)
        if cacheable:
            _code_host_cache[key] = pair
        _ptrace(f"host pair created for {label}", cached=cacheable,
                total_hosts=len(_code_host_cache))
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


def _host_label(host):
    """Short perf-trace label for a code host ("Toggles140234_dict")."""
    return str(getattr(host, "name", "?")).replace("##code_cache_", "")


def _post_symbol_attach(dict_host, gen, flat):
    """Attach a computed {symbol: SymbolUsage} map onto the host's held gp at the
    next frame boundary (Melty.post_to_render). Deferred-not-inline because the gp
    is walked live every frame and inserting __symbol_usages__ keys mid-iteration
    raises 'dictionary changed size during iteration' (see _index_host_in_place).
    Shared by the inline fast path and the background recompute path."""
    from meltygui.code.libcst_conversion import _distribute_by_name

    def _attach():
        gp = dict_host._held()
        if not isinstance(gp, dict):
            # The computed symbols had nowhere to land (host holds no parse yet)
            # - the work is wasted and must be re-triggered later.
            _ptrace("attach: DROPPED — host holds no parse yet",
                    host=_host_label(dict_host))
            return
        with _pspan("attach: distribute on render thread", min_ms=2.0,
                    host=_host_label(dict_host), names=len(flat) if flat else 0):
            # Same-names attach (per-keystroke offset remap: cursor moved,
            # symbol text unchanged) skips the consumer sweep - the editor reads
            # the_usage LIVE each draw (us_call in draw_text perf), so it
            # tracks positions without a repaint order, and the depth-8 force
            # draw was rebuilding the structured pane on each keystroke. A
            # changed name set (new/removed symbol) still notifies so usage
            # boxes/links appear and disappear promptly.
            prev = getattr(gp, "symbol_usage", None)
            same_names = (isinstance(prev, dict) and isinstance(flat, dict)
                          and prev.keys() == flat.keys())
            # Stamp the generation only when the compute actually landed on
            # the live buffer. A hold (typing quiet-gate / inflight dedup)
            # leaves the stale index uncached - stamping THAT as current-gen
            # blocked the ensure pass's retry forever, so a symbol typed
            # mid-hold (a newly imported name) never got indexed or tinted.
            # On a stale attach, clear the nudge key so the per-frame ensure
            # pass respawns (stagger-throttled) until a real gen fires.
            from meltygui.code.libcst_conversion import usages_fresh_for_address
            if usages_fresh_for_address(dict_host.child_kwargs.get("jump_to")):
                gp._symbol_gen = gen
            else:
                gp._symbol_gen = None
                dict_host._auto_index_key = None
            if flat:
                gp.symbol_usage = flat
                _distribute_by_name(gp, flat)
            if not same_names:
                dict_host._notify_consumers(name="symbol index attached")
            else:
                _ptrace("attach: names unchanged — consumer sweep skipped",
                        host=_host_label(dict_host))

    Melty.post_to_render(_attach)


def _ensure_symbol_index(dict_host, str_host, code_dict, jump_to=None):
    """Auto-index trigger: re-run the cache's chain_in when the held parse
    predates the current symbol index — either it never got a symbol pass
    (fresh session: the host's first parse ran before any editor stamped
    jump_to into its child_kwargs) or the background cache warmer
    (SymbolIndexCache) advanced a generation because src files changed, so
    cross-file callers may have moved. The chain itself attaches the symbols
    (cst_module_to_dict's auto pass); this only supplies `jump_to` and the
    re-run edge. One nudge per (parse identity, generation), so a span whose
    index is legitimately empty doesn't re-trigger every frame.

    The trigger keys on the file's PENDING-edit generation, not just the parse
    identity + index generation: a blank-line edit bumps pending_gen WITHOUT a new
    parse or an index-gen bump, and the symbol POSITIONS must follow it. The old
    `_symbol_gen == gen` gate (index gen only) froze the symbols at the last
    REPARSE — so a newline's offset waited for the ~0.5s cst→dict reparse. Now the
    inline offset fires per pending edit and tracks the live buffer.

    Tiers: a position-only edit (blank-line shift) is handled INLINE here and
    attaches next frame — no cooperative yield, no stagger, no background hop.
    The probe itself stays cheap per keystroke because the O(names) remap is
    debounced inside _compute_symbol_usages (_SHIFT_MAT_MIN_S): mid-burst it
    serves the held base (an identity no-op attach) and materializes the
    composite shift a few times a second. A `_NEEDS_RECOMPUTE` (within-line / substantial edit) that the
    chain's reparse already covers (parse indexed at the current index gen) is left
    to that reparse — we do NOT spawn a recompute per keystroke. Only a genuinely
    gen-stale parse (fresh parse / cross-file warmer bump) takes the deferred
    background recompute behind the yield + stagger."""
    global _last_auto_index_time
    if dict_host is None or not isinstance(code_dict, dict):
        return
    if not (Toggles.enable_jedi
            and Toggles.TextEditor.SymbolUsages.auto_index
            and not Toggles.jedi_correctness):
        return
    import meltygui.code.libcst_conversion as _lc
    gen = _lc._index_generation
    # An incremental merge carried the previous flat symbol map but could not
    # redistribute the leaf-node __symbol_usages__ (shared-dict mutation from
    # the worker - see _carry_symbols). Run the frame-boundary attach on the
    # carried map now - at a STALE generation on purpose: the carried sites
    # predate the merge's changes, so this map washes immediately (verified
    # sites survive, drifted ones drop) while still reading as "needs a symbol
    # pass" - the deferred recompute fires and lands at input-quiet.
    if getattr(code_dict, "_needs_distribute", False):
        _flat = getattr(code_dict, "symbol_usage", None)
        code_dict._needs_distribute = False
        if isinstance(_flat, dict) and _flat:
            _post_symbol_attach(dict_host, max(0, gen - 1), _flat)
            return
    if gen < 1:
        # Cold store (deleted / first-run pickle): the generation gate only
        # opens via a warmer build or a src-watch bump, and the periodic
        # warmer daemon's autostart is disabled (b4a3a9c, perf) - so if no
        # src edit ever occurs, gen stays 0 and the usage graph would stay
        # empty FOREVER. Kick exactly one warmer build; its gate bump
        # (gen 0 -> 1 even with no src changes) unblocks the trigger and
        # spans then recompute lazily per open tab. Guard on sys so the same
        # module identities / re-execs share the one-shot.
        if not getattr(sys, "_symbol_index_cold_kick", False):
            sys._symbol_index_cold_kick = True
            _ptrace("ensure-index: cold store, kicking one-off warmer build")
            _lc.SymbolIndexCache.rebuild()
        _ptrace_rl("ensure-gen0",
                   "ensure-index: waiting for first warmer build (gen=0)",
                   min_interval=5.0)
        return  # warmer hasn't built yet; we retry once it bumps
    if jump_to is None and str_host is not None:
        cs = host_code_state(str_host)
        jump_to = getattr(cs, "address", None) if cs is not None else None
    if jump_to is None:
        return  # host hasn't resolved its span yet
    if dict_host.child_kwargs.get("jump_to") is not jump_to:
        dict_host.child_kwargs["jump_to"] = jump_to
    from meltygui.editor.pending_save import PendingSave
    pgen = PendingSave.pending_gen_for(jump_to.path)
    key = (id(code_dict), gen, pgen)
    if getattr(dict_host, "_auto_index_key", None) == key:
        return  # already handled this parse + index gen + pending edit

    # FAST PATH (inline, render thread): exact cache hit or blank-line offset only
    # — ~sub-ms–2ms, GIL-cheap. Attach next frame so highlights track the edit,
    # skipping the yield/stagger/background hop. Fires per pending edit so a
    # newline offsets eagerly instead of waiting for the reparse.
    _t_probe0 = time.monotonic()
    flat = _lc.compute_symbol_usages_for_address(jump_to, fast_only=True)
    if isinstance(flat, getattr(_lc, "InterimUsages", ())):
        # A real recompute is owed, but a prior (stale/invalid) result exists -
        # attach it ONCE as a first-paint stopgap so the editor washes
        # immediately instead of showing nothing (sites verify-recover against
        # the live buffer in _collect_node_spans; mismatches drop). Then fall
        # through to the deferred recompute: _post_symbol_attach sees the sig
        # isn't fresh, stamps _symbol_gen=None, and clears the nudge key, so
        # the trigger loop keeps firing until the real compute lands.
        if getattr(dict_host, "_interim_attach_key", None) != key:
            dict_host._interim_attach_key = key
            _ptrace(f"ensure-index: interim stale attach (recompute pending, probe "
                    f"{(time.monotonic() - _t_probe0) * 1000:.1f}ms)",
                    host=_host_label(dict_host), names=len(flat.flat))
            _post_symbol_attach(dict_host, gen, flat.flat)
        flat = _lc._NEEDS_RECOMPUTE
    if flat is not _lc._NEEDS_RECOMPUTE:
        dict_host._auto_index_key = key
        _ptrace(f"ensure-index: inline attach (probe "
                f"{(time.monotonic() - _t_probe0) * 1000:.1f}ms)",
                host=_host_label(dict_host), names=len(flat))
        _post_symbol_attach(dict_host, gen, flat)
        return

    # Not linearly offsettable: If this parse is already indexed at the current
    # index gen, the symbols are correct except for THIS pending edit's positions -
    # the chain's reparse will refresh them; don't spawn a recompute per keystroke.
    if getattr(code_dict, "_symbol_gen", None) == gen:
        dict_host._auto_index_key = key  # handled (mark so we don't re-probe/frame)
        _ptrace("ensure-index: positions left to next reparse (parse already at gen)",
                host=_host_label(dict_host))
        return

    # SLOW PATH (gen-stale parse: fresh parse / warmer bump) - a real recompute,
    # deferred to a background thread behind the no-drag yield + stagger.
    if not _lc._wait_for_no_drag(max_wait=0.0):
        _ptrace_rl(("ensure-drag", id(dict_host)),
                   "ensure-index: deferred (mid-drag)", host=_host_label(dict_host))
        return  # mid-gesture - don't even start; retried next frame
    if time.monotonic() - _last_auto_index_time < _AUTO_INDEX_STAGGER_S:
        _ptrace_rl(("ensure-stagger", id(dict_host)),
                   "ensure-index: deferred (stagger window)", host=_host_label(dict_host))
        return  # another host nudged recently - stagger, retry later
    _last_auto_index_time = time.monotonic()
    dict_host._auto_index_key = key
    _ptrace("ensure-index: spawning background recompute (gen-stale parse)",
            host=_host_label(dict_host), gen=gen)
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
    from meltygui.code.libcst_conversion import compute_symbol_usages_for_address
    from meltygui.code.libcst_conversion import _wait_for_no_drag
    _t_ih0 = time.monotonic()
    if not _wait_for_no_drag(label=f"index-host {_host_label(dict_host)}"):
        # Gesture outlasted the wait - bail rather than steal GIL time from
        # it. Clearing the in-flight key lets the editor-side nudge (or
        # the next generation bump) retry once the user lets go.
        dict_host._auto_index_key = None
        _ptrace("index-host: bailed (drag outlasted wait)", host=_host_label(dict_host))
        return
    address = dict_host.child_kwargs.get("jump_to")
    if address is None:
        cs = host_code_state(str_host)
        address = getattr(cs, "address", None) if cs is not None else None
        if address is None:
            _ptrace("index-host: no address resolved yet — skipped",
                    host=_host_label(dict_host))
            return
        # Persist for the chain: future reparses attach via cst_module_to_dict.
        dict_host.child_kwargs["jump_to"] = address
    try:
        flat = compute_symbol_usages_for_address(address)
    except Exception as _e:
        _ptrace(f"index-host: compute RAISED {type(_e).__name__}: {_e}",
                host=_host_label(dict_host))
        return
    _ptrace(f"index-host: computed in {(time.monotonic() - _t_ih0) * 1000:.0f}ms, posting attach",
            host=_host_label(dict_host), names=len(flat))
    # Attach at the next frame, - the gp is walked live every frame and a
    # mid-walk insert would raise (see _post_symbol_attach). Re-fetches the
    # held gp there (a reparse may have replaced it; sites are file-absolute).
    _post_symbol_attach(dict_host, gen, flat)


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
    if not (Toggles.enable_jedi
            and Toggles.TextEditor.SymbolUsages.auto_index
            and not Toggles.jedi_correctness):
        return
    _t_wake0 = time.monotonic()
    _woken = 0
    hosts = list(_code_host_cache.values())
    _ptrace("wake-stale-hosts: sweep start (0.25s sleep between hosts)",
            gen=gen, hosts=len(hosts))
    for sh, dh in hosts:
        gp = dh._held()
        if not isinstance(gp, dict) or getattr(gp, "_symbol_gen", None) == gen:
            continue
        if getattr(dh, "_auto_index_key", None) == (id(gp), gen):
            continue
        dh._auto_index_key = (id(gp), gen)
        _woken += 1
        _index_host_in_place(sh, dh, gen)
        time.sleep(0.25)
    _ptrace(f"wake-stale-hosts: sweep done in {(time.monotonic() - _t_wake0) * 1000:.0f}ms",
            gen=gen, woken=_woken)


def _register_index_bump_hook():
    import meltygui.code.libcst_conversion as _lc
    cbs = getattr(_lc, "_index_bump_callbacks", None)
    if cbs is None:
        return  # older libcst_conversion.py loaded
    cbs[:] = [cb for cb in cbs
              if getattr(cb, "__name__", "") != "_wake_stale_code_hosts"]
    cbs.append(_wake_stale_code_hosts)


_register_index_bump_hook()


def _host_relint_and_fixes(dict_host, _str_host, wds):
    """The code host's lint-refresh + suggestions pull, shared by BOTH editor
    routes (draw_text_from_code_cache and the NEW_CODE tabs' text pane).
    Returns the {line: [import stmts]} for draw_text's import_fixes, or None.

    Lint-only refresh (no reparse): a pending edit anywhere in this FILE (an
    import removed in another view, a reverted entry) changes what the
    missing-import lint should report — queue_save sets _relint_pending via
    _kick_relint and wakes us through the host's consumer registry. Runs
    check_source + the suggestion scan alone on a worker and swaps
    ModesState.last_lint / last_imports; callers' marker extraction sees the
    fresh lists the same frame they land.

    A cst-cache-hit boot skipped both passes entirely (lint_deferred) — that
    converts into a pending relint here. Launch floor: kicks can arrive per
    queued keystroke save (echo bursts included); one relint per second per
    host is plenty — when suppressed the flag stays LATCHED, so a later
    frame runs the trailing state and the final answer is never lost."""
    for v in (getattr(wds, "misc", None) or {}).values():
        if isinstance(v, ModesState) and (getattr(v, '_lint_deferred', False)
                                         or not hasattr(dict_host, '_last_relint_t')):
            v._lint_deferred = False
            dict_host._relint_pending = True
    _relint = bool(getattr(dict_host, '_relint_pending', False))
    if _relint:
        _rl_now = time.monotonic()
        if _rl_now - getattr(dict_host, '_last_relint_t', 0.0) < 1.0:
            _relint = False         # retry soon - flag stays set
            if getattr(dict_host, '_relint_timer', None) is None:
                import threading
                def wake_relint():
                    def notify():
                        dict_host._relint_timer = None
                        dict_host._notify_consumers(name='deferred lint ready')
                        request_render()
                    Melty.post_to_render(notify)
                delay = max(0.01, 1.0 - (_rl_now - dict_host._last_relint_t))
                dict_host._relint_timer = threading.Timer(delay, wake_relint)
                dict_host._relint_timer.daemon = True
                dict_host._relint_timer.start()
        else:
            dict_host._relint_pending = False
            dict_host._last_relint_t = _rl_now
    _lk = dict_host.child_kwargs.get('run_chain_kwargs') or {}
    # The runner is a render_func: polling it every frame cost its wrapper
    # (~0.2 ms) for nothing while idle. Call it only from a start edge
    # until that run has reported (busy → done), then stop polling.
    if _relint:
        dict_host._relint_active = True
    if (_lk.get('lint_path') and len(_str_host.values()) > 0
            and getattr(dict_host, '_relint_active', False)):
        _rl_done, _rl_payload = run_in_background(
            _run_relint,
            child_kwargs={'input_value': list(_str_host.values())[0],
                          'lint_path': _lk.get('lint_path'),
                          'lint_span': _lk.get('lint_span', False)},
            name=f"relint{id(dict_host)}", start=_relint, debounce_ms=400)
        if _rl_done:
            dict_host._relint_active = False    # reported; stop polling
        if _rl_done and isinstance(_rl_payload, dict):
            for v in (getattr(wds, "misc", None) or {}).values():
                if isinstance(v, ModesState):
                    if v.last_lint != _rl_payload["lint"]:
                        v.last_lint = _rl_payload["lint"]
                    _rl_imports = _rl_payload.get("imports") or {}
                    if getattr(v, "last_imports", None) != _rl_imports:
                        v.last_imports = _rl_imports
    for v in (getattr(wds, "misc", None) or {}).values():
        if isinstance(v, ModesState):
            # The suggestions channel rides to draw_text as its own kwarg
            # (import_fixes) - independent of the error markers.
            return getattr(v, "last_imports", None) or None
    return None


def _error_markers(err, lint):
    """The (line, msg) marker list for the editor: the parse/compile error,
    then the lint findings. Errors only — import suggestions travel on their
    own channel (ModesState.last_imports → draw_text's import_fixes)."""
    markers = []
    if err is not None:
        line = (getattr(err, "editor_line", None) or getattr(err, "lineno", None)
                or getattr(err, "raw_line", None) or 1)
        msg = (getattr(err, "message", None) or getattr(err, "msg", None)
               or str(err))
        markers.append((line, msg))
    markers += list(lint or ())
    return markers


from meltygui.view.code_view import draw_text_from_code_cache


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
        markers = _error_markers(err, lint)
        cache_error = {"__error__": markers[0][1], "__line__": markers[0][0], "__errors__": markers}
        dict_host._err_view_memo = (err, lint, cache_error)
        return cache_error
    return None


from meltygui.view.code_view import draw_code_tabs_from_cache
