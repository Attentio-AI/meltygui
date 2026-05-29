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
from src.lsd.gl_gui.background import Background
from src.lsd.gl_gui.model.core_model.draw_state import Pin, Anchor
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
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
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.core_views.text_editor import draw_text
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


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
    
    print(f"[function_to_address] Resolved address for {input_value.__name__} in {source_file}: lines {start_lineno}-{start_lineno + len(source_lines) - 1}")

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
    # Resolve on EVERY call (guarded by the mtime cache), like function_to_address
    # - NOT gated on `changed`. Gating let the cached span go stale after a sibling
    # edit shifted the class's lines, so a save wrote to the wrong range. A save
    # bumps mtime, so the next render re-resolves fresh line numbers.
    if not isinstance(input_value, type) or input_value.__module__ in ('builtins', '_collections_abc'):
        return changed, None
    try:
        import inspect
        source_file = inspect.getfile(input_value)
        FileWatch.register_draw_state(draw_state, Path(source_file))

        # Cache the getsourcelines result by (input, file mtime) so typing
        # doesn't re-read+tokenize the file; a save bumps mtime and we re-resolve.
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


@render_func()
def class_to_address_incl_overrides(input_value: type, draw_state, changed=False):
    """Like class_to_address, but extends the span UPWARD over a contiguous
    leading `# [...]` override comment immediately above the class.

    getsourcelines starts at `class X:`, so a comment above it falls outside the
    span — meaning a module-level override comment would never round-trip and the
    code_comment lens would re-add/duplicate it. Pulling the comment into the span
    (loaded as the module header) makes add/update/delete stable.

    Resolves on EVERY call (guarded by the mtime cache), like function_to_address
    — NOT gated on `changed`. Gating let the cached span go stale after another
    edit shifted the class's lines, so the whole-class-span save wrote to the
    wrong range (duplicated/dropped lines, failed delete). A save bumps mtime, so
    the next render re-resolves fresh line numbers."""
    if not isinstance(input_value, type) or input_value.__module__ in ('builtins', '_collections_abc'):
        return changed, None
    try:
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import _parse_override_comment
        source_file = inspect.getfile(input_value)
        FileWatch.register_draw_state(draw_state, Path(source_file))
        try:
            mtime = Path(source_file).stat().st_mtime
        except OSError:
            mtime = None
        cached = getattr(draw_state, '_addr_cache', None)
        if cached is not None and cached[0] is input_value and cached[1] == mtime:
            return changed, cached[2]

        _evict_linecache(source_file)
        source_lines, start_lineno = inspect.getsourcelines(input_value)
        start0 = start_lineno - 1
        end0 = start0 + len(source_lines)

        data = Path(source_file).read_bytes()
        newline = _detect_newline(data)
        try:
            file_lines = data.decode("utf-8").split(newline)
        except UnicodeDecodeError:
            file_lines = data.decode("latin-1").split(newline)
        ext_start = start0
        j = start0 - 1
        while j >= 0 and _parse_override_comment(file_lines[j].strip()) is not None:
            ext_start = j
            j -= 1

        address = Address(Path(source_file), ext_start, end0,
                          source=input_value, watcher_ds=draw_state)
        draw_state._addr_cache = (input_value, mtime, address)
        return changed, address
    except (TypeError, OSError, tokenize.TokenError, SyntaxError):
        return changed, None


@render_func(background=True)
def load_cst_module(input_value: Address):

    text = _load_span(input_value)
    converted_cst = cst.parse_module(text)
    general_parse = cst_module_to_dict(converted_cst) 
    general_parse.address = input_value
    general_parse.file_path = input_value.path
    print(f"Loaded {input_value.path} lines {input_value.start}-{input_value.end}")

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


def _import_stmt_end(lines, i):
    """Index of the LAST line of the (possibly multi-line) import statement that
    starts at line i — following backslash continuations and unclosed parens, so
    callers never split a continued import."""
    stmt = lines[i]
    while i + 1 < len(lines) and (
            stmt.rstrip().endswith("\\") or stmt.count("(") > stmt.count(")")):
        i += 1
        stmt += "\n" + lines[i]
    return i, stmt


