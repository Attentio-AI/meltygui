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
from pathlib import Path

import imgui
import libcst as cst

from src.lsd.gl_gui.melty import FileWatch, Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.address import (
    Address, _evict_linecache, shift_sibling_linenos,
)
from src.lsd.gl_gui.view.core_conversion.chain_converters import (
    _load_span, _ensure_import_lines, record_compile,
)
from src.lsd.gl_gui.view.core_conversion.file_converters import (
    _detect_newline, _recompile, _recompile_class, _recompile_module,
)
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    cst_module_to_dict,
)
from src.lsd.gl_gui.view.core_views.core_render import render_func
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
    if isinstance(source, type):
        _recompile_class(source, code_str, str(file_path))
    elif isinstance(source, types.FunctionType):
        _recompile(source, code_str, str(file_path))
    elif isinstance(source, types.ModuleType):
        _recompile_module(source, code_str, str(file_path))


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

    def is_file_stale(self):
        if self.address is None:
            return False
        try:
            s = self.address.path.stat()
            return s.st_mtime != self.file_mtime or s.st_size != self.file_size
        except OSError:
            return True

    def mark_file_current(self):
        print("marking file current")
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
        cst_tree = cst.parse_module(self.text_cache)
        self.code_tree_cache = cst_module_to_dict(cst_tree)


class TestClass:
    some_val = 123
    new_bool = False
    a_dict = {"x": 1, "y": 2}


def slow_task(**kwargs):
    import time
    print("Starting slow task...")
    time.sleep(1)
    print("Slow task completed.")
    return {"result": "This is the result of the slow task", "kwargs": kwargs}


@window()
@render_func(use_cache=True)
def editor_window():

    code_file_io(TestClass)
    return False, None


class LoadingState:
    def __init__(self):
        self._loading = False
        self.cached_result = UNSET
        self.run_next = None
        self.pending_change = False
        self.error = None

def load_file(ref: Address) -> str:
    """Read the line span from disk."""
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.split(newline)
    return newline.join(lines[ref.start:ref.end])

UNSET = object()

@render_func(use_cache=True, selectable=False, temp=True)
def run_in_background(input_value, loading_state: LoadingState, draw_state, child_kwargs, start=False, **kwargs):
    if start:
        loading_state.run_next = input_value, child_kwargs
        draw_state.invalidate()
        request_render()

    if loading_state.run_next is not None:
        if not loading_state._loading:
            def run(run_next):
                value, background_kwargs = run_next
                try:
                    loading_state.cached_result = value(**background_kwargs)
                except Exception as exc:
                    loading_state.error = exc
                finally:
                    loading_state._loading = False
                    loading_state.pending_change = True
                    request_render()

            if Melty.frame_count < 4:
                run(run_next=loading_state.run_next)
            else:
                threading.Thread(target=run, kwargs={"run_next":loading_state.run_next}).start()
                loading_state.run_next = None
                loading_state._loading = True


    if loading_state._loading:
        return False, loading_state.cached_result

    if loading_state.pending_change:
        loading_state.pending_change = False
        draw_state.invalidate_up(max_depth=20)
        request_render()
        return True, loading_state.cached_result
    else:
        return False, loading_state.cached_result


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  editable_source - the whole round-trip, one function                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
@render_func(use_cache=True, selectable=False, searchable=True, disable_scroll=False, temp=True)
def code_file_io(input_value, code_state: CodeState, view_func=RenderFuncs.draw_text, auto_load=True,
                 child_kwargs=None, draw_state=None, save=True, load=False, recompile=False, ensure_import=None,
                    s_key_pressed=None, enter_key_pressed=None, unique=None, **kwargs):

    try:
        # ── 1. Resolve the source's line span ─────────────────────────────────────
        address = _resolve_address(input_value, draw_state)
        code_state.address = address

        if address is None:
            imgui.text_colored(
                f"editable_source: can't resolve source for {type(input_value).__name__}",
                1.0, 0.4, 0.0)
            return False, None

        if auto_load:
            if code_state.text_cache is UNSET:
                load = True
                code_state.text_cache = None

        elif code_state.is_file_stale():
            imgui.text_colored(" file changed on disk", 1.0, 0.8, 0.3)
            if RenderFuncs.button("Load", width=100, height=24, name=f"reload")[0]:
                load = True
            imgui.same_line()
            if RenderFuncs.button("Keep mine", width=100, height=24, name=f"keepmine")[0]:
                code_state.mark_file_current()

        changed, new_text = run_in_background(load_file,
                                              child_kwargs={"ref": address},
                                              name=f"load", start=load)
        if changed:
            code_state.text_cache = new_text
            code_state.parse_cst()
            code_state.mark_file_current()
            draw_state.invalidate_up(max_depth=10)
            request_render()

        # ── 3. Edit - the actual call ─────────────────────────────────────────────
        changed, value = view_func(input_value=code_state.text_cache,
                                   jump_to=address, **(child_kwargs or {}))
        if changed:
            code_state.text_cache = value
            code_state.parse_cst()
            code_state.mark_file_stale()

        # ── 4. Save / recompile - synchronous, only on a real edit ────────────────
        save_hotkey = bool(s_key_pressed and s_key_pressed.ctrl)
        recompile_hotkey = bool(enter_key_pressed and enter_key_pressed.ctrl)

        if (save and changed) or save_hotkey:
            _write_span(address, code_state.text_cache, ensure_import)
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10)
            code_state.mark_file_current()

        if RenderFuncs.button("Recompile", height=24, name=f"recompile")[0]:
            recompile = True
            changed = True

        if (recompile and changed) or recompile_hotkey:
            print(f"Recompiling... {code_state.text_cache}")
            recompile_source(input_value, code_state.text_cache, address.path)
            record_compile(address)
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10)
    except Exception as e:
        imgui.text_colored(f"editable_source error: {e}", 1.0, 0.4, 0.0)

    # This is the root function, end of the line.
    return False, None
