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
import tokenize
import types
from pathlib import PosixPath, Path

import imgui
import libcst as cst

from src.lsd.gl_gui.melty import FileWatch, Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
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
    _evict_linecache(source_file)
    FileWatch.register_draw_state(draw_state, Path(source_file))

    if changed:
        print(f"function_to_address: input changed for {input_value.__name__}, checking file {source_file}")
    try:
        source_lines, start_lineno = inspect.getsourcelines(unwrapped)
    except (OSError, TypeError, tokenize.TokenError, SyntaxError) as e:
        print(f"Could not get source lines for {input_value.__name__} in {source_file}: {e}")
        return changed, None

    return changed, Address(Path(source_file), start_lineno - 1,
                            start_lineno - 1 + len(source_lines), source=input_value,
                            watcher_ds=draw_state)


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
        print(f"CLASS TO ADDRESS: FILE WATCH INPUT -- CHANGED")

        if input_value.__module__ in ('builtins', '_collections_abc'):
            return False, None
        try:
            import inspect
            source_file = inspect.getfile(input_value)
            FileWatch.register_draw_state(draw_state, Path(source_file))
            _evict_linecache(source_file)
            source_lines, start_lineno = inspect.getsourcelines(input_value)
            return changed, Address(Path(source_file), start_lineno - 1,
                                    start_lineno - 1 + len(source_lines), source=input_value,
                                    watcher_ds=draw_state)
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

@render_func(background=False)
def do_recompile(input_value, code_str, file_path, changed=False):
    print(f"do_recompile: {changed}")
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


@render_func(use_cache=True)
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
def address_to_general_parse(input_value: Address, pending=False, changed=False, draw_state=None, load=False):
    """Load node: class → cst.Module.

    - Resolves Address from the class on first call
    - Watches file mtime for external changes
    - Shows Load / Revert buttons when file changes on disk
    - Returns (True, cst.Module) when loaded, (False, cached) otherwise
    """
    if pending or changed:
        clicked, result = run_button(load_cst_module, with_kwargs={"input_value": input_value},
                                     clicked=load)
        if clicked:
            return result

    # ── Steady state ──────────────────────────────────────────
    return False, None

@render_func(use_cache=True)
def general_parse_to_address(input_value: GeneralParse, pending=False, draw_state=None,
                             changed=False, recompile=False, save=False):
    """GeneralParse dict → Address. Handles recompile and save for any source type."""
    address = input_value.address
    if not isinstance(address, Address):
        imgui.text_colored("Saving unavailable...\ninput_value.address is not set",
                           1.0, 0.0, 0.0)
        return False, None
    source = address.source

    show_recompile = not recompile or (pending or changed)
    show_save = not save or (pending or changed)

    # Skip expensive dict→CST conversion when neither block will execute
    if not show_recompile and not show_save:
        return False, address

    convert_finished, back_to_cst = dict_to_cst(input_value=input_value, changed=changed)
    if isinstance(back_to_cst, Pending):
        return False, back_to_cst
    code_str = back_to_cst.code
    if show_recompile:
        if source is not None:
            run_button(do_recompile, clicked=recompile and pending, name="do_recompile",
                        with_kwargs={"input_value": address.source,
                                 "code_str": code_str,
                                 "file_path": address.path})

            imgui.dummy(1,1)
    if show_save:
        if source is not None:
            clicked, result = run_button(_do_save, with_kwargs={"input_value": address,
                                         "code_str": code_str},
                                         clicked=save)
            if clicked:
                Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)
                return True, address
    return False, address


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


@render_func(use_cache=True)
def str_to_general_parse(input_value, reference=None, changed=False, draw_state=None):
    input_str = str(input_value)
    if isinstance(reference, GeneralParse):
        if changed:
            try:
                cst_module = cst.parse_module(input_str)
                general_parse = cst_module_to_dict(cst_module)
                general_parse.address = reference.address
                return True, general_parse
            except Exception as e:
                reference.source = input_str
                imgui.text_colored(f"Error parsing code: {e}", 1.0, 0.0, 0.0)
                return True, reference
        else:
            return False, None
    else:
        cst_module = cst.parse_module(input_str)
        general_parse = cst_module_to_dict(cst_module)
        return False, general_parse