def _ensure_import_lines(lines, module, name):
    """If `name` isn't already imported in `lines`, insert `from module import
    name` after the file's leading import block. Returns (lines, inserted_count).

    Best-effort textual scan (no parse — this runs inside the save write). Treats
    the run of leading import/comment/blank/docstring lines as the import block
    and inserts after it. Continuation-aware (backslash + parens) so it never
    inserts in the middle of a multi-line import."""
    # ── Dedup: is `name` already imported? (join continuations before checking) ──
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("import ") or s.startswith("from "):
            end, stmt = _import_stmt_end(lines, i)
            syms = stmt.split("import", 1)[1] if "import" in stmt else ""
            for ch in "(),\\\n":
                syms = syms.replace(ch, " ")
            if name in [t.split(".")[0] for t in syms.split()]:
                return lines, 0
            i = end + 1
            continue
        i += 1

    # ── Find the insert point: end of the leading import block ──────────────────
    insert_idx = 0
    i = 0
    in_doc = None          # triple-quote delimiter while inside a module docstring
    seen_code = False
    while i < len(lines):
        s = lines[i].strip()
        if in_doc is not None:                       # inside a multi-line docstring
            insert_idx = i + 1
            if in_doc in s:
                in_doc = None
            i += 1
            continue
        if s == "" or s.startswith("#"):
            i += 1
            continue
        if not seen_code and (s.startswith('"""') or s.startswith("'''")):
            q = s[:3]
            seen_code = True
            insert_idx = i + 1
            if not (len(s) > 3 and s.count(q) >= 2):  # not a one-line docstring
                in_doc = q
            i += 1
            continue
        if s.startswith("import ") or s.startswith("from "):
            seen_code = True
            end, _ = _import_stmt_end(lines, i)       # skip past continuations
            insert_idx = end + 1
            i = end + 1
            continue
        break  # first real code - stop scanning the import block
    new_lines = list(lines)
    new_lines.insert(insert_idx, f"from {module} import {name}")
    return new_lines, 1


@render_func(background=True)
def _do_save(input_value, code_str, ensure_import=None):
    """Write code_str back into the file at the Address's span.

    ensure_import=(module, name) also inserts a missing import in the SAME write
    (atomic — avoids a second racing write), so e.g. a synthesized @defaults
    decorator has its import. The import insert shifts line numbers; our own
    Address is adjusted, siblings re-resolve via the mtime bump."""
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

    inserted = 0
    if ensure_import is not None:
        lines, inserted = _ensure_import_lines(lines, ensure_import[0], ensure_import[1])

    final_text = newline.join(lines)
    FileWatch.set_hash_from_content(input_value.path, final_text, draw_state=input_value._watcher_ds)

    try:
        input_value.path.write_text(final_text, encoding="utf-8")

        if old_start is not None:
            resolved_old_end = old_end if old_end is not None else old_start + len(new_lines)
            new_end = old_start + len(new_lines)
            delta = new_end - resolved_old_end

            input_value.end = new_end
            # Account for an import inserted above our span so the Address stays
            # valid this frame (siblings heal on the next mtime-driven re-resolve).
            if inserted:
                input_value.start = old_start + inserted
                input_value.end = new_end + inserted
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
                                    height=30, draw=True, value=0.4, saturation=1.5,
                                    name=f"{input_value.__name__}{draw_state.unique}_run")[0]:
        with_kwargs['changed'] = True
        changed, value = input_value(**with_kwargs)
        if isinstance(value, Pending):
            draw_state._running = input_value
            return False, None

        draw_state._running = False
        return True, (changed, value)

    return False, None


