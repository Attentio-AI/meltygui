"""File tree — a general-purpose directory browser with collapsable folders.

Unlike folder_files (which materializes file CONTENT through a RenderHost),
this view is display-only: it walks the directory and draws just the names,
straight to the window draw_list — no per-row widgets, no draw_states per
file (the fast_dock.py interaction model: event params for clicks, blit
cache while idle, wrapper re-renders every frame while hovered). Click a
folder to expand/collapse it; double-click a file to open it
(FileTreeState.open_file, a stub for now).

Frame-to-frame state (which folders are expanded, selection) lives in
FileTreeState, injected by annotation the same way GLState/CodeState are.
"""

import colorsys
from pathlib import Path

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import with_alpha
from meltygui.core.melty import Melty
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.modes import Modes
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.core_render import render_func
from meltygui.core.layout.header_runtime import _brightness_clamp_fn
import meltygui.model.import_graph_model as file_graph
from meltygui.model.import_graph_model import start_build
from meltygui.core.runtime.toggles import Toggles
from meltygui.files.folder_files import folder_proxy
from meltygui.files.folder_files import watch_folder
from meltygui.files.folder_files import _file_meta
from meltygui.core.input.drag_drop_core import DragDrop
from meltygui.core.cache.tile_cache import add_shadow
from meltygui.core.rendering.window_decoration import window

from meltygui.core.runtime.paths import application_root
from meltygui.state.file_state import ROOT     # .../src



def open_file(path):
    """Route `path` into the code editor (summons the editor window)."""
    from meltygui.core.runtime.extensions import open_source as open_in_editor
    open_in_editor(str(path))


from meltygui.state.file_state import FileTreeState


def _children(folder):
    """Visible entries, folders first, case-insensitive by name."""
    try:
        entries = [p for p in folder.iterdir()
                   if not p.name.startswith(".") and p.name != "__pycache__"]
    except OSError:
        return []
    return sorted(entries, key=lambda p: (p.is_file(), p.name.lower()))


def _flatten(folder, expanded, depth=0, rows=None):
    """The tree as drawn: one (path, depth) per visible row."""
    if rows is None:
        rows = []
    for p in _children(folder):
        rows.append((p, depth))
        if p.is_dir() and str(p) in expanded:
            _flatten(p, expanded, depth + 1, rows)
    return rows


def _meta():
    """AppModel's path → FileMeta store (None before the model exists)."""
    return _file_meta()


def _tint_of(meta, path):
    """The row's background tint (rgb), or None for an unpainted file — no
    stored tint, or FileMeta's black-transparent default."""
    from meltygui.models.file_meta import FileMeta
    entry = meta.get(str(path)) if meta is not None else None
    tint = FileMeta.painted_tint(entry)
    return tuple(tint[:3]) if tint else None


def _ordered_children(folder, meta, position):
    """`folder`'s visible entries in FILE-META ORDER: an entry's position in
    the file_meta dict is its rank; entries the dict doesn't know yet trail
    in the natural order (folders first, case-insensitive by name)."""
    natural = _children(folder)
    unknown = float("inf")
    ranked = [(position.get(str(p), unknown), i, p) for i, p in enumerate(natural)]
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [p for _r, _i, p in ranked]


def _flatten_ordered(folder, expanded, meta, position, depth=0, rows=None):
    """The tree as drawn — one (path, depth) per visible row — in meta order."""
    if rows is None:
        rows = []
    for p in _ordered_children(folder, meta, position):
        rows.append((p, depth))
        if p.is_dir() and str(p) in expanded:
            _flatten_ordered(p, expanded, meta, position, depth + 1, rows)
    return rows


