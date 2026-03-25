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
def file_ref_to_cst(input_value: FileRef, changed=False, draw_state=None):
    """Load node: class → cst.Module.

    - Resolves FileRef from the class on first call
    - Watches file mtime for external changes
    - Shows Load / Revert buttons when file changes on disk
    - Returns (True, cst.Module) when loaded, (False, cached) otherwise
    """

    ref = input_value

    # ── First load ────────────────────────────────────────────
    if changed:
        text = _load_span(ref)
        draw_state._loaded_text = text
        draw_state._loaded_cst = cst.parse_module(text)
        draw_state.mark_file_current()
        return True, draw_state._loaded_cst

    imgui.text("chain_cls_load")

    # ── Steady state ──────────────────────────────────────────
    return False, None


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

@render_func()
def class_to_file_ref(input_value: type, draw_state, changed=False):
    if changed:
        print(f"class_to_file_ref: input changed for class {input_value.__name__}, checking file")

    if input_value.__module__ in ('builtins', '_collections_abc'):
        return False, None
    try:

        import inspect
        source_file = inspect.getfile(input_value)
        FileWatch.register_draw_state(draw_state, Path(source_file))
        _evict_linecache(source_file)
        source_lines, start_lineno = inspect.getsourcelines(input_value)
        return changed, FileRef(Path(source_file), start_lineno - 1,
                       start_lineno - 1 + len(source_lines))
    except (TypeError, OSError):
        return changed, None


@render_func(use_cache=True)
def chain_cls_save(input_value, changed=False, draw_state=None):
    """Save node: source string → write to disk + hotswap class.

    - Compares input against baseline to detect unsaved edits
    - Shows Save button when dirty
    - On save: writes file, hotswaps class, updates fileref
    - Returns (changed, value) — changed=True only after successful save
    """
    # input_value is a source string (from cst.Module.code via upstream)
    if not isinstance(input_value, str):
        # If upstream hasn't produced a string yet, pass through
        return False, input_value

    # ── Establish baseline on first pass ──────────────────────
    if not hasattr(draw_state, '_save_baseline') or draw_state._save_baseline is None:
        draw_state._save_baseline = input_value
        return False, input_value

    # ── Dirty detection ───────────────────────────────────────────
    is_dirty = (input_value != draw_state._save_baseline)

    if not is_dirty:
        return False, input_value

    # ── Show save UI ──────────────────────────────────────────
    imgui.text("Unsaved changes")
    if imgui.button("Save##chain_save"):
        class_ref = draw_state._original_input_ref
        ref = draw_state._fileref

        if ref is not None:
            # Hotswap class
            if class_ref is not None:
                try:
                    _recompile_class(class_ref, input_value, str(ref.path))
                except Exception as e:
                    imgui.text(f"Error: {e}")
                    return False, input_value

            # Write file
            full_data = ref.path.read_bytes()
            newline = _detect_newline(full_data)
            try:
                text = full_data.decode("utf-8")
            except UnicodeDecodeError:
                text = full_data.decode("latin-1")
            lines = text.split(newline)
            new_lines = input_value.split(newline)
            lines[ref.start:ref.end] = new_lines
            ref.path.write_text(newline.join(lines), encoding="utf-8")

            # Update fileref
            new_ref = FileRef(ref.path, ref.start,
                              ref.start + len(new_lines))
            draw_state._fileref = new_ref
            if class_ref is not None:
                update_fileref_cache(class_ref, new_ref)

            # Update baseline
            draw_state._save_baseline = input_value
            return True, input_value

    return False, input_value


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Passthrough: cst.Module → source string                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@render_func(use_cache=True)
def chain_cst_to_str(input_value, draw_state=None):
    """Convert cst.Module → source string. Pure converter, no UI."""
    if isinstance(input_value, cst.Module):
        return True, input_value.code
    return False, input_value


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CST <-> dict converters (thin wrappers for chain compatibility)               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@render_func(use_cache=True)
def chain_cst_to_dict(input_value, draw_state=None):
    """cst.Module → GeneralParse dict. Pure converter, no UI."""
    if isinstance(input_value, cst.Module):
        return True, cst_module_to_dict(input_value)
    return False, input_value


@render_func(use_cache=True)
def chain_dict_to_cst(input_value, draw_state=None):
    """GeneralParse dict → cst.Module. Pure converter, no UI."""
    if isinstance(input_value, dict) and "__cst__" in input_value:
        from src.lsd.gl_gui.view.core_conversion.path_finder import Pending
        result = dict_to_cst_module(input_value)
        if isinstance(result, Pending):
            return False, input_value
        return True, result
    return False, input_value

