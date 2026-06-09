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
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.model.core_model.draw_state import TabState
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace, get_exception_frames
from src.lsd.gl_gui.view.core_conversion import hotswap_guard
from src.lsd.gl_gui.view.core_conversion.address import (
    Address, _evict_linecache,
)
from src.lsd.gl_gui.view.core_conversion.chain_converters import (
    record_compile, _enclosing_function,
)
from src.lsd.gl_gui.view.core_conversion.file_converters import (
    _recompile, _recompile_class, _recompile_module,
)
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    cst_module_to_dict, dict_to_cst_module,
)
from src.lsd.gl_gui.view.core_conversion.new_codecs import Codec, CallSite, Decorations, type_to_codec, \
    extension_to_codec
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save_exclude, no_save
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.invalidation_tracker import Note
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Core helpers - load / write / recompile (plain synchronous, no chain magic)  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


def save_file(address, code_str, codec=None, ensure_import=None, parent_ds=None):
    """Write the edited value back through the resolved codec (span splice for
    code, whole-file for images, etc.)."""
    current_time = datetime.now().strftime("%H:%M:%S")
    print(f"{current_time} Saved {address.path} from {parent_ds.name}")
    codec.save(address=address, data=code_str, ensure_import=ensure_import)


def recompile_source(source, code_str, file_path, address=None):
    """Hotswap the edited code in place (no disk write) — do_recompile's dispatch.

    type / function / module: code_str IS the whole object's source, so it
    recompiles directly. A CallSite is different — code_str is a single statement
    inside a function body, which redefines nothing on its own — so we recompile
    the ENCLOSING function instead (see _recompile_caller). Decorations is the same
    shape: code_str is just the `@...` block, which redefines nothing alone, so we
    recompile the WHOLE decorated object (see _recompile_decorations)."""
    result = None
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
    some_val = 102
    some_other_val = 76
    some = []
    tint=(0.52,0.80,0.688)

    # [tint=(0.7722222, 0.5336913466453552, 0.17589502036571503)]
    def some_func(a=97, b=-70):
        imgui.set_cursor_pos((0,0))
    some_line = 87
    myflot = 5
    tint = (0.52, 0.80, 0.688)
    some_tuple = (101, 1)
    
    

    list_new = [1,1,1]
    # [tint=(0.80, 0.3665185570716858, 0.11555557698011398)]
    class NestedClass:
        so = 31    

    some_nested = NestedClass()

    new_bool = True
    a_dict = {"x": -40, "y": 53}


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


@window()
@render_func(use_cache=True)
def editor_window():
    code_file_io(
        TestClass,
        mode=Modes.NEW_CODE
    )
    return False, None


@window()
@render_func(use_cache=True)
def editor_window_2():
    code_file_io(
        TestClass,
        view_func=convert_in_and_out,
        auto_load_edits=False,
        auto_load=False,
        auto_save=False,
        child_kwargs={
            # convert_in_and_out runs the chains; draw_with_view_funcs draws the
            # tabs/columns. string_to_cst_module's output is named "code_tree" -
            # draw_text reads it to highlight parse errors (a failed parse arrives
            # as the exception value). cst_module_to_dict's output is "code_dict",
            # which draw_collection consumes. draw_text gets the raw string as its
            # input_value (no route key).
            "view_func": draw_with_view_funcs,
            "chain_in": [string_to_cst_module, cst_module_to_dict],
            "chain_out": [dict_to_cst_module, cst_module_to_string],
            "route": {
                string_to_cst_module: "code_tree",
                cst_module_to_dict: "code_dict",
                RenderFuncs.draw_collection: "code_dict",
            },
            "child_kwargs": {
                "view_funcs": [RenderFuncs.draw_text, RenderFuncs.draw_collection],
            },
        },
    )
    return False, None


@window()
@render_func(use_cache=True, disable_scroll=True)
def draw_collection_code():
    # view_func=draw_modes: text | dict tabs over one shared file-IO layer.
    code_file_io(
        RenderFuncs.draw_collection,
        mode=Modes.NEW_CODE
    )
    return False, None


