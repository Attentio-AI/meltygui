"""
Chain-compatible converter nodes for the unified render pipeline.

Each function is a @render_func that:
  - Owns its own file I/O (no external load_data / save_data)
  - Manages pending UI (Load / Save / Revert buttons) via draw_state
  - Returns (changed, value) like every other chain node

These are NEW functions — the old converters in file_converters.py
and libcst_conversion.py stay untouched for backward compat.
"""
import inspect
import threading
import time
import tokenize
import types
from pathlib import PosixPath, Path

import imgui
import libcst as cst

from src.lsd.gl_gui.melty import FileWatch, Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending, PendingState
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_conversion.address import (
    Address, to_address, update_address_cache, _evict_linecache,
    shift_sibling_linenos,
)
from src.lsd.gl_gui.view.core_conversion.file_converters import (
    _detect_newline, _recompile, _recompile_class, _recompile_module,
)
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    cst_module_to_dict, dict_to_cst_module, GeneralParse,
)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Load node: class → cst.Module                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _load_span(ref: Address) -> str:
    """Read the line span from disk."""
    data = ref.path.read_bytes()
    newline = _detect_newline(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.split(newline)
    return newline.join(lines[ref.start:ref.end])


@render_func(use_cache=True)
def chain_cls_load(input_value, draw_state=None):
    """Load node: class → cst.Module.

    - Resolves Address from the class on first call
    - Watches file mtime for external changes
    - Shows Load / Revert buttons when file changes on disk
    - Returns (True, cst.Module) when loaded, (False, cached) otherwise
    """
    # ── Resolve ref on first encounter ────────────────────────
    if draw_state._address is None:
        ref = to_address(input_value)
        if ref is None:
            return False, input_value
        draw_state._address = ref
        draw_state._original_input_ref = input_value

    ref = draw_state._address

    # ── First load ────────────────────────────────────────────
    if not hasattr(draw_state, '_loaded_text') or draw_state._loaded_text is None:
        text = _load_span(ref)
        draw_state._loaded_text = text
        draw_state._loaded_cst = cst.parse_module(text)
        draw_state.mark_file_current()
        return True, draw_state._loaded_cst

    imgui.text("chain_cls_load: ")

    # ── File change detection ─────────────────────────────────
    if draw_state.is_file_stale():
        # Refresh address from cache (another view may have changed line count)
        fresh_ref = to_address(input_value)
        if fresh_ref is not None:
            draw_state._address = fresh_ref
            ref = fresh_ref

        imgui.text("File changed on disk")
        if imgui.button("Load##chain_load"):
            text = _load_span(ref)
            draw_state._loaded_text = text
            draw_state._loaded_cst = cst.parse_module(text)
            draw_state.mark_file_current()
            return True, draw_state._loaded_cst

        imgui.same_line()
        if imgui.button("Revert##chain_load"):
            # Return the last loaded cst - no file I/O
            draw_state.mark_file_current()
            return True, draw_state._loaded_cst

    # ── Steady state ──────────────────────────────────────────
    return False, draw_state._loaded_cst


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Save chain: cst.Module → class (save to disk + hotswap)                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@render_func(interrupt_source_for=Path)
def path_to_address(input_value: Path):
    """Job of interrupt source is to return True when the input has changed on disk"""
    address = Address(input_value)
    return False, address


@render_func()
def function_to_address(input_value: types.FunctionType, draw_state, changed=False):
    """Extract Address from a function object."""

    unwrapped = inspect.unwrap(input_value)
    source_file = inspect.getfile(unwrapped)
    FileWatch.register_draw_state(draw_state, Path(source_file))

    # inspect.getsourcelines reads + tokenizes the whole file (and _evict_linecache
    # forces a fresh read), so it ran every frame while typing. Cache the resolved
    # Address and re-resolve only when the input or the file's mtime changes:
    # typing doesn't write the file, so it's a cache hit; a save bumps mtime and
    # we re-resolve once with fresh line numbers.
    try:
        mtime = Path(source_file).stat().st_mtime
    except OSError:
        mtime = None
    cached = getattr(draw_state, '_addr_cache', None)
    if cached is not None and cached[0] is input_value and cached[1] == mtime:
        return changed, cached[2]

    _evict_linecache(source_file)
    try:
        source_lines, start_lineno = inspect.getsourcelines(unwrapped)
    except (OSError, TypeError, tokenize.TokenError, SyntaxError) as e:
        print(f"Could not get source lines for {input_value.__name__} in {source_file}: {e}")
        return changed, None

    address = Address(Path(source_file), start_lineno - 1,
                      start_lineno - 1 + len(source_lines), source=input_value,
                      watcher_ds=draw_state)
    draw_state._addr_cache = (input_value, mtime, address)
    return changed, address


@render_func()
def module_to_address(input_value: types.ModuleType, draw_state, changed=False):
    if changed:
        source_file = Path(input_value.__file__)
        FileWatch.register_draw_state(draw_state, source_file)
        if changed:
            pass

        return changed, Address(source_file, source=input_value, watcher_ds=draw_state)
    else:
        return changed, None


########################################### CHAIN START
@render_func()
def class_to_address(input_value: type, draw_state, changed=False):
    if changed:
        if input_value.__module__ in ('builtins', '_collections_abc'):
            return False, None
        try:
            import inspect
            source_file = inspect.getfile(input_value)
            FileWatch.register_draw_state(draw_state, Path(source_file))

            # See function_to_address: cache the getsourcelines result by
            # (input, file mtime) so typing doesn't re-read+tokenize the file.
            try:
                mtime = Path(source_file).stat().st_mtime
            except OSError:
                mtime = None
            cached = getattr(draw_state, '_addr_cache', None)
            if cached is not None and cached[0] is input_value and cached[1] == mtime:
                return changed, cached[2]

            _evict_linecache(source_file)
            source_lines, start_lineno = inspect.getsourcelines(input_value)
            address = Address(Path(source_file), start_lineno - 1,
                              start_lineno - 1 + len(source_lines), source=input_value,
                              watcher_ds=draw_state)
            draw_state._addr_cache = (input_value, mtime, address)
            return changed, address
        except (TypeError, OSError, tokenize.TokenError, SyntaxError):
            return changed, None
    else:
        return changed, None


@render_func(background=True)
def load_cst_module(input_value: Address):
    text = _load_span(input_value)
    converted_cst = cst.parse_module(text)
    general_parse = cst_module_to_dict(converted_cst) 
    general_parse.address = input_value
    general_parse.file_path = input_value.path

    if Toggles.slow_down_threads:
        for i in range(5):
            import time
            time.sleep(0.1)
            print(f"Simulating slow load... {i + 1}/5")

    return True, general_parse


########################
# draw_collection
########################

# Last successful recompile time per Address, for the "fresh" indicator next
# to the file-load button in address_to_general_parse. Keyed by Address: the
# same Address object flows through the chain (address → general_parse →
# address), so the store side (general_parse_to_address) and the display side
# (address_to_general_parse) agree on the key.
_last_compile_times: dict = {}


def record_compile(address):
    """Stamp `address` as compiled just now."""
    _last_compile_times[address] = time.time()


def get_compile_time(address):
    """Last compile time for `address`, or None if never compiled this session."""
    return _last_compile_times.get(address)


@render_func(background=False)
def do_recompile(input_value, code_str, file_path, changed=False):
    """Dispatch recompile to the right handler based on source type."""
    if isinstance(input_value, type):
        _recompile_class(input_value, code_str, str(file_path))
    elif isinstance(input_value, types.FunctionType):
        _recompile(input_value, code_str, str(file_path))
    elif isinstance(input_value, types.ModuleType):
        _recompile_module(input_value, code_str, str(file_path))
    else:
        print(f"Unknown source type {type(input_value).__name__}, skipping recompile")
    return True, None


@render_func(background=True)
def _do_save(input_value, code_str):
    """Write code_str back into the file at the Address's span."""
    full_data = input_value.path.read_bytes()
    newline = _detect_newline(full_data)
    try:
        text = full_data.decode("utf-8")
    except UnicodeDecodeError:
        text = full_data.decode("latin-1")
    lines = text.split(newline)
    new_lines = code_str.split(newline)

    old_start = input_value.start
    old_end = input_value.end

    lines[old_start:old_end] = new_lines
    final_text = newline.join(lines)
    FileWatch.set_hash_from_content(input_value.path, final_text, draw_state=input_value._watcher_ds)

    try:
        input_value.path.write_text(final_text, encoding="utf-8")

        if old_start is not None:
            resolved_old_end = old_end if old_end is not None else old_start + len(new_lines)
            new_end = old_start + len(new_lines)
            delta = new_end - resolved_old_end

            input_value.end = new_end
            input_value._hash = input_value._compute_hash()

            shift_sibling_linenos(input_value.source, input_value.path,
                                  after_lineno=resolved_old_end, delta=delta)
        else:
            input_value._hash = input_value._compute_hash()

        if Toggles.slow_down_threads:
            for i in range(5):
                import time
                time.sleep(0.1)
                print(f"Simulating slow load... {i + 1}/5")
    except Exception as e:
        imgui.text_colored(f"Error saving file: {e}", 1.0, 0.0, 0.0)

    return True, input_value


@render_func(background=True)
def dict_to_cst(input_value, changed=False):
    back_to_cst = dict_to_cst_module(input_value)
    if Toggles.slow_down_threads:
        for i in range(5):
            import time
            time.sleep(0.1)
            print(f"Simulating slow load... {i + 1}/5")

    return False, back_to_cst


@render_func(use_cache=True, selectable=False)
def run_button(input_value: any, with_kwargs=None, draw_state=None, clicked=False):
    is_render_func = hasattr(input_value, "__render_func__")
    if not is_render_func:
        imgui.text_colored(f"Value of type {type(input_value).__name__} needs @render_func",
                           1.0, 0.5, 0.0)
        return False, None
    if with_kwargs is None:
        with_kwargs = {}

    if hasattr(input_value, "__header_defaults__"):
        run_in_background = input_value.__header_defaults__.get("background", False)
    else:
        run_in_background = True

    running = draw_state._running is input_value if run_in_background else False

    fa_run_arrow = ""
    from src.lsd.gl_gui.view.core_views.new_core_view import button
    if clicked or running or button(f"{fa_run_arrow} {input_value.__name__}##{draw_state.unique}",
     height=30, draw=True, value=0.4, saturation=1.5)[0]:
        with_kwargs['changed'] = True
        changed, value = input_value(**with_kwargs)
        if isinstance(value, Pending):
            draw_state._running = input_value
            return False, None

        draw_state._running = False
        return True, (changed, value)

    return False, None


@render_func(use_cache=True)
def address_to_general_parse(input_value: Address, pending=False, unique=None, changed=False, draw_state=None, auto_load=True, load=False):
    """Load node: class → cst.Module.

    - Resolves Address from the class on first call
    - Watches file mtime for external changes
    - Shows Load / Revert buttons when file changes on disk
    - Returns (True, cst.Module) when loaded, (False, cached) otherwise
    """
    if draw_state.frame_count < 2 and auto_load:
        load = True

    from src.lsd.gl_gui.view.core_views.new_core_view import button
    file_name = input_value.path.name if input_value.path is not None else "Unknown file"
    folder_icon = ""
    if button(f"{folder_icon} {file_name}", height=30, draw=True, value=0.4, saturation=1.5)[0]:
        from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
        line_number = input_value.start + 1 if input_value.start is not None else None
        threading.Thread(
            target=open_in_intellij,
            args=(str(input_value.path),),
            kwargs={"line_number": line_number},
            daemon=True,
        ).start()

    # Last-compiled indicator: shows the wall-clock time of the most recent
    # recompile for this file (Ctrl+Enter or the do_recompile button).
    compile_time = get_compile_time(input_value)
    if compile_time is not None:
        check_icon = ""
        imgui.same_line()
        imgui.align_text_to_frame_padding()
        imgui.text_colored(f"{check_icon} compiled {time.strftime('%H:%M:%S', time.localtime(compile_time))}",
                           0.55, 0.8, 0.55, 1.0)

    if pending or changed:
        clicked, result = run_button(load_cst_module, with_kwargs={"input_value": input_value},
                                     clicked=load, name=f"load_cst_module{unique}")
        if clicked:
            return result

    # ── Steady state ──────────────────────────────────────────
    return False, None

@render_func(use_cache=True)
def general_parse_to_address(input_value: GeneralParse, pending=False, draw_state=None, unique=None,
                             changed=False, recompile=False, save=False, s_key_pressed=None,
                             enter_key_pressed=None):
    """GeneralParse dict → Address. Handles recompile and save for any source type."""
    address = input_value.address
    if not isinstance(address, Address):
        imgui.text_colored("Saving unavailable...\ninput_value.address is not set",
                           1.0, 0.0, 0.0)
        return False, None
    source = address.source

    show_recompile = True
    show_save = not save or (pending or changed)

    save_hotkey = s_key_pressed and s_key_pressed.ctrl
    if save_hotkey:
        _do_save(address, code_str=input_value.source)
        Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)

    # Ctrl+Enter: hotswap the edited code without writing to disk. Mirrors the
    # Ctrl+S save hotkey above, but routes through do_recompile instead.
    recompile_hotkey = enter_key_pressed and enter_key_pressed.ctrl
    if recompile_hotkey and source is not None:
        do_recompile(input_value=source, code_str=input_value.source, file_path=address.path)
        record_compile(address)
        Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)

    # Skip expensive dict→CST conversion when neither block will execute
    if not show_recompile and not show_save:
        return False, address

    convert_finished, back_to_cst = dict_to_cst(input_value=input_value, changed=changed)
    if isinstance(back_to_cst, Pending):
        return False, back_to_cst
    code_str = back_to_cst.code
    if show_recompile:
        if source is not None:
            recompiled, _ = run_button(do_recompile, clicked=recompile and pending, name=f"do_recompile{unique}",
                        with_kwargs={"input_value": address.source,
                                 "code_str": code_str,
                                 "file_path": address.path})
            if recompiled:
                record_compile(address)
                # Refresh the cached file path node so its compiled indicator updates.
                Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)

            imgui.dummy(1,1)
    if show_save:
        if source is not None:
            clicked, result = run_button(_do_save, with_kwargs={"input_value": address,
                                         "code_str": code_str},
                                         clicked=save)
            if clicked:
                if draw_state.parent_window is not None:
                    Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)
                    return True, address
    return False, address


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                                focus: a reversible lens node                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _focus_get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _focus_set(obj, key, value):
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)


