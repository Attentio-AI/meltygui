"""
Single-function source round-trip — the load/save bookend, collapsed.

`Mode.CODE` today is a five-node chain:

    address_in  →  load  →  editor  →  save  →  address_out

Those five nodes cooperate through `run_chain` + the shared `CacheTree`, and the
change signal is smeared across them: `changed` is threaded both in (as a kwarg)
and out (as a return), `reference` is reached out of the cache so the save side
can see the prior parse, `Pending` is smuggled in the value slot while background
load/convert/save are in flight, and a `_lens_save_pending` latch on the
draw_state holds the save intent across that background latency.

`editable_source` replaces the whole chain with ONE @render_func whose state is
scoped to its own draw_state. The trick that lets the state collapse:

  * load is SYNCHRONOUS and cached once (re-offered only when the file changes on
    disk) — so there is no background load, no Pending, no run_button dispatch.
  * save is SYNCHRONOUS and fires only on a real edit — so there is no
    `_lens_save_pending` latch (the latch only existed to survive the background
    convert), no Pending, and no `request_render` spin.

With load cached and save synchronous, `changed` has exactly one producer (the
nested editor call) and one consumer (the save below it in the same function).
Nothing crosses a node boundary, so there is nothing for `run_chain` / `CacheTree`
to thread. The conversion reads top to bottom.

Trade-off vs. the chain: `dict_to_cst_module` + the file write run inline on the
frame an edit lands. Collection edits are discrete (a pick, an add/delete), so
that is one synchronous pass per edit, not per keystroke. A text editor that
emits on every keystroke wants its own debounce (see str_to_general_parse) and is
a different `editor` callable — this function is agnostic to which it gets.
"""

import inspect
import threading
import time
import tokenize
import types
from enum import Enum
from pathlib import Path

import imgui
import libcst as cst

from src.lsd.gl_gui.melty import FileWatch, Melty
from src.lsd.gl_gui.model.app_model import AppModel, TensorView
from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.model.model_enums import RelaxedEnum
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.view.core_conversion.address import (
    Address, _evict_linecache, shift_sibling_linenos,
)
from src.lsd.gl_gui.view.core_conversion.chain_converters import (
    _load_span, _ensure_import_lines, record_compile, class_to_address, address_to_general_parse,
)
from src.lsd.gl_gui.view.core_conversion.file_converters import (
    _detect_newline, _recompile, _recompile_class, _recompile_module,
)
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    cst_module_to_dict, dict_to_cst_module,
)
from src.lsd.gl_gui.view.core_views.core_render import render_func, get_draw_state, strhash
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save_exclude
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Core helpers - load / write / recompile (plain synchronous, no chain magic)  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _resolve_address(source, draw_state):
    """source object (class / function / module) → Address for its line span.

    Mirrors class_to_address / function_to_address / module_to_address, folded
    into one dispatch. Resolves on EVERY call but guards the expensive
    getsourcelines behind an (input, mtime) cache on the draw_state: typing
    doesn't write the file, so it's a cache hit; a save bumps mtime and we
    re-resolve once with fresh line numbers (important — a sibling edit shifts
    the span, and a stale span would write to the wrong range)."""
    if isinstance(source, types.ModuleType):
        source_file = Path(source.__file__)
        FileWatch.register_draw_state(draw_state, source_file)
        return Address(source_file, source=source, watcher_ds=draw_state)

    if isinstance(source, types.FunctionType):
        unwrapped = inspect.unwrap(source)
    elif isinstance(source, type) and source.__module__ not in ('builtins', '_collections_abc'):
        unwrapped = source
    else:
        return None

    try:
        source_file = inspect.getfile(unwrapped)
    except TypeError:
        return None
    FileWatch.register_draw_state(draw_state, Path(source_file))

    try:
        mtime = Path(source_file).stat().st_mtime
    except OSError:
        mtime = None
    cached = getattr(draw_state, '_addr_cache', None)
    if cached is not None and cached[0] is source and cached[1] == mtime:
        return cached[2]

    _evict_linecache(source_file)
    try:
        source_lines, start_lineno = inspect.getsourcelines(unwrapped)
    except (OSError, TypeError, tokenize.TokenError, SyntaxError) as e:
        if draw_state._addr_cache is not None:
            return draw_state._addr_cache[2]

        print(f"[editable_source] could not resolve {getattr(source, '__name__', source)}: {e}")
        return None

    address = Address(Path(source_file), start_lineno - 1,
                      start_lineno - 1 + len(source_lines),
                      source=source, watcher_ds=draw_state)
    draw_state._addr_cache = (source, mtime, address)
    return address


