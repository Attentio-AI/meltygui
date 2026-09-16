"""Layout view functions and supporting definitions."""
from meltygui.core.layout_core import dock_header
from meltygui.core.melty import Melty
from meltygui.model.layout_model import Columns
from meltygui.model.layout_model import Rows
from meltygui.core.core_render import render_func
from meltygui.core.core_decoration import Core
from meltygui.state.core_undo import NavUndo
from meltygui.core.toggles import Toggles
from meltygui.core.toggles import WindowManager
import meltygui_imgui as imgui


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False,
             is_default_for=Columns)
def draw_columns(input_value, column_widths=None, column_edges=None,
                 draw_state=None, resizable=True, child_kwargs=None, **kwargs):
    """Side-by-side cells lined up with shared draggable edges.

    Edges are {"x": float} dicts in window coordinates (see the edge-model
    comment above). This view owns its interior edges (auto-state
    ``column_edges``); when it renders as a cell of another columns view it
    adopts the enclosing cell's two edge objects as its far edges
    (``left_edge``/``right_edge`` kwargs, passed by reference). All edges
    register on the ROOT WINDOW, which solves collisions once per frame and
    drives its own frame from the direct row's far edges; this view just
    queues drags and lines its cells up with its edges.
    """
    from meltygui.model.layout_model import Rows
    from meltygui.core.column_core import ColumnLayout
    from meltygui.core.render_dispatch import draw_any

    if child_kwargs is None:
        child_kwargs = {}

    if isinstance(input_value, dict):
        keys = [k for k in input_value
                if not (isinstance(k, str) and (k.startswith("_") or k.endswith("_")))]
    elif isinstance(input_value, (list, tuple)):
        keys = list(range(len(input_value)))
    else:
        imgui.text(f"draw_columns: no view for {type(input_value).__name__}")
        return False, input_value

    if not keys:
        return False, input_value
    n_cols = len(keys)

    cols = ColumnLayout(draw_state, n_cols, column_edges=column_edges,
                        column_widths=column_widths,
                        left_edge=kwargs.get("left_edge"),
                        right_edge=kwargs.get("right_edge"),
                        resizable=resizable)

    # ----- cells: lined up with their edges; nested Columns get the cell's
    # edge objects by reference -----
    changed = False
    for idx, key in enumerate(keys):
        item = input_value[key]
        with cols.cell(idx) as cell_width:
            item_kwargs = {"name": f"{key}", "align_header": False,
                           "width": cell_width} | child_kwargs
            if isinstance(item, Columns):
                item_kwargs |= {"left_edge": cols.edges[idx],
                                "right_edge": cols.edges[idx + 1]}
            if isinstance(item, (Columns, Rows)):
                # Row edges pass THROUGH a Columns cell: a Rows nested in
                # this cell adopts the enclosing row's edges (handed to us
                # by draw_rows), so its cells will collide with them.
                for key in ("top_edge", "bottom_edge"):
                    if kwargs.get(key) is not None:
                        item_kwargs[key] = kwargs[key]
            item_changed, out_value, cell_ds = draw_any(
                item, return_extras=True, **item_kwargs)
            cols.note_child(idx, cell_ds)  # border follows the child's corners
        if item_changed:
            changed = True
            if isinstance(input_value, (dict, list)):
                input_value[key] = out_value

    cols.finish()

    return changed, input_value


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False,
             is_default_for=Rows)
