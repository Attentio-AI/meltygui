"""Import graph — every project file as a labelled box, every import a line.

The whole `file_graph.ImportGraph` drawn straight to the window draw_list
(the file_tree / fast_dock model: no per-node widgets, event params for
gestures, blit cache while idle). The graph's layered layout
(file_graph.layout_graph: column = dependency depth, left → right, rows
ordered to keep lines short) gives the ORDER; this view sizes it in
pixels — each node is a rounded box around its file name, a column is as
wide as its widest box, rows share one pitch — so nothing overlaps, then
projects it: screen = origin + (box − centre) · fit · zoom + pan, where
zoom 1 fits the whole graph in the window.

Gestures: middle-drag pans, wheel zooms about the cursor, click a box to
select it (its importers light in the file's tint, its imports in the
washed variant, everything else fades), Esc clears, `/` resets the camera.
Box size follows USAGE (importer count, log scale) through the font scale,
so the hubs read at a glance. Shares the graph with render_file_tree
through file_graph.current(): either window's Build button serves both.
"""

from __future__ import annotations

import colorsys
import math
from pathlib import Path

import imgui
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import flat_button, _brightness_clamp_fn
from src.lsd.gl_gui.view.playground import file_graph
from src.lsd.gl_gui.view.playground.file_graph import start_build
from src.lsd.gl_gui.view.playground.file_tree import ROOT, _meta, _tint_of, open_file


class GraphViewState(DictConversion):
    """Injected per-draw_state state (`graph_view_state: GraphViewState = None`):
    the camera and the selection persist between sessions; the graph and
    the in-flight build (underscored) do not."""
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.zoom = 1.0
        self.pan_x = 0.0        # px, screen coordinates
        self.pan_y = 0.0
        self.selected = None    # str path of the selected node
        self._graph = None
        self._build = None

    def select(self, path):
        self.selected = str(path) if path is not None else None

    @property
    def selected_path(self):
        return Path(self.selected) if self.selected else None


