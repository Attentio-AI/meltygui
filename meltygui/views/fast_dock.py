"""Fast Dock: the Dock's window list drawn directly to the draw list.

One @render_func body replaces draw_collection + the per-row
draw_managed_window / button / draw_tuple widgets. Rows are plain draw-list
rects/text with manual hit-testing, so a frame costs a handful of draw calls
instead of a render_func wrapper per widget. It still lives inside a normal
melty window (Mode.WINDOW chrome: drag, header, scroll, blit cache). Open
rows' buttons get their shadows from add_shadow() — standalone depth marks
that need no per-button draw_state for the compositor to see.

Interaction model: while the view is hovered the wrapper invalidates its tile
every frame (core_render's _bounding_hovered branch), so hover highlights and
clicks resolve inside the body with no per-row state. While NOT hovered the
tile is a cached blit — external changes (a window closed via its own X, a new
registration, a tint edit elsewhere) are caught by fast_dock_sync(), called
once per frame from the always-rendering root.
"""
import colorsys
import ctypes
import struct

import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles, WindowManager
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_glow, add_shadow, clear_glows
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.core_undo import NavUndo
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.core_views.search_glow import draw_search_highlight

_last_signature = None


def _row_tint(managed_window, window_draw_state):
    """The tint shown for a row: instance attr wins over the draw_state's,
    same precedence as draw_managed_window."""
    window_value = managed_window.input_value
    if window_value is not None and getattr(window_value, "tint", None) is not None:
        return window_value.tint
    return window_draw_state.tint


def _row_icon(name, managed_window, window_draw_state):
    """The icon shown for a row: the window draw_state's kwargs win over the
    @window registration's — same fallback order as the tint lookup in
    window_index."""
    icon = (window_draw_state._kwargs or {}).get("icon") if window_draw_state is not None else None
    if not icon:
        label = str(name).split("##")[0].strip()
        registration = (Melty.annotated_window_classes.get(label)
                        or Melty.annotated_window_classes.get(str(name)))
        if registration is not None:
            window_class, window_kwargs = registration
            icon = window_kwargs.get("icon") or getattr(window_class, "icon", None)
    return icon


def _dock_signature():
    rows = []
    for managed_window in Melty.registered_windows.values():
        window_draw_state = managed_window.draw_state
        if window_draw_state is None:
            continue
        name = str(window_draw_state.name)
        rows.append((name, window_draw_state.closed, window_draw_state.live,
                     _row_tint(managed_window, window_draw_state),
                     _row_icon(name, managed_window, window_draw_state)))
    rows.sort(key=lambda row: row[0])
    return tuple(rows)


def fast_dock_sync():
    """Once per frame from the root: repaint the Fast Dock when any window's
    dock-visible state changed OUTSIDE the dock (its own close button, a new
    window registering, a tint edit elsewhere). The dock's own clicks happen
    while it's hovered, where the wrapper already re-renders every frame."""
    global _last_signature
    signature = _dock_signature()
    if signature != _last_signature:
        _last_signature = signature
        Melty.cache.invalidate_up_by_obj(Melty.registered_windows, force=True)
        request_render()


def _summon(window_draw_state, dock_draw_state, row_top):
    """Reposition `window_draw_state` just right of the dock at this row and
    raise it — the same math as the old dock's name/target buttons, now via
    Melty.summon_window so the placement is bounded to the display (a row low
    in a long dock would otherwise open the window with its bottom off the
    bottom of the screen)."""
    this_window_right = dock_draw_state.abs_left + dock_draw_state.width
    Core.melty.summon_window(window_draw_state, this_window_right + 10, row_top)


def _mix(style_manager, tint, value, factor, saturation):
    # Rows whose window never set a tint fall back to this bright neutral.
    fallback_tint = (2.558, 0.5, 0.5)
    color = tint if (isinstance(tint, tuple) and len(tint) >= 3) else fallback_tint
    return style_manager.make_color_rgb(color[0], color[1], color[2], value=value,
                                        factor=factor, saturation_scale=saturation)


def _color_u32(color, alpha=1.0):
    return imgui.get_color_u32_rgba(color[0], color[1], color[2], alpha)