@render_func(use_cache=False, show_bg=False, selectable=False, show_name=False,
             with_header=None)
def focus(input_value, path=(), default=None, draw_state=None, changed=False, **kwargs):
    """Descend a STATIC key `path` into `input_value` to a single leaf, render
    that leaf with its normal renderer (draw_tuple, for a tint), and write any
    edit back into the container *in place*.

    This is the one primitive that turns the existing reversible converter pairs
    (class_to_address ↔ address_to_class, address_to_general_parse ↔
    general_parse_to_address) into full read/write lenses: drop `focus` where
    `draw_collection` would sit in a chain and it edits just the focused leaf.

    Contract — identical to every other chain node: returns (changed, value).
      - read   : leaf exists → render the picker; (False, input_value) until edited.
      - write  : on a picker edit, mutate container[path] and return (True, input_value)
                 so a downstream save node runs once.
      - add    : leaf missing → show a "+ Add" button; clicking creates the leaf
                 (and any missing intermediate dicts) with `default` and returns
                 (True, input_value), so the same save node persists the new value
                 (e.g. writes a fresh `# [tint=(...)]` comment for a code source).

    `path` is a constant supplied by the lens definition — it is NEVER stored in
    draw_state (which is GC'd on a short TTL). The only transient thing is
    `input_value`, the container reference flowing through the chain; it lives for
    this frame only. The reverse direction works because we keep the parent
    container in the value — the same move Address makes by carrying `.source`.

    Handles dict-key and attribute access uniformly, so the same node focuses a
    GeneralParse dict (code-comment / decoration tint), a draw_state, or a data
    class instance.
    """
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_tuple, button

    if not path:
        imgui.text_colored("focus: empty path", 1.0, 0.4, 0.0)
        return False, input_value

    leaf_key = path[-1]

    # Walk to the leaf's parent; note where (if anywhere) the chain breaks so the
    # add path knows it many containers to create.
    parent = input_value
    reachable = True
    for key in path[:-1]:
        nxt = _focus_get(parent, key)
        if nxt is None:
            reachable = False
            break
        parent = nxt

    leaf = _focus_get(parent, leaf_key) if reachable else None

    # ── Present: the reusable picker (every lens funnels through this) ─────────
    if leaf is not None:
        tint_changed, new_tint = draw_tuple(leaf, name=str(leaf_key))
        if tint_changed:
            _focus_set(parent, leaf_key, new_tint)
            return True, input_value  # hand the parent container to the save side
        return False, input_value

    # ── Absent: offer to create it ───────────────────────────────────────────────
    if button(f" Add {leaf_key}", height=26)[0]:
        node = input_value
        for key in path[:-1]:           # create missing intermediate containers
            child = _focus_get(node, key)
            if child is None:
                child = {}
                _focus_set(node, key, child)
            node = child
        _focus_set(node, leaf_key, default if default is not None else (0.485, 0.61, 0.76))
        return True, input_value
    return False, input_value


