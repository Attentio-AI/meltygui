"""Reusable file presentation; values and view state are supplied by callers."""
from pathlib import Path
from meltygui.core.runtime.paths import debug_log_path
from meltygui.core.files.file_explorer_core import _trace_browser_size
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.core.rendering.render_funcs import RenderFuncs
from collections import defaultdict
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import with_alpha
from meltygui.core.melty import Melty
from meltygui.state.file_state import FileExplorerState
from meltygui.state.file_state import FileSelectorState
from meltygui.state.file_state import FileTreeState
from meltygui.state.file_state import ShortcutState
from meltygui.core.runtime.toggles import Tint
from meltygui.core.runtime.toggles import Toggles
from meltygui.state.file_state import ROOT
import colorsys
import difflib
import meltygui_imgui as imgui
import os



@render_func(tint=(0.36, 0.46, 0.59), show_bg=True, selectable=False)
def draw_file_tree(input_value: dict, draw_state, root: Path = None):
    """Edit a held directory tree without owning its I/O or host envelope."""
    return RenderFuncs.draw_collection(
        input_value, name=root.name if root is not None else "Files",
        show_add_delete=True, new_item_type=str, temp=True,
        width=draw_state.content_width, disable_scroll=True, show_bg=True,
        child_kwargs={"show_bg": True, "bg_offset": -4,
                      "child_kwargs": {"show_bg": False, "is_tree": False,
                                       "expanded": True},
                      "mode": Modes.FILE_TREE})


@render_func(tint=(0.18, 0.11, 0.11), selectable=False)
def draw_file_metadata(input_value: dict):
    """Edit the supplied metadata mapping without looking up global stores."""
    return RenderFuncs.draw_collection(input_value, name="file_meta", is_tree=True,
                                      show_add_delete=True)