@render_func(use_cache=True, selectable=False)
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
    # if button(f"{folder_icon} {file_name}", height=30, value=0.4, saturation=1.5)[0]:
    #     from src.lsd.gl_gui.utils.jump_to_editor import open_in_intellij
    #     line_number = input_value.start + 1 if input_value.start is not None else None
    #     threading.Thread(
    #         target=open_in_intellij,
    #         args=(str(input_value.path),),
    #         kwargs={"line_number": line_number},
    #         daemon=True,
    #     ).start()

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

@render_func(use_cache=True, selectable=False)
def general_parse_to_address(input_value: GeneralParse=None, pending=False, draw_state=None, unique=None,
                             changed=False, recompile=False, save=False, s_key_pressed=None,
                             enter_key_pressed=None, ensure_import=None):
    """GeneralParse dict → Address. Handles recompile and save for any source type.

    ensure_import=(module, name) is forwarded to _do_save so a synthesized
    decorator (e.g. @defaults) gets its import inserted in the same write."""
    address = input_value.address
    if not isinstance(address, Address):
        imgui.text_colored("Saving unavailable...\ninput_value.address is not set",
                           1.0, 0.0, 0.0)
        return False, None
    source = address.source

    # Latch the save intent across the background dict_to_cst latency. An edit
    # (focus Add/Delete, a single color pick) sets changed=True for ONE frame -
    # which lands exactly on dict_to_cst's Pending and is gone by the time the
    # conversion completes, so the save was just dropped. Hold the intent until
    # _do_save actually completes, then clear it.
    if changed:
        draw_state._lens_save_pending = True
    save_pending = getattr(draw_state, '_lens_save_pending', False)

    show_recompile = True
    show_save = not save or pending or changed or save_pending

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
        if back_to_cst.state == PendingState.ERROR:
            # Conversion failed (e.g. unparseable edit) - give up the latch so it
            # doesn't spin requesting renders forever.
            draw_state._lens_save_pending = False
        elif save_pending:
            # Still converting. Keep the latch + keep frames coming so the save
            # fires the moment the (background) code_str lands.
            request_render()
        return False, back_to_cst
    code_str = back_to_cst.code
    if show_recompile:
        if source is not None:
            from src.lsd.gl_gui.view.mode import Mode
            recompiled, _ = run_button(do_recompile, clicked=recompile and pending, name=f"do_recompile{unique}",
                        with_kwargs={"input_value": address.source,
                                 "code_str": code_str,
                                 "file_path": address.path}, mode=Mode.FLOATING, pin_to_clip=Pin.PARENT,
                                       parent_anchor=Anchor.BOTTOM_LEFT,
                                       tint=(0.3, 0.4, 0.6))
            if recompiled:
                record_compile(address)
                # Refresh the cached file path node so its compiled indicator updates.
                Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)

            imgui.dummy(1,1)
    if show_save:
        if source is not None:
            clicked, result = run_button(_do_save, with_kwargs={"input_value": address,
                                         "code_str": code_str,
                                         "ensure_import": ensure_import},
                                         clicked=save or save_pending)
            # We reached a real code_str and dispatched the write to the background
            # thread, which writes regardless of further pumping. Clear the latch
            # on dispatch (not on completion) so an errored/never-completing save
            # can't spin the latch forever; Background invalidates on completion.
            draw_state._lens_save_pending = False
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