def reorder_siblings(meta, siblings, dragged, insert_index):
    """Move `dragged` to `insert_index` (pre-removal coordinates, the Reorder
    convention) within `siblings` and write the new order into the file_meta
    dict: every sibling gets an entry, and the siblings' existing SLOTS in the
    dict (their key positions) are refilled in the new order, so nothing else
    in the dict moves. Returns True when the order changed."""
    from meltygui.models.file_meta import FileMeta
    keys = [str(p) for p in siblings]
    dragged_key = str(dragged)
    if dragged_key not in keys:
        return False
    current = keys.index(dragged_key)
    new_keys = list(keys)
    new_keys.pop(current)
    target = insert_index - 1 if current < insert_index else insert_index
    target = max(0, min(target, len(new_keys)))
    if target == current:
        return False
    new_keys.insert(target, dragged_key)
    for k in keys:
        if not isinstance(meta.get(k), dict):
            meta[k] = FileMeta()
    slots = [i for i, k in enumerate(meta) if k in set(keys)]
    items = list(meta.items())
    for slot, k in zip(slots, new_keys):
        items[slot] = (k, meta[k])
    meta.clear()
    meta.update(items)
    return True


from meltygui.view.file_view import render_file_tree
render_file_tree = window(initial={'width': 320, 'height': 540}, tint=(0.72, 0.79, 0.85))(render_file_tree)


def _apply_row_drop(meta, dragged, visible, insert_index, position):
    """Translate a flattened-row drop into a sibling reorder. Returns True
    when the file_meta order changed."""
    if meta is None:
        return False
    parent = dragged.parent
    siblings = _ordered_children(parent, meta, position)
    below = visible[insert_index][0] if 0 <= insert_index < len(visible) else None
    above = visible[insert_index - 1][0] if 0 < insert_index <= len(visible) else None
    if below is not None and below.parent == parent:
        sibling_index = siblings.index(below)
    elif above is not None and above.parent == parent:
        sibling_index = siblings.index(above) + 1
    elif above is not None and above.is_dir() and above == parent:
        sibling_index = 0                      # dropped right above its own folder row
    else:
        return False
    return reorder_siblings(meta, siblings, dragged, sibling_index)


# # ── Framework file tree ──────────────────────────────────────────────────────
# # The same directory rendered through the framework: folder_files' RenderHost
# # (folder_io) holds the {name: Path | dict} tree - background loading, diff
# # reconcile, and the change poller for free - and draw_collection renders it
# # under Mode.FILE_TREE_NAMES (view/modes.py): folders are drawn with
# # expandollapsing headers, add/delete, drag reorder - all framework - files route
# # by name to draw_file_name below, which shows just the name. Contrast with
# # the flat draw-list tree above: ~no code here, one draw_state per file item.
#
# # Disabled while diagnosing frame-time: registering the repo ROOT causes the
# # folder poller to _poll_loop rescan 143k entries (venv included) every
# # second, starving the render thread.
# # files_host = folder_proxy(ROOT, "FileTreeRoot")
#
#
# @render_func(is_render_for="PosixPath", show_bg=False, selectable=True,
#              use_cache=True, is_tree=True, with_header=draw_header)
# def draw_file_name(input_value=None, draw_state=None,
#                    left_mouse_double_clicked=False, **kwargs):
#     # The header draws the name (the dict key) and carries selection/drag -
#     # the body is only the double-click → open handler.
#     if left_mouse_double_clicked:
#         open_file(input_value)
#     return False, input_value
#
#
# # @window(input_value=files_host, tint=(0.42, 0.36, 0.54), disable_scroll=False, mode=Modes.WINDOW)
# @render_func(show_bg=True, use_cache=False, shadow=True, selectable=False)
# def render_file_tree_melty(input_value=None, draw_state=None, **kwargs):
#     from meltygui.core.rendering.render_funcs import RenderFuncs
#     watch_folder(ROOT, draw_state)
#     # Same shape as draw_folder_files: the host holds the tree one level down
#     # under "value"; a simple top-level draw_collection, and the names-only
#     # mode passed to the host.
#     tree = input_value.get("value") if isinstance(input_value, dict) else {}
#     RenderFuncs.draw_collection(tree if tree is not None else {}, name=ROOT.name,
#                                 show_add_delete=True, new_item_type=str, temp=True,
#                                 width=draw_state.content_width,
#                                 show_scroll=True, show_bg=True,
#                                 child_kwargs={"show_bg": True, "bg_offset": -4,
#                                               "mode": Modes.FILE_TREE_NAMES})
#     return True, None