@render_func()
def draw_external_changes(draw_state=None):
    from meltygui.editor.external_changes import ExternalChanges
    from meltygui.editor.pending_save import _diff_lines_with_numbers

    from meltygui.core.melty import Melty
    from meltygui.core.melty import FileWatch
    ExternalChanges._window_ds = draw_state
    RenderFuncs.draw_function(ExternalChanges.dismiss_all, tint=(0, 0, 0, 1), show_bg=False, shadow=False, icon=None)

    # DEBUG: what the tracker will actually see. A file only gets a baseline if
    # its text sat in Melty.code_cache when the fs event fired (old_lines is the
    # popped cache entry), so per watched file show whether it's cached NOW -
    # "no" means an external edit to it would be skipped silently.
    watched = sorted(FileWatch.path_to_draw_states.keys())
    lines = [f"tracked entries: {len(ExternalChanges.originals)}"]
    for p, orig in sorted(ExternalChanges.originals.items()):
        lines.append(f"  {p}  (baseline {len(str(orig).splitlines())} lines)")
    lines.append(f"watched dirs: {len(FileWatch._watched_dirs)}   "
                 f"code_cache entries: {len(Melty.code_cache)}")
    lines.append(f"watched files ({len(watched)}):")
    for p in watched:
        cached = "cached" if p in Melty.code_cache else "NOT in code_cache"
        lines.append(f"  {p}  [{len(FileWatch.path_to_draw_states[p])} views, {cached}]")
    RenderFuncs.draw_text("\n".join(lines), show_name=True, name="watch debug")

    entries = []
    for path, original in list(ExternalChanges.originals.items()):
        # Once the studio itself writes the file, disk equals in-process truth
        # again - the external change is resolved, drop the entry. This
        # also self-heals the truncate race: a mid-write fs event can slip
        # past the event-time is_self_write check (disk hash not final yet)
        # and record a bogus entry, but by render time the flush has landed.
        if FileWatch.is_self_write(path):
            with open(debug_log_path("ext_changes_debug.log"), "a") as _f:
                _f.write(f"pop self_write {path} recorded={FileWatch._self_write_hashes.get(path)} "
                         f"disk={FileWatch._get_hash(path)}\n")
            ExternalChanges.untrack(path)
            continue
        current = Melty.read_code(path)
        if current is not None and \
                current.splitlines(keepends=True) == str(original).splitlines(keepends=True):
            # Drifted back to the baseline (e.g. an outside edit was undone).
            # Only heal when no absorb moved the SYNC frame past the baseline:
            # after an absorb, live code is the absorbed state, so a disk
            # change is a sync→disk drift the next recompile must absorb -
            # untracking here would leave live and disk silently diverged.
            _synced = ExternalChanges.synced.get(path)
            if _synced is None or str(_synced).splitlines(keepends=True) \
                    == current.splitlines(keepends=True):
                with open(debug_log_path("ext_changes_debug.log"), "a") as _f:
                    _f.write(f"pop drift_back {path}\n")
                ExternalChanges.untrack(path)
                continue
        entries.append((path, original, current))

    # Stale-blit guard. The event frame renders with the cache BYPASSED (the
    # _external_change flag), so its output is never captured into the blit
    # tiles - without this, the next frame replays the pre-event tile and the
    # fresh diff flickers away. When the rendered diff actually changes,
    # force-invalidate this window's own subtree so the tiles re-capture.
    # id(current) is a content-free change signal: code_cache holds each text
    # until the watcher pops it, and every re-read is a new str object. The
    # absorption marker joins the sig so the "absorbed" annotation appearing
    # (same texts, new baseline) still re-captures the tiles.
    sig = tuple((p, id(c), ExternalChanges.absorbed.get(p)) for p, _, c in entries)
    if draw_state.misc.get("_ext_sig") != sig:
        draw_state.misc["_ext_sig"] = sig
        Melty.cache.invalidate_up(draw_state._tile_id, force=True, max_depth=6)

    for path, original, current in entries:
        file_name = Path(path).name
        if current is None:
            if RenderFuncs.button(f" Dismiss##{path}", name=f"dismiss {path}",
                                  tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
                ExternalChanges.untrack(path)
            RenderFuncs.draw_text(f"{path}: deleted or unreadable", name=f"{file_name}##{path}")
            continue
        new_lines = current.splitlines(keepends=True)
        old_lines = str(original).splitlines(keepends=True)

        diff = difflib.unified_diff(
            fromfile=path, tofile=path,
            a=old_lines, b=new_lines, n=3,
        )
        # Whole-file diffs, so the @@ hunk numbers ARE the file's line numbers
        # - base 0 (see _diff_lines_with_numbers; pending_save spans shift by
        # their address.start, a whole file starts at 0).
        content_lines, line_numbers = _diff_lines_with_numbers(diff, 0)
        diff_str = "".join(content_lines)

        if RenderFuncs.button(f" Dismiss##{path}", name=f"dismiss {path}",
                              tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
            # Dismiss = accept the on-disk state as the new baseline: the
            # entry drops, and the next external edit re-baselines from
            # whatever the cache holds then.
            ExternalChanges.untrack(path)
        if ExternalChanges.is_absorbed(path, current):
            RenderFuncs.draw_text("absorbed into pending queue (recompiled) — "
                                  "Dismiss to clear",
                                  name=f"absorbed {path}", tint=(0.45, 0.75, 0.45),
                                  show_bg=False)
        RenderFuncs.draw_text(diff_str, show_name=True, name=f"{file_name}##{path}",
                              is_diff=True, line_numbers=line_numbers)


def draw_changed_file_header(path, tint, draw_state, view_id, width, height=23.0,
                             added=None, removed=None, active=False, prefix="", shadow_offset=None,
                             background=True):
    """Shared draw-list file header for the diff column and chat tool summaries.

    `shadow_offset` overrides the tab-state lift for BOTH the wash and the
    button (0 = flat); None keeps the CodeEditor tab offsets.
    `background=False` draws the label only — no wash, no button bg, no
    shadow (the files column passes it for a file with no painted tint).
    Inactive labels use the column's own compare_file_text_* knobs, brighter
    than the tab bar's inactive tabs: the column has no bg to read against.
    """
    from meltygui.editor.file_header import _ellipsize
    from meltygui.editor.source_ui import _tab_text_color
    from meltygui.view.header_view import flat_button
    from meltygui.core.cache.tile_marks import add_shadow

    if active:
        text_color = _tab_text_color(tint, Toggles.CodeEditor.tab_active_text_brightness,
            Toggles.CodeEditor.tab_active_text_saturation, Toggles.CodeEditor.tab_active_text_min_brightness)
        brightness, saturation = Toggles.CodeEditor.tab_active_bg_brightness, Toggles.CodeEditor.tab_active_bg_saturation
        maximum, alpha = Toggles.CodeEditor.tab_active_bg_max_brightness, 0.9
        shadow = Toggles.CodeEditor.tab_active_shadow_offset
    else:
        text_color = _tab_text_color(tint, Toggles.CodeEditor.compare_file_text_brightness,
            Toggles.CodeEditor.tab_inactive_text_saturation, Toggles.CodeEditor.compare_file_text_min_brightness)
        brightness, saturation = Toggles.CodeEditor.tab_inactive_bg_brightness, Toggles.CodeEditor.tab_inactive_bg_saturation
        maximum, alpha = Toggles.CodeEditor.tab_inactive_bg_max_brightness, Toggles.CodeEditor.tab_inactive_bg_alpha
        shadow = Toggles.CodeEditor.tab_inactive_shadow_offset
    if shadow_offset is not None:
        shadow = shadow_offset
    if not background:
        alpha, shadow = 0.0, 0.0
    x, y = imgui.get_cursor_screen_pos()
    dl = imgui.get_window_draw_list()
    channel = Melty.get_channel()
    if Melty.channels_split:
        dl.channels_set_current(channel - 1)
    try:
        if background:
            wash = Melty.style_manager.make_color_rgb(*tint[:3],
                value=Toggles.CodeEditor.compare_file_bg_value, factor=0.8, saturation_scale=1.0)
            if shadow_offset is None or shadow_offset:
                add_shadow((x, y, width, height), offset=1.0 if shadow_offset is None else shadow_offset, corner_radius=4.0)
            dl.add_rect_filled(x, y, x + width, y + height,
                              pack_color(*wash[:3], 1.0), rounding=4.0)
        clicked = flat_button("", draw_state, view_id=view_id, width=width, height=height,
            color=tuple(tint[:3]), factor=0.1, tint_value=brightness, saturation=saturation,
            max_bg_brightness=maximum, alpha=alpha, shadow_offset=shadow, event="left_mouse_down")
    finally:
        if Melty.channels_split:
            dl.channels_set_current(channel)
    inset = Melty.px(2)
    counts = f"+{added} -{removed}" if added is not None and removed is not None else ""
    size = imgui.calc_text_size(counts)
    reserve = size.x + Melty.px(8) if counts else 0
    label = _ellipsize((prefix + "  " if prefix else "") + Path(path).name, max(0, width - inset * 2 - reserve))
    label_size = imgui.calc_text_size(label)
    dl.add_text(x + inset + Melty.px(2), y + (height - label_size.y) * 0.5 - Melty.px(1),
                pack_color(*text_color[:3], 1.0), label)
    if counts:
        left, top = x + width - inset - size.x, y + (height - size.y) * 0.5
        dl.add_text(left, top, pack_color(*Tint.change_count(added=True), 1.0), f"+{added}")
        dl.add_text(left + imgui.calc_text_size(f"+{added} ").x, top,
                    pack_color(*Tint.change_count(added=False), 1.0), f"-{removed}")
    return clicked


@render_func()
def draw_pending_saves():
    from meltygui.editor.pending_save import PendingSave
    from meltygui.editor.pending_save import _pending_diff_memo
    from meltygui.editor.pending_save import _render_diff_blocks

    pass
    from meltygui.core.rendering.render_dispatch import draw_any
    RenderFuncs.draw_function(PendingSave.apply_all_saves, icon="", tint=(0,0,0,1), show_bg=False)
    # name= keeps its draw_state distinct from apply_all_saves' (both calls
    # would otherwise derive the same file-name identity); run_in_thread so
    # the hotswaps run outside the render loop like every other recompile.
    # result_fade_frames: the check mark + "Recompiled ..." summary hold
    # briefly, then fade themselves (same fade model as code_file_io's
    # recompile_status) instead of parking forever.
    RenderFuncs.draw_function(PendingSave.recompile_all, name="recompile_all", icon="",
                              tint=(0,0,0,1), show_bg=False, run_in_thread=True,
                              result_fade_frames=30, temp=True)

    # Result of the last external-change absorb (recompile_all's merge pass).
    # Persistent, no fading if a save rewrote the file - the per-file
    # MERGED / ADOPTED / CONFLICT outcome stays visible until dismissed.
    if PendingSave.merge_results:
        if RenderFuncs.button(" Dismiss##merge_results", name="dismiss merge_results",
                              tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
            PendingSave.merge_results = []
        RenderFuncs.draw_text("\n".join(PendingSave.merge_results), show_name=True,
                              name="external merge results")

    # Drop memo entries whose queue entry is gone (applied/re reverted).
    for _k in list(_pending_diff_memo):
        if _k not in PendingSave.pending_saves:
            _pending_diff_memo.pop(_k, None)
    for address, (codec, kwargs) in list(PendingSave.pending_saves.items()):
        if address in PendingSave.originals:
            original_data = PendingSave.originals[address]
            new_data = kwargs.get("data")
            old_data = original_data
            # Identity-memoized diff (content-free keys - all texts are
            # fresh objects on change): recompute only on real edges. A
            # typing edge (same original, new pending text) splices through
            # the incremental differ off the previous blocks; anything else
            # runs the full matcher once. The +/-/context text and the
            # and line numbers (base = address.start) render straight from
            # the blocks - no unified_diff pass.
            m = _pending_diff_memo.get(address)
            if m is None or m[0] != id(old_data) or m[1] != id(new_data):
                from meltygui.editor.diff import _diff_blocks
                from meltygui.editor.diff import _incremental_diff_blocks
                old_s, new_s = str(old_data), str(new_data)
                blocks = None
                if m is not None and m[0] == id(old_data) and m[2] is not None:
                    prev_new, prev_blocks = m[2]
                    blocks = _incremental_diff_blocks(old_s, prev_new, new_s,
                                                      prev_blocks)
                if blocks is None:
                    blocks = _diff_blocks(old_s, new_s)
                content_lines, line_numbers = _render_diff_blocks(
                    old_s.split("\n"), new_s.split("\n"), blocks,
                    address.start or 0)
                m = (id(old_data), id(new_data), (new_s, blocks),
                     "".join(content_lines), line_numbers)
                _pending_diff_memo[address] = m
            diff_str, line_numbers = m[3], m[4]
            if not diff_str:
                continue               # no-op edit (pending == original)

            file_name = address.path.name
            line_range = (f"({address.start}:{address.end})"
                          if address.start is not None else "(whole file)")
            name = f"{file_name} {line_range}"
            if RenderFuncs.button(f" Revert##{name}", name=f"revert {name}",
                                  tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
                # Revert: queue the load-time text as a fresh pending edit.
                # The entry stays in the queue (so sibling views still resolve
                # their text through pending_data_for and pick up the revert),
                # but data == original makes it a no-op for the diff view,
                # recompile_all, and the eventual disk write.
                PendingSave.queue_save(address, codec, **{**kwargs, "data": old_data})
                # Deferred saves never touch disk, so no watcher wakes on its
                # own - dispatch the file event so every view of this file
                # reloads and picks the reverted text up from the pending cache.
                PendingSave._wake_file_watchers(address.path)
            RenderFuncs.draw_text(diff_str, show_name=True, name=name,
                                  is_diff=True, line_numbers=line_numbers)
        else:
            # Baseline against DISK, never the pending cache: a plain
            # codec.load answers with the queued edit itself, and stamping the
            # edit as its own "original" reclassifies the entry as a no-op
            # (dropped on the next disk write, invisible in this diff) and
            # poisons the merge base. source_text pins the load to disk.
            from meltygui.core.melty import Melty
            disk_text = Melty.read_code(address.path) if address.path is not None else None
            PendingSave.originals[address] = codec.load(
                address=address, **{**kwargs, "source_text": disk_text})
            imgui.text("No original data to compare against for address: {}".format(address))


def paint_breadcrumbs(draw_state, path, click=None, crumb_height=24.0, left_pad=6.0,
                      folder_bg_boost=-0.12, text_mix=0.5, width=None, file_metadata=None):
    """The path strip: every segment of `path` a crumb on the window draw
    list at the cursor, the last one bright (the current directory / the
    file), the rest dim, a painted folder wearing its file-meta tint, the
    hovered one a faint wash. Advances the cursor by the strip's height.
    `click` is the caller's injected left-click position (screen space, or
    None): returns the clicked segment's Path when it is not the last one,
    else None. Shared by the file listing (`draw_file_listing`, the strip
    over the rows) and `draw_breadcrumbs` (the strip on its own)."""
    from meltygui.files.fast_file_explorer import row_tint_bg
    from meltygui.files.fast_file_explorer import tinted_text
    from meltygui.models.file_meta import FileMeta

    # [tint=(0.55, 0.72, 0.95)]
    crumb_separator = "  /  "
    text_rgba = (0.92, 0.92, 0.92, 1.0)
    text_col = pack_color(*text_rgba)
    dim_col = pack_color(0.6, 0.63, 0.68, 1.0)
    crumb_hover_col = pack_color(1.0, 1.0, 1.0, 0.12)

    px = Melty.px
    pad, crumb_h = px(left_pad), px(crumb_height)
    parts = Path(path).parts
    meta = file_metadata
    row_bg = row_tint_bg()
    draw_list = imgui.get_window_draw_list()
    content_w = width if width is not None else (draw_state.content_width or draw_state.width or 240)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    x0, y0 = imgui.get_cursor_screen_pos()
    crumbs = []                              # (x_left, x_right, target Path)
    cx = x0 + pad
    crumb_y = y0 + (crumb_h - imgui.get_font_size()) * 0.5
    for i, part in enumerate(parts):
        label = part if part != os.sep else os.sep
        label_w = imgui.calc_text_size(label).x
        target = Path(*parts[:i + 1])
        crumbs.append((cx - px(3), cx + label_w + px(3), target))
        hovered = hover_ok and cx - px(3) <= mouse_x < cx + label_w + px(3) and y0 <= mouse_y < y0 + crumb_h
        crumb_tint = FileMeta.painted_tint(meta.get(str(target))) if meta is not None else None
        if crumb_tint:
            draw_list.add_rect_filled(cx - px(3), y0 + px(2), cx + label_w + px(3), y0 + crumb_h - px(2),
                                      row_bg(crumb_tint, folder_bg_boost or 0.0), rounding=px(3))
        if hovered:
            draw_list.add_rect_filled(cx - px(3), y0 + px(2), cx + label_w + px(3), y0 + crumb_h - px(2),
                                      crumb_hover_col, rounding=px(3))
        last = i == len(parts) - 1
        crumb_col = (tinted_text(text_rgba, crumb_tint, text_mix) if crumb_tint
                     else text_col if last else dim_col)
        draw_list.add_text(cx, crumb_y, crumb_col, label)
        cx += label_w
        if not last and part == os.sep:
            cx += px(8)  # Keep the root chip separate from the first folder.
        elif not last:
            draw_list.add_text(cx, crumb_y, dim_col, crumb_separator)
            cx += imgui.calc_text_size(crumb_separator).x
    imgui.dummy(content_w, crumb_h)
    if click is not None and crumbs and y0 <= click[1] < y0 + crumb_h:
        for left, right, target in crumbs[:-1]:
            if left <= click[0] < right:
                return target
    return None


class _CrumbTints:
    """draw_dropdown's `row_tints` for a crumb menu, looked up live: a row's
    value is its path, its tint the one painted on it in the file-meta store
    (no dict built over a listing that may never open)."""

    def __init__(self, file_metadata):
        self.file_metadata = file_metadata

    def __bool__(self):
        return self.file_metadata is not None

    def get(self, path):
        from meltygui.models.file_meta import FileMeta
        return FileMeta.painted_tint(self.file_metadata.get(path))


def _crumb_menu(directory, on_path, file_metadata, memo):
    """One crumb's dropdown rows, {icon + name: path}: `directory`'s folders
    then files in the file browser's order (`ordered_rows`), memoized in
    `memo` by the directory's mtime. Hidden entries stay out unless
    `on_path` (the entry the crumb strip continues through) is one."""
    from meltygui.files.fast_file_explorer import row_icon
    from meltygui.model.file_metadata_model import ordered_rows
    from meltygui.model.file_model import _dir_mtime_ns
    from meltygui.model.file_model import list_directory

    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f"\uf07b"
    # [tint=(0.55, 0.72, 0.95)]
    file_icon = f"\uf15b"
    show_hidden = on_path is not None and on_path.name.startswith(".")
    key = (str(directory), _dir_mtime_ns(directory), show_hidden)
    if memo.get("key") != key:
        meta = file_metadata
        rows = ordered_rows(list_directory(directory, show_hidden), meta)
        memo["key"] = key
        memo["rows"] = {
            f"{row_icon(path, is_dir, meta.get(str(path)) if meta is not None else None, folder_icon, file_icon)}  {path.name}":
                str(path)
            for path, is_dir in rows}
    return memo["rows"]


# A strip's scratch, keyed by (host draw_state unique, strip name): its open
# crumbs' (draw_state, DropDownState), the listing memos and the path last
# drawn. Not draw_state.misc - that is the host's persisted view state.
_crumb_strips = {}


def draw_breadcrumbs(input_value: str, draw_state, width=None, crumb_height=24.0, left_pad=6.0,
                     folder_bg_boost=-0.12, text_mix=0.5, file_metadata=None,
                     crumb_pad=4.0, menu_min_width=260.0, name="breadcrumbs"):
    """A path strip whose every segment is a DROPDOWN (`fast_draw_dropdown`: its
    search box, keyboard nav, fast leaf rows) over the directory that
    segment lives in — a folder crumb lists the folder, the file crumb its
    siblings — for a host that shows one path: the code editor draws it
    along the top of the selected file's column (``show_breadcrumbs=True``),
    the mirror of the tab bar along its bottom. `input_value` is the path (a
    directory or a file; the last crumb is the bright one). Rows and crumbs
    wear their painted file-meta tint (`file_metadata`, default the shared
    store). Listings are lazy (read while a menu shows). A strip wider than
    the view drops its leading crumbs. A picked row returns ``(True, path)``
    once — a file or a directory, the host deciding what each means;
    otherwise ``(False, input_value)``.

    A plain function, no @render_func: it draws at the cursor into the
    HOST's tile, `width` wide (default the host's content width), and
    advances the cursor by the strip's height. `draw_state` is the host's;
    two strips on one host take different `name`s."""
    from meltygui.core.layout.dropdown_core import _dd_close
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.files.fast_file_explorer import row_tint_bg
    from meltygui.files.fast_file_explorer import tinted_text
    from meltygui.hdr_color import unpack_color
    from meltygui.models.file_meta import FileMeta
    from meltygui.models.file_meta import file_meta_store
    from meltygui.view.dropdown_view import TRIGGER_TEXT_INSET
    from meltygui.view.dropdown_view import fast_draw_dropdown

    if not input_value:
        return False, input_value
    crumb_separator = "  /  "
    text_rgba = (0.92, 0.92, 0.92, 1.0)
    dim_rgb = (0.6, 0.63, 0.68)

    px = Melty.px
    pad, crumb_h, inner = px(left_pad), px(crumb_height), px(crumb_pad)
    meta = file_metadata if file_metadata is not None else file_meta_store()
    parts = Path(input_value).parts
    targets = [Path(*parts[:i + 1]) for i in range(len(parts))]
    content_w = width if width is not None else (draw_state.content_width or draw_state.width or 240)

    # ── the path moved under an open menu: close it (its crumb may be gone) ──
    strip = _crumb_strips.setdefault((draw_state.unique, name), {"states": {}, "memos": {}, "path": None})
    crumb_states, memos = strip["states"], strip["memos"]
    mine_open = any(ds is Melty.popover_focused_ds for ds, _state in crumb_states.values())
    if strip["path"] != input_value:
        strip["path"] = input_value
        for ds, state in crumb_states.values():
            if Melty.popover_focused_ds is ds:
                Melty.popover_focused_ds = None
                _dd_close(state)
        mine_open = False
    if not mine_open:
        memos.clear()                            # a reopen re-reads icons / order

    # ── widths; a strip wider than the view drops its LEADING crumbs ──
    # A crumb is followed by the dim separator; the root chip ("/") by a gap.
    widths = [imgui.calc_text_size(part).x + TRIGGER_TEXT_INSET + 2 * inner for part in parts]
    separator_w = imgui.calc_text_size(crumb_separator).x
    gaps = [0.0 if i == len(parts) - 1 else px(8) if part == os.sep else separator_w
            for i, part in enumerate(parts)]
    first = 0
    while first < len(parts) - 1 and pad + sum(widths[first:]) + sum(gaps[first:]) > content_w:
        first += 1
    if first == len(parts) - 1:
        # The last crumb alone: it ellipsizes inside the strip (nothing clips it).
        widths[first] = max(px(18), min(widths[first], content_w - pad))

    def menu_source(index):
        """The rows of crumb `index`, resolved by draw_dropdown only while its
        menu shows; the highlight starts on the entry the strip continues through."""
        target = targets[index]
        last = index == len(targets) - 1
        directory = target if not last or target.is_dir() else target.parent
        on_path = (targets[index + 1] if not last
                   else target if directory != target else None)
        rows = _crumb_menu(directory, on_path, meta, memos.setdefault(index, {}))
        held = crumb_states.get(index)
        if held is not None and on_path is not None:
            label = next((label for label, path in rows.items() if path == str(on_path)), None)
            held[1].selected_path = (label,) if label is not None else ()
        return rows

    row_bg = row_tint_bg()
    draw_list = imgui.get_window_draw_list()
    x0, y0 = imgui.get_cursor_screen_pos()
    cx = x0 + pad
    separator_y = y0 + (crumb_h - imgui.get_font_size()) * 0.5
    dim_col = pack_color(*dim_rgb, 1.0)
    picked = None
    for i in range(first, len(parts)):
        target, last = targets[i], i == len(parts) - 1
        crumb_tint = FileMeta.painted_tint(meta.get(str(target)))
        if crumb_tint:
            draw_list.add_rect_filled(cx, y0 + px(2), cx + widths[i], y0 + crumb_h - px(2),
                                      row_bg(crumb_tint, folder_bg_boost or 0.0), rounding=px(3))
            text_color = unpack_color(tinted_text(text_rgba, crumb_tint, text_mix))[:3]
        else:
            text_color = text_rgba[:3] if last else dim_rgb
        imgui.set_cursor_screen_pos((cx, y0))
        # STABLE identity (the index, never the path): the popover is a
        # latching window, so a name keyed on the file would orphan it.
        result = fast_draw_dropdown(
            str(target), collection={}, collection_source=lambda index=i: menu_source(index),
            display_label=parts[i], name=f"{name}_crumb_{i}", width=widths[i], height=crumb_h,
            trigger_height=crumb_h, show_header=False, shadow=False, show_button_bg=False,
            text_pad=inner, trigger_text_color=text_color, trigger_caret=("", ""),
            row_tints=_CrumbTints(meta), menu_min_width=px(menu_min_width), return_extras=True)
        crumb_ds = result[2] if len(result) > 2 else None
        state = (getattr(crumb_ds, "misc", None) or {}).get("drop_down_state") if crumb_ds is not None else None
        if state is not None:
            crumb_states[i] = (crumb_ds, state)
        if result[0] and result[1]:
            picked = result[1]
        cx += widths[i]
        if not last and parts[i] != os.sep:
            draw_list.add_text(cx, separator_y, dim_col, crumb_separator)
        cx += gaps[i]
    imgui.set_cursor_screen_pos((x0, y0))
    imgui.dummy(content_w, crumb_h)
    if picked is not None and picked != input_value:
        request_render()
        return True, str(picked)
    return False, input_value


@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=False,
             show_add_delete=False, is_tree=False, show_bg=False, shadow=False)
def draw_file_listing(input_value: str, draw_state, explorer_state: FileExplorerState, file_metadata=None,
                      left_mouse_down=False, left_mouse_double_clicked=False,
                      right_mouse_down=False,
                      ctrl_up_key_pressed=False, up_key_pressed=False, down_key_pressed=False,
                      enter_key_pressed=False, escape_key_pressed=False,
                      row_height=20.0, left_pad=6.0, glyph_width=18.0, crumb_height=24.0,
                      show_tint_chips=True, chip_size=17.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                      select_boost=0.22, plain_select_boost=0.06, select_shadow=2.0,
                      select_rounding=3.0, chip_mix=0.55, hover_boost=0.06, hover_alpha=0.05, text_mix=0.5, icon_mix=0.9,
                      folder_bg_boost=-0.12, folder_bg_rounding=0.0, drag_rows=True, menu_target=None,
                      type_to_search=True, search_tint=(1.0, 0.82, 0.3), search_dim=0.45,
                      search_flash_frames=36, show_crumbs=True, show_hidden=None,
                      **kwargs):
    """The path strip + rows of one directory (see the module docstring).
    `show_crumbs=False` leaves the strip out (a host drawing the crumbs in
    its own toolbar, where they stay put while the rows scroll).
    Returns ``(True, path)`` on navigation / a file double-click, else
    ``(False, input_value)``. `show_tint_chips` puts the tint chip / brush
    before each row's icon; `default_tint` is what the brush stamps. The
    selected row is its own tint brightened by `select_boost` (an unpainted
    row: `default_tint` by the smaller `plain_select_boost`, so it stays
    close to the background), lifted off the list by an add_shadow of
    `select_shadow` depth (0 disables it). `chip_mix` pulls the tint chip's
    colour toward its row background (0 = the raw tint). Hover is the same
    tint brightened by `hover_boost` on the selected row; any other hovered
    row gets only a faint white wash of `hover_alpha`. A painted
    row wears its tint on its text (mixed `text_mix` toward the tint) and
    its icon (`icon_mix`, stronger), not as a row background. `type_to_search`
    is the keyboard search of the module docstring: `search_tint` colours
    the matched letters and the pill, the non-matching rows' text fades to
    `search_dim` of its alpha while there are matches, and the flash on the
    row a keystroke lands on fades over `search_flash_frames` frames."""
    from meltygui.model.file_model import _dir_mtime_ns
    from meltygui.files.fast_file_explorer import _scroll_row_into_view
    from meltygui.model.file_metadata_model import apply_row_drop
    from meltygui.files.fast_file_explorer import chip_swatch
    from meltygui.files.fast_file_explorer import claim_keyboard
    from meltygui.model.file_model import list_directory
    from meltygui.model.file_metadata_model import ordered_rows
    from meltygui.files.fast_file_explorer import row_icon
    from meltygui.files.fast_file_explorer import row_tint_bg
    from meltygui.files.fast_file_explorer import search_hits
    from meltygui.files.fast_file_explorer import search_keys
    from meltygui.files.fast_file_explorer import search_typed
    from meltygui.model.file_metadata_model import set_row_order
    from meltygui.files.fast_file_explorer import tint_control
    from meltygui.model.file_metadata_model import set_row_tint
    from meltygui.files.fast_file_explorer import tinted_text
    from meltygui.core.files.file_explorer_core import watch_directory
    from meltygui.models.file_meta import FileMeta
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.cache.tile_marks import add_shadow
    from meltygui.core.cache.tile_marks import clear_glows
    from meltygui.core.input.drag_drop_core import DragDrop

    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    file_icon = f""
    text_rgba = (0.92, 0.92, 0.92, 1.0)
    text_col = pack_color(*text_rgba)
    dim_col = pack_color(0.6, 0.63, 0.68, 1.0)
    folder_rgba = (0.78, 0.84, 0.92, 1.0)
    folder_col = pack_color(*folder_rgba)
    search_wash = pack_color(search_tint[0], search_tint[1], search_tint[2], 0.30)
    search_col = pack_color(search_tint[0], search_tint[1], search_tint[2], 1.0)
    no_match_col = pack_color(0.95, 0.55, 0.5, 1.0)
    hover_wash = pack_color(1.0, 1.0, 1.0, hover_alpha)

    state = explorer_state
    # The selected row's add_shadow is RETAINED under this draw_state until
    # the caller opens its group again: a selection that vanishes (the file
    # trashed, Esc, a new directory) would otherwise keep its shadow.
    clear_glows(draw_state)
    px = Melty.px
    row_h, pad, glyph_w, crumb_h = px(row_height), px(left_pad), px(glyph_width), px(crumb_height)
    chip = px(chip_size) if show_tint_chips else 0.0
    # The tint control leads the row; icon & name shift right past it.
    chip_x = 0.0
    text_x = pad + (chip + px(6) if show_tint_chips else 0.0)
    directory = Path(input_value if input_value else Path.home()).expanduser()
    dir_key = str(directory)
    draw_list = imgui.get_window_draw_list()
    content_w = draw_state.content_width or (draw_state.width or 240)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_down.x, left_mouse_down.y)
             if (left_mouse_down and hasattr(left_mouse_down, "x")) else None)
    double_click = ((left_mouse_double_clicked.x, left_mouse_double_clicked.y)
                    if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None)
    right_press = ((right_mouse_down.x, right_mouse_down.y)
                   if (right_mouse_down and hasattr(right_mouse_down, "x")) else None)
    meta = file_metadata
    row_bg = row_tint_bg()

    def navigate(target):
        """Leave `dir_key` for `target`: remember where this listing was
        scrolled so a return lands there, and hand the path to the caller."""
        state.scroll_by_dir[dir_key] = draw_state.scroll_offset[1]
        request_render()
        return True, str(target)

    # ── a new directory: restore the scroll, drop the selection, move the watch ──
    if state._last_dir != dir_key:
        state._last_dir = dir_key
        state.selected = None
        state._search, state._search_for = "", None
        draw_state.scroll_offset = (0.0, state.scroll_by_dir.get(dir_key, 0.0))
        draw_state.invalidate()
    watch_directory(draw_state, state, dir_key)

    # ── listing, memoized by the directory's mtime (renames / new files bump it) ──
    mtime = _dir_mtime_ns(directory)
    listing = state._listing
    if show_hidden is not None:
        state.show_hidden = show_hidden
    if listing is None or listing[:3] != (dir_key, mtime, state.show_hidden):
        listing = state._listing = (dir_key, mtime, state.show_hidden,
                                    list_directory(directory, state.show_hidden))
    rows = ordered_rows(listing[3], meta)

    # ── the path strip: every segment a crumb; click = jump there ──
    if show_crumbs:
        target = paint_breadcrumbs(draw_state, directory, click=click,
                                   crumb_height=crumb_height, left_pad=left_pad,
                                   file_metadata=file_metadata,
                                   folder_bg_boost=folder_bg_boost, text_mix=text_mix)
        if target is not None:
            return navigate(target)

    # ── content height: rows + the top inset (the fast_dock rule) ──
    rows_x, rows_y = imgui.get_cursor_screen_pos()
    # ── a painted directory: a tint wash below the breadcrumb strip ──
    dir_tint = FileMeta.painted_tint(meta.get(dir_key)) if meta is not None else None
    if dir_tint and folder_bg_boost is not None:
        wash = getattr(draw_state, "abs_clip_rect", None)
        if wash is None:
            wash = (draw_state.abs_left, draw_state.abs_top,
                    draw_state.abs_left + (draw_state.width or 0),
                    draw_state.abs_top + (draw_state.height or 0))
        wash_top = max(wash[1], rows_y)
        if wash_top < wash[3]:
            draw_list.add_rect_filled(wash[0], wash_top, wash[2], wash[3],
                                      row_bg(dir_tint, folder_bg_boost), rounding=px(folder_bg_rounding))

    top_inset = (rows_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(content_w, max(1.0, len(rows) * row_h + max(0.0, top_inset)))

    # ── input on rows ──
    # The tint control's widget (chip / brush) at the row's left edge is
    # the chip's own click, never the row's - a double-click there must
    # not navigate.
    chip_x = rows_x + pad
    clip = getattr(draw_state, "abs_clip_rect", None)

    drag_left = chip_x + chip + px(2) if show_tint_chips else rows_x

    def row_at(point, chips=False):
        if point is None or not (rows_x <= point[0] <= rows_x + content_w):
            return None
        if not chips and show_tint_chips and point[0] < drag_left:
            return None
        index = int((point[1] - rows_y) // row_h)
        return index if 0 <= index < len(rows) else None

    selected_index = None
    if state.selected is not None:
        for i, (path, _is_dir) in enumerate(rows):
            if str(path) == state.selected:
                selected_index = i
                break

    # ── the keyboard: the type-to-search queue when this listing owns it,
    # else the hover-routed key params (the find box has the keyboard, say) ──
    owns_keys = type_to_search and claim_keyboard(draw_state)
    query = state._search if type_to_search else ""
    step = (1 if down_key_pressed else 0) - (1 if up_key_pressed else 0)
    if owns_keys:
        keys = search_keys()
        query, step, enter_key_pressed, ctrl_up_key_pressed, escape_key_pressed = \
            search_typed(query, keys)
        if keys:
            draw_state.invalidate()
            request_render()
    if query != state._search:
        state._search = query
    rows_top = rows_y - draw_state.abs_top + draw_state.scroll_offset[1]
    view_rect = clip if clip is not None else (
        draw_state.abs_left, draw_state.abs_top,
        draw_state.abs_left + (draw_state.width or 0), draw_state.abs_top + (draw_state.height or 0))

    def flash_row(index):
        """Melty.emphasize on row `index`: a fire-and-forget flash whose rect
        follows the scroll, scissored to the listing (keyed by the row's
        path, so a new target starts a fresh fade)."""
        top = rows_top + index * row_h

        def rect(ds=draw_state, top=top, h=row_h, x0=rows_x, w=content_w):
            y = ds.abs_top + top - ds.scroll_offset[1]
            return (x0, y, x0 + w, y + h)

        def clip_rect(ds=draw_state, fallback=view_rect):
            return getattr(ds, "abs_clip_rect", None) or fallback

        Melty.emphasize(f"explorer search {draw_state._tile_id} {rows[index][0]}", rect,
                        clip=clip_rect, rounding=px(select_rounding),
                        fade_frames=search_flash_frames)

    def jump_to_row(index):
        """Select row `index` and bring it into view; the flash only when the
        landing row CHANGES (a keystroke that keeps the same best match, or
        the same step target, is quiet)."""
        moved = index != selected_index
        state.selected = str(rows[index][0])
        _scroll_row_into_view(draw_state, index, row_h, rows_top, centre=True,
                              content_h=len(rows) * row_h + max(0.0, top_inset))
        if moved:
            flash_row(index)
        request_render()
        return index

    hit = row_at(double_click)
    if hit is not None:
        return navigate(rows[hit][0])
    hit = row_at(click)
    if hit is not None:
        state.selected = str(rows[hit][0])
        selected_index = hit
        request_render()
    # A right-press selects the row under it (the chip column included): the
    # wrapper opens the context menu on the release, on that row.
    hit = row_at(right_press, chips=True)
    if hit is not None:
        state.selected = str(rows[hit][0])
        selected_index = hit
        draw_state.invalidate()
        request_render()
    elif right_press is not None and state.selected is not None:
        # Empty space: the menu acts on the dir, not a stale selection.
        state.selected = None
        selected_index = None
        draw_state.invalidate()
        request_render()
    if menu_target is not None:
        menu_target["path"] = state.selected if selected_index is not None else dir_key
    if ctrl_up_key_pressed and directory.parent != directory:
        return navigate(directory.parent)
    if enter_key_pressed and selected_index is not None:
        return navigate(rows[selected_index][0])
    if escape_key_pressed and state.selected is not None:
        state.selected = None
        draw_state.invalidate()
        request_render()

    # ── the search: rank the rows, land on the best, step through the rest ──
    # A query change (or a selection that left no matches: a click in the
    # directory changed under the watch) lands on the best match; Up / Down
    # / Tab walk the matches in listing order, wrapping. Without a query the
    # steps walk the whole listing as before. No match: the selection stays
    # where it was and the listing says so.
    hits, best = search_hits(rows, query) if query else ([], None)
    hit_rows = {index: spans for index, _rank, spans in hits}
    if query and hits:
        positions = [index for index, _rank, _spans in hits]
        if state._search_for != query or selected_index not in hit_rows:
            selected_index = jump_to_row(positions[best])
        elif step:
            at = positions.index(selected_index)
            selected_index = jump_to_row(positions[(at + step) % len(positions)])
    elif step and rows and not query:
        selected_index = (0 if selected_index is None and step > 0
                          else len(rows) - 1 if selected_index is None
                          else max(0, min(len(rows) - 1, selected_index + step)))
        state.selected = str(rows[selected_index][0])
        _scroll_row_into_view(draw_state, selected_index, row_h, rows_top)
        request_render()
    state._search_for = query if query else None
    if query and not hits and step:
        request_render()
    dimmed = bool(query and hits)

    # ── rows: viewport-culled, straight to the draw list ──
    text_y_pad = (row_h - imgui.get_font_size()) * 0.5
    ghost_alpha = 0.9
    drag_keys = []          # the visible rows' paths, in on_drag call order
    first_visible = None    # index into `rows` of drag_keys[0]
    for i, (path, is_dir) in enumerate(rows):
        ry0 = rows_y + i * row_h
        ry1 = ry0 + row_h
        if clip is not None and (ry1 < clip[1] or ry0 > clip[3]):
            continue
        key = str(path)
        entry = meta.get(key) if meta is not None else None
        tint = FileMeta.painted_tint(entry)
        icon = row_icon(path, is_dir, entry, folder_icon, file_icon)
        spans = hit_rows.get(i) if dimmed else None
        name_rgba, glyph_rgba = (folder_rgba if is_dir else text_rgba), text_rgba
        if dimmed and spans is None:
            name_rgba = name_rgba[:3] + (name_rgba[3] * search_dim,)
            glyph_rgba = glyph_rgba[:3] + (glyph_rgba[3] * search_dim,)
        if tint:
            name_col = tinted_text(name_rgba, tint, text_mix)
            icon_col = tinted_text(glyph_rgba, tint, icon_mix)
        elif dimmed and spans is None:
            name_col = pack_color(*name_rgba)
            icon_col = pack_color(*glyph_rgba)
        else:
            name_col = icon_col = folder_col if is_dir else text_col
        if drag_rows:
            # The tab bar's immediate-mode DragDrop: the row (past the chip
            # column) is its own drag handle. While THIS row is the drag,
            # on_drag has parked the cursor at the ghost: paint the row
            # there on the overlay, but leave the inline row alone
            # (DragDrop treats it as the home / drop target).
            if first_visible is None:
                first_visible = i
            drag_keys.append(path)
            drag = DragDrop.on_drag((drag_left, ry0, rows_x + content_w, ry1), key=key,
                                    draw_state=draw_state)
            if drag:
                ghost = drag.draw_list
                ghost.add_rect_filled(drag.x, drag.y, drag.x + drag.w, drag.y + drag.h,
                                      pack_color(*row_bg.rgb(tint or default_tint, select_boost),
                                                 ghost_alpha),
                                      rounding=px(select_rounding))
                ghost.add_text(drag.x + px(4), drag.y + text_y_pad, icon_col, icon)
                ghost.add_text(drag.x + px(4) + glyph_w, drag.y + text_y_pad, name_col, path.name)
                DragDrop.end_drag()
                continue
        row_hovered = hover_ok and rows_x <= mouse_x <= rows_x + content_w and ry0 <= mouse_y < ry1
        # Selection / hover are brighter steps of the row's own tint
        # (the default tint when unpainted); they stack.
        boost = (select_boost if tint else plain_select_boost) if i == selected_index else 0.0
        if i == selected_index and select_shadow:
            add_shadow((rows_x, ry0, content_w, row_h), offset=select_shadow,
                       corner_radius=px(select_rounding), clip=clip, draw_state=draw_state)
        if boost:
            draw_list.add_rect_filled(rows_x, ry0, rows_x + content_w, ry1,
                                      row_bg(tint or default_tint,
                                             boost + (hover_boost if row_hovered else 0.0)),
                                      rounding=px(select_rounding))
        elif row_hovered:
            draw_list.add_rect_filled(rows_x, ry0, rows_x + content_w, ry1, hover_wash,
                                      rounding=px(select_rounding))
        draw_list.add_text(rows_x + text_x, ry0 + text_y_pad, icon_col, icon)
        name_x = rows_x + text_x + glyph_w
        if spans:
            # The matched letters: a wash of the search tint over them.
            name = path.name
            for start, end in spans:
                sx0 = name_x + imgui.calc_text_size(name[:start]).x
                sx1 = sx0 + imgui.calc_text_size(name[start:end]).x
                draw_list.add_rect_filled(sx0 - px(1), ry0 + px(2), sx1 + px(1), ry1 - px(2),
                                          search_wash, rounding=px(2))
        draw_list.add_text(name_x, ry0 + text_y_pad, name_col, path.name)
        if show_tint_chips:
            swatch = None
            if tint:
                swatch = chip_swatch(tint, row_bg.rgb(tint, boost), chip_mix)
            tint_control(draw_state, key, tint, chip_x, ry0 + (row_h - chip) * 0.5, chip,
                         ry0 + text_y_pad, row_hovered, default_tint, swatch=swatch,
                         setter=lambda value, path=key: set_row_tint(meta, path, value),
                         show_brush=i == selected_index)

    # ── the search pill: the query and "n of m", bottom right of the view ──
    if query:
        icon_search = "\uf002"
        count = (f"{positions.index(selected_index) + 1} of {len(hits)}"
                 if hits and selected_index in hit_rows else "no match")
        pill_h = px(26)
        pad_x, gap = px(11), px(12)
        query_w = imgui.calc_text_size(query).x
        icon_w = imgui.calc_text_size(icon_search).x
        count_w = imgui.calc_text_size(count).x
        pill_w = pad_x + icon_w + px(8) + query_w + gap + count_w + pad_x
        px1 = view_rect[2] - px(14)
        py1 = view_rect[3] - px(12)
        px0 = max(view_rect[0] + px(6), px1 - pill_w)
        py0 = py1 - pill_h
        draw_list.add_rect_filled(px0 + px(1), py0 + px(2), px1 + px(1), py1 + px(2),
                                  pack_color(0.0, 0.0, 0.0, 0.35), rounding=pill_h * 0.5)
        draw_list.add_rect_filled(px0, py0, px1, py1,
                                  pack_color(*row_bg.rgb(default_tint, -0.02), 0.97),
                                  rounding=pill_h * 0.5)
        draw_list.add_rect(px0, py0, px1, py1, pack_color(1.0, 1.0, 1.0, 0.14),
                           rounding=pill_h * 0.5)
        pill_ty = py0 + (pill_h - imgui.get_font_size()) * 0.5
        draw_list.add_text(px0 + pad_x, pill_ty, search_col, icon_search)
        draw_list.add_text(px0 + pad_x + icon_w + px(8), pill_ty, text_col, query)
        draw_list.add_text(px1 - pad_x - count_w, pill_ty,
                           dim_col if hits else no_match_col, count)

    # ── close the drag body: paint the between-row slots, apply a drop ──
    # A reorder lands in on_drag call order = the visible rows, a contiguous
    # slice of `rows`; splice the new slice in and stamp the whole
    # directory's order (folders and files together - the stamps are the order
    # from now on). Cross-collection kinds ("insert" / "remove") are ignored:
    # rows only reorder here.
    if drag_rows:
        drop = DragDrop.on_drop(draw_state=draw_state)
        order = apply_row_drop(rows, drag_keys, first_visible, drop)
        if order is not None:
            set_row_order(meta, order)
            draw_state.invalidate()
            request_render()

    return False, input_value


@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=True, use_cache=True, freeze_resize=True,
             show_add_delete=False, is_tree=False, show_bg=False, shadow=False)
def draw_shortcuts(input_value: str, draw_state, file_metadata=None, left_mouse_clicked=False,
                   shortcut_state: ShortcutState = None, row_height=22.0,
                   show_tint_chips=True, chip_size=17.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                   show_projects=True, **kwargs):
    """The shortcuts column on its own: home, the XDG user directories and
    the root as draw-list rows, then a **Projects** section — every folder
    marked with meltygui.mark_project (the shared file-meta store's flag) —
    and a click returns ``(True, path)`` once. `input_value` is the
    directory the host shows (or None): the deepest shortcut / project
    holding it is the CURRENT row (brighter, lifted by a shadow, its brush
    showing). Every row leads with its folder's tint control (the same
    file-meta tint the listings wear). Shortcuts drag to reorder, the order
    persisting in `shortcut_state`; projects keep the store's path order.
    The explorer draws this in its first cell; the code editor draws it as
    a leading column (`show_shortcuts=True`), a pick selecting the project
    in its injected `EditorProjectState`."""
    from meltygui.core.runtime.extensions import source_folders as project_roots
    from meltygui.files.fast_file_explorer import chip_swatch
    from meltygui.files.fast_file_explorer import row_tint_bg
    from meltygui.files.fast_file_explorer import shortcut_directories
    from meltygui.files.fast_file_explorer import tint_control
    from meltygui.model.file_metadata_model import set_row_tint
    from meltygui.files.fast_file_explorer import tinted_text
    from meltygui.models.file_meta import FileMeta
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.cache.tile_marks import add_shadow
    from meltygui.core.cache.tile_marks import clear_glows
    from meltygui.core.input.drag_drop_core import DragDrop

    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    computer_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    left_pad = 8.0
    # [tint=(0.55, 0.72, 0.95)]
    glyph_width = 20.0
    text_rgba = (0.85, 0.88, 0.92, 1.0)
    text_col = pack_color(*text_rgba)
    # [tint=(0.55, 0.72, 0.95)]
    hover_boost = 0.06
    # [tint=(0.55, 0.72, 0.95)]
    hover_alpha = 0.05
    # [tint=(0.55, 0.72, 0.95)]
    text_mix = 0.5
    # [tint=(0.55, 0.72, 0.95)]
    icon_mix = 0.9
    hover_wash = pack_color(1.0, 1.0, 1.0, hover_alpha)

    px = Melty.px
    clear_glows(draw_state)      # the current row's retained shadow
    directory = Path(input_value).expanduser() if input_value else None
    draw_list = imgui.get_window_draw_list()
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_clicked.x, left_mouse_clicked.y)
             if (left_mouse_clicked and hasattr(left_mouse_clicked, "x")) else None)
    chip = px(chip_size) if show_tint_chips else 0.0
    text_x = px(left_pad) + (chip + px(6) if show_tint_chips else 0.0)
    meta = file_metadata
    row_bg = row_tint_bg()
    x, y = imgui.get_cursor_screen_pos()
    width = float(draw_state.content_width or draw_state.width or px(190))
    row_h = px(row_height)
    shortcuts = shortcut_directories()
    ranks = {key: i for i, key in enumerate(shortcut_state.order)}
    shortcuts.sort(key=lambda item: ranks.get(str(item[1]), len(ranks)))
    # The second section: every folder marked as a project - an alphabetical
    # list of the projects, the same rows in path order, no drag-reorder
    # (the set is the store's, not this column's).
    projects = ([(root.name or str(root), root) for root in project_roots()]
                if show_projects else [])
    # The shortcut / project that holds the current directory: the deepest one.
    current = None
    if directory is not None:
        for label, path in shortcuts + projects:
            if path == directory or path in directory.parents:
                if current is None or len(path.parts) > len(current.parts):
                    current = path
    text_y_pad = (row_h - imgui.get_font_size()) * 0.5
    chip_x = x + px(left_pad)
    # The tint control's column: the chip's own click, never the row's.
    click_left = chip_x + chip + px(2) if show_tint_chips else x
    section_gap = row_h * 0.5

    def draw_rows(rows, y, draggable):
        """One section of rows from `y` down; returns the picked path."""
        picked = None
        for i, (label, path) in enumerate(rows):
            ry0 = y + i * row_h
            ry1 = ry0 + row_h
            key = str(path)
            tint = FileMeta.painted_tint(meta.get(key)) if meta is not None else None
            icon = computer_icon if path == Path(os.sep) else folder_icon
            label_col = tinted_text(text_rgba, tint, text_mix) if tint else text_col
            icon_col = tinted_text(text_rgba, tint, icon_mix) if tint else text_col
            drag = DragDrop.on_drag((click_left, ry0, x + width, ry1), key=key,
                                    draw_state=draw_state) if draggable else None
            if drag:
                ghost = drag.draw_list
                ghost.add_rect_filled(drag.x, drag.y, drag.x + drag.w, drag.y + drag.h,
                                      row_bg(tint or default_tint, 0.22), rounding=px(4))
                ghost.add_text(drag.x + px(4), drag.y + text_y_pad, icon_col, icon)
                ghost.add_text(drag.x + px(4) + px(glyph_width), drag.y + text_y_pad,
                               label_col, label)
                DragDrop.end_drag()
                continue
            row_hovered = hover_ok and x <= mouse_x < x + width and ry0 <= mouse_y < ry1
            # The current row: its tint (default when unpainted), brighter,
            # lifted above the column by a soft shadow; hover is a smaller
            # brightening of the same tint.
            boost = (0.22 if tint else 0.06) if path == current else 0.0
            if path == current:
                add_shadow((x, ry0, width, row_h), offset=2.0, corner_radius=px(4),
                           clip=getattr(draw_state, "abs_clip_rect", None), draw_state=draw_state)
            if boost:
                draw_list.add_rect_filled(x, ry0, x + width, ry1,
                                          row_bg(tint or default_tint,
                                                 boost + (hover_boost if row_hovered else 0.0)),
                                          rounding=px(4))
            elif row_hovered:
                draw_list.add_rect_filled(x, ry0, x + width, ry1, hover_wash, rounding=px(4))
            draw_list.add_text(x + text_x, ry0 + text_y_pad, icon_col, icon)
            draw_list.add_text(x + text_x + px(glyph_width), ry0 + text_y_pad, label_col, label)
            if (click is not None and click_left <= click[0] < x + width and ry0 <= click[1] < ry1
                    and path != directory):
                picked = path
            if show_tint_chips:
                swatch = None
                if tint:
                    swatch = chip_swatch(tint, row_bg.rgb(tint, boost))
                tint_control(draw_state, key, tint, chip_x, ry0 + (row_h - chip) * 0.5, chip,
                             ry0 + text_y_pad, row_hovered, default_tint, swatch=swatch,
                             setter=lambda value, path=key: set_row_tint(meta, path, value),
                             show_brush=path == current)
        return picked

    picked = draw_rows(shortcuts, y, draggable=True)
    total_h = len(shortcuts) * row_h
    if projects:
        # A dim "Projects" heading half a row below the shortcuts.
        head_y = y + total_h + section_gap
        draw_list.add_text(x + text_x, head_y + text_y_pad,
                           imgui.get_color_u32_rgba(*text_rgba[:3], text_rgba[3] * 0.55),
                           "Projects")
        picked = draw_rows(projects, head_y + row_h, draggable=False) or picked
        total_h += section_gap + row_h + len(projects) * row_h
    drop = DragDrop.on_drop(draw_state=draw_state)
    if drop is not None and drop.kind == "reorder" and drop.apply(shortcuts):
        shortcut_state.order = [str(path) for _label, path in shortcuts]
        picked = None
    imgui.dummy(width, total_h)
    if picked is not None:
        request_render()
        return True, str(picked)
    return False, input_value