def _load_parse(address):
    """Address → GeneralParse. Synchronous: read the span, parse, dict-ify, and
    tag the parse with its Address/path so the editor and save side agree on
    where it lives."""
    parse = cst_module_to_dict(cst.parse_module(_load_span(address)))
    parse.address = address
    parse.file_path = address.path
    return parse


def _write_span(address, code_str, ensure_import=None):
    """Write code_str back into the file at the Address's span — the synchronous
    body of the old @render_func(background=True) _do_save, minus the Pending.

    ensure_import=(module, name) inserts a missing import in the SAME write so a
    synthesized decorator (e.g. @defaults) resolves. Updates the Address span in
    place and shifts siblings so this frame's Address stays valid; siblings heal
    on the next mtime-driven re-resolve."""
    full = address.path.read_bytes()
    newline = _detect_newline(full)
    try:
        text = full.decode("utf-8")
    except UnicodeDecodeError:
        text = full.decode("latin-1")
    lines = text.split(newline)
    new_lines = code_str.split(newline)

    old_start, old_end = address.start, address.end
    if old_start is None:                     # whole-file (module) address
        lines = new_lines
    else:
        lines[old_start:old_end] = new_lines

    inserted = 0
    if ensure_import is not None:
        lines, inserted = _ensure_import_lines(lines, ensure_import[0], ensure_import[1])

    final_text = newline.join(lines)
    # Set the watch hash before writing so our own write doesn't read back as a
    # stale external change.
    FileWatch.set_hash_from_content(address.path, final_text, draw_state=address._watcher_ds)
    address.path.write_text(final_text, encoding="utf-8")

    if old_start is not None:
        resolved_old_end = old_end if old_end is not None else old_start + len(new_lines)
        new_end = old_start + len(new_lines)
        delta = new_end - resolved_old_end
        address.end = new_end
        if inserted:                          # import added above our span
            address.start = old_start + inserted
            address.end = new_end + inserted
        address._hash = address._compute_hash()
        shift_sibling_linenos(address.source, address.path,
                              after_lineno=resolved_old_end, delta=delta)
    else:
        address._hash = address._compute_hash()


def recompile_source(source, code_str, file_path):
    """Hotswap the edited code in place (no disk write) — do_recompile's dispatch."""
    result = None
    if isinstance(source, type):
        result = _recompile_class(source, code_str, str(file_path))
    elif isinstance(source, types.FunctionType):
        result =_recompile(source, code_str, str(file_path))
    elif isinstance(source, types.ModuleType):
        result = _recompile_module(source, code_str, str(file_path))

    return result



class TestClass:
    some_val = 66
    some_other_val = 49
    some = []
    # [tint=(0,0.2,1)]
    some_line = 2
    myfloat =  3.822
    some_tuple = (-15,1,1)
    class MyEnum(RelaxedEnum):
        SOME_VAL = 19
    
    aval = ProfileMode.OVERHEAD
    some_other = 0
    # [tint=(0.9069767594337463, 0.5192674398422241, 0.029529478400945663)]
    class NestedClass:
        so=0
    enum2 = MyEnum.SOME_VAL
    some_val = 66
    new_bool = True
    a_dict = {"x": -52, "y": 53}

def slow_task(**kwargs):
    import time
    print("Starting slow task...")
    time.sleep(1)
    print("Slow task completed.")
    return {"result": "This is the result of the slow task", "kwargs": kwargs}


@window()
@render_func(use_cache=True)
def editor_window():
    imgui.text("auto_save=False")
    code_file_io(TestClass, auto_save=False)
    return False, None


