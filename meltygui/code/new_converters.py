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
one: it shows a tab per repr (text | structured), running a `chain_in` once to
convert (e.g. str → cst → dict for `draw_collection`) and `chain_out` back on
edit. `_run_convert` runs those chains inline (bare functions, no threads) and
treats a parse failure as a VALUE — a cst error over half-typed source is a
normal editor state, surfaced as UI (and as a red line highlight, see
`text_editor`), with structured views falling back to the last good parse
(`ModesState`).

ASYNC + DEBOUNCE. load / save / recompile each run through `run_in_background`,
a one-shot worker keyed by a distinct `name=` so they never clobber each other.
Load is effectively cached (re-offered only on disk change); save auto-fires on
edit but is debounced (`save_debounce_ms`) so a burst of keystrokes collapses
into one write — a one-shot timer wakes the loop at the deadline instead of
spinning `request_render`. An explicit Save / Ctrl+S bypasses the debounce.

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
    Address,
)
from src.lsd.gl_gui.view.core_conversion.chain_converters import (
    record_compile,
)
from src.lsd.gl_gui.view.core_conversion.file_converters import (
    _recompile, _recompile_class, _recompile_module,
)
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    cst_module_to_dict, dict_to_cst_module,
)
from src.lsd.gl_gui.view.core_conversion.new_codecs import Codec, type_to_codec, extension_to_codec
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save_exclude
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Core helpers - load / write / recompile (plain synchronous, no chain magic)  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


def save_file(address, code_str, codec=None, ensure_import=None, draw_state=None):
    """Write the edited value back through the resolved codec (span splice for
    code, whole-file for images, etc.)."""
    current_time = datetime.now().strftime("%H:%M:%S")
    print(f"{current_time} Saved {address.path} from {draw_state.name} {draw_state.parent_window.name}")
    codec.save(address=address, data=code_str, ensure_import=ensure_import)



def recompile_source(source, code_str, file_path):
    """Hotswap the edited code in place (no disk write) — do_recompile's dispatch."""
    result = None
    if isinstance(source, type):
        result = _recompile_class(source, code_str, str(file_path))
    elif isinstance(source, types.FunctionType):
        result = _recompile(source, code_str, str(file_path))
    elif isinstance(source, types.ModuleType):
        result = _recompile_module(source, code_str, str(file_path))
    return result


class TestClass:
    some_val = 76
    some_other_val = 40
    some = []
   
     # [tint=(0,0.2,1)]
    def some_func(a=-26, b=3):
        print(a, b)
        
    some_func(77,-36)

    some_line = 87
    myflot = 5
    tint = (0.08707411, 0.1469433, 0.1627907156944275)
    some_tuple = (68, 1)

    # [tint=(0.9069767594337463, 0.5192674398422241, 0.029529478400945663)]
    class NestedClass:
        so = 1

    some_val = 76
    new_bool = True
    a_dict = {"x": -52, "y": 53}