@render_func(use_cache=False, show_bg=False, selectable=False, is_tree=False, with_header=draw_header)
def focus(input_value, path=(), default=None, kind=None, draw_state=None, unique=None, changed=False, **kwargs):
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

    Handles dict-key and attribute access uniformly, so the same node focuses aj
    GeneralParse dict (code-comment / decoration tint), a draw_state, or a data
    class instance.
    """
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_tuple, button

    changed, new_value = False, input_value
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

    # Clean label: the lens name + a plain target name - the class for code
    # lenses, the enclosing fn for the caller, the data class for an instance.
    # No internal keys (__overrides__ etc.); the "where" lives in the jump button.
    if len(path) > 1:
        target = str(path[0])                        # class name (code lenses)
    elif isinstance(input_value, dict):
        _a = input_value.get("__address__")
        target = getattr(getattr(_a, "source", None), "__name__", None) \
            if isinstance(_a, Address) else None     # caller's enclosing fn
    elif input_value is not None and type(input_value).__name__ != "DrawState":
        target = type(input_value).__name__          # data instance class
    else:
        target = None
    base = kind or str(draw_state.name)
    label = f"{base} · {target}" if target else base

    imgui.same_line()
    imgui.text(label)

    # Code-jump button - open the source where this lens's value lives. The call
    # dict carries __address__; a GeneralParse carries .address. Live other values
    # (draw_state / instance) have no source location, so no button is shown.
    _addr = input_value.get("__address__") if isinstance(input_value, dict) else None
    if _addr is None:
        _addr = getattr(input_value, "address", None)

    if isinstance(_addr, Address) and _addr.path is not None:
        _line = (_addr.start or 0) + 1
        imgui.same_line()
        if button(f"{_addr.path.name}:{_line}##{unique}", height=24, name=f"jump{base}{leaf_key}{unique}")[0]:
            import threading
            from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
            threading.Thread(target=open_in_intellij, args=(str(_addr.path),),
                             kwargs={"line_number": _line}, daemon=True).start()

    # ── Present: the reusable picker (every lens funnels through this) ─────────
    if leaf is not None:
        tint_changed, new_tint = draw_tuple(leaf, with_header=draw_header, name=f"{unique}{leaf_key}_tuple_{unique}")
        if tint_changed:
            _focus_set(parent, leaf_key, new_tint)
            changed, new_value = True, input_value  # hand the mutated container to the save node
        # Delete: drop this source's override so it stops winning. For a dict
        # (code/caller) the key is popped → dict_to_cst() removes it from source on
        # save; for a live attr it's set to None. Return changed so the save side
        # persists the removal, same as an edit.
        imgui.same_line()

        delete_icon = ""
        if button(f"{delete_icon}##{unique}", name=f"{leaf_key}_delete_{unique}", height=26)[0]:
            if isinstance(parent, dict):
                parent.pop(leaf_key, None)
            else:
                _focus_set(parent, leaf_key, None)
            changed, new_value = True, input_value
    else:

        # ── Not present: offer to add it ───────────────────────────────────────────────
        if button(f"##{leaf_key}{unique}", name=f"{leaf_key}_add_{unique}", height=26)[0]:
            node = input_value
            for key in path[:-1]:           # create missing intermediate containers
                child = _focus_get(node, key)
                if child is None:
                    child = {}
                    _focus_set(node, key, child)
                node = child
            try:
                _focus_set(node, leaf_key, default if default is not None else (0.485, 0.61, 0.76))
                changed, new_value = True, input_value

            except Exception as e:
                print_stack_trace(exception=e)
                print("Error setting value in focus node:", e)
                changed, new_value = False, input_value

    return changed, new_value


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Caller kwarg lens nodes: edit a kwarg literal at the call site                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
#
# These let the context menu edit `draw_text(..., tint=(1,0,1))` by parsing the
# CALLER's statement. The call site comes from draw_state._call_site - the
# (filename, lineno) resolved once (via caller_site) when frames were grabbed on
# menu-open in core_render; never re-walked from the live stack. Reuses the
# existing cst_call_to_dict / dict_to_cst_call converters (the same ones that
# parse @decorator(...) calls).

_DISPATCH_SKIP = ("core_render.py",)  # the render_func wrapper lives here


def caller_site(frames):
    """Walk outward from the render_func wrapper to the first frame that ISN'T
    render-dispatch machinery, and return its (filename, lineno).

    Widgets are re-dispatched via Melty.draw -> draw_state._wrapper(**kwargs)
    (melty.py:1074), so the frame directly above the wrapper is often the
    DISPATCHER, not the user's draw_text(...) call. We skip the render_func
    wrapper (core_render.py) and the Melty.draw frame so the result is the real
    caller — the user's call for a directly-invoked widget, or the dispatch's
    caller for a re-dispatched one.

    Returning just (filename, lineno) is critical: the frames list carries each
    frame's f_locals (the whole AppModel, tensors, cyclic refs). Feeding that
    into draw_any/render_func would hash/compare it and hang. The lens root calls
    this so the chain's input_value is a tiny, cheap-to-hash tuple."""
    if not frames:
        return None
    # frames is outermost-first; walk innermost-first to find the nearest caller.
    for idx, entry in enumerate(reversed(frames)):

        filename, lineno, func_name = entry[0], entry[1], entry[2]
        base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base in _DISPATCH_SKIP:
            continue
        # Skip the re-dispatch machinery so the site is the user's draw_xxx(...)
        # call, not the dispatch's `draw_state._wrapper(**kwargs)`. These are
        # func-name-specific (NOT whole-file): new_core_view.py also holds real
        # user render code, so only its `draw_any` dispatch frame is skipped.
        if base == "melty.py" and func_name == "draw":            # Melty.draw re-dispatch
            continue
        if base == "new_core_view.py" and func_name == "draw_any":  # draw_any dispatch
            continue

        return filename, lineno
    return None