@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=True,
             show_add_delete=False, is_tree=False, show_bg=False, shadow=False)
def draw_fast_file_explorer(input_value: str, draw_state, file_metadata=None, column_edges=None,
                            left_mouse_clicked=False, ctrl_up_key_pressed=False,
                            shortcuts_width=190.0, shortcut_row_height=22.0, column_gap=6.0,
                            show_tint_chips=True, chip_size=17.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                            context_menu=None, drag_rows=True, folder_bg_boost=-0.12,
                            folder_bg_rounding=0.0, type_to_search=True, show_crumbs=True,
                            layout_out=None, show_hidden=None, **kwargs):
    """A ColumnLayout with two cells: `draw_shortcuts` (a click navigates)
    and `draw_file_listing`, sharing one draggable edge
    (`column_edges`, persisted by auto-state). Returns what the listing
    returns; Ctrl+Up works from anywhere over the explorer. Shortcut rows
    wear their directory's tint like the listing's, with the same leading
    tint control (`show_tint_chips`, `chip_size`, `default_tint`).
    `context_menu` = {label: callable(path)} is the listing's right-click
    menu; each callable gets the path of the right-clicked row, or of the
    directory (see the module docstring). `drag_rows` / `folder_bg_boost`
    go to the listing, as does `type_to_search` (the keyboard search of the
    module docstring; False leaves the keyboard alone), and `show_crumbs`
    (False drops the listing's path strip for a host that draws its own).
    `layout_out`, a dict, receives ``listing_left``: the absolute x where
    the listing column's content starts, so a host toolbar can line up with
    it (the frame's value lands after this call; a host drawing above the
    explorer reads last frame's). Shortcuts drag to reorder, with their own
    persisted order."""
    _trace_browser_size("file-explorer-size", draw_state,
                        content_width=draw_state.content_width,
                        size_change=getattr(draw_state, "size_change", None))
    from meltygui.files.fast_file_explorer import row_tint_bg
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.core.cache.tile_marks import clear_glows
    from meltygui.core.layout.column_core import ColumnLayout

    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    computer_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    left_pad = 8.0
    # [tint=(0.55, 0.72, 0.95)]
    glyph_width = 20.0
    text_rgba = (0.85, 0.88, 0.92, 1.0)
    text_col = pack_color(*text_rgba)
    # [tint=(0.55, 0.72, 0.95)]
    hover_boost = 0.06
    # [tint=(0.55, 0.72, 0.95)]
    hover_alpha = 0.05
    # [tint=(0.55, 0.72, 0.95)]
    text_mix = 0.5
    # [tint=(0.55, 0.72, 0.95)]
    icon_mix = 0.9
    hover_wash = pack_color(1.0, 1.0, 1.0, hover_alpha)

    px = Melty.px
    clear_glows(draw_state)      # The current shortcut's retained shadow (see the listing)
    directory = Path(input_value if input_value else Path.home()).expanduser()
    draw_list = imgui.get_window_draw_list()
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_clicked.x, left_mouse_clicked.y)
             if (left_mouse_clicked and hasattr(left_mouse_clicked, "x")) else None)
    chip = px(chip_size) if show_tint_chips else 0.0
    text_x = px(left_pad) + (chip + px(6) if show_tint_chips else 0.0)
    meta = file_metadata
    row_bg = row_tint_bg()
    # The listing writes what the right-click landed on here each run; the
    # menu item callables (built here, run by the wrapper's items menu with no
    # label) read it when picked.
    menu_target = {"path": str(directory)}
    menu_items = None
    if context_menu:
        menu_items = {label: (lambda _action=action: _action(menu_target["path"]))
                      for label, action in context_menu.items()}

    # The band: from the flow cursor to the bottom of the view.
    body_top = imgui.get_cursor_screen_pos()[1]
    body_bottom = draw_state.abs_top + (draw_state.height or px(600))
    body_height = max(px(120), body_bottom - body_top)
    columns = ColumnLayout(draw_state, 2, column_edges=column_edges,
                           column_widths=[shortcuts_width, None], column_mins=[110, 200],
                           padding=px(column_gap), padding_y=0, border_color=None)

    result = (False, input_value)
    picked = None
    with columns.cell(0, height=body_height) as width:
        changed_s, picked_s = draw_shortcuts(
            str(directory), name="shortcuts", file_metadata=file_metadata, width=width, height=body_height,
            row_height=shortcut_row_height, show_tint_chips=show_tint_chips,
            chip_size=chip_size, default_tint=default_tint, disable_scroll=True,
            left_mouse_clicked=left_mouse_clicked)
        picked = Path(picked_s) if changed_s else None
    with columns.cell(1, height=body_height) as width:
        if layout_out is not None:
            layout_out["listing_left"] = imgui.get_cursor_screen_pos()[0]
        changed, value = draw_file_listing(str(directory), name="listing", file_metadata=file_metadata, width=width,
                                           height=body_height, disable_scroll=False,
                                           context_menu=menu_items, menu_target=menu_target,
                                           drag_rows=drag_rows, folder_bg_boost=folder_bg_boost,
                                           folder_bg_rounding=folder_bg_rounding,
                                           show_tint_chips=show_tint_chips, chip_size=chip_size,
                                           default_tint=default_tint, type_to_search=type_to_search,
                                           show_crumbs=show_crumbs, show_hidden=show_hidden)
        if changed:
            result = (True, value)
    columns.finish()

    if result[0]:
        return result
    if picked is not None:
        request_render()
        return True, str(picked)
    if ctrl_up_key_pressed and directory.parent != directory:
        request_render()
        return True, str(directory.parent)
    return False, input_value