@window()
@render_func(use_cache=True)
def editor_window_3():
    code_file_io(slow_task, auto_load_edits=False, auto_load=False)
    return False, None


@window()
@render_func(use_cache=True, selectable=False, disable_scroll=True)
def test_toggles():
    code_file_io(Toggles, mode=Modes.NEW_CODE)
    return False, None


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


def load_file(input_value: Address, codec: Codec = None, **kwargs) -> str:
    """Read the value through the resolved codec (span for code, whole file for
    images, etc.)."""
    return codec.load(input_value)


UNSET = object()
LOADING = object()


@render_func(use_cache=True, selectable=False, temp=True)
def run_in_background(input_value, loading_state: LoadingState, unique,
                      draw_state, child_kwargs, start=False, timeout=20,
                      debounce_ms=400, wait_for_drag=False, **kwargs):
    if Melty.frame_count < 10:
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
        elif wait_for_drag and (imgui.is_mouse_down(0) or imgui.is_mouse_down(1)):
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
                    note= Note(name="Run in background complete", tint=(0.5, 1.0, 0.5), draw_state=draw_state)
                    Melty.cache.invalidate(draw_state._tile_id, note=note)
                    request_render()

            if Melty.frame_count < 3:
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

    if loading_state._loading:
        loading_for = Melty.frame_count - loading_state._loading_start_frame
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
        # Set the frame an edit changes the buffer; consumed next frame to force a
        # reconvert (so chain_in re-parses the new text and surfaces syntax errors)
        # even when nothing external changed.
        self._reconvert = False
        self._recompiled_on_frame = None
        self.recompile_result = None

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


def _run_chain_in(input_value, chain=None, **extra):
    """Background entry point for the forward (chain_in) conversion.

    A plain module-level function (NOT a @render_func) so run_in_background can
    call it directly on its worker thread without touching any imgui/Melty global
    state. Runs the whole chain via _run_convert and returns the full `routed`
    dict — every column's input in one shared payload. The result/exception is
    folded back into ModesState on the main thread when the worker completes."""
    result, routed = _run_convert(chain, input_value, **extra)
    error = result if isinstance(result, Exception) else None
    # cst parsed clean - run the compiler check, to surface the syntax errors libcst
    # is too lenient to flag (duplicate args/kwargs, ...). Same red-highlight path.
    if error is None and isinstance(input_value, str):
        error = _compile_check(input_value)
    return {"routed": routed, "error": error}


