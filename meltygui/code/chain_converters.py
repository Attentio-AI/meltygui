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
import types
from pathlib import PosixPath, Path

import imgui
import libcst as cst

from src.lsd.gl_gui.melty import FileWatch
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_conversion.fileref import (
    FileRef, to_fileref, update_fileref_cache, _evict_linecache,
)
from src.lsd.gl_gui.view.core_conversion.file_converters import (
    _detect_newline, _recompile_class,
)
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    cst_module_to_dict, dict_to_cst_module, GeneralParse,
)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Load node: class → cst.Module                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _load_span(ref: FileRef) -> str:
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

    - Resolves FileRef from the class on first call
    - Watches file mtime for external changes
    - Shows Load / Revert buttons when file changes on disk
    - Returns (True, cst.Module) when loaded, (False, cached) otherwise
    """
    # ── Resolve ref on first encounter ────────────────────────
    if draw_state._fileref is None:
        ref = to_fileref(input_value)
        if ref is None:
            return False, input_value
        draw_state._fileref = ref
        draw_state._original_input_ref = input_value

    ref = draw_state._fileref

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
        # Refresh fileref from cache (another node may have updated line count)
        fresh_ref = to_fileref(input_value)
        if fresh_ref is not None:
            draw_state._fileref = fresh_ref
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
def path_to_file_ref(input_value: Path):
    """Job of interrupt source is to return True when the input has changed on disk"""
    file_ref = FileRef(input_value)
    return False, file_ref

@render_func()
def function_to_file_ref(input_value: types.FunctionType, draw_state, changed=False):
    """Extract FileRef from a function object."""

    unwrapped = inspect.unwrap(input_value)
    source_file = inspect.getfile(unwrapped)
    _evict_linecache(source_file)
    FileWatch.register_draw_state(draw_state, Path(source_file))

    if changed:
        print(f"function_to_file_ref: input changed for {input_value.__name__}, checking file {source_file}")

    source_lines, start_lineno = inspect.getsourcelines(unwrapped)
    return changed, FileRef(Path(source_file), start_lineno - 1,
                   start_lineno - 1 + len(source_lines))

@render_func()
def module_to_file_ref(input_value: types.ModuleType, draw_state, changed=False):
    source_file = Path(input_value.__file__)
    FileWatch.register_draw_state(draw_state, source_file)
    if changed:
        print(f"module_to_file_ref: input changed for module {input_value.__name__}, checking file {source_file}")

    return changed, FileRef(source_file)


########################################### CHAIN START
@render_func()
def class_to_file_ref(input_value: type, draw_state, changed=False):
    if changed:
        print(f"CLASS TO FILEREF: FILE WATCH INPUT -- CHANGED")

    if input_value.__module__ in ('builtins', '_collections_abc'):
        return False, None
    try:
        import inspect
        source_file = inspect.getfile(input_value)
        FileWatch.register_draw_state(draw_state, Path(source_file))
        _evict_linecache(source_file)
        source_lines, start_lineno = inspect.getsourcelines(input_value)
        return changed, FileRef(Path(source_file), start_lineno - 1,
                       start_lineno - 1 + len(source_lines), source=input_value)
    except (TypeError, OSError):
        return changed, None

@render_func(use_cache=True)
def file_ref_to_general_parse(input_value: FileRef, changed=False, draw_state=None):
    """Load node: class → cst.Module.

    - Resolves FileRef from the class on first call
    - Watches file mtime for external changes
    - Shows Load / Revert buttons when file changes on disk
    - Returns (True, cst.Module) when loaded, (False, cached) otherwise
    """

    # External change or file meta unset vs loaded
    # from src.lsd.gl_gui.view.type_conversion.cache_base import UNSET_VALUE
    changed |= draw_state._file_meta == UNSET_VALUE

    if changed:
        print(f"FILE REF TO CST --- CHANGED")
        if imgui.button("Load##file_ref_to_cst"):
            text = _load_span(input_value)
            draw_state._file_meta = input_value.get_meta()
            converted_cst = cst.parse_module(text)
            general_parse = cst_module_to_dict(converted_cst)
            general_parse.file_ref = input_value
            return True, general_parse


    # ── Steady state ──────────────────────────────────────────
    return False, None

########################
# draw_collection
########################


@render_func(use_cache=True)
def general_parse_to_file_ref(input_value: GeneralParse, draw_state=None, changed=False):
    """GeneralParse dict → cst.Module. Pure converter, no UI."""
    file_ref = input_value.file_ref
    back_to_cst = dict_to_cst_module(input_value)
    code_str = back_to_cst.code
    if changed:
        if imgui.button("Recompile"):
            class_ref = file_ref.source
                # Hotswap class
            if class_ref is not None and isinstance(class_ref, type):
                try:
                    _recompile_class(class_ref, code_str, str(file_ref.path))
                except Exception as e:
                    imgui.text(f"Error: {e}")
                    return False, None
            else:
                print(f"No class ref found for this file ref, skipping hotswap {file_ref.path}")

        imgui.same_line()

        if imgui.button("Save"):
            class_ref = file_ref.source
            # Hotswap class
            if class_ref is not None and isinstance(class_ref, type):
                try:
                    _recompile_class(class_ref, code_str, str(file_ref.path))
                except Exception as e:
                    imgui.text(f"Error: {e}")
                    return False, None
            else:
                print(f"No class ref found for this file ref, skipping hotswap {file_ref.path}")

            # Write file
            full_data = file_ref.path.read_bytes()
            newline = _detect_newline(full_data)
            try:
                text = full_data.decode("utf-8")
            except UnicodeDecodeError:
                text = full_data.decode("latin-1")
            lines = text.split(newline)
            new_lines = code_str.split(newline)
            lines[file_ref.start:file_ref.end] = new_lines
            final_text = newline.join(lines)
            FileWatch.set_hash_from_content(file_ref.path, final_text)
            file_ref.path.write_text(newline.join(lines), encoding="utf-8")
            new_ref = FileRef(file_ref.path, file_ref.start,
                              file_ref.start + len(new_lines), source=class_ref)

            draw_state._file_meta = new_ref.get_meta()
            # FileWatch.set_hash(file_ref.path)

            return True, file_ref
    #
    # code_str = back_to_cst.code
    # FileWatch.set_hash_from_content(file_ref.path, code_str)

    return False, None

@render_func(use_cache=True)
def file_ref_to_class(input_value, changed=False, draw_state=None):

    pass

@render_func(use_cache=True)
def cst_to_file_ref(input_value, changed=False, draw_state=None):

    pass



@render_func(use_cache=True)
def chain_cst_to_str(input_value, draw_state=None):
    """Convert cst.Module → source string. Pure converter, no UI."""
    if isinstance(input_value, cst.Module):
        return True, input_value.code
    return False, input_value