@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=True,
             show_bg=False, shadow=False, determines_height=False)
def draw_file_selector(input_value: str | None = None, draw_state=None,
                       selector_state: FileSelectorState = None,
                       escape_key_pressed=False, choose_folder=False,
                       context_menu=None, browse=None, show_hidden=None, file_metadata=None):
    """Return (True, absolute_path) once when a file is activated.

    Navigation stays here. input_value seeds the initial directory (home
    by default); later opens remember the last directory. Double-click or
    Enter selects a file. Cancel / the window close button returns no change.
    In an OS child, selection closes the window. Pass open_requested=True
    for one frame to open/reopen it; False leaves its current state alone.
    Omit open_requested to open immediately on the first call.

    ``choose_folder=True`` makes it a FOLDER picker: files are inert, a
    "Choose Folder" button beside Cancel returns the directory being
    shown (double-click still descends). ``context_menu`` = {label:
    callable(path)} is the listing's right-click menu (the explorer's).
    ``browse`` = a directory to navigate to — a path or ``(path, token)``
    — applied once per distinct value; keep passing it.
    """
    from meltygui.core.runtime.app import pressed
    from meltygui.view.control_view import draw_button
    from meltygui.view.file_view import draw_fast_file_explorer

    if selector_state.directory is None:
        directory = Path(input_value or Path.home()).expanduser().resolve()
        selector_state.directory = str(directory if directory.is_dir() else directory.parent)
    # ``browse``: a directory to show - a path, or ``(path, token)`` where
    # a new token re-applies the same path - applied once per distinct
    # value (the settings editor's shortcuts column pick). An OS-child body
    # runs on its own thread, so the host keeps passing the token rather
    # than passing it for one frame.
    if browse != getattr(selector_state, "_browse_applied", None):
        selector_state._browse_applied = browse
        target = browse[0] if isinstance(browse, tuple) else browse
        if target:
            target = Path(target).expanduser()
            if target.is_dir():
                selector_state.directory = str(target)
    left, top, right, bottom = draw_state.get_content_rect()
    _trace_browser_size("file-selector-size", draw_state,
                        content=(left, top, right, bottom),
                        explorer=(right - left, max(120, bottom - top - 35)))
    changed, picked = draw_fast_file_explorer(
        selector_state.directory, name='files', width=right - left,
        file_metadata=file_metadata,
        height=max(120, bottom - top - 35),
        folder_bg_boost=-0.23, folder_bg_rounding=10.0, context_menu=context_menu,
        show_hidden=show_hidden)
    # The explorer's flow cursor includes the full listing height, even
    # when that listing is clipped/scrolling. Anchor the reserved footer
    # to the selector's viewport so resizing cannot push it off-window.
    imgui.set_cursor_screen_pos((left, bottom - 30))
    if choose_folder:
        chosen, _ = draw_button(label='Choose Folder', name='choose', show_header=False,
                                width=130, height=25)
        imgui.same_line()
        if chosen:
            draw_state.closed = True
            return True, selector_state.directory
    cancelled, _ = draw_button(label='Cancel', name='cancel', show_header=False,
                               width=90, height=25)
    if cancelled or escape_key_pressed or pressed('escape'):
        draw_state.closed = True
        return False, None
    if changed:
        path = Path(picked).expanduser().resolve()
        if path.is_dir():
            selector_state.directory = str(path)
        elif path.is_file() and not choose_folder:
            draw_state.closed = True
            return True, str(path)
    return False, None