def _first_call(module):
    """The outermost cst.Call in a parsed statement (don't descend into nested
    calls), or None."""
    found = {}

    class _V(cst.CSTVisitor):
        def visit_Call(self, node):
            if 'c' not in found:
                found['c'] = node
            return False  # outermost only

    module.visit(_V())
    return found.get('c')


def _module_for_file(target_path):
    """The live module object whose __file__ resolves to target_path, or None.

    Filters on basename before the (syscall-heavy) Path.resolve() so we don't
    stat every module in sys.modules — that loop was a measurable chunk of the
    tint-tab open cost."""
    import sys
    target_name = target_path.name
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if not f:
            continue
        if f.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] != target_name:
            continue
        try:
            if Path(f).resolve() == target_path:
                return m
        except (OSError, ValueError):
            continue
    return None


# (filename, lineno, mtime) -> resolved function object (or None). Keyed on mtime
# so a hotswap/edit of the file invalidates the entry; the sys.modules walk +
# co_firstlineno scan is otherwise repeated every time the caller row is shown.
_ENCLOSING_FN_CACHE = {}


def _enclosing_function(filename, lineno):
    """The live function object whose `def` encloses (filename, lineno) — the
    nearest def at or above the line, walking module + class scopes. Lets the
    caller lens hotswap that function after a literal in its body is edited.
    Returns None for closures/nested funcs not reachable from module vars."""
    try:
        target = Path(filename).resolve()
    except (OSError, ValueError):
        return None
    try:
        mtime = target.stat().st_mtime
    except OSError:
        mtime = None
    cache_key = (str(target), lineno, mtime)
    if cache_key in _ENCLOSING_FN_CACHE:
        return _ENCLOSING_FN_CACHE[cache_key]
    module = _module_for_file(target)
    if module is None:
        _ENCLOSING_FN_CACHE[cache_key] = None
        return None
    best = {"fn": None, "line": -1}

    def consider(fn):
        inner = inspect.unwrap(fn)
        code = getattr(inner, "__code__", None)
        if code is None:
            return
        try:
            same = Path(code.co_filename).resolve() == target
        except (OSError, ValueError):
            same = code.co_filename == str(target)
        if same and code.co_firstlineno <= lineno and code.co_firstlineno > best["line"]:
            best["line"] = code.co_firstlineno
            best["fn"] = inner

    def walk(scope):
        for val in list(vars(scope).values()):
            if isinstance(val, types.FunctionType):
                consider(val)
            elif isinstance(val, (staticmethod, classmethod)):
                f = getattr(val, "__func__", None)
                if isinstance(f, types.FunctionType):
                    consider(f)
            elif isinstance(val, type):
                walk(val)

    walk(module)
    _ENCLOSING_FN_CACHE[cache_key] = best["fn"]
    return best["fn"]