def _floor_value(rgb, min_value):
    """`rgb` with its hsv value raised to at least `min_value` (hue and
    saturation kept). Same floor as open_files._tab_text_color's
    min_brightness: a dark window tint otherwise scales the open row's text
    toward black."""
    if min_value <= 0.0:
        return rgb
    h, s, v = colorsys.rgb_to_hsv(*rgb[:3])
    if v >= min_value:
        return rgb
    return colorsys.hsv_to_rgb(h, s, min(min_value, 1.0)) + tuple(rgb[3:])


def _scale_saturation(rgb, scale):
    """`rgb` with its hsv saturation multiplied by `scale` (hue and value
    kept). This works where a saturation_scale into _mix does not: _mix's
    make_color_rgb only applies saturation_scale to the THEME-derived half
    of the blend, and at icon_bg_factor 0.45 most of the tile color is the
    raw window tint — so the knob barely moved the result. Scaling the
    FINAL color desaturates all of it."""
    if scale >= 1.0:
        return rgb
    h, s, v = colorsys.rgb_to_hsv(*rgb[:3])
    return colorsys.hsv_to_rgb(h, s * max(0.0, scale), v) + tuple(rgb[3:])


# (font_size, glyph) → the glyph's ink rect relative to the add_text pen.
_glyph_ink_cache = {}


def _draw_glyph_ink_centered(draw_list, glyph, center_x, center_y, color_u32):
    """add_text with the glyph's INK rect centered on (center_x, center_y).

    calc_text_size measures the font's LINE BOX; an icon glyph's ink sits
    wherever the face put it inside that box, so box-centering reads a few
    px off per glyph (the Important tiles made it visible). imgui does know
    the true ink rect — it is the quad add_text emits. So: draw at the
    box-centered guess, read the 4 vertices just written off the draw list,
    shift them onto the true center in place (same frame, no flicker), and
    cache the pen→ink offset so every later draw positions the pen exactly.
    A glyph missing from the atlas emits no quad and keeps the box guess."""
    line_box = imgui.calc_text_size(glyph)
    pen_x = center_x - line_box[0] / 2.0
    pen_y = center_y - line_box[1] / 2.0
    key = (imgui.get_font_size(), glyph)
    ink = _glyph_ink_cache.get(key)
    if ink is not None:
        ink_x0, ink_y0, ink_x1, ink_y1 = ink
        draw_list.add_text(center_x - (ink_x0 + ink_x1) / 2.0,
                           center_y - (ink_y0 + ink_y1) / 2.0, color_u32, glyph)
        return
    vertex_start = draw_list.vtx_buffer_size
    draw_list.add_text(pen_x, pen_y, color_u32, glyph)
    if draw_list.vtx_buffer_size != vertex_start + 4:
        return                                            # glyph not in the atlas: box guess stands
    vertex_base = draw_list.vtx_buffer_data + vertex_start * imgui.VERTEX_SIZE
    raw = ctypes.string_at(vertex_base, 3 * imgui.VERTEX_SIZE)
    ink_x0, ink_y0 = struct.unpack_from("ff", raw, 0)
    ink_x1, ink_y1 = struct.unpack_from("ff", raw, 2 * imgui.VERTEX_SIZE)
    _glyph_ink_cache[key] = (ink_x0 - pen_x, ink_y0 - pen_y, ink_x1 - pen_x, ink_y1 - pen_y)
    shift_x = center_x - (ink_x0 + ink_x1) / 2.0
    shift_y = center_y - (ink_y0 + ink_y1) / 2.0
    if abs(shift_x) > 0.01 or abs(shift_y) > 0.01:
        for vertex_index in range(4):
            position = ctypes.cast(vertex_base + vertex_index * imgui.VERTEX_SIZE,
                                   ctypes.POINTER(ctypes.c_float))
            position[0] += shift_x
            position[1] += shift_y