@render_func()
def file_watch_debug(draw_state=None):
    from meltygui.core.files.file_watch_core import _symbol_index_view

    from meltygui.core.melty import Melty
    from meltygui.core.melty import FileWatch
    from meltygui.editor.external_changes import ExternalChanges

    RenderFuncs.draw_function(FileWatch.watch_project_files, icon="",
                              tint=(0, 0, 0, 1), show_bg=False, run_in_thread=True,
                              result_fade_frames=30)

    from meltygui.editor.pending_save import PendingSave

    view_paths = {p: len(dss) for p, dss in FileWatch.path_to_draw_states.items()}
    tracked = FileWatch.project_tracked
    all_paths = sorted(set(view_paths) | set(tracked))

    refs_cache, span_cache, snap, index_gen = _symbol_index_view()
    spans_by_file = defaultdict(list)
    for (p, start, end), (sig, _usages) in list(span_cache.items()):
        spans_by_file[p].append(sig)

    _mtimes = {}

    def _mtime(p):
        if p not in _mtimes:
            try:
                _mtimes[p] = os.stat(p).st_mtime
            except OSError:
                _mtimes[p] = None
        return _mtimes[p]

    def _sym_marks(p):
        """Usage-symbol status for one file: warmer refs cache + span usages."""
        out = []
        entry = refs_cache.get(p)
        if entry is not None:
            # entry[0] is (mtime, pending_gen); disk-freshness is the mtime
            # half. A pre-hotswap twin module can still hold old-format bare
            # float entries - treat those as the mtime too.
            _sig = entry[0]
            fresh = (_sig[0] if isinstance(_sig, tuple) else _sig) == _mtime(p)
            out.append(f"refs {len(entry[1])}" if fresh else "refs STALE")
        elif p in snap:
            out.append("refs evicted")     # counted by the warmer, but cold
        else:
            out.append("no idx")           # never indexed (no live module)
        sigs = spans_by_file.get(p)
        if sigs:
            try:
                pg = PendingSave.pending_gen_for(Path(p))
                ok = sum(1 for s in sigs
                         if s and s[0] == _mtime(p) and s[1] == pg
                         and (s[2] or s[3] == index_gen))
            except Exception:
                ok = "?"
            out.append(f"usages {ok}/{len(sigs)} fresh")
        return out

    refs_fresh = sum(1 for p, entry in refs_cache.items()
                     if entry is not None and entry[0] == _mtime(p))
    cached = sum(1 for p in all_paths if p in Melty.code_cache)
    lines = [
        f"watched dirs: {len(FileWatch._watched_dirs)}   "
        f"files known: {len(all_paths)} (project-tracked {len(tracked)}, "
        f"view-watched {len(view_paths)})",
        f"baselined in code_cache: {cached}/{len(all_paths)}   "
        f"external drift tracked: {len(ExternalChanges.originals)}",
        f"symbol index: gen {index_gen}   refs cached {len(refs_cache)} file(s) "
        f"({refs_fresh} fresh)   span usages {len(span_cache)} "
        f"across {len(spans_by_file)} file(s)",
        "",
    ]

    by_dir = defaultdict(list)
    for p in all_paths:
        by_dir[str(Path(p).parent)].append(p)

    # Dirs watched but currently holding no tracked file still matter (events
    # within them can auto-track new .py files) - show them at the end.
    empty_dirs = sorted(d for d in FileWatch._watched_dirs if d not in by_dir)

    for d in sorted(by_dir):
        watched = "" if d in FileWatch._watched_dirs else "   [dir NOT scheduled!]"
        lines.append(f"{d}{watched}")
        for p in by_dir[d]:
            marks = []
            if p in Melty.code_cache:
                marks.append("cached")
            else:
                marks.append("UNCACHED — edits invisible")
            n = view_paths.get(p)
            if n:
                marks.append(f"{n} view{'s' if n > 1 else ''}")
            if p in tracked:
                marks.append("tracked")
            if p in ExternalChanges.originals:
                marks.append("DRIFT")
            marks.extend(_sym_marks(p))
            lines.append(f"    {Path(p).name}  [{', '.join(marks)}]")
    if empty_dirs:
        lines.append("")
        lines.append(f"watched dirs with no known files ({len(empty_dirs)}):")
        lines.extend(f"    {d}" for d in empty_dirs)

    RenderFuncs.draw_text("\n".join(lines), show_name=True, name="watch status")


