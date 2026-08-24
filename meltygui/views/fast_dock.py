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

import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles, WindowManager
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.core_undo import NavUndo
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
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
        rows.append((str(window_draw_state.name), window_draw_state.closed,
                     window_draw_state.live, _row_tint(managed_window, window_draw_state)))
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


@render_func(use_cache=True, selectable=False, show_add_delete=False, is_tree=False,
             show_name=False, searchable=True, shadow=True, tint=(0.24, 0.75, 0.68))
def draw_fast_dock(input_value, draw_state, style_manager=None, hide_internal=False,
                   left_mouse_down=False, search_text="", **kwargs):
    # input_value is Melty.registered_windows - a plain defaultdict - so there
    # is no is_default_for registration: the root calls this view explicitly.

    # ---- styling ----
    open_bg_value, open_text_value = 0.16, 1.357          # name button, window open
    open_factor, open_saturation = 0.659, 1.315
    closed_bg_value, closed_text_value = 0.045, 0.341     # name button, window closed
    closed_factor, closed_saturation = 0.90, 1.091
    target_bg_value, target_text_value = 0.103, 1.269     # summon button
    target_factor, target_saturation = 0.799, 0.764
    hover_bg_boost, hover_text_boost = 0.05, 1.5
    text_saturation = 0.8
    live_tint = (0.409, 0.1, 0.1)                         # the bolt on live windows

    # ---- geometry, authored at ui_scale 1.0 and scaled once per frame ----
    # The dock draws straight to the draw list, so nothing here follows the
    # scale the way a laid-out widget does - every one of these goes through
    # px(), or the rows lose their 31px boxes while the labels grow out of
    # them.
    px = Melty.px
    # [tint=(0.939, 0.453, 0.245)]
    row_height = px(31.0)
    row_gap = px(4.0)
    row_stride = row_height + row_gap
    # [tint=(0.35, 0.85, 0.94)]
    name_x = px(20.0)                                     # rows' left inset (the live bolt sits in it)
    target_width = px(26.0)
    right_pad = px(4.0)
    corner = px(6.0)
    text_nudge_x, text_nudge_y = px(2.0), px(-1.0)        # optical centering of text
    name_target_gap = px(6.0)                             # gap between name and summon button
    name_pad_x = px(10.9)                                 # left inset for icon/name text
    icon_gap = px(6.0)                                    # gap between icon and name
    manager_row_text_x, manager_row_text_y = px(8.0), px(7.0)  # "Window Manager" label offsets

    if style_manager is None:
        style_manager = Melty.style_manager
    draw_list = imgui.get_window_draw_list()
    origin_x, origin_y = imgui.get_cursor_screen_pos()
    content_width = draw_state.content_width or (draw_state.width or 200)
    # Flipped whenever this frame mutated window state (a row click, a stale
    # window pruned) - it is the view's `bool` return (style guide rule 10).
    changed = False

    # ---- collect + sort rows (same grouping as WINDOW_MANAGER_SORTED) ----
    # [tint=(0.989, 0.17, 0.497)]
    rows = []
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
        rows.append((name, managed_window, window_draw_state))
    rows.sort(key=lambda row: row[0].lower())

    # Dummy rows set the full content height so the window scrolls normally.
    # Keep the trailing row_gap as bottom padding so the last row isn't clipped.
    # The scroll clamp is content_height - clipped_height, but clipped_height
    # spans the WHOLE window (header included) while rows start below the
    # header - boost the content height by that top inset or the last row can
    # never scroll fully into view.
    top_inset = (origin_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(content_width, max(1.0, len(rows) * row_stride + max(0.0, top_inset)))

    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    press = left_mouse_down
    # [tint=(0.62, 0.47, 0.95)]
    click = (press.x, press.y) if (press and hasattr(press, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)

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

    names_lower = tuple(row[0].split("##")[0].lower() for row in rows)

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

    for row_index, (name, managed_window, window_draw_state) in enumerate(rows):
        row_top = origin_y + row_index * row_stride
        row_bottom = row_top + row_height

        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        icon = _row_icon(name, managed_window, window_draw_state)

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
        if is_open:
            text_color = _floor_value(text_color, Toggles.FastDock.active_text_min_brightness)
            # Shadow under the open row's name button - a standalone depth
            # mark (rows aren't draw_states the compositor can see). Clipped
            # to the dock's rect: partially scrolled rows still draw here.
            add_shadow((name_left, row_top, name_right - name_left, row_height), offset=11,
                       corner_radius=corner, clip=clip)
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

        # ---- target (summon) button - only for open windows ----
        if is_open:
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
            target_icon = f""
            target_icon_size = imgui.calc_text_size(target_icon)
            draw_list.add_text(target_left + (target_width - target_icon_size[0]) / 2.0 + text_nudge_x,
                               row_top + (row_height - target_icon_size[1]) / 2.0 + text_nudge_y,
                               _color_u32(target_text_color), target_icon)

        # ---- live indicator ----
        if window_draw_state.live:
            live_icon = f""
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