def slow_task(**kwargs):
    import time
    print("Starting slow task...")
    time.sleep(1)

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
        view_func=draw_modes,
        auto_load_edits=False,
        auto_load=False,
        auto_save=False,
        child_kwargs={
            "modes": [RenderFuncs.draw_text, RenderFuncs.draw_collection],
            "chain_in": [string_to_cst_module, cst_module_to_dict],
            "chain_out": [dict_to_cst_module, cst_module_to_string],
            # string_to_cst_module's output is named "code_tree" - draw_text reads
            # it to highlight parse errors (a failed parse arrives as the exception
            # value). cst_module_to_dict's output is "code_dict" - draw_collection
            # consumes it. draw_text still gets the raw string as its input_value.
            "route": {
                string_to_cst_module: "code_tree",
                cst_module_to_dict: "code_dict",
                RenderFuncs.draw_collection: "code_dict",
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
        self.run_next = None
        self.pending_change = False
        self.error = None
        self._loading_start_frame = None
        # Wall-clock deadline (time.time()) the queued task must wait until before
        # it launches. None = no debounce. Each fresh `start` pushes it out.
        self.debounce_deadline = None
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
def run_in_background(input_value, loading_state: LoadingState,
                      draw_state, child_kwargs, start=False, timeout=20,
                      debounce_ms=0, **kwargs):
    if start:
        loading_state.run_next = input_value, child_kwargs
        if debounce_ms:
            # Debounce: defer the launch until the input goes quiet. Re-start on
            # every start (a burst of typing keeps pushing it out), and wake the
            # loop ONCE at the deadline via a one-shot timer - never busy-spin
            # request_render per frame, or we peg the whole render thread. The
            # run_next field above always holds the LATEST value, so the
            # eventual single run uses the final value.
            loading_state.debounce_deadline = time.time() + debounce_ms / 1000.0
            if loading_state._debounce_timer is not None:
                loading_state._debounce_timer.cancel()
            timer = threading.Timer(debounce_ms / 1000.0, request_render)
            timer.daemon = True
            loading_state._debounce_timer = timer
            timer.start()
            draw_state.invalidate()
        else:
            loading_state.debounce_deadline = None
            draw_state.invalidate()
            request_render()

    if loading_state.run_next is not None:
        deadline = loading_state.debounce_deadline
        if deadline is not None and time.time() < deadline:
            # Inside the quiet window - keep this draw_state dirty so the deadline
            # render re-runs this frame, but DON'T request_render: the one-shot
            # timer above wakes the loop exactly once when the deadline lands.
            draw_state.invalidate()
        else:
            loading_state.debounce_deadline = None
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
                finally:
                    loading_state._loading = False
                    loading_state.pending_change = True
                    Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=10)
                    request_render()

            if Melty.frame_count < 1:
                run(run_next_inner=loading_state.run_next)
            else:
                run_next = loading_state.run_next
                if not loading_state._loading:
                    loading_state.run_next = None
                    threading.Thread(target=run, kwargs={"run_next_inner": run_next}).start()
                    loading_state._loading_start_frame = Melty.frame_count
                    if loading_state.run_next is run_next:
                        loading_state.run_next = None

    if loading_state._loading:
        loading_for = Melty.frame_count - loading_state._loading_start_frame
        return False, LOADING

    if loading_state.pending_change and loading_state.run_next is None:
        loading_state.pending_change = False
        draw_state.invalidate_up(max_depth=20)
        request_render()
        return True, loading_state.cached_result
    else:
        return False, loading_state.cached_result


@no_save_exclude()
class CodeState(DictConversion):
    def __init__(self):
        super().__init__()
        self.text_cache = UNSET
        self.code_tree_cache = None
        self.address = None
        self.file_mtime = None
        self.file_size = None
        self.auto_load = False
        self.pending_save = False
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

    def parse_cst(self):
        if isinstance(self.text_cache, str):
            cst_tree = cst.parse_module(self.text_cache)
            self.code_tree_cache = cst_module_to_dict(cst_tree)
        else:
            self.code_tree_cache = None


@render_func()
def string_to_cst_module(input_value, **kwargs):
    cst_tree = cst.parse_module(input_value)
    return True, cst_tree


@render_func()
def cst_module_to_string(input_value, **kwargs):
    code_str = input_value.code
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
        sig = inspect.signature(inner)
        accepts_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        call = {'input_value': value, **routed, **extra, **node_kwargs}
        if not accepts_var_kw:
            call = {k: v for k, v in call.items() if k in sig.parameters}
        try:
            result = inner(**call)
        except Exception as e:
            return e, routed
        if isinstance(result, tuple) and len(result) == 2:
            _, value = result
        else:
            value = result
        if route and node in route:
            routed[route[node]] = value
    return value, routed


def _compile_check(text):
    """Second-pass syntax check, catching errors libcst's lenient parser lets
    through but Python's own compiler rejects — duplicate args (`def f(x, x)`),
    repeated kwargs (`foo(a=1, a=1)`), `return`/`yield` outside a function, etc.

    Returns the SyntaxError (carrying a real `lineno` for the red highlight) or
    None if it compiles clean. Runs ONLY after libcst already parsed the buffer,
    so it never double-reports a plain syntax error — it only *adds* the class of
    mistakes cst misses.

    Dedented first because the editor can hold an indented span (a nested class
    as getsourcelines returns it); `compile` rejects a leading indent the same
    way `_recompile_class` handles it. NOTE: this is a pure syntax/compile check —
    it does NOT catch undefined names / typos (`print(myvarr)`), which are runtime
    NameErrors, not SyntaxErrors, and need scope analysis (pyflakes) to detect."""
    if not isinstance(text, str):
        return None
    import textwrap
    try:
        compile(textwrap.dedent(text), "<editor>", "exec")
        return None
    except SyntaxError as e:
        return e
    except Exception:
        # Any non-SyntaxError exception (e.g. ValueError on null bytes) isn't the
        # user's code being wrong in a way we can pin to a line - ignore it.
        return None


def _run_chain_in(input_value, chain=None, route=None, seed=None, **extra):
    """Background entry point for the forward (chain_in) conversion.

    A plain module-level function (NOT a @render_func) so run_in_background can
    call it directly on its worker thread without touching any imgui/Melty global
    state. Runs the whole chain via _run_convert and returns the full `routed`
    dict — every column's input in one shared payload. The result/exception is
    folded back into ModesState on the main thread when the worker completes."""
    routed = dict(seed) if seed else {}
    result, routed = _run_convert(chain, input_value, route=route, routed=routed, **extra)
    error = result if isinstance(result, Exception) else None
    # cst parsed clean - run the compiler check, to surface the syntax errors libcst
    # is too lenient to flag (duplicate args/kwargs, ...). Same red-highlight path.
    if error is None and isinstance(input_value, str):
        error = _compile_check(input_value)
    return {"routed": routed, "error": error}


class ModesState:
    """Per-window scratch for draw_modes.

    chain_in (str → cst → dict, ...) is O(buffer) and runs on a BACKGROUND thread
    via run_in_background, so it can't block the render loop. This holds the
    cross-frame state that makes that work while keeping every column in sync:

    last_good — the routed outputs of the last conversion that SUCCEEDED. Every
      selected column reads the SAME last_good, so they never drift apart. While a
      fresh conversion is in flight (or one throws on half-typed source) the views
      keep rendering off this snapshot instead of blanking out.
    last_input — the source text the last background run was started from. A new
      run is triggered only when the input actually changes (the editor
      re-renders every frame with identical text otherwise)."""

    def __init__(self):
        self.last_good = {}
        self.last_input = UNSET
        self.last_error = None

def compute_height(draw_state):
    view_top = draw_state.abs_top
    delta_from_top = view_top - draw_state.parent_window.abs_top
    fill_height = draw_state.parent_window.height - delta_from_top - 15
    return fill_height

@render_func(use_cache=True, show_bg=False, selectable=False, disable_scroll=True,
             shadow=False, indent_size=0, with_footer=None, fill_height=True)
def draw_modes(input_value, modes=None, chain_in=None, chain_out=None, route=None,
               tab_state: TabState = None, modes_state: ModesState = None,
               changed=False, child_kwargs=None, column_widths=None,
               draw_state=None, unique=0, **kwargs):
    """General form of the round-trip that used to be hardcoded in
    code_file_io_wrapped. Takes the loaded text and:

      * runs `chain_in` ONCE (e.g. string -> cst -> dict), stashing every output
        named in `route` so it can be handed to whichever view wants it;
      * shows a tab per entry in `modes` (each a view render_func, or a
        `(view_func, kwargs)` tuple) and renders the selected ones side by side;
      * feeds each view the routed value `route[view_func]` as its input_value
        (e.g. draw_collection <- the dict). A view with NO route entry gets the
        raw text instead (draw_text <- the string). Every routed value also rides
        in as a kwarg, so a view picks up extras it wants (draw_text <- jump_to /
        code_tree);
      * when a view edits a routed (converted) value, the edit is run back
        through `chain_out` to canonical text. A view editing the raw text
        returns it directly — no chain_out needed.

    NOTE: the params are `chain_in` / `chain_out`, NOT convert_in / convert_out —
    the latter are reserved kwargs the render_func wrapper still interprets as the
    old deprecated convert path."""
    if child_kwargs is None:
        child_kwargs = {}
    if modes is None:
        modes = [RenderFuncs.draw_text]

    def view_of(m):
        return m[0] if isinstance(m, tuple) else m

    # Pr out entries that didn't survive (de)serialization - e.g. a saved
    # state from before function refs round-tripped, which would leave None here.
    tab_state.selected_tabs = [t for t in tab_state.selected_tabs if view_of(t) is not None]
    if not tab_state.selected_tabs:
        tab_state.selected_tabs = [modes[0]]

    names = [getattr(view_of(m), '__name__', str(m)) for m in modes]
    tab_changed, new_tabs = RenderFuncs.draw_tab_bar(indent_size=0, z_offset=-1,
        input_value=tab_state.selected_tabs, collection=modes, names=names, bg_offset=2, wrap=True,
        tab_height=26, show_bg=True, name=f"mode_tabs{unique}", as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    # chain_in runs ONCE on a background thread (it's O(buffer): cst parse + dict
    # transform) and its outputs are SHARED across every selected tab - that's what
    # keeps the columns in sync. We trigger a fresh run only when the input text
    # changes; in between, and while a run is in flight, every column reads the
    # same modes_state.last_good snapshot.
    #
    # The current address rides in as `jump_to`; views that want it (draw_text)
    # pick it up alongside the other outputs, so seed the shared `routed` with it.
    routed = dict(modes_state.last_good)
    if 'jump_to' in kwargs:
        routed['jump_to'] = kwargs['jump_to']


    chain_in_error = modes_state.last_error
    if chain_in:
        input_changed = input_value != modes_state.last_input
        if input_changed:
            modes_state.last_input = input_value
        # One worker per draw_modes instance (distinct name=). It snapshots the
        # input at trigger time, runs _run_chain_in off-thread, and re-renders on
        # completion. `start` only on a real input change so we don't respawn the
        # parse every frame.
        finished, payload = run_in_background(
            _run_chain_in,
            child_kwargs={"input_value": input_value, "chain": chain_in,
                          "route": route, "seed": child_kwargs},
            name=f"chain_in{unique}", start=input_changed)
        if finished and isinstance(payload, dict):
            # Fold the fresh outputs into the shared snapshot. last_good
            # only ever holds clean values; a parse failure leaves the last
            # good values in place so columns keep rendering.
            # good values in place so columns keep rendering.
            modes_state.last_error = payload.get("error")
            for name, val in payload["routed"].items():
                modes_state.last_good[name] = val
                if name != 'jump_to':
                    routed[name] = val
            chain_in_error = modes_state.last_error

    # Route the error to the views: draw_text reads `error` and lights up the
    # offending source line in red. Two sources merge here:
    #   - recompile_error - passed down by code_file_io (the last hotswap failure).
    #     Python's compiler pins a better line than libcst, so it WINS when present
    #     - UNLESS it's a stale SyntaxError: once chain_in parses the buffer clean
    #     (chain_in_error is None) the error was fixed, so we ignore it. A recompile
    #     *runtime* error (not a SyntaxError) parses fine, so it survives until the
    #     next Run.
    #   - chain_in_error - the background parse failure (live, per-keystroke).
    # None when everything is clean, which clears any prior error. (code_tree
    # carries the parsed tree on success but not the failure - _run_convert stops
    # before writing a failing node's route entry - so the exception travels up.)
    recompile_error = kwargs.get('error')
    if isinstance(recompile_error, SyntaxError) and chain_in_error is None:
        recompile_error = None  # buffer parses again → the recompile syntax error is fixe
    routed['error'] = recompile_error or chain_in_error

    out_changed, out_value = False, input_value
    for idx, mode in enumerate(tab_state.selected_tabs):
        view_func = view_of(mode)
        mode_kwargs = mode[1] if isinstance(mode, tuple) else {}

        arg_name = route.get(view_func) if route else None
        if arg_name is None:
            # No routed input - this view gets the raw text (e.g. draw_text). It
            # is unaffected by a chain_in failure; it sees what the user typed.
            uses_converted = False
            view_input = input_value
        else:
            uses_converted = True
            fresh = routed.get(arg_name)
            if fresh is not None:
                view_input = fresh
            elif arg_name in modes_state.last_good:
                # chain_in failed since producing this value - keep the last one
                # that worked so the structured view doesn't blank out.
                view_input = modes_state.last_good[arg_name]
            else:
                # Never had a good conversion (invalid on first load): render the
                # error in place of this view rather than crash.
                imgui.text_colored(
                    f" {getattr(view_func, '__name__', 'view')}: {chain_in_error}",
                    1.0, 0.5, 0.0)
                continue

        call_kwargs = {**child_kwargs, **routed, **mode_kwargs}

        # Pass external change as `draw=`, NOT `changed=`: `draw` just bypasses
        # the view's cache for a redraw; `changed` would set the view's sticky
        # _external_change / _pending edit flags, so a forced redraw would come
        # back reported AS an edit - with auto_save that becomes a
        # save -> reload -> redraw -> save feedback spin. (Matches the ol
        # hardcoded `draw_collection(..., draw=changed)`.)
        if column_widths is not None and len(column_widths) > idx:
            column_width = column_widths[idx]
        else:
            column_width = None


        m_changed, m_out = view_func(input_value=view_input, draw=changed, disable_scroll=False, show_header=False,
                                     column=idx, column_width=column_width, show_add_delete=False, name=f"{modes[idx].__name__}##{unique}",
                                     selectable=False,
                                     **call_kwargs)
        if not m_changed:
            continue
        else:
            draw_state.invalidate_up(max_depth=3)


        if uses_converted and chain_out:
            result, _ = _run_convert(chain_out, m_out)
            if isinstance(result, Exception):
                imgui.text_colored(f" chain_out: {result}", 1.0, 0.5, 0.0)
            else:
                out_value, out_changed = result, True
        else:
            out_value, out_changed = m_out, True

    return out_changed, out_value


def code_file_footer(input_value, code_state, **kwargs):
    if code_state.address is not None:
        imgui.text(str(code_state.address.path))
    else:
        imgui.text(f"Address not resolved for {input_value.__class__.__name__}")
    return False, None

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  editable_source - the whole round-trip, one function                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
@render_func(use_cache=True, selectable=False, searchable=False, with_footer=code_file_footer, disable_scroll=True)
def code_file_io(input_value, code_state: CodeState, codec=None, view_func=RenderFuncs.draw_text, auto_load=True,
                 auto_load_edits=False,
                 child_kwargs=None, draw_state=None, auto_save=True, auto_recompile_edits=False, save=False, load=False,
                 recompile=False, save_debounce_ms=600,
                 ensure_import=None, s_key_pressed=None, enter_key_pressed=None, unique=None, **kwargs):
    try:
        imgui.dummy(0,0)

        if child_kwargs is None:
            child_kwargs = {}
        # ── 1. Resolve the source's line span ─────────────────────────────────────
        if codec is None:
            if type(input_value) in type_to_codec:
                codec = type_to_codec[type(input_value)]
            elif isinstance(input_value, (str, Path)):
                codec = extension_to_codec.get(Path(str(input_value)).suffix)

        address = codec.resolve_address(input_value, draw_state, code_state=code_state)
        code_state.address = address
        top_line_height = 30
        external_change = False
        imgui.same_line(spacing=0)

        if address is None:
            RenderFuncs.draw_text(
                f"editable_source: can't resolve source for {type(input_value).__name__}",
                name=f"resolve_error{unique}",
                mode=Modes.WINDOW)
            return False, None

        if auto_load:
            if draw_state.frame_count < 1:
                load = True
                code_state.text_cache = None
                code_state.mark_file_current()

        if not auto_recompile_edits and code_state.text_cache is not UNSET and code_state.text_cache is not None:
            play_icon = "\uf04b"
            recompile = \
                RenderFuncs.button(f"{play_icon} Run", tint=(0, 0.4, 0.1), height=top_line_height,
                                   name="recompile_btn")[0]

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

        if code_state.is_file_stale() and not code_state.pending_save:
            if auto_load_edits:
                load = True
                code_state.mark_file_current()
            else:
                imgui.same_line(spacing=0)
                if RenderFuncs.button("Load", width=100, height=top_line_height, name=f"reload")[0]:
                    load = True

                if not code_state.pending_save:
                    imgui.same_line()
                    if RenderFuncs.button("Keep mine", width=100, height=top_line_height, name=f"keepmine")[0]:
                        save = True

        if not auto_save and code_state.pending_save:
            imgui.same_line(spacing=0)
            if RenderFuncs.button("Save", width=100, height=top_line_height, name=f"save")[0]:
                save = True

        if auto_save:
            imgui.same_line(spacing=0)
            imgui.text(f"Auto-save")

        changed, new_text = run_in_background(load_file,
                                              child_kwargs={"input_value": address, 'codec': codec},
                                              name=f"load", start=load)
        if new_text is LOADING:
            code_state.mark_file_current()

        elif changed:
            code_state.text_cache = new_text
            code_state.mark_file_current()
            draw_state.invalidate_up(max_depth=6)
            code_state.pending_save = False
            external_change = True
            request_render()


        # ── 3. Edit - the actual call ─────────────────────────────────────────────
        if code_state.text_cache is not UNSET and code_state.text_cache is not None:

            child_kwargs['jump_to'] = address
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
            edited, value = view_func(input_value=code_state.text_cache, changed=external_change, **child_kwargs)

            if edited:
                code_state.text_cache = value
                code_state.mark_file_current()
                code_state.pending_save = True
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
        saved, result = run_in_background(save_file,
                                          child_kwargs={"address": address,
                                                        "codec": codec,
                                                        "code_str": code_state.text_cache,
                                                        "ensure_import": ensure_import,
                                                        "draw_state": draw_state},
                                          name="save", start=save_start,
                                          debounce_ms=save_debounce)
        if result == LOADING:
            code_state.mark_file_current()
        elif saved:
            # Our own write bumped mtime; clear the stale flag set on edit so the
            # next frame doesn't read the disk as an external change.
            code_state.mark_file_current()
            code_state.pending_save = False
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10)

        # Recompile (hot reload, no disk write): button, Ctrl+Enter, or recompile=True
        # on edit. Same runner, its own loading_state.
        recompile_start = (recompile) or recompile_hotkey
        changed, result = run_in_background(recompile_source,
                                            child_kwargs={"source": input_value,
                                                          "code_str": code_state.text_cache,
                                                          "file_path": address.path},
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

    # This is the root function, end of the line.
    return False, None