@window()
@render_func(use_cache=True)
def editor_window_2():
    imgui.text("auto_load_edits=True")
    code_file_io(TestClass, auto_load_edits=True)
    return False, None


@window()
@render_func(use_cache=True)
def editor_window_4():
    imgui.text("auto_load_edits=True")
    code_file_io(TestClass, auto_load_edits=True)
    return False, None

@window()
@render_func(use_cache=True)
def editor_window_3():
    imgui.text("auto_load_edits=False")
    imgui.text("auto_load=False")
    code_file_io(TestClass, auto_load_edits=False, auto_load=False)
    return False, None

class LoadingState:
    def __init__(self):
        self._loading = False
        self.cached_result = UNSET
        self.run_next = None
        self.pending_change = False
        self.error = None
        self._loading_start_frame = None



def load_file(input_value: Address, **kwargs) -> str:
    """Read the line span from disk."""
    data = input_value.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.split(newline)
    return newline.join(lines[input_value.start:input_value.end])

UNSET = object()
LOADING = object()

@render_func(use_cache=True, selectable=False, temp=True)
def run_in_background(input_value, loading_state: LoadingState,
                      draw_state, child_kwargs, start=False, timeout=20, **kwargs):
    if start:
        loading_state.run_next = input_value, child_kwargs
        draw_state.invalidate()
        request_render()

    if loading_state.run_next is not None:
        def run(run_next_inner):
            loading_state._loading = True
            value, background_kwargs = run_next_inner
            # Never run a @render_func WRAPPER on this background thread - the wrapper
            # mutates process-global Melty stacks (depth, unique_stack, ...) on
            # entry/exit, causing races the main render loop. Grab the bare inner
            # function; plain functions pass through unchanged.
            value = getattr(value, '__wrapped__', value)
            try:
                loading_state.cached_result = value(**background_kwargs)
            except Exception as exc:
                loading_state.error = exc
                print_stack_trace(exception=exc)
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
        self.recompiled_on_frame = None
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


class ConverterState:
    def __init__(self):
        self.output_value = None

@render_func()
def string_to_cst_module(input_value, **kwargs):
    cst_tree = cst.parse_module(input_value)
    return True, cst_tree

@render_func()
def cst_module_to_string(input_value, **kwargs):
    code_str = input_value.code
    return True, code_str

@window()
@render_func(use_cache=True)
def test_new_run_chain(input_value, **kwargs):

    chain = [
        class_to_address,
        load_file,
        string_to_cst_module
    ]
    changed, value = run_chain(TestClass, chain=chain)

    return False, None

def synchronous_run_chain(input_value, chain, route, chain_unique="", **kwargs):
    """Run the chain on run_in_background's worker thread.

    Each node is a @render_func, but we call its BARE inner function
    (func.__wrapped__) — NOT the wrapper. The wrapper mutates process-global
    Melty stacks (depth, unique_stack, mode_stack, melty_window_stack, ...) on
    entry/exit; running that here while the main loop keeps rendering races on
    that shared state and freezes/crashes the studio. The inner function is pure
    logic and is safe off the main thread. _converter_mode only elided imgui
    *drawing* — it never made the wrapper thread-safe.

    Nodes that want a draw_state (caching, FileWatch, _addr_cache) get a stable
    per-node one via get_draw_state, so their caches survive across runs just
    like the wrapper's would. Returns are normalized: a node may hand back
    (changed, value) or a bare value (plain converters like load_file)."""
    value = input_value
    to_route = {}

    for i, func in enumerate(chain):
        if isinstance(func, tuple):
            func, node_kwargs = func
        else:
            node_kwargs = {}

        # The bare function - skip the global-stack-mutating wrapper entirely.
        inner = getattr(func, '__wrapped__', func)
        # Stable key per (chain instance, node) so caches persist across runs and
        # don't clash with the main loop's shared draw_states.
        node_ds = get_draw_state(strhash(f"chainnode|{chain_unique}|{func.__name__}|{i}"))

        call_kwargs = dict(node_kwargs)
        call_kwargs['input_value'] = value
        call_kwargs['changed'] = True
        for arg_name, arg_val in to_route.values():
            call_kwargs[arg_name] = arg_val

        # Pass only what the func accepts (its real signature, sans the render
        # args the wrapper wants to inject). draw_state only if it wants one.
        sig = inspect.signature(inner)
        accepts_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        if 'draw_state' in sig.parameters or accepts_var_kw:
            call_kwargs['draw_state'] = node_ds
        if not accepts_var_kw:
            call_kwargs = {k: v for k, v in call_kwargs.items() if k in sig.parameters}

        try:
            result = inner(**call_kwargs)
        except Exception as e:
            return e

        # A node may return (changed, value) or a bare value.
        if isinstance(result, tuple) and len(result) == 2:
            changed, value = result
        else:
            changed, value = True, result

        if route is not None and func in route:
            arg_name = route[func]
            to_route[arg_name] = arg_name, value

    return value