@window(initial={"width": 720, "height": 620}, tint=(0.42, 0.36, 0.54), disable_scroll=True)
@render_func(tint=(0.42, 0.36, 0.54), auto_resize=False, selectable=False)
def render_import_graph(input_value=None, draw_state=None,
                        graph_view_state: GraphViewState = None, root=ROOT,
                        left_mouse_down=False, left_mouse_double_clicked=False,
                        middle_mouse_drag=None, scroll_y_changed=None,
                        escape_key_pressed=False, slash_key_pressed=False,
                        **kwargs):
    # ── knobs ────────────────────────────────────────────────────────────────
    # Box geometry (px at ui_scale 1): padding around the name, corner
    # radius, the gap between rows and between columns; the name's font
    # scale grows with usage (the file's importer count on the graph's log
    # scale, 0..1), so a hub's box is bigger.
    # [tint=(0.65, 0.55, 0.95)]
    box_pad_x = 8.0
    # [tint=(0.65, 0.55, 0.95)]
    box_pad_y = 4.0
    # [tint=(0.65, 0.55, 0.95)]
    box_radius = 6.0
    # [tint=(0.65, 0.55, 0.95)]
    row_gap = 6.0
    # [tint=(0.65, 0.55, 0.95)]
    column_gap = 60.0
    # [tint=(0.65, 0.55, 0.95)]
    usage_font_scale_max = 1.5
    # [tint=(0.65, 0.55, 0.95)]
    edge_alpha = 0.10
    # [tint=(0.65, 0.55, 0.95)]
    faded_alpha = 0.18            # everything unrelated while a node is selected
    # [tint=(0.65, 0.55, 0.95)]
    highlight_fallback_tint = (0.55, 0.72, 0.95)
    # [tint=(0.65, 0.55, 0.95)]
    importer_value_boost = 0.35
    # [tint=(0.65, 0.55, 0.95)]
    importer_saturation = 0.45
    # [tint=(0.65, 0.55, 0.95)]
    bg_theme_factor = 0.1
    # [tint=(0.65, 0.55, 0.95)]
    toolbar_height = 26.0
    # [tint=(0.65, 0.55, 0.95)]
    unpainted_node = (0.55, 0.57, 0.62)

    state = graph_view_state
    px = Melty.px
    dl = imgui.get_window_draw_list()
    cw = draw_state.content_width or (draw_state.width or 400)

    # ── toolbar: build button + status (same as the tree's) ─────────────────
    build = state._build
    if build is not None and not build.running:
        if build.result is not None:
            state._graph = build.result
        state._build = None
        draw_state.invalidate()
    if state._graph is None and file_graph.current() is not None:
        state._graph = file_graph.current()
        draw_state.invalidate()
    if flat_button("Build import graph##import_graph", draw_state,
                   view_id="import_graph_build", height=px(toolbar_height) - px(4),
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
        status = f"{graph.file_count} files, {graph.edge_count} imports"
        if state.selected:
            status += f"  ·  {Path(state.selected).name}"
    else:
        status = "no graph yet — build one"
    dl.add_text(status_x, status_y, imgui.get_color_u32_rgba(0.75, 0.78, 0.82, 1.0), status)
    imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + px(4))
    x0, y0 = imgui.get_cursor_screen_pos()
    height = max(1.0, (draw_state.height or 400) - (y0 - draw_state.abs_top) - px(4))
    imgui.dummy(cw, height)
    if graph is None or not getattr(graph, "layers", None):
        return False, None

    # ── pixel layout from the graph in column/row order ───────────────────────
    # Box sizes memoized per (name, scale) on the state (underscored, not
    # saved); a column is its widest box, rows share one pitch = the
    # tallest box + row gap, so boxes never overlap.
    size_memo = state.__dict__.setdefault("_size_memo", {})
    font_size = imgui.get_font_size()

    def font_scale_of(p):
        return 1.0 + (usage_font_scale_max - 1.0) * graph.usage(p)

    def box_size(p):
        scale = font_scale_of(p)
        key = (p.name, round(scale, 3))
        size = size_memo.get(key)
        if size is None:
            text = imgui.calc_text_size(p.name)
            size = size_memo[key] = (text.x * scale + 2 * px(box_pad_x),
                                     font_size * scale + 2 * px(box_pad_y))
        return size

    sizes = {p: box_size(p) for layer in graph.layers for p in layer}
    pitch = max(h for _w, h in sizes.values()) + px(row_gap)
    boxes = {}                      # p → (x, y, w, h) in layout px (unzoomed)
    column_x = 0.0
    for layer in graph.layers:
        widest = max(sizes[p][0] for p in layer)
        total_h = len(layer) * pitch - px(row_gap)
        y = -total_h * 0.5
        for p in layer:
            w, h = sizes[p]
            boxes[p] = (column_x + (widest - w) * 0.5, y + (pitch - px(row_gap) - h) * 0.5, w, h)
            y += pitch
        column_x += widest + px(column_gap)
    total_w = column_x - px(column_gap)
    tallest = max(len(layer) for layer in graph.layers) * pitch
    fit = min(cw / max(total_w, 1.0), height / max(tallest, 1.0), 1.0)

    # ── camera ───────────────────────────────────────────────────────────────
    if slash_key_pressed:
        state.zoom, state.pan_x, state.pan_y = 1.0, 0.0, 0.0
    if middle_mouse_drag is not None:
        state.pan_x += middle_mouse_drag.dx
        state.pan_y += middle_mouse_drag.dy
    origin_x, origin_y = x0 + cw * 0.5, y0 + height * 0.5
    if scroll_y_changed is not None:
        # Zoom about the cursor: the box under the mouse stays put.
        factor = math.exp(0.23 * scroll_y_changed.value)
        new_zoom = min(50.0, max(0.1, state.zoom * factor))
        mx, my = imgui.get_mouse_pos()
        ratio = new_zoom / state.zoom
        state.pan_x = (mx - origin_x) - ((mx - origin_x) - state.pan_x) * ratio
        state.pan_y = (my - origin_y) - ((my - origin_y) - state.pan_y) * ratio
        state.zoom = new_zoom
    scale = fit * state.zoom
    centre_x = total_w * 0.5

    def to_screen(box):
        x, y, w, h = box
        return (origin_x + (x - centre_x) * scale + state.pan_x,
                origin_y + y * scale + state.pan_y, w * scale, h * scale)

    screen = {p: to_screen(box) for p, box in boxes.items()}
    meta = _meta()

    # ── selection / hover ────────────────────────────────────────────────────
    if escape_key_pressed and state.selected is not None:
        state.select(None)
        request_render()
    mx, my = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered

    def node_at(pt):
        if pt is None:
            return None
        for p, (sx, sy, sw, sh) in screen.items():
            if sx <= pt[0] <= sx + sw and sy <= pt[1] <= sy + sh:
                return p
        return None

    click = ((left_mouse_down.x, left_mouse_down.y)
             if (left_mouse_down and hasattr(left_mouse_down, "x")) else None)
    if click is not None:
        state.select(node_at(click))
        request_render()
    dclick = ((left_mouse_double_clicked.x, left_mouse_double_clicked.y)
              if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None)
    if dclick is not None:
        hit = node_at(dclick)
        if hit is not None:
            open_file(hit)
    hovered = node_at((mx, my)) if hover_ok else None

    selected = state.selected_path
    if selected is not None and selected not in screen:
        selected = None
    imports = graph.imports_of(selected) if selected is not None else set()
    importers = graph.importers_of(selected) if selected is not None else set()
    related = imports | importers | ({selected} if selected is not None else set())

    # ── colours ──────────────────────────────────────────────────────────────
    style_manager = Melty.style_manager
    brightness_clamp = _brightness_clamp_fn()
    fill_memo = {}

    def node_fill(tint, alpha):
        """The tab bar's mix of the file tint (same knobs as the tree rows)."""
        key = (tint, alpha)
        col = fill_memo.get(key)
        if col is None:
            mixed = style_manager.make_color_rgb(
                tint[0], tint[1], tint[2],
                value=Toggles.CodeEditor.tab_active_bg_brightness,
                factor=bg_theme_factor,
                saturation_scale=Toggles.CodeEditor.tab_active_bg_saturation,
                alpha=1.0)
            mixed = brightness_clamp(mixed[0], mixed[1], mixed[2], 0.0,
                                     Toggles.CodeEditor.tab_active_bg_max_brightness)
            col = fill_memo[key] = imgui.get_color_u32_rgba(mixed[0], mixed[1], mixed[2], alpha)
        return col

    selected_tint = (_tint_of(meta, selected) if selected is not None else None) or highlight_fallback_tint
    hue, saturation, value = colorsys.rgb_to_hsv(*selected_tint)
    importers_tint = colorsys.hsv_to_rgb(hue, saturation * importer_saturation,
                                         min(1.0, value + importer_value_boost))
    imports_col = imgui.get_color_u32_rgba(*selected_tint, 0.9)
    importers_col = imgui.get_color_u32_rgba(*importers_tint, 0.9)
    edge_col = imgui.get_color_u32_rgba(0.8, 0.85, 0.95, edge_alpha)
    faded_edge_col = imgui.get_color_u32_rgba(0.8, 0.85, 0.95, edge_alpha * faded_alpha)
    text_col = imgui.get_color_u32_rgba(0.92, 0.92, 0.92, 1.0)
    faded_text_col = imgui.get_color_u32_rgba(0.92, 0.92, 0.92, faded_alpha)
    outline_col = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.85)

    # ── edges (under the boxes): importer → imported, right edge → left edge
    # when the line runs left → right (the layered case), centre → centre
    # for a cycle's back edge ────────────────────────────────────────────────
    clip = getattr(draw_state, "abs_clip_rect", None)

    def visible(box):
        sx, sy, sw, sh = box
        return clip is None or (sx + sw >= clip[0] and sx <= clip[2] and sy + sh >= clip[1] and sy <= clip[3])

    def anchors(box_a, box_b):
        ax, ay, aw, ah = box_a
        bx, by, bw, bh = box_b
        if ax + aw <= bx:
            return (ax + aw, ay + ah * 0.5), (bx, by + bh * 0.5)
        return (ax + aw * 0.5, ay + ah * 0.5), (bx + bw * 0.5, by + bh * 0.5)

    for importer, targets in graph.imports.items():
        box_a = screen.get(importer)
        if box_a is None:
            continue
        for imported in targets:
            box_b = screen.get(imported)
            if box_b is None or not (visible(box_a) or visible(box_b)):
                continue
            a, b = anchors(box_a, box_b)
            if selected is None:
                col, thickness = edge_col, 1.0
            elif importer == selected:
                col, thickness = imports_col, 1.5          # what the selection imports
            elif imported == selected:
                col, thickness = importers_col, 1.5        # who imports the selection
            else:
                col, thickness = faded_edge_col, 1.0
            dl.add_line(a[0], a[1], b[0], b[1], col, thickness)

    # ── boxes: rounded rect in the file's tint, the name inside ──────────────
    radius = px(box_radius) * scale
    for p, (sx, sy, sw, sh) in screen.items():
        if not visible((sx, sy, sw, sh)):
            continue
        tint = _tint_of(meta, p) or unpainted_node
        faded = selected is not None and p not in related
        alpha = faded_alpha if faded else 1.0
        dl.add_rect_filled(sx, sy, sx + sw, sy + sh, node_fill(tint, alpha), rounding=radius)
        if p == selected:
            dl.add_rect(sx - px(1), sy - px(1), sx + sw + px(1), sy + sh + px(1), outline_col, rounding=radius, thickness=2.0)
        elif p in importers:
            dl.add_rect(sx, sy, sx + sw, sy + sh, importers_col, rounding=radius, thickness=1.5)
        elif p in imports:
            dl.add_rect(sx, sy, sx + sw, sy + sh, imports_col, rounding=radius, thickness=1.5)
        elif p == hovered:
            dl.add_rect(sx, sy, sx + sw, sy + sh, outline_col, rounding=radius, thickness=1.0)
        # The name at the box's own font scale × the camera scale (window
        # font scale retargets the entire list's font size; reset right after).
        text_scale = font_scale_of(p) * scale
        if text_scale * font_size >= 4.0:            # unreadable below this: skip
            imgui.set_window_font_scale(text_scale)
            dl.add_text(sx + px(box_pad_x) * scale, sy + px(box_pad_y) * scale,
                        faded_text_col if faded else text_col, p.name)
            imgui.set_window_font_scale(1.0)

    return False, None