def _resolve_call_address(input_value):
    """(filename, lineno) -> Address of the enclosing call STATEMENT, with the
    enclosing function attached as .source.

    Plain function (no imgui, no draw_state) so it can run on a Background thread.
    Uses the stdlib `ast` module (C-accelerated) to find the outermost Call
    covering the line, so multi-line calls round-trip as one statement.

    NOTE: do NOT use libcst's MetadataWrapper+PositionProvider here. That pass is
    pure-Python and O(whole file); on a large caller file (new_core_view.py) it
    took ~1.1s AND, being CPU-bound, held the GIL — starving the render thread for
    the duration (an 800ms+ frame stall). ast.parse + ast.walk does the same span
    lookup in single-digit ms because the parse is in C. The small per-statement
    span is re-parsed with libcst downstream (address_to_call_parse), where the
    cost is bounded by the statement, not the file."""
    import ast
    filename, lineno = input_value
    path = Path(filename)
    address = Address(path, lineno - 1, lineno)  # line default
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        best_key = None
        best_node = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            start = node.lineno
            end = getattr(node, 'end_lineno', None) or start
            if start <= lineno <= end:
                key = (start, -end)  # outermost wins
                if best_key is None or key < best_key:
                    best_key = key
                    best_node = node
        if best_node is not None:
            s = best_node.lineno
            e = best_node.end_lineno or s
            address = Address(path, s - 1, e)
            # Column span (UTF-8 byte offsets, per ast) of the call WITHIN its
            # line span. Lets address_to_call_parse extract just the call
            # expression even when it's embedded in a larger statement
            # (e.g. `if ... or button(...):`) and splice the edit back without
            # disturbing the surrounding prefix/suffix.
            address._call_cols = (best_node.col_offset, best_node.end_col_offset)
    except Exception as ex:
        print(f"_resolve_call_address: could not resolve call span in {filename}:{lineno}: {ex}")

    # Enclosing function = recompile hint (None → save-only).
    address.source = _enclosing_function(filename, lineno)
    return address