@render_func(use_cache=False, selectable=False, temp=True)
def run_chain(input_value,  chain, unique, changed=False, draw_state=None, converter_state: ConverterState=None,
              route=None, **kwargs):
    if converter_state.output_value == UNSET or converter_state.output_value is LOADING:
         converter_state.output_value = None
    start = changed

    child_kwargs = {}
    child_kwargs['input_value'] = input_value
    child_kwargs["chain"] = chain
    child_kwargs["route"] = route
    child_kwargs["chain_unique"] = unique
    child_kwargs["changed"] = True

    finished, result = run_in_background(synchronous_run_chain, start=start, child_kwargs=child_kwargs)
    if isinstance(result, Exception):
        return result, converter_state.output_value

    if result == LOADING:
        return False, converter_state.output_value

    if finished:
        converter_state.output_value = result
        return True, converter_state.output_value


    return False, converter_state.output_value


@render_func(use_cache=True, fill_height=True, selectable=False, temp=True)
def code_file_io_wrapped(input_value, view_func=None, changed=False, **kwargs):

    chain = [
        string_to_cst_module,
        cst_module_to_dict,
    ]

    chain_out = [
        dict_to_cst_module,
        cst_module_to_string
    ]


    # if view_func is not None:
    #     _, converted_value = run_chain(input_value, changed=changed, name="code_convert_in", chain=chain)
    #     if isinstance(converted_value, Exception):
    #         imgui.text(f"Error: {str(converted_value)}")
    #         return changed, input_value
    #     dict_changed, new_dict = view_func(converted_value, draw=changed)
    #     _, to_str_value = run_chain(new_dict, changed=dict_changed, name="code_convert_out", chain=chain_out)
    #     if isinstance(to_str_value, Exception):
    #         imgui.text(f"Error: {str(to_str_value)}")
    #         return changed, input_value

    text_changed, value = RenderFuncs.draw_text(input_value=input_value, column=0)


    result, dict_val = run_chain(value, changed=text_changed or changed, name="code_convert_in", chain=chain)
    if dict_val is None:
        return False, None

    if isinstance(result, Exception):
        imgui.text_colored(f"Error: {str(result)}", 1.0, 0.4, 0.0)
        # return text_changed, value

    dict_changed, new_dict = RenderFuncs.draw_collection(dict_val, column=1, draw=changed)

    result, to_str_value = run_chain(new_dict, changed=dict_changed, name="code_convert_out", chain=chain_out)
    if isinstance(changed, Exception):
        imgui.text_colored(f"Error: {str(changed)}", 1.0, 0.4, 0.0)
        # return False, to_str_value

    if dict_changed:
        return True, to_str_value

    elif text_changed:
        return True, value

    return False, value

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  editable_source - the whole round-trip, one function                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
@render_func(use_cache=True, selectable=False, searchable=True, disable_scroll=False, temp=True)
def code_file_io(input_value, code_state: CodeState, view_func=RenderFuncs.draw_text, auto_load=True, auto_load_edits=False,
                 child_kwargs=None, draw_state=None, auto_save=True, auto_recompile_edits=False, save=False, load=False, recompile=False,
                 ensure_import=None, s_key_pressed=None, enter_key_pressed=None, unique=None, **kwargs):
    try:
        if child_kwargs is None:
            child_kwargs = {}
        # ── 1. Resolve the source's line span ─────────────────────────────────────
        address = _resolve_address(input_value, draw_state)
        code_state.address = address
        top_line_height = 24
        external_change = False

        if address is None:
            imgui.text_colored(
                f"editable_source: can't resolve source for {type(input_value).__name__}",
                1.0, 0.4, 0.0)
            return False, None

        if auto_load:
            if draw_state.frame_count < 1:
                load = True
                code_state.text_cache = None
                code_state.mark_file_current()

        if not auto_recompile_edits and code_state.text_cache is not UNSET and code_state.text_cache is not None:
            play_icon = "\uf04b"
            recompile = RenderFuncs.button(f"{play_icon} Run", tint=(0, 0.4, 0.1), height=top_line_height, name="recompile_btn")[0]
            imgui.same_line()

        if code_state.recompiled_on_frame is not None:
            duration = 10
            recompiled_on = Melty.frame_count - code_state.recompiled_on_frame
            fade_out = min(1.0, max(0.0, 2.0 - (max(0, recompiled_on) / duration)))
            if fade_out >= 0:
                imgui.same_line()
                checkmark_icon_fa = "\uf00c"
                imgui.text_colored(f"{checkmark_icon_fa}", 0.0, 1.0, 0.0, fade_out)
                draw_state.invalidate()
                request_render()

            if code_state.recompile_result is not None:
                imgui.same_line()
                imgui.text_colored(f"{str(code_state.recompile_result)}", 1.0, 0.4, 0.0)


        if code_state.is_file_stale() and not code_state.pending_save:
            imgui.same_line()
            if auto_load_edits:
                load = True
                code_state.mark_file_current()
            else:
                imgui.same_line()
                if RenderFuncs.button("Load", width=100, height=top_line_height, name=f"reload")[0]:
                    load = True

                if not code_state.pending_save:
                    imgui.same_line()
                    if RenderFuncs.button("Keep mine", width=100, height=top_line_height, name=f"keepmine")[0]:
                        save = True

        if not auto_save and code_state.pending_save:
            imgui.same_line()
            if RenderFuncs.button("Save", width=100, height=top_line_height, name=f"save")[0]:
                save = True

        changed, new_text = run_in_background(load_file,
                                              child_kwargs={"input_value": address},
                                              name=f"load", start=load)
        if new_text is LOADING:
            code_state.mark_file_current()

        elif changed:
            code_state.text_cache = new_text
            code_state.mark_file_current()
            draw_state.invalidate_up(max_depth=3)
            code_state.pending_save = False
            external_change = True
            request_render()

        # ── 3. Edit - the actual call ─────────────────────────────────────────────
        if code_state.text_cache is not UNSET and code_state.text_cache is not None:
            imgui.set_cursor_screen_pos((draw_state.abs_left,
                                         draw_state.abs_top +
                                         draw_state.header_height +
                                         top_line_height))

            child_kwargs['jump_to'] = address
            edited, value = code_file_io_wrapped(input_value=code_state.text_cache, changed=external_change, **child_kwargs)
            #
            # edited, value = code_file_io_wrapped(input_value=code_state.text_cache,
            #                                      changed=external_change, **child_kwargs)

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
        # the disk disk write.
        save_start = (auto_save and edited) or save_hotkey or save
        saved, result = run_in_background(_write_span,
                                     child_kwargs={"address": address,
                                                   "code_str": code_state.text_cache,
                                                   "ensure_import": ensure_import},
                                     name="save", start=save_start)
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
            code_state.recompiled_on_frame = None
        elif result == UNSET:
            pass
        else:
            if changed:
                code_state.recompiled_on_frame = Melty.frame_count
                record_compile(address)
                Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10)
                request_render()
            code_state.recompile_result = result


    except Exception as e:
        imgui.text_colored(f"editable_source error: {e}", 1.0, 0.4, 0.0)

    # This is the root function, end of the line.
    return False, None
