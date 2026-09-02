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

import imgui
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header, flat_button, _brightness_clamp_fn
from src.lsd.gl_gui.view.playground import file_graph
from src.lsd.gl_gui.view.playground.file_graph import start_build
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.playground.folder_files import folder_proxy, watch_folder, _file_meta
from src.lsd.gl_gui.view.core_views.drag_drop import DragDrop
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

ROOT = Path(__file__).resolve().parents[4]     # .../src



def open_file(path):
    """Route `path` into the code editor (summons the editor window)."""
    from src.lsd.gl_gui.view.playground.open_files import open_in_editor
    open_in_editor(str(path))


class FileTreeState(DictConversion):
    """Injected per-draw_state state (`file_tree_state: FileTreeState = None`).
    A DictConversion so it PERSISTS: every non-underscore attribute is saved
    between sessions (style guide rules 6–7) — which folders were open and
    what was selected come back on restart. Paths are stored as STRINGS
    (a Path inside a set is not on the legacy to_dict primitive list)."""
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.expanded = set()   # str paths of open folders
        self.selected = None    # str path of the last single-clicked file
        self._graph = None      # file_graph.ImportGraph once built (not saved)
        self._build = None      # file_graph.ImportBuildBuilder while building

    def is_expanded(self, path):
        return str(path) in self.expanded

    def toggle(self, path):
        self.expanded.symmetric_difference_update({str(path)})

    def select(self, path):
        self.selected = str(path) if path is not None else None

    @property
    def selected_path(self):
        return Path(self.selected) if self.selected else None

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
        if p.is_dir() and str(p) in expanded:
            _flatten(p, expanded, depth + 1, rows)
    return rows


def _meta():
    """AppModel's path → FileMeta store (None before the model exists)."""
    return _file_meta()


def _tint_of(meta, path):
    """The row's background tint (rgb), or None for an unpainted file — no
    stored tint, or FileMeta's black-transparent default."""
    from src.lsd.gl_gui.model.app_model import FileMeta
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
    from src.lsd.gl_gui.model.app_model import FileMeta
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


@window(initial={"width": 320, "height": 540}, tint=(0.72, 0.79, 0.85))
@render_func(tint=(0.32, 0.42, 0.54), auto_resize=False, selectable=False)
def render_file_tree(input_value=None, draw_state=None,
                     file_tree_state: FileTreeState = None, root=ROOT,
                     left_mouse_down=False, left_mouse_double_clicked=False,
                     escape_key_pressed=False, **kwargs):
    # Geometry authored at ui_scale 1.0 — scaled through Melty.px per frame.
    # [tint=(0.55, 0.72, 0.95)]
    row_height = 20.0
    # [tint=(0.55, 0.72, 0.95)]
    indent_per_level = 16.0
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
    dl.add_text(status_x, status_y, imgui.get_color_u32_rgba(0.75, 0.78, 0.82, 1.0), status)
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
    meta = _meta()
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

    text_col = imgui.get_color_u32_rgba(0.92, 0.92, 0.92, 1.0)
    unused_col = imgui.get_color_u32_rgba(*unused_text)
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
            bg = bg_memo[tint] = imgui.get_color_u32_rgba(mixed[0], mixed[1], mixed[2], 1.0)
        return bg
    hover_col = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.08)
    select_col = imgui.get_color_u32_rgba(0.4, 0.6, 0.9, 0.35)

    imports_col = imgui.get_color_u32_rgba(*imports_tint, 0.9)
    importers_col = imgui.get_color_u32_rgba(*importers_tint, 0.9)
    both_col = imgui.get_color_u32_rgba(*[(a + b) / 2 for a, b in zip(imports_tint, importers_tint)], 0.9)
    imports_wash = imgui.get_color_u32_rgba(*imports_tint, 0.14)
    importers_wash = imgui.get_color_u32_rgba(*importers_tint, 0.14)
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
        # The bg is indented with the text: it starts in the row's chevron
        # column and runs to the right edge, so nesting appears as a stair.
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
                bar_col = (bar_col & 0x00FFFFFF) | (int(0.9 * scale * 255) << 24)
                wash_col = (wash_col & 0x00FFFFFF) | (int(0.14 * scale * 255) << 24)
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
#     from src.lsd.gl_gui.render_funcs import RenderFuncs
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