def draw_rows(input_value, row_heights=None, row_edges=None,
              draw_state=None, resizable=True, child_kwargs=None, **kwargs):
    """Stacked cells lined up with shared draggable edges — draw_columns
    along y.

    Edges are {"y": float} dicts in window coordinates. This view owns its
    interior edges (auto-state ``row_edges``); as a cell of another rows
    view it adopts the enclosing cell's two edge objects as its far edges
    (``top_edge``/``bottom_edge`` kwargs, by reference); as the window's
    rows view it adopts the window's top/bottom frame edges. Every cell is
    pinned to its row's height AND the rows' width (a passed height alone
    collapses a child's width), so a Columns cell fills its row.
    """
    from meltygui.model.layout_model import Columns
    from meltygui.core.column_core import RowLayout
    from meltygui.core.render_dispatch import draw_any

    if child_kwargs is None:
        child_kwargs = {}

    if isinstance(input_value, dict):
        keys = [k for k in input_value
                if not (isinstance(k, str) and (k.startswith("_") or k.endswith("_")))]
    elif isinstance(input_value, (list, tuple)):
        keys = list(range(len(input_value)))
    else:
        imgui.text(f"draw_rows: no view for {type(input_value).__name__}")
        return False, input_value

    if not keys:
        return False, input_value
    n_rows = len(keys)

    rows = RowLayout(draw_state, n_rows, row_edges=row_edges,
                     row_heights=row_heights,
                     top_edge=kwargs.get("top_edge"),
                     bottom_edge=kwargs.get("bottom_edge"),
                     resizable=resizable)

    changed = False
    cell_width = rows.inner_width()
    for idx, key in enumerate(keys):
        item = input_value[key]
        with rows.cell(idx) as cell_height:
            item_kwargs = {"name": f"{key}", "align_header": False,
                           "height": cell_height,
                           "width": cell_width} | child_kwargs
            if isinstance(item, (Rows, Columns)):
                # A nested Rows adopts this row's edges; a Columns carries
                # them through to any Rows in ITS cells (draw_columns).
                item_kwargs |= {"top_edge": rows.edges[idx],
                                "bottom_edge": rows.edges[idx + 1]}
                # And column edges pass THROUGH a Rows cell to the_columns.
                for key in ("left_edge", "right_edge"):
                    if kwargs.get(key) is not None:
                        item_kwargs[key] = kwargs[key]
            item_changed, out_value, cell_ds = draw_any(
                item, return_extras=True, **item_kwargs)
            rows.note_child(idx, cell_ds)
        if item_changed:
            changed = True
            if isinstance(input_value, (dict, list)):
                input_value[key] = out_value

    rows.finish()

    return changed, input_value


@render_func(use_cache=True, selectable=False, show_add_delete=False, show_close=False, is_tree=False,
             show_name=False, searchable=True, shadow=True, with_header=dock_header)
