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

from pathlib import Path

import imgui
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.playground.folder_files import folder_proxy, watch_folder
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

ROOT = Path(__file__).parents[4]

# Geometry authored for ui_scale 1.0 - scaled through Melty.px per frame.
ROW_H = 20.0
INDENT = 16.0
PAD = 6.0
GLYPH_W = 12.0


def open_file(path):
    """Stub — will route `path` into an editor view. Both trees land here."""
    print(f"open_file: {path}")


class FileTreeState:
    """Injected per-draw_state state (`file_tree_state: FileTreeState = None`)."""
    _owner_ds = None

    def __init__(self):
        self.expanded = set()   # Paths of open folders
        self.selected = None    # last single-clicked Path

    def open_file(self, path):
        open_file(path)


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
        if p.is_dir() and p in expanded:
            _flatten(p, expanded, depth + 1, rows)
    return rows


@window(initial={"width": 320, "height": 540}, tint=(0.32, 0.42, 0.54))
@render_func(tint=(0.32, 0.42, 0.54), auto_resize=False, selectable=False)
def render_file_tree(input_value=None, draw_state=None,
                     file_tree_state: FileTreeState = None, root=ROOT,
                     left_mouse_down=False, left_mouse_double_clicked=False,
                     **kwargs):
    state = file_tree_state
    px = Melty.px
    row_h, indent, pad, glyph_w = px(ROW_H), px(INDENT), px(PAD), px(GLYPH_W)

    dl = imgui.get_window_draw_list()
    x0, y0 = imgui.get_cursor_screen_pos()
    cw = draw_state.content_width or (draw_state.width or 240)

    rows = _flatten(Path(root), state.expanded)
    # One dummy reports the content height so the window scrolls normally.
    imgui.dummy(cw, max(1.0, len(rows) * row_h))

    mx, my = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_down.x, left_mouse_down.y)
             if (left_mouse_down and hasattr(left_mouse_down, "x")) else None)
    dclick = ((left_mouse_double_clicked.x, left_mouse_double_clicked.y)
              if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None)
    clip = getattr(draw_state, "abs_clip_rect", None)

    text_col = imgui.get_color_u32_rgba(0.9, 0.9, 0.9, 1.0)
    dim_col = imgui.get_color_u32_rgba(0.75, 0.8, 0.9, 1.0)
    hover_col = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.08)
    select_col = imgui.get_color_u32_rgba(0.4, 0.6, 0.9, 0.25)

    for i, (p, depth) in enumerate(rows):
        ry0 = y0 + i * row_h
        ry1 = ry0 + row_h

        def in_row(pt):
            return pt is not None and x0 <= pt[0] <= x0 + cw and ry0 <= pt[1] <= ry1

        if p.is_dir():
            if in_row(click):
                state.expanded.symmetric_difference_update({p})
                request_render()
        else:
            if in_row(click):
                state.selected = p
                request_render()
            if in_row(dclick):
                state.open_file(p)

        if clip is not None and (ry1 < clip[1] or ry0 > clip[3]):
            continue

        x = x0 + pad + depth * indent
        if p == state.selected:
            dl.add_rect_filled(x0, ry0, x0 + cw, ry1, select_col)
        if hover_ok and x0 <= mx <= x0 + cw and ry0 <= my <= ry1:
            dl.add_rect_filled(x0, ry0, x0 + cw, ry1, hover_col)

        if p.is_dir():
            cx, cy = x + px(4.0), ry0 + row_h / 2
            r = px(4.0)
            tri = ([(cx - r + 1, cy - r), (cx + r - 1, cy), (cx - r + 1, cy + r)]
                   if p not in state.expanded
                   else [(cx - r, cy - r + 1), (cx + r, cy - r + 1), (cx, cy + r - 1)])
            dl.add_triangle_filled(*tri[0], *tri[1], *tri[2], dim_col)
            dl.add_text(x + glyph_w, ry0 + px(2.0), dim_col, p.name)
        else:
            dl.add_text(x + glyph_w, ry0 + px(2.0), text_col, p.name)

    return False, None


# ── Framework file tree ──────────────────────────────────────────────────────
# The same directory rendered through the framework. folder_files' RenderHost
# (folder_io) holds the {name: Path | dict} tree - background loading, disk
# reconcile, and the change poller for free - and draw_collection renders it
# with Mode.FILE_TREE_NAMES (view/mode.py): folders are dict entries
# (collapsing headers, add/delete, drag-drop - all framework), files route
# by type to draw_file_name below, which is just the filename. Contrast with
# the raw draw-list tree above: ~no code here, one draw_state per row there.

# Disabled while diagnosing load-time: registering the repo ROOT with the
# folder poller makes _poll_loop _scan 143k entries (venv included) every
# second, starving the draw thread.
# files_host = folder_proxy(ROOT, "FileTreeNames")


@render_func(is_default_for="PosixPath", show_bg=False, selectable=True,
             use_cache=True, is_tree=False, with_header=draw_header)
def draw_file_name(input_value=None, draw_state=None,
                   left_mouse_double_clicked=False, **kwargs):
    # The header draws the name (the dict key) and carries selection/drag -
    # the body is only the double-click → open handler.
    if left_mouse_double_clicked:
        open_file(input_value)
    return False, input_value


# @widget(input_value=files_host, tint=(0.42, 0.36, 0.54), disable_scroll=True, mode=Modes.WINDOW)
@render_func(show_bg=True, use_cache=True, shadow=True, selectable=False)
def render_file_tree_melty(input_value=None, draw_state=None, **kwargs):
    from src.lsd.gl_gui.render_funcs import RenderFuncs
    watch_folder(ROOT, draw_state)
    # Same shape as draw_folder_files: the host contains the tree one level down
    # under "value"; a plain top-level draw_collection, with the names-only
    # mode applied to the children.
    tree = input_value.get("value") if isinstance(input_value, dict) else {}
    RenderFuncs.draw_collection(tree if tree is not None else {}, name=ROOT.name,
                                show_add_delete=True, new_item_type=str, temp=True,
                                width=draw_state.content_width,
                                disable_scroll=True, show_bg=True,
                                child_kwargs={"show_bg": True, "bg_offset": -4,
                                              "mode": Modes.FILE_TREE_NAMES})
    return False, None