def _run_chain_out(input_value, chain=None, **extra):
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
    nodes without a matching param ignore it (_run_convert filters by signature)."""
    result, _ = _run_convert(chain, input_value, **extra)
    if isinstance(result, Exception):
        return {"error": result}
    return {"value": result}


class ModesState:
    """Per-window scratch for draw_modes.

    chain_in (str → cst → dict, ...) is O(buffer) and runs on a BACKGROUND thread
    via run_in_background, so it can't block the render loop. This holds the
    cross-frame state that makes that work while keeping every column in sync:

    last_good — the routed outputs of the last conversion that SUCCEEDED. Every
      selected column reads the SAME last_good, so they never drift apart. While a
      fresh conversion is in flight (or one throws on half-typed source) the views
      keep rendering off this snapshot instead of blanking out.
    last_error — the parse/compile error from the last run, or None when clean."""

    def __init__(self):
        self.last_good = {}
        self.last_error = None


def compute_height(draw_state):
    return None
    # return min(draw_state., 400)


@render_func(use_cache=True, show_bg=False, selectable=False, disable_scroll=True,
             shadow=False, indent_size=0, with_footer=None, fill_height=True)
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
             shadow=False, indent_size=0, with_footer=None, fill_height=True, temp=True)
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
    if chain_in:
        chain_in_kwargs = {**forwarded, "input_value": input_value,
                           "chain": chain_in, "route": route}
        finished, payload = run_in_background(
            _run_chain_in,
            child_kwargs=chain_in_kwargs,
            name=f"chain_in{unique}", start=external_change)
        if external_change:
            note = Note(name="convert_in_out, chain in start", tint=(1, 0.5, 0))
            draw_state._parent.invalidate(note=note)
            print(f"[CIOV start] ds={id(draw_state):x} fc={draw_state.frame_count} "
                  f"gfc={Melty.frame_count} unique={unique}")
        external_change = False
        if finished and isinstance(payload, dict):
            modes_state.last_error = payload.get("error")
            for name, val in payload["routed"].items():
                modes_state.last_good[name] = val
                routed[name] = val

            chain_in_error = modes_state.last_error
            external_change = True
            note = Note(name="Convert in and out, chain in finished", tint=(1, 0.5, 1.0), draw_state=draw_state)
            draw_state._parent.invalidate(note=note)
            print(f"[CIOV finish] ds={id(draw_state):x} fc={draw_state.frame_count} "
                  f"gfc={Melty.frame_count} unique={unique} "
                  f"routed_keys={list(payload['routed'].keys())}")

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
    if external_change:
        # edge frame: chain_in just finished -> what are we handing the user?
        print(f"[CIOV->view] ds={id(draw_state):x} fc={draw_state.frame_count} "
              f"primary_key={primary_key} primary={type(primary).__name__} "
              f"is_none={primary is None}")
    edited, edited_value = view_func(input_value=primary, external_change=external_change,
                                     **child_kwargs)
    converted_edit = edited_value if (edited and edited_value is not None) else UNSET
    if edited:
        draw_state.invalidate(note=Note(name="convert_in_out, view func edit", tint=(1.0, 0.5, 0), draw_state=draw_state))

    # ── (identical) chain_out in background ───────────────────────────────────────
    if chain_out:
        co_start = converted_edit is not UNSET
        indent = _common_indent(input_value)
        co_changed, co_payload = run_in_background(
            _run_chain_out,
            child_kwargs={"input_value": converted_edit if co_start else None,
                          "chain": chain_out, "indent": indent},
            name=f"chain_out{unique}", start=co_start)
        if co_changed and isinstance(co_payload, dict):
            if co_payload.get("error") is not None:
                imgui.text_colored(f" chain_out: {co_payload['error']}", 1.0, 0.5, 0.0)
            elif "value" in co_payload:
                out_changed, out_value = True, co_payload["value"]

    return out_changed, out_value


def code_file_footer(input_value, code_state, **kwargs):
    if code_state.address is not None:
        imgui.text(str(code_state.address.path))
    return False, None


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  editable_source - the whole round-trip, one function                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
@render_func(use_cache=True, selectable=False, with_header=draw_header, searchable=False, disable_scroll=True)
def code_file_io(input_value, code_state: CodeState, codec=None, view_func=RenderFuncs.draw_text, auto_load=True,
                 auto_load_edits=False, min_height=20,
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
            # File-path fallback by extension -- only for a GENUOUS path or a
            # bare str. Exact-type the str check so an extended primitive like
            # CodeLine(str) (whose type is SOURCE, not a FILE) isn't whacked
            # into being interpreted as a path. Path uses isinstance so real
            # path objects (PosixPath, a Path subclass) still match.
            if codec is None and (isinstance(input_value, Path) or type(input_value) is str):
                codec = extension_to_codec.get(Path(str(input_value)).suffix)

        if codec is None:
            imgui.text(f"No codec for type: {type(input_value).__name__}")
            return False, None

        address = codec.resolve_address(input_value, draw_state, code_state=code_state)
        code_state.address = address
        top_line_height = 30
        external_change = False
        imgui.same_line(spacing=0)

        if address is None:
            return False, None

        if auto_load:
            if draw_state.frame_count < 1:
                load = True
                code_state.text_cache = None
                code_state.mark_file_current()

        if not auto_recompile_edits and code_state.text_cache is not UNSET and code_state.text_cache is not None:
            play_icon = "\uf04b"
            recompile = \
                RenderFuncs.button(f"{play_icon} Run",
                                   tint=(0.05678745, 0.5, 0.2, 0.5),
                                   height=top_line_height,
                                   name=f"recompile_btn{unique}")[0]

        if Toggles.enable_jedi:
            imgui.same_line(spacing=0)
            search_icon = "\uf002"
            run_jedi = RenderFuncs.button(f"{search_icon} Index",
                                          tint=(0.8, 0.54, 0.2),
                                          height=top_line_height,
                                          name="jedi_index_btn")[0] or run_jedi

        if code_state._recompiled_on_frame is not None:
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

        if code_state.is_file_stale() and not code_state._pending_save:
            if auto_load_edits:
                load = True
                code_state.mark_file_current()
            else:
                imgui.same_line(spacing=0)
                if RenderFuncs.button("Load", width=100, height=top_line_height, name=f"reload{unique}")[0]:
                    load = True

                if not code_state._pending_save:
                    imgui.same_line()
                    if RenderFuncs.button("Keep mine", width=100, height=top_line_height, name=f"keepmine{unique}")[0]:
                        save = True

        if not auto_save and code_state._pending_save:
            imgui.same_line(spacing=0)
            if RenderFuncs.button("Save", width=100, height=top_line_height, name=f"save{unique}")[0]:
                save = True

        if auto_save:
            imgui.same_line(spacing=8)
            imgui.align_text_to_frame_padding()
            imgui.text_colored(f"Auto-save", *(0.6, 1.0, 0.1, 1.0))

        changed, new_text = run_in_background(load_file,
                                              child_kwargs={"input_value": address, 'codec': codec},
                                              name=f"load{unique}", start=load)
        if new_text is LOADING:
            code_state.mark_file_current()

        elif changed:
            code_state.text_cache = new_text
            code_state.mark_file_current()
            draw_state.invalidate_up(max_depth=6)
            code_state._pending_save = False
            external_change = True
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
            child_kwargs['error'] = _runtime_error or _recompile_error
            child_kwargs['root_input'] = input_value
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
        recompile_hotkey = bool(enter_key_pressed and enter_key_pressed.ctrl)

        # Save: write the edited span back to disk off the main thread. The text is
        # snapshotted into child_kwargs at trigger time, so a later edit can't race
        # the in-flight write. Auto-save-on-edit is debounced so a burst of
        # keystrokes collapses into one write after typing pauses; the explicit
        # save button / Ctrl+S fires immediately (debounce 0).
        explicit_save = save_hotkey or save
        save_start = (auto_save and edited) or explicit_save
        save_debounce = 0 if explicit_save else save_debounce_ms
        time = datetime.now().strftime("%H:%M:%S")
        if save_start:
            note = Note(name="Code_file_io save start", tint=(1, 0.5, 0))
            # Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=4, note=note)

        saved, result = run_in_background(save_file,
                                          child_kwargs={"address": address,
                                                        "codec": codec,
                                                        "code_str": code_state.text_cache,
                                                        "ensure_import": ensure_import,
                                                        "parent_ds": draw_state},
                                          name=f"save{draw_state.name}", start=save_start,
                                          debounce_ms=save_debounce,
                                          wait_for_drag=not explicit_save)
        if result == LOADING:
            code_state.mark_file_current()

        elif saved:
            # Our own write bumped mtime; clear the stale flag set on edit so the
            # next frame doesn't read the disk as an external change.
            code_state.mark_file_current()
            code_state._pending_save = False
            note = Note(name="On saved, code_file_io", tint=(0.5, 1.0, 1.0), draw_state=draw_state)
            Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=4, note=note)

        # Recompile (hot reload, no disk write): button, Ctrl+Enter, or recompile=True
        # on edit. Same runner, its own loading_state.
        recompile_start = (recompile) or recompile_hotkey
        changed, result = run_in_background(recompile_source,
                                            child_kwargs={"source": input_value,
                                                          "code_str": code_state.text_cache,
                                                          "file_path": address.path,
                                                          "address": address},
                                            name="recompile", start=recompile_start)
        if result == LOADING:
            print("Starting recompile...")
            code_state._recompiled_on_frame = None
        elif result == UNSET:
            pass
        else:
            if changed:
                print("Recompile successful-------------------------------")
                code_state._recompiled_on_frame = Melty.frame_count
                record_compile(address)
                Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10)
                request_render()
            code_state.recompile_result = result


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

    # This is the root function, end of the line.
    return edited, code_state.text_cache