def dock_header(draw_state=None, style_manager=None, **kwargs):
    """The Fast Dock's window header: the standard chrome plus the All /
    Important tab strip drawn IN the header band. Drawn from the body the
    strip was clipped whenever tab_pad_y lifted it above the content rect;
    as header content it owns the band legitimately. This function is
    draw-only: it stamps each tab's screen rect on
    draw_state._dock_tab_rects and the BODY resolves clicks there, where
    the left_mouse_down event reliably arrives."""
    # The wrapper hands us the cursor already parked at the header origin -
    # capture it BEFORE the chrome draws and moves it.
    header_x, header_y = imgui.get_cursor_screen_pos()
    changed = draw_header(draw_state=draw_state, style_manager=style_manager, **kwargs)

    # ---- layout (same px discipline as the body) ----
    px = Melty.px
    # [tint=(0.35, 0.85, 0.94)]
    tab_strip_x = px(30.0)                                # left inset: sits right of the tint widget
    tab_height = px(26.0)
    tab_gap = px(6.6)                                     # gap between tabs
    tab_pad_x = px(10.0)                                  # tab label side padding
    tab_nudge_y = px(0.0)                                 # vertical trim from the header band
    corner = px(6.0)
    text_nudge_x, text_nudge_y = px(2.0), px(-1.0)        # optical centering of glyphs

    # ---- styling (the body's open/closed row families) ----
    open_bg_value, open_text_value = 0.16, 1.357
    closed_bg_value, closed_text_value = 0.045, 0.463
    hover_bg_boost, hover_text_boost = 0.05, 1.5
    open_saturation, text_saturation = 1.315, 0.8

    if style_manager is None:
        style_manager = Melty.style_manager
    draw_list = imgui.get_window_draw_list()
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    dock_tab = getattr(draw_state, "dock_tab", None) or "all"
    clip = getattr(draw_state, "abs_clip_rect", None)

    tab_left = header_x + tab_strip_x
    tab_top = header_y + tab_nudge_y
    tab_bottom = tab_top + tab_height
    tab_rects = []
    for tab_key, tab_label in (("all", "All"), ("important", "Important")):
        tab_width = imgui.calc_text_size(tab_label)[0] + 2.0 * tab_pad_x
        tab_right = tab_left + tab_width
        is_active = dock_tab == tab_key
        tab_hovered = (hover_ok and tab_left <= mouse_x <= tab_right
                       and tab_top <= mouse_y <= tab_bottom)
        bg_value = (open_bg_value if is_active else closed_bg_value) \
            + (hover_bg_boost if tab_hovered else 0.0)
        text_value = (open_text_value if is_active else closed_text_value) \
            + (hover_text_boost if tab_hovered else 0.0)
        if is_active:
            add_shadow((tab_left, tab_top, tab_width, tab_height), offset=11,
                       corner_radius=corner, clip=clip)
        # Tabs carry no tint of their own: factor=1.0 makes make_color_rgb
        # ignore the rgb args and return the window's current tint at the
        # desired value/saturation - the strip follows the header tint.
        if is_active or tab_hovered:
            tab_bg = style_manager.make_color_rgb(0.0, 0.0, 0.0, value=bg_value, factor=1.0,
                                                  saturation_scale=open_saturation)
            draw_list.add_rect_filled(tab_left, tab_top, tab_right, tab_bottom,
                                      _color_u32(tab_bg), rounding=corner)
        tab_text = _floor_value(
            style_manager.make_color_rgb(0.0, 0.0, 0.0, value=text_value, factor=1.0,
                                         saturation_scale=text_saturation),
            Toggles.FastDock.active_text_min_brightness if is_active
            else Toggles.FastDock.inactive_text_min_brightness)
        label_size = imgui.calc_text_size(tab_label)
        draw_list.add_text(tab_left + tab_pad_x + text_nudge_x,
                           tab_top + (tab_height - label_size[1]) / 2.0 + text_nudge_y,
                           _color_u32(tab_text), tab_label)
        tab_rects.append((tab_key, (tab_left, tab_top, tab_right, tab_bottom)))
        tab_left = tab_right + tab_gap
    draw_state._dock_tab_rects = tuple(tab_rects)
    return changed


@render_func(use_cache=True, selectable=False, show_add_delete=False, show_close=False, is_tree=False,
             show_name=False, searchable=True, shadow=True, with_header=dock_header)
def draw_fast_dock(input_value, draw_state, style_manager=None, hide_internal=False,
                   dock_tab="all", left_mouse_down=False, search_text="", **kwargs):
    # input_value is Melty.registered_windows - a plain defaultdict - so there
    # is no is_default_for registration: the root calls this view explicitly.
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
    # draw_states and calling each node's _search_matcher (melty.search_walk)
    # - rows here aren't draw_states, so this view is its own single matcher
    # node claiming one slot per matching row name, in the same ordinal order
    # the row loop draws them, keeping count and current-index aligned.
    from src.lsd.gl_gui.melty import SearchTerm
    from src.lsd.gl_gui.view.core_views.new_core_view import _fuzzy_key_match
    from src.lsd.gl_gui.view.core_views.text_editor import _scroll_into_view

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