@render_func(use_cache=True)
def address_to_class(input_value, changed=False, draw_state=None):
    pass


@render_func(use_cache=True)
def address_to_function(input_value, changed=False, draw_state=None):
    pass


@render_func(use_cache=True)
def address_to_module(input_value, changed=False, draw_state=None):
    pass


@render_func(use_cache=True)
def cst_to_address(input_value, changed=False, draw_state=None):
    pass


@render_func(use_cache=True)
def general_parse_to_str(input_value, changed=False, draw_state=None):
    """Convert cst.Module → source string. Pure converter, no UI."""
    if isinstance(input_value, GeneralParse):
        return changed, input_value.source
    else:
        return False, None


@render_func(background=True)
def parse_source_to_general(input_value):
    """Parse an edited source string back into a GeneralParse, on a background
    thread. cst.parse_module + cst_module_to_dict are O(buffer); running them
    inline on str_to_general_parse blocked every keystroke. Mirrors CODE_UI's
    load_cst_module. A syntax error mid-edit raises here and surfaces as a
    PendingState.ERROR (Background.run catches it)."""
    cst_module = cst.parse_module(str(input_value))
    return True, cst_module_to_dict(cst_module)


# Wait this long after the last edit before parsing. A full-buffer parse is
# CPU-bound Python (cst.parse_module + cst_module_to_dict), so doing it per
# keystroke blocks the next frame whether it runs inline or on a GIL-bound
# pool thread. The parse only feeds save/round-trip, not the displayed text, so
# deferring it until typing settles keeps keystrokes smooth.
_PARSE_DEBOUNCE_S = 0.1


