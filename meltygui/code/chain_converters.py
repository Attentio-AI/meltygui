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
import types
from pathlib import PosixPath, Path

import imgui
import libcst as cst

from src.lsd.gl_gui.melty import FileWatch, Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_conversion.address import (
    Address, to_address, update_address_cache, _evict_linecache,
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

    source_lines, start_lineno = inspect.getsourcelines(unwrapped)
    return changed, Address(Path(source_file), start_lineno - 1,
                   start_lineno - 1 + len(source_lines), source=input_value)

@render_func()
def module_to_address(input_value: types.ModuleType, draw_state, changed=False):
    if changed:
        source_file = Path(input_value.__file__)
        FileWatch.register_draw_state(draw_state, source_file)
        if changed:
            print(f"module_to_address: input changed for module {input_value.__name__}, checking file {source_file}")

        return changed, Address(source_file, source=input_value)
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
                           start_lineno - 1 + len(source_lines), source=input_value)
        except (TypeError, OSError):
            return changed, None
    else:
        return changed, None

@render_func(background=True)
def load_cst_module(input_value: Address):
    text = _load_span(input_value)
    converted_cst = cst.parse_module(text)
    general_parse = cst_module_to_dict(converted_cst)
    general_parse.address = input_value

    if Toggles.slow_down_threads:
        for i in range(5):
            import time
            time.sleep(0.1)
            print(f"Simulating slow load... {i+1}/5")

    return True, general_parse

########################
# draw_collection
########################

@render_func(background=False)
def do_recompile(input_value, code_str, file_path):
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
    lines[input_value.start:input_value.end] = new_lines
    final_text = newline.join(lines)
    FileWatch.set_hash_from_content(input_value.path, final_text)
    input_value.path.write_text(final_text, encoding="utf-8")

    if Toggles.slow_down_threads:
        for i in range(5):
            import time
            time.sleep(0.1)
            print(f"Simulating slow load... {i + 1}/5")

    return True, input_value

@render_func(background=True)
def save_cst_module(input_value):
    back_to_cst = dict_to_cst_module(input_value)
    if Toggles.slow_down_threads:
        for i in range(5):
            import time
            time.sleep(0.1)
            print(f"Simulating slow load... {i + 1}/5")

    return False, back_to_cst


@render_func(use_cache=False)
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

    running = draw_state._running if run_in_background else False

    fa_run_arrow = "\uf04b"
    if clicked or running or imgui.button(f"{fa_run_arrow} {input_value.__name__}##{draw_state.unique}"):
        with_kwargs['changed'] = True
        changed, value = input_value(**with_kwargs)
        if isinstance(value, Pending):
            draw_state._running = True
            return False, None

        draw_state._running = False
        return True, (changed, value)

    return False, None

@render_func()
def address_to_general_parse(input_value: Address, pending=False, changed=False, draw_state=None, load=False):
    """Load node: class → cst.Module.

    - Resolves Address from the class on first call
    - Watches file mtime for external changes
    - Shows Load / Revert buttons when file changes on disk
    - Returns (True, cst.Module) when loaded, (False, cached) otherwise
    """

    if pending:
        clicked, result = run_button(load_cst_module, with_kwargs={"input_value":input_value},
                            clicked=load)
        if clicked:
            draw_state._file_meta = input_value.get_meta()
            return result

    # ── Steady state ──────────────────────────────────────────
    return False, None





@render_func(use_cache=True)
def general_parse_to_address(input_value: GeneralParse, draw_state=None, pending=False,
                             changed=False, recompile=False, save=False):
    """GeneralParse dict → Address. Handles recompile and save for any source type."""
    address = input_value.address


    if pending or changed:
        changed, back_to_cst = save_cst_module(input_value, changed=changed)
        if isinstance(back_to_cst, Pending):
            return False, back_to_cst


        code_str = back_to_cst.code
        source = address.source
        if source is not None:
            clicked, result = run_button(do_recompile, with_kwargs={"input_value": address.source,
                                                                    "code_str": code_str,
                                                                    "file_path": address.path},
                                                          clicked=recompile)

            #
            clicked, result = run_button(_do_save, name="do_save", with_kwargs={"input_value": address,
                                                                "code_str": code_str},
                                                                clicked=save)
            #
            if clicked:
                return True, address

    return changed, address

    # if pending or draw_state._loading:
    #     changed, back_to_cst = save_cst_module(input_value, changed=changed)
    #     if isinstance(back_to_cst, Pending):
    #         return False, back_to_cst
    #
    #     code_str = back_to_cst.code
    #     recompile_loading = draw_state._loading and draw_state._loading.originated == do_recompile
    #     if recompile_loading or recompile or imgui.button(f"Recompile##{draw_state.unique}"):
    #         source = address.source
    #         if source is not None:
    #             try:
    #                 re_changed, re_result = do_recompile(source, code_str=code_str,
    #                                                      file_path=address.path, changed=changed)
    #                 if isinstance(re_result, Pending):
    #                     return False, re_result
    #             except Exception as e:
    #                 imgui.text(f"Error: {e}")
    #                 return False, None
    #         else:
    #             print(f"No source ref for {address.path}, skipping recompile")
    #
    #
    #     save_loading = draw_state._loading and draw_state._loading.originated == _do_save
    #     if save_loading or save or imgui.button(f"Save##{draw_state.unique}"):
    #         if draw_state._loading:
    #             print(str(draw_state._loading.originated.__name__))
    #         source = address.source
    #         if source is not None:
    #             try:
    #                 do_recompile(source, code_str=code_str, save=False,
    #                              file_path=address.path, changed=changed)
    #             except Exception as e:
    #                 imgui.text(f"Error: {e}")
    #                 return False, None
    #         else:
    #             print(f"No source ref for {address.path}, skipping recompile")
    #
    #         save_changed, save_result = _do_save(address, code_str=code_str, changed=changed)
    #         if isinstance(save_result, Pending):
    #             return False, save_result
    #
    #         return True, address
    #
    # return False, None

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
def chain_cst_to_str(input_value, draw_state=None):
    """Convert cst.Module → source string. Pure converter, no UI."""
    if isinstance(input_value, cst.Module):
        return True, input_value.code
    return False, input_value