@render_func(tint=(0.32, 0.42, 0.54), auto_resize=False, selectable=False)
def render_file_tree(input_value=None, draw_state=None,
                     file_tree_state: FileTreeState = None, root=ROOT, file_metadata=None,
                     left_mouse_down=False, left_mouse_double_clicked=False,
                     escape_key_pressed=False, **kwargs):
    # Geometry authored at ui_scale 1.0 — scaled through Melty.px per frame.
    # [tint=(0.55, 0.72, 0.95)]
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.header_view import flat_button
    from meltygui.core.cache.tile_marks import add_shadow
    from meltygui.core.input.drag_drop_core import DragDrop
    from meltygui.core.layout.header_runtime import _brightness_clamp_fn
    from meltygui.model.import_graph_model import start_build
    from meltygui.core.files.file_tree_core import _apply_row_drop
    from meltygui.core.files.file_tree_core import _flatten_ordered
    from meltygui.core.files.file_tree_core import _tint_of
    import meltygui.model.import_graph_model as file_graph

    row_height = 20.0
    # Per nesting level, in px at ui_scale 1.0 — kept tiny so a deep tree
    # doesn't march off to the right (Lukas 09-04: 2 px, no stair).
    # [tint=(0.55, 0.72, 0.95)]
    indent_per_level = 2.0
    left_pad = 6.0
    glyph_width = 12.0
    # Row backgrounds are the files' own tints (FileMeta) run through the
    # SAME colour pipeline as the editor's active tabs (flat_button: the
    # style manager's mix under Toggles.CodeEditor.tab_active_bg_* + the
    # brightness clamp), so every row bg stays dark enough for light text.
    # [tint=(0.55, 0.72, 0.95)]
    bg_theme_factor = 0.1
    # With an import graph built every file rises with its USAGE — importer
    # count on the graph's log scale (ImportGraph.usage, 0..1) — through a
    # drop shadow (add_shadow depth lift, the editor's active-tab mechanism)
    # AND its name's font size, so the files everything depends on pop out
    # of the tree. While a file is SELECTED only it and its highlighted files
    # cast: the selection and its IMPORTERS are RAISED by highlight_lift, its
    # IMPORTS are RECESSED by the same amount (a negative add_shadow offset
    # carves the row in, the surroundings cast into it), every other file
    # drops flat; the font keeps following usage throughout. A file that is
    # both reads as an importer.
    # [tint=(0.55, 0.72, 0.95)]
    usage_lift = 6.0
    # [tint=(0.55, 0.72, 0.95)]
    highlight_lift = 3.0
    # Font scale range over usage (clamped so the biggest still fits the
    # row); a file NOBODY imports is greyed out.
    # [tint=(0.55, 0.72, 0.95)]
    usage_font_scale_min = 0.9
    # [tint=(0.55, 0.72, 0.95)]
    usage_font_scale_max = 1.3
    # [tint=(0.55, 0.72, 0.95)]
    unused_text = (0.55, 0.57, 0.6, 0.6)
    # Import-graph highlights wear the SELECTED file's own tint: what it
    # imports in the tint itself, what imports it in a lighter, washed-out
    # variant (importer_value_boost / importer_saturation), both blended
    # for a file in both directions. A folder holding hidden hits gets the
    # same mark at reduced alpha. An unpainted selection uses the fallback.
    # [tint=(0.55, 0.72, 0.95)]
    highlight_fallback_tint = (0.55, 0.72, 0.95)
    # [tint=(0.55, 0.72, 0.95)]
    importer_value_boost = 0.35
    # [tint=(0.55, 0.72, 0.95)]
    importer_saturation = 0.45
    # [tint=(0.55, 0.72, 0.95)]
    highlight_bar_width = 3.0
    # [tint=(0.55, 0.72, 0.95)]
    toolbar_height = 26.0

    state = file_tree_state
    px = Melty.px
    row_h, indent, pad, glyph_w = px(row_height), px(indent_per_level), px(left_pad), px(glyph_width)

    dl = imgui.get_window_draw_list()
    cw = draw_state.content_width or (draw_state.width or 240)

    # ── toolbar: the import-graph build button + status ───────────────────
    build = state._build
    if build is not None and not build.running:
        if build.result is not None:
            state._graph = build.result
        state._build = None
        draw_state.invalidate()
    if state._graph is None and file_graph.current() is not None:
        state._graph = file_graph.current()      # built by the graph graph
        draw_state.invalidate()
    if flat_button("Build import graph##file_tree", draw_state,
                   view_id="file_tree_build_graph", height=px(toolbar_height) - px(4),
                   event="left_mouse_down"):
        if state._build is None:
            state._build = start_build(Path(root))
            request_render()
    status_x, status_y = imgui.get_item_rect_max().x + px(8), imgui.get_item_rect_min().y + px(4)
    graph = state._graph
    if build is not None and build.running:
        status = "building…"
    elif build is not None and build.error:
        status = build.error
    elif graph is not None:
        status = f"{graph.file_count} files, {graph.edge_count} imports, {graph.seconds:.1f}s"
        if graph.errors:
            status += f", {len(graph.errors)} unparsed"
    else:
        status = "no graph yet"
    dl.add_text(status_x, status_y, pack_color(0.75, 0.78, 0.82, 1.0), status)
    imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + px(4))
    x0, y0 = imgui.get_cursor_screen_pos()

    # Esc (while the tree is open) clears the selection - and with it the
    # highlights and the fixed-lift shadows.
    if escape_key_pressed and state.selected is not None:
        state.select(None)
        request_render()

    # Highlight sets for the selected file (files only - folders are marked
    # when they hold a hit that isn't visible).
    imports, importers = set(), set()
    selected_path = state.selected_path
    if graph is not None and selected_path is not None:
        imports = graph.imports_of(selected_path)
        importers = graph.importers_of(selected_path)
    hits = imports | importers

    # Row order comes from the file_meta dict: a path's key position is its
    # rank among its siblings (drag-reorder rewrites those positions).
    meta = file_metadata
    position = ({k: i for i, k in enumerate(meta)} if meta is not None else {})
    rows = _flatten_ordered(Path(root), state.expanded, meta, position)
    # One dummy reports the total height so the container scrolls normally.
    # The scroll clamp is content_height - clipped_height, but clipped_height
    # spans the WHOLE window (header included) while the rows start below the
    # header + toolbar - pad the reported height by that top inset (the
    # fast_dock rule) or the last row can never scroll fully into view.
    top_inset = (y0 + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(cw, max(1.0, len(rows) * row_h + max(0.0, top_inset)))
    selected_tint = _tint_of(meta, selected_path) if selected_path is not None else None
    imports_tint = selected_tint or highlight_fallback_tint
    hue, saturation, value = colorsys.rgb_to_hsv(*imports_tint)
    importers_tint = colorsys.hsv_to_rgb(hue, saturation * importer_saturation,
                                         min(1.0, value + importer_value_boost))

    mx, my = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_down.x, left_mouse_down.y)
             if (left_mouse_down and hasattr(left_mouse_down, "x")) else None)
    dclick = ((left_mouse_double_clicked.x, left_mouse_double_clicked.y)
              if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None)
    clip = getattr(draw_state, "abs_clip_rect", None)

    text_col = pack_color(0.92, 0.92, 0.92, 1.0)
    unused_col = pack_color(*unused_text)
    font_size = imgui.get_font_size()
    # The scaled name must still fit the row.
    font_scale_max = min(usage_font_scale_max, row_h / max(font_size, 1.0))
    style_manager = Melty.style_manager
    brightness_clamp = _brightness_clamp_fn()
    bg_memo = {}

    def row_bg(tint):
        """The tab bar's bg colour for `tint` (memoized per body run)."""
        bg = bg_memo.get(tint)
        if bg is None:
            mixed = style_manager.make_color_rgb(
                tint[0], tint[1], tint[2],
                value=Toggles.CodeEditor.tab_active_bg_brightness,
                factor=bg_theme_factor,
                saturation_scale=Toggles.CodeEditor.tab_active_bg_saturation,
                alpha=1.0)
            mixed = brightness_clamp(mixed[0], mixed[1], mixed[2], 0.0,
                                     Toggles.CodeEditor.tab_active_bg_max_brightness)
            bg = bg_memo[tint] = pack_color(mixed[0], mixed[1], mixed[2], 1.0)
        return bg
    hover_col = pack_color(1.0, 1.0, 1.0, 0.08)
    select_col = pack_color(0.4, 0.6, 0.9, 0.35)

    imports_col = pack_color(*imports_tint, 0.9)
    importers_col = pack_color(*importers_tint, 0.9)
    both_col = pack_color(*[(a + b) / 2 for a, b in zip(imports_tint, importers_tint)], 0.9)
    imports_wash = pack_color(*imports_tint, 0.14)
    importers_wash = pack_color(*importers_tint, 0.14)
    folder_alpha = 0.45

    def highlight_of(p):
        """(bar colour, wash colour, alpha scale) for a row, or None."""
        if not hits:
            return None
        if p.is_dir():
            inside = [h for h in hits if p in h.parents]
            if not inside:
                return None
            in_imports = any(h in imports for h in inside)
            in_importers = any(h in importers for h in inside)
            scale = folder_alpha
        else:
            in_imports, in_importers = p in imports, p in importers
            if not (in_imports or in_importers):
                return None
            scale = 1.0
        if in_imports and in_importers:
            return both_col, imports_wash, scale
        if in_imports:
            return imports_col, imports_wash, scale
        return importers_col, importers_wash, scale

    def paint_row(draw_list, rx, ry, p, depth, tint, hovered, selected, shadow=True):
        """One row's visuals at (rx, ry): tint bg, hover/select wash, folder
        chevron, name. Shared by the inline rows and the drag ghost (which
        rides the overlay list outside the shadow marks' clip: shadow=False)."""
        # The bg is indented with the text: it starts at the row's chevron
        # column and runs to the right edge.
        x = rx + pad + depth * indent
        usage = None                      # None if no graph / a folder
        if graph is not None and not p.is_dir():
            usage = graph.usage(p)
        if shadow and usage is not None:
            if selected_path is not None:
                if p == selected_path or p in importers:
                    lift = highlight_lift
                elif p in imports:
                    lift = -highlight_lift
                else:
                    lift = 0.0
            else:
                lift = usage_lift * usage
            if lift != 0.0:
                add_shadow((x, ry, rx + cw - x, row_h), offset=lift, corner_radius=0.0)
        if tint is not None:
            draw_list.add_rect_filled(x, ry, rx + cw, ry + row_h, row_bg(tint))
        if selected:
            draw_list.add_rect_filled(x, ry, rx + cw, ry + row_h, select_col)
        if hovered:
            draw_list.add_rect_filled(x, ry, rx + cw, ry + row_h, hover_col)
        mark = highlight_of(p) if shadow else None
        if mark is not None:
            bar_col, wash_col, scale = mark
            if scale < 1.0:
                bar_col = with_alpha(bar_col, 0.9 * scale)
                wash_col = with_alpha(wash_col, 0.14 * scale)
            draw_list.add_rect_filled(x, ry, rx + cw, ry + row_h, wash_col)
            draw_list.add_rect_filled(x, ry, x + px(highlight_bar_width), ry + row_h, bar_col)
        if p.is_dir():
            cx, cy = x + px(4.0), ry + row_h / 2
            radius = px(4.0)
            tri = ([(cx - radius + 1, cy - radius), (cx + radius - 1, cy), (cx - radius + 1, cy + radius)]
                   if not state.is_expanded(p)
                   else [(cx - radius, cy - radius + 1), (cx + radius, cy - radius + 1), (cx, cy + radius - 1)])
            draw_list.add_triangle_filled(*tri[0], *tri[1], *tri[2], text_col)
        name_col = unused_col if usage == 0.0 else text_col
        if usage is not None:
            # set_window_font_scale retargets the draw list's font size at
            # once (imgui's AddText uses g.Fonts), so the name scales
            # without a second font face; reset right after.
            scale = usage_font_scale_min + (font_scale_max - usage_font_scale_min) * usage
            imgui.set_window_font_scale(scale)
            draw_list.add_text(x + glyph_w, ry + (row_h - font_size * scale) * 0.5, name_col, p.name)
            imgui.set_window_font_scale(1.0)
        else:
            draw_list.add_text(x + glyph_w, ry + px(2.0), name_col, p.name)

    # on_drag call order == index into `visible` (DropEvent indices count
    # on_drag calls, so only rows that were used as handles are indexed).
    visible = []
    for i, (p, depth) in enumerate(rows):
        ry0 = y0 + i * row_h
        ry1 = ry0 + row_h

        def in_row(pt):
            return pt is not None and x0 <= pt[0] <= x0 + cw and ry0 <= pt[1] <= ry1

        if p.is_dir():
            if in_row(click):
                state.toggle(p)
                request_render()
        else:
            if in_row(click):
                state.select(p)
                request_render()
            if in_row(dclick):
                state.open_file(p)

        if clip is not None and (ry1 < clip[1] or ry0 > clip[3]):
            continue

        visible.append((p, depth))
        tint = _tint_of(meta, p)
        # Immediate-mode DragDrop (the editor tab bar's model): the row rect
        # is its own drag handle. While THIS row is the active drag - on_drag
        # has parked the cursor at the drop position on the overlay list -
        # paint the same row there and leave the inline slot empty (the
        # home/cancel target; DragDrop draws the feedback lines).
        drag = DragDrop.on_drag((x0, ry0, x0 + cw, ry1), key=str(p),
                                draw_state=draw_state)
        if drag:
            paint_row(drag.draw_list, drag.x, drag.y, p, depth, tint,
                      hovered=False, selected=False, shadow=False)
            DragDrop.end_drag()
            continue

        hovered = hover_ok and x0 <= mx <= x0 + cw and ry0 <= my <= ry1
        paint_row(dl, x0, ry0, p, depth, tint, hovered, p == selected_path)

    # A landed drop reorders within ONE folder: the slot must be beside a
    # sibling of the dragged row (before the row at insert_index, or after
    # the row above it); anything else - another folder, a cross-collection
    # kind - is ignored.
    drop = DragDrop.on_drop(horizontal=False, draw_state=draw_state)
    if drop is not None and drop.kind == "reorder" and drop.index is not None:
        dragged = visible[drop.index][0] if 0 <= drop.index < len(visible) else None
        if dragged is not None and _apply_row_drop(meta, dragged, visible,
                                                   drop.insert_index, position):
            draw_state.invalidate()
            request_render()

    return False, None