@render_func(use_cache=True)
def str_to_general_parse(input_value, reference=None, changed=False, draw_state=None):
    input_str = str(input_value)
    has_ref = isinstance(reference, GeneralParse)

    # Debounce the parse: while the text is still changing (or hasn't been
    # stable for _PARSE_DEBOUNCE_S), keep showing the prior parse and don't
    # touch the UI. request_render keeps frames coming until the timer elapses.
    if input_str != getattr(draw_state, '_parse_pending_str', None):
        draw_state._parse_pending_str = input_str
        draw_state._parse_pending_at = time.monotonic()
        request_render()
        return False, reference if has_ref else None
    if (input_str != getattr(draw_state, '_last_propagated_str', None)
            and time.monotonic() - getattr(draw_state, '_parse_pending_at', 0.0) < _PARSE_DEBOUNCE_S):
        request_render()
        return False, reference if has_ref else None

    # Parse the edited text back to a parse tree off the main thread.
    # `reference` is the chain's cached prior parse for this position; we keep
    # it flowing while the background parse is pending or the edit is unstable,
    # so downstream save/recompile always get a valid (last-good) parse.
    _c, general_parse = parse_source_to_general(input_value=input_str)

    if isinstance(general_parse, Pending) or general_parse is None:
        if isinstance(general_parse, Pending) and general_parse.state == PendingState.ERROR:
            # Incomplete/invalid syntax mid-edit: keep the edited text live and
            # surface the error; don't push a broken parse downstream.
            if has_ref:
                reference.source = input_str
            imgui.text_colored("Error parsing code", 1.0, 0.0, 0.0)
        # Background still running (or nothing changed): keep the previous parse.
        return False, reference if has_ref else None

    if not has_ref:
        return False, general_parse
    general_parse.address = reference.address
    # Propagate changed=True once, on the frame a new parse first lands, so
    # downstream save/recompile can notice it without re-firing every frame.
    fresh = getattr(draw_state, '_last_propagated_str', None) != input_str
    draw_state._last_propagated_str = input_str
    return fresh, general_parse