@render_func()
def caller_to_address(input_value, draw_state, changed=False):
    """(filename, lineno) -> Address spanning the call STATEMENT at the call site.

    The actual resolution (full-file libcst parse + PositionProvider + enclosing
    function lookup) runs on a Background thread via _resolve_call_address — never
    on the UI thread — and is cached by (filename, lineno, mtime). Returns
    (changed, None) while the background resolve is pending; the caller row fills
    in once it lands. Input is the lightweight site from caller_site() — never the
    raw frames (which carry f_locals)."""
    if not input_value or len(input_value) != 2:
        return changed, None
    filename, lineno = input_value
    path = Path(filename)
    # Register the watch on THIS draw_state so _do_save's set_hashed_content
    # (called with address._watcher_ds = this draw_state) actually opens the
    # self-write suppress window; otherwise our own write bounces back as an
    # external change. Idempotent: register_draw_state returns early if already
    # registered.
    FileWatch.register_draw_state(draw_state, path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    cached = getattr(draw_state, '_caller_addr_cache', None)
    if cached is not None and cached[0] == (filename, lineno) and cached[1] == mtime:
        return changed, cached[2]

    parent_tile = (draw_state._parent._tile_id
                   if draw_state._parent is not None else draw_state._tile_id)
    result = Background.run(
        _resolve_call_address,
        user_id=str(draw_state.unique) + "|caller_addr",
        func_kwargs={"input_value": (filename, lineno)},
        invalidate_id=parent_tile,
        on_frame=Melty.frame_count,
    )
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        result = result[0]
    if not isinstance(result, Address):
        return changed, None  # still resolving on the background thread

    result._watcher_ds = draw_state
    draw_state._caller_addr_cache = ((filename, lineno), mtime, result)
    return changed, result


def _split_span_at_call(span_lines, sc, ec, newline):
    """Split a call's line span into (prefix, call_text, suffix) at the call's
    column offsets. `sc`/`ec` are UTF-8 byte offsets (ast convention) into the
    first/last line, so slice in bytes and decode — correct even when the line
    has multi-byte chars before/inside the call (e.g. font-icon literals)."""
    first = span_lines[0].encode("utf-8")
    last = span_lines[-1].encode("utf-8")
    prefix = first[:sc].decode("utf-8")
    suffix = last[ec:].decode("utf-8")
    if len(span_lines) == 1:
        call_text = first[sc:ec].decode("utf-8")
    else:
        middle = span_lines[1:-1]
        call_text = newline.join(
            [first[sc:].decode("utf-8")] + middle + [last[:ec].decode("utf-8")])
    return prefix, call_text, suffix


@render_func(use_cache=True)
def address_to_call_parse(input_value, draw_state=None, changed=False, load=False):
    """Address(call statement) -> dict of the call's kwargs via cst_call_to_dict.

    Carries the enclosing span module + address on the dict (dunder keys, ignored
    by focus and by dict_to_cst_call's edit scan) so the save node can write back.
    Re-parses only when the file's mtime changes; otherwise returns the cached
    dict so focus renders every frame."""
    if not isinstance(input_value, Address):
        return changed, None
    address = input_value
    try:
        mtime = address.path.stat().st_mtime
    except OSError:
        mtime = None
    cached = getattr(draw_state, '_call_dict_cache', None)
    if cached is not None and cached[0] == address._hash and cached[1] == mtime:
        return False, cached[2]
    try:
        # Extract JUST the call expression from its line span using the column
        # offsets resolved by _resolve_call_address. parse_module rejects a span
        # that's indented or only part of a statement (e.g. `if c or button(...):`
        # gets "expected INDENT"); slicing out the bare call sidesteps both. The
        # prefix/suffix around the call on its first/last line are kept so the
        # save node can splice the edit back without disturbing them.
        data = address.path.read_bytes()
        newline = _detect_newline(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        span_lines = text.split(newline)[address.start:address.end]
        cols = getattr(address, "_call_cols", None)
        if cols is not None and span_lines:
            call_prefix, call_text, call_suffix = _split_span_at_call(
                span_lines, cols[0], cols[1], newline)
        else:
            # No column info (ast found no embedded call, line fallback). Treat
            # the dedented span as the call; nothing to slice around it.
            import textwrap
            call_text = textwrap.dedent(newline.join(span_lines))
            call_prefix = call_suffix = ""
        span_module = cst.parse_module(call_text)
    except Exception as ex:
        print(f"address_to_call_parse: parse failed for {address.path}: {ex}")
        return False, cached[2] if cached else None
    call_node = _first_call(span_module)
    if call_node is None:
        return False, None
    converter = Melty._converters.get((cst.Call, dict))
    d = converter(call_node)
    d["__call_module__"] = span_module
    d["__address__"] = address
    d["__call_prefix__"] = call_prefix
    d["__call_suffix__"] = call_suffix

    # Human label for the picker/buttons: called-name @ file:line (enclosing fn).
    func_node = call_node.func
    if isinstance(func_node, cst.Name):
        call_name = func_node.value
    elif isinstance(func_node, cst.Attribute):
        call_name = func_node.attr.value
    else:
        call_name = "call"
    fn = getattr(address, "source", None)
    fn_name = getattr(fn, "__name__", None)
    line_no = (address.start or 0) + 1
    suffix = f" ({fn_name})" if fn_name else ""
    d["__label__"] = f"{call_name}  {address.path.name}:{line_no}{suffix}"

    draw_state._call_dict_cache = (address._hash, mtime, d)
    return True, d


def _build_call_code(d):
    """Edited call-kwargs dict -> the full line-span source, with the rebuilt call
    spliced back between its original prefix/suffix.

    Plain + synchronous: rebuilding one call statement (dict_to_cst_call +
    deep_replace of a tiny span module) is microseconds, unlike the class chain's
    whole-module reconstruction. It is NOT a background node — that was the bug:
    as a background node it returned Pending on the edit frame, so the save branch
    was skipped and the edit lost; and Background.run deep-hashed the CST-laden
    dict on the UI thread every frame.

    The call was parsed in isolation (just the expression), so new_module.code is
    the bare call. We re-attach the prefix (indentation + any leading `if … or `)
    and suffix (`[0]:`, etc.) captured at parse time, so an embedded call splices
    back into its statement untouched. Continuation lines keep their original
    absolute indentation (preserved through the round-trip)."""
    span_module = d.get("__call_module__")
    old_call = d.get("__cst__")
    converter = Melty._converters.get((dict, cst.Call))
    new_call = converter(d)
    new_module = span_module.deep_replace(old_call, new_call)
    code = new_module.code.rstrip("\r\n")
    return d.get("__call_prefix__", "") + code + d.get("__call_suffix__", "")


@render_func(background=True)
def recompile_caller_fn(input_value, changed=False):
    """Hotswap the enclosing function from its full, post-save source on disk.

    The save side writes the edited literal into the file; this reads the whole
    enclosing `def` back (fresh — linecache evicted) and reuses the function
    hotswap (_recompile) so the edit goes live. Recompiling just the statement
    span wouldn't redefine anything, which is why this loads the full function."""
    address = input_value
    fn = getattr(address, "source", None)
    if not isinstance(fn, types.FunctionType):
        return False, None
    unwrapped = inspect.unwrap(fn)
    _evict_linecache(str(address.path))
    try:
        src_lines, _start = inspect.getsourcelines(unwrapped)
    except (OSError, TypeError, tokenize.TokenError, SyntaxError) as e:
        print(f"recompile_caller_fn: could not read source for {unwrapped.__name__}: {e}")
        return False, None
    _recompile(unwrapped, "".join(src_lines), str(address.path))
    return True, fn


@render_func(use_cache=True, selectable=False)
def call_dict_to_save(input_value, draw_state=None, unique=None, changed=False,
                      recompile=False, save=False, s_key_pressed=None):
    """Edited call-kwargs dict -> write the call statement back to source.

    Disk writes go through run_button(_do_save) (background), like
    general_parse_to_address — never a synchronous/inline write. The statement
    code is assembled synchronously (it's a single tiny statement, unlike the
    class chain's whole-module reconstruction, so it doesn't need a background
    node) and only on save. Saving fires once per edit (changed) or on Ctrl+S; the
    file mtime bump then makes address_to_call_parse re-parse a fresh dict.

    The recompile button hotswaps the enclosing function (address.source, set by
    caller_to_address) from its full post-save source — so it only appears when
    that function resolved; otherwise this is save-only (shows on next load)."""
    d = input_value
    if not isinstance(d, dict):
        return False, d
    address = d.get("__address__")
    if not isinstance(address, Address):
        imgui.text_colored("Saving unavailable...\nno call address", 1.0, 0.0, 0.0)
        return False, None
    source = address.source

    # Recompile button (async). It reads the enclosing function's FULL post-save
    # source itself, so it does not need the statement code - show it whenever a
    # source resolved, independent of edits.
    if source is not None:
        run_button(recompile_caller_fn, clicked=recompile, name=f"recompile_caller{unique}",
                   with_kwargs={"input_value": address})
        imgui.dummy(1, 1)

    # Save - ALWAYS async, through run_button(_do_save) (_do_save is
    # @render_func(background=True)), exactly like general_parse_to_address. Never a
    # bare _do_save(...) - that wrote to disk synchronously. Fires on an edit
    # (save) or Ctrl+S. The statement code is assembled synchronously (pure
    # CST→string, no disk I/O) and only when saving: doing it every frame
    # deep-hashed the CST on the UI thread, and as a background node it returned
    # Pending on the edit frame and dropped the save.
    save_hotkey = bool(s_key_pressed and s_key_pressed.ctrl)
    if changed or save_hotkey:
        run_button(_do_save, clicked=(save or save_hotkey), name=f"do_save{unique}",
                   with_kwargs={"input_value": address, "code_str": _build_call_code(d)})
        if Melty.cache is not None and draw_state.parent_window is not None:
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