def draw_fast_dock(input_value, draw_state, style_manager=None, hide_internal=False,
                   dock_tab="all", left_mouse_down=False, search_text="", **kwargs):
    # input_value is Melty.registered_windows - a plain defaultdict - so there
    # is no is_default_for registration: the root calls this view explicitly.
    from meltygui.core.fonts import Font
    from meltygui.core.glfw_utils import request_render
    from meltygui.view.search_view import draw_search_highlight
    from meltygui.core.tile_cache import add_glow
    from meltygui.core.tile_cache import add_shadow
    from meltygui.core.tile_cache import clear_glows
    from meltygui.core.dock_core import _color_u32
    from meltygui.core.dock_core import _draw_glyph_ink_centered
    from meltygui.core.dock_core import _floor_value
    from meltygui.core.dock_core import _mix
    from meltygui.core.dock_core import _row_icon
    from meltygui.core.dock_core import _scale_saturation
    from meltygui.core.dock_core import _summon

    imgui.dummy(0, 10)
    # ---- styling ----
    open_bg_value, open_text_value = 0.16, 1.357          # name button, window open
    open_factor, open_saturation = 0.659, 1.315
    closed_bg_value, closed_text_value = 0.045, 0.463     # name button, window closed
    closed_factor, closed_saturation = 0.46, 0.630
    target_bg_value, target_text_value = 0.103, 1.269     # summon button
    target_factor, target_saturation = 0.799, 0.764
    hover_bg_boost, hover_text_boost = 0.05, 1.5
    text_saturation = 0.8
    # Important tab's tiles mix like APP ICONS: more raw window tint (lower
    # factor keeps more of the tint's source rgb) at a higher saturation and
    # value than the row buttons, so each tile reads as its window's colour.
    icon_bg_value, icon_bg_saturation, icon_bg_factor = 0.32, 2.2, 0.45
    icon_bg_closed_value = 0.12                           # dimmer value for a closed window's icon
    icon_bg_closed_saturation = 0.45                      # closed tiles sit muted (multiplier on the saturation)
    live_tint = (0.409, 0.1, 0.1)                         # the bolt on live rows (All tab)
    # Important tab: live shows as a little circle badge riding the tile's
    # top-right corner, iOS-notification style. Fixed design colour (rule 18).
    live_badge_color = (0.86, 0.24, 0.2)

    # ---- icons (glyph literals so the editor renders them as a picker) ----
    target_icon = f""                                    # summon button
    live_icon = f""                                      # live-row bolt
    recent_fallback_icon = f""                           # Recently added tile of a window with no icon

    # ---- geometry, authored at ui_scale 1.0 and scaled once per frame ----
    # The dock draws straight to the draw list, so nothing here follows the
    # scale the way a laid-out widget does - every one of these goes through
    # px(), or the rows lose their 31px boxes while the labels grow out of
    # them.
    px = Melty.px
    important = dock_tab == "important"
    # [tint=(0.939, 0.453, 0.245)]
    row_height = px(40) if important else px(25.0)
    # [tint=(0.939, 0.453, 0.245)]
    icon_box = px(40)                                     # Important tab: SQUARE tile around the icon
    if important:
        # The tile is always square (icon_box a constant): a tile taller than the
        # row keeps the line spacing up here instead of squashing.
        row_height = max(row_height, icon_box)
    row_gap = px(12) if important else px(2.0)
    row_stride = row_height + row_gap
    # [tint=(0.35, 0.85, 0.94)]
    # Rows' left inset: the All tab keeps it for the live bolt; the Important
    # tab's live state rides the tile as a badge, so its rows sit flush left.
    name_x = px(3.8) if important else px(20.0)
    live_badge_inset = px(3.0)                            # live bolt's center in from the tile corner
    target_width = px(20.0)
    right_pad = px(1)

    # Default radius for name/rows/summon, a custom one for the icon tiles
    corner = px(6)
    icon_corner = px(12)                                # Important tab's icon tile
    # [tint=(0.939, 0.453, 0.245)]
    icon_nudge_x, icon_nudge_y = px(0.0), px(0.0)         # optical centering of the tile's glyph
    text_nudge_x, text_nudge_y = px(2.0), px(-1.0)        # optical centering of glyphs
    name_target_gap = px(6.0)                             # gap between name and summon button
    name_pad_x = px(10)                                 # left inset for icon/name text
    icon_gap = px(6)                                    # gap between icon and name
    manager_row_text_x, manager_row_text_y = px(8.0), px(7.0)  # "Window Manager" label offsets
    # Important tab's section headings ("Recently added" / "Important"):
    # a small muted label on its own line above the section's tiles.
    heading_height = px(18.0)
    heading_gap = px(8.0)                               # space between a section's last tile and the next heading
    heading_value, heading_saturation = 0.62, 0.4       # theme mix of the heading text
    if style_manager is None:
        style_manager = Melty.style_manager
    draw_list = imgui.get_window_draw_list()
    origin_x, origin_y = imgui.get_cursor_screen_pos()
    content_width = draw_state.content_width or (draw_state.width or 200)
    # Flipped whenever this frame mutated window state (a row click, a stale
    # window pruned) - it is the view's `bool` return (style guide rule 10).
    changed = False
    # Clear this body run's glow group: the Important tab's open tiles re-emit
    # below; a run that stops emitting (tab switch, window closed) sheds its
    # retained marks, while cache-served frames keep glowing.
    clear_glows(draw_state)

    # ---- collect + sort rows (same grouping as WINDOW_MANAGER_SORTED) ----
    # [tint=(0.989, 0.17, 0.497)]
    rows = []
    candidates = []                                       # every dock-visible row before the icon filter
    for key, managed_window in list(input_value.items()):
        window_draw_state = managed_window.draw_state
        if window_draw_state is None:
            continue
        name = str(window_draw_state.name)
        if name in WindowManager.excluded_windows or str(key) in WindowManager.excluded_windows:
            continue
        if hide_internal and not window_draw_state._kwargs.get("icon", None):
            continue
        if (not window_draw_state.persistent and not window_draw_state.seen
                and window_draw_state.closed):
            Core.melty.delete_window(window_draw_state)
            changed = True
            continue
        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        icon = _row_icon(name, managed_window, window_draw_state)
        candidates.append((name, managed_window, window_draw_state, icon))
        if dock_tab == "important" and not icon:
            continue
        rows.append((name, managed_window, window_draw_state, icon))
    rows.sort(key=lambda row: row[0].lower())

    # ---- Important tab: "Recently added" = the newest first-seen names
    # (AppModel.render_windows via melty.recent_windows), newest first, icon
    # or not - a window with no icon wears recent_fallback_icon on its tile.
    # Rows are placed as (row_top, row) so a heading can inter between the
    # sections; the regular tiles follow under their own "Important" label.
    # Names that no longer resolve to a registered window are skipped.
    sections = [(None, rows)]
    if important:
        by_name = {row[0]: row for row in candidates}
        recent_rows = []
        for recent_name in Melty.recent_windows(Toggles.FastDock.recently_added_count):
            row = by_name.get(recent_name)
            if row is not None:
                name, managed_window, window_draw_state, icon = row
                recent_rows.append((name, managed_window, window_draw_state,
                                    icon or recent_fallback_icon))
        if recent_rows:
            sections = [("Recently added", recent_rows), ("Important", rows)]
    # [tint=(0.989, 0.17, 0.497)]
    placed = []                                           # (row_top, row) in draw order
    headings = []                                         # (heading_top, label)
    cursor_y = origin_y
    for section_label, section_rows in sections:
        if section_label is not None:
            if placed:
                cursor_y += heading_gap
            headings.append((cursor_y, section_label))
            cursor_y += heading_height
        for row in section_rows:
            placed.append((cursor_y, row))
            cursor_y += row_stride

    # Dummy rows set the full content height so the window scrolls normally.
    # Keep the trailing row_gap as bottom padding so the last row isn't clipped.
    # The scroll clamp is content_height - clipped_height, but clipped_height
    # spans the WHOLE window (header included) while rows start below the
    # header - boost the content height by that top inset or the last row can
    # never scroll fully into view.
    top_inset = (origin_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(content_width, max(1.0, (cursor_y - origin_y) + max(0.0, top_inset)))

    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    press = left_mouse_down
    # [tint=(0.62, 0.47, 0.95)]
    click = (press.x, press.y) if (press and hasattr(press, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)

    # ---- tab switching (All / Important) ----
    # The strip is drawn by dock_header in the HEADER band, where the body's
    # left_mouse_down param never reaches (the window's own header handlers
    # block that area). Each tab is therefore its own on_action sub-rect over
    # the screen rect the header stamped: priority_delta=4 outranks the
    # header chrome (flat_button's rule), and as body on_actions they replay
    # on blit-cache hits, so tabs stay interactive on cached frames too.
    for tab_key, tab_rect in (getattr(draw_state, "_dock_tab_rects", None) or ()):
        pressed = draw_state.on_action("left_mouse_down", view_id="dock_tab_" + tab_key,
                                       rect=tab_rect, priority_delta=4)
        if pressed is not None and dock_tab != tab_key:
            draw_state.dock_tab = tab_key                 # auto-update: persists + invalidates
            changed = True
            request_render()

    # ---- name loop-geometry (loop-invariant; only y varies per row) ----
    target_right = origin_x + content_width - right_pad
    target_left = target_right - target_width
    name_left, name_right = origin_x + name_x, target_left - name_target_gap

    # ---- local find-bar search ----
    # The window's find UI (searchable=True) counts matches by walking
    # draw_states and calling each node's _search_matcher (meltygui.draw_walk)
    # - rows here aren't draw_states, so this view is its own single matcher
    # node claiming one slot per matching row name, in the same ordinal order
    # the row loop draws them, keeping count and current-index aligned.
    from meltygui.core.melty import SearchTerm
    from meltygui.model.search_model import _fuzzy_key_match
    from meltygui.editor.text_editor import _scroll_into_view

    names_lower = tuple(row[0].split("##")[0].lower() for _, row in placed)

    def _search_matcher(term, session, names=names_lower):
        query = str(term).lower()
        if query:
            session.claim(sum(1 for name in names if _fuzzy_key_match(query, name)))

    draw_state._search_matcher = _search_matcher

    # Session resolution mirrors draw_collection: a forwarded SearchTerm in
    # search_text, else our own session when this view hosts the find UI.
    term = search_text or (draw_state.search_text if draw_state.search_active else "")
    if isinstance(term, SearchTerm):
        session = term
    elif draw_state.search_active and draw_state._search_session is not None:
        session = draw_state._search_session
    else:
        session = None
    query = str(term).lower() if (session is not None and term) else ""
    current_local = draw_state._search_active_local if query else None
    match_ordinal = 0
    # Required for Ctrl+Enter (search_activate_target): the current match's
    # row rect, so the injected "click the target" lands on the row instead of
    # the view's center.
    draw_state._search_current_rect = None

    # Highlights are deferred to a second pass AFTER the row loop: the current
    # match's radial glow spills over neighbouring rows, so drawn in-row it gets
    # painted over by the later row's background rect. draw_search_highlight
    # clips its own font out of the glow, so drawing it on top stays legible.
    highlight_rects = []

    # ---- section headings (Important tab with a Recently added section) ----
    heading_color = _color_u32(style_manager.make_color_rgb(
        0.0, 0.0, 0.0, value=heading_value, factor=1.0, saturation_scale=heading_saturation))
    for heading_top, heading_label in headings:
        if clip is not None and (heading_top + heading_height < clip[1] or heading_top > clip[3]):
            continue
        heading_size = imgui.calc_text_size(heading_label)
        draw_list.add_text(name_left + text_nudge_x,
                           heading_top + (heading_height - heading_size[1]) / 2.0 + text_nudge_y,
                           heading_color, heading_label)

    for row_top, (name, managed_window, window_draw_state, icon) in placed:
        row_bottom = row_top + row_height

        # Match bookkeeping runs for EVERY row - clipped ones too - so the
        # ordinal sequence stays aligned with the matcher's count, and the
        # current match can scroll into view from off-screen.
        display = name.split("##")[0]
        is_match = bool(query) and _fuzzy_key_match(query, display.lower())
        is_current = is_match and current_local is not None and match_ordinal == current_local
        if is_match:
            match_ordinal += 1
        if is_current:
            draw_state._search_current_rect = (name_left, row_top,
                                               name_right - name_left, row_height)
            if session.scroll_to:
                _scroll_into_view(draw_state, row_top, row_bottom, center=True)

        if clip is not None and (row_bottom < clip[1] or row_top > clip[3]):
            continue

        tint = window_draw_state.locate_tint

        if name == "Window Manager":
            text_color = _mix(style_manager, tint, target_text_value, 1.0, text_saturation)
            draw_list.add_text(origin_x + name_x + manager_row_text_x,
                               row_top + manager_row_text_y, _color_u32(text_color), name)
            continue

        # ---- hit-testing (x is loop-invariant, hoisted above) ----
        in_target = target_left <= mouse_x <= target_right and row_top <= mouse_y <= row_bottom
        in_name = name_left <= mouse_x <= name_right and row_top <= mouse_y <= row_bottom

        # ---- name button (open vs closed styling from the old dock) ----
        is_open = not window_draw_state.closed
        name_hovered = hover_ok and in_name
        if is_open:
            factor, bg_value, text_value, saturation = (open_factor, open_bg_value,
                                                        open_text_value, open_saturation)
        else:
            factor, bg_value, text_value, saturation = (closed_factor, closed_bg_value,
                                                        closed_text_value, closed_saturation)
        bg_color = _mix(style_manager, tint, bg_value + (hover_bg_boost if name_hovered else 0.0),
                        factor, saturation)
        text_color = _mix(style_manager, tint,
                          text_value + (hover_text_boost if name_hovered else 0.0),
                          factor, text_saturation)
        # Legibility floor on the text's hsv value: a dark window tint would
        # otherwise scale it toward black. Change the floors in Toggles.FastDock.
        text_color = _floor_value(text_color, Toggles.FastDock.active_text_min_brightness if is_open
                                  else Toggles.FastDock.inactive_text_min_brightness)
        if important:
            # An Important tab: the background is a rounded tile around the
            # icon alone; the name rides outside it to the right, bare. Open
            # tiles get a shadow AND emit light (add_glow), so the active set
            # reads at a glance. The icon draws a glyph up through the
            # DEJAVU_SANS_24 group (its merged FONTAWESOME_18).
            box_left = name_left
            box_right = box_left + icon_box
            box_top = row_top + (row_height - icon_box) / 2.0
            box_bottom = box_top + icon_box
            # The tile always shows; a CLOSED window's is dimmer and flat -
            # no shadow, no glow - so the lit, floating tiles read as open.
            tile_color = _mix(style_manager, tint,
                              (icon_bg_value if is_open else icon_bg_closed_value)
                              + (hover_bg_boost if name_hovered else 0.0),
                              icon_bg_factor, icon_bg_saturation)
            
            # todo take the tint and boost the saturation 
            glow_color = _mix(style_manager, tint,
                                        0.0, 0.5, 1.0)
            
            if not is_open:
                # Post-mix desaturation - the _mix saturation knob only
                # touches the tint half of the blend (see _scale_saturation).
                tile_color = _scale_saturation(tile_color, icon_bg_closed_saturation)
            if is_open:
                add_shadow((box_left, box_top, icon_box, icon_box), offset=11,
                           corner_radius=icon_corner, clip=clip)
                # offset=0: the glow's receiver band tops out at emitter
                # offset + Toggles.glow_mask_upper_offset (8 steps); at the
                # default offset 2 that reached +10 - one quantized step
                # under the tile's's +11 mark - so glow bled onto the
                # tile box and icon. At 0 the band ends at +8 and the glow
                # is cast on the WINDOW around the tile, never over it.
                glow_padding = -1
                add_glow((box_left + glow_padding, box_top + glow_padding, 
                          icon_box - glow_padding*2, icon_box-glow_padding*2), glow_color[:3],
                         offset=0.0, corner_radius=icon_corner, clip=clip,
                         draw_state=draw_state)
            draw_list.add_rect_filled(box_left, box_top, box_right, box_bottom,
                                      _color_u32(tile_color), rounding=icon_corner)
            if is_match:
                highlight_rects.append((row_top, row_bottom, is_current))
            icon_font = Melty.font_mgr.get(Font.DEJAVU_SANS_24) if Melty.font_mgr is not None else None
            if icon_font is not None:
                imgui.push_font(icon_font)
            # The glyph's INK rect (not its line box) centers on the tile -
            # see _draw_glyph_ink_centered; the ridges stay inside optical trim.
            _draw_glyph_ink_centered(draw_list, icon,
                                     box_left + icon_box / 2.0 + icon_nudge_x,
                                     box_top + icon_box / 2.0 + icon_nudge_y,
                                     _color_u32(text_color))
            if icon_font is not None:
                imgui.pop_font()
            # ---- live indicator: a bare red bolt over the tile corner ----
            if window_draw_state.live:
                _draw_glyph_ink_centered(draw_list, live_icon,
                                         box_right - live_badge_inset,
                                         box_top + live_badge_inset,
                                         _color_u32(live_badge_color, alpha=1.0))
            text_x = box_right + icon_gap
        else:
            if is_open:
                # Shadow under the open row's name button - a visible presence
                # mark (rows aren't draw_states the compositor can use). Clipped
                # to the dock's rect: partially scrolled rows still draw here.
                add_shadow((name_left, row_top, name_right - name_left, row_height), offset=11,
                           corner_radius=corner, clip=clip)

            # [tint=(0.444, 0.427, 0.393, 1.0), show_tint=True]
            show_inactive_bg = False
            if is_open or show_inactive_bg:
                draw_list.add_rect_filled(name_left, row_top, name_right, row_bottom,
                                          _color_u32(bg_color), rounding=corner)

            if is_match:
                highlight_rects.append((row_top, row_bottom, is_current))

            text_x = name_left + name_pad_x + text_nudge_x

            if icon:
                icon_size = imgui.calc_text_size(icon)
                draw_list.add_text(text_x, row_top + (row_height - icon_size[1]) / 2.0 + text_nudge_y,
                                   _color_u32(text_color), icon)
                text_x += icon_size[0] + icon_gap

        text_size = imgui.calc_text_size(display)
        draw_list.add_text(text_x, row_top + (row_height - text_size[1]) / 2.0 + text_nudge_y,
                           _color_u32(text_color), display)

        # ---- target (summon) button - open windows, and only while the
        # pointer is on the row (the row tile re-renders on the hover
        # edges, so the button appears/disappears with the pointer) ----
        row_hovered = hover_ok and row_top <= mouse_y <= row_bottom
        if is_open and row_hovered:
            target_hovered = hover_ok and in_target
            target_bg_color = _mix(style_manager, tint,
                                   target_bg_value + (hover_bg_boost if target_hovered else 0.0),
                                   target_factor, target_saturation)
            target_text_color = _mix(style_manager, tint,
                                     target_text_value + (hover_text_boost if target_hovered else 0.0),
                                     target_factor, text_saturation)
            add_shadow((target_left, row_top, target_right - target_left, row_height),
                       corner_radius=corner, clip=clip)
            draw_list.add_rect_filled(target_left, row_top, target_right, row_bottom,
                                      _color_u32(target_bg_color), rounding=corner)
            target_icon_size = imgui.calc_text_size(target_icon)
            draw_list.add_text(target_left + (target_width - target_icon_size[0]) / 2.0 + text_nudge_x,
                               row_top + (row_height - target_icon_size[1]) / 2.0 + text_nudge_y,
                               _color_u32(target_text_color), target_icon)

        # ---- live indicator (All tab's bolt; Important has the badge) ----
        if window_draw_state.live and not important:
            live_icon_size = imgui.calc_text_size(live_icon)
            draw_list.add_text(origin_x + (name_x - live_icon_size[0]) / 2.0,
                               row_top + (row_height - live_icon_size[1]) / 2.0 + text_nudge_y,
                               _color_u32(live_tint), live_icon)

        # ---- clicks ----
        if click is not None and row_top <= click[1] <= row_bottom:
            click_x = click[0]
            if name_left <= click_x <= name_right:
                was_closed = window_draw_state.closed
                if window_draw_state.closed:
                    window_draw_state.closed = False
                    _summon(window_draw_state, draw_state, row_top)
                else:
                    window_draw_state.closed = True
                NavUndo.record_window(window_draw_state, was_closed, window_draw_state.closed)
                Core.melty.cache.invalidate_up_by_obj(managed_window)
                changed = True
                request_render()
            elif is_open and target_left <= click_x <= target_right:
                _summon(window_draw_state, draw_state, row_top)
                Core.melty.cache.invalidate_up_by_obj(managed_window)
                changed = True
                request_render()

    # ---- search highlights (second pass, over every row's background) ----
    for highlight_top, highlight_bottom, highlight_current in highlight_rects:
        draw_search_highlight(draw_list, name_left, highlight_top, name_right, highlight_bottom,
                              current=highlight_current, rounding=corner)

    return changed, input_value
