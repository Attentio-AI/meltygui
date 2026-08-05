"""Fast Dock: the Dock's window list drawn directly to the draw list.

One @render_func body replaces draw_collection + the per-row
draw_managed_window / button / draw_tuple widgets. Rows are plain draw-list
rects/text with manual hit-testing, so a frame costs a handful of draw calls
instead of a render_func wrapper per widget. It still lives inside a normal
melty window (Mode.WINDOW chrome: drag, header, scroll, blit cache) — only the
per-row framework shadows are gone, since individual buttons no longer exist
as draw_states the compositor can see.

Interaction model: while the view is hovered the wrapper invalidates its tile
every frame (core_render's _bounding_hovered branch), so hover highlights and
clicks resolve inside the body with no per-row state. While NOT hovered the
tile is a cached blit — external changes (a window closed via its own X, a new
registration, a tint edit elsewhere) are caught by fast_dock_sync(), called
once per frame from the always-rendering root.
"""
import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import WindowManager
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.search_glow import draw_search_highlight

# Row constants, authored for ui_scale 1.0. The dock draws straight to the draw
# list, so nothing here follows the font the way a laid-out widget does - every
# one of these goes through Melty.px() into a scaled local at the top of
# draw_fast_dock, or the rows keep their 31px height while the labels grow out
# of them.
ROW_H = 31.0
ROW_GAP = 4.0
ROW_STRIDE = ROW_H + ROW_GAP
SWATCH_W = 18.0
SWATCH_X = 18.0
NAME_X = 42.0
TARGET_W = 26.0
RIGHT_PAD = 4.0
CORNER = 6.0
TARGET_ICON = ""
LIVE_ICON = ""
LIVE_TINT = (0.409, 0.1, 0.1)
DEFAULT_TINT = (2.558, 0.5, 0.5)

_last_sig = None


def _row_tint(mw, wds):
    """The tint shown for a row: instance attr wins over the draw_state's,
    same precedence as draw_managed_window."""
    iv = mw.input_value
    if iv is not None and getattr(iv, "tint", None) is not None:
        return iv.tint
    return wds.tint


def _set_row_tint(mw, wds, tint):
    iv = mw.input_value
    if iv is not None and hasattr(iv, "tint"):
        iv.tint = tint
    wds.tint = tint


def _dock_signature():
    rows = []
    for mw in Melty.registered_windows.values():
        wds = mw.draw_state
        if wds is None:
            continue
        rows.append((str(wds.name), wds.closed, wds.live, _row_tint(mw, wds)))
    rows.sort(key=lambda r: r[0])
    return tuple(rows)


def fast_dock_sync():
    """Once per frame from the root: repaint the Fast Dock when any window's
    dock-visible state changed OUTSIDE the dock (its own close button, a new
    window registering, a tint edit elsewhere). The dock's own clicks happen
    while it's hovered, where the wrapper already re-renders every frame."""
    global _last_sig
    sig = _dock_signature()
    if sig != _last_sig:
        _last_sig = sig
        Melty.cache.invalidate_up_by_obj(Melty.registered_windows, force=True)
        request_render()


def _summon(wds, dock_ds, row_top):
    """Reposition `wds` just right of the dock at this row and raise it —
    the same math as the old dock's name/target buttons."""
    this_window_right = dock_ds.abs_left + dock_ds.width
    from_zero_x = wds.abs_left - wds.window_pos[0]
    from_zero_y = wds.abs_top - wds.window_pos[1]
    wds.window_pos = (this_window_right + 10 - from_zero_x, row_top - from_zero_y)
    Core.melty.move_window_to_front(wds)


def _mix(style_manager, tint, value, factor, saturation):
    c = tint if (isinstance(tint, tuple) and len(tint) >= 3) else DEFAULT_TINT
    return style_manager.make_color_rgb(c[0], c[1], c[2], value=value,
                                        factor=factor, saturation_scale=saturation)


@render_func(use_cache=True, selectable=False, show_add_delete=False, is_tree=False,
             show_name=False, searchable=True, shadow=True)
def draw_fast_dock(input_value, draw_state, style_manager=None,
                   left_mouse_down=False, search_text="", **kwargs):
    # ---- styling ----
    open_bg_value, open_text_value = 0.16, 1.357          # name button, window open
    open_factor, open_saturation = 0.659, 1.315
    closed_bg_value, closed_text_value = 0.035, 0.305     # name button, window closed
    closed_factor, closed_saturation = 0.92, 0.872
    target_bg_value, target_text_value = 0.103, 1.023     # summon button
    target_factor, target_saturation = 0.799, 0.764
    hover_bg_boost, hover_text_boost = 0.05, 1.5
    text_saturation = 0.8
    empty_swatch_color = (1.0, 1.0, 1.0, 0.25)

    # ---- geometry, authored at ui_scale 1.0 and scaled once per frame ----
    px = Melty.px
    row_h, row_stride = px(ROW_H), px(ROW_STRIDE)
    swatch_w, swatch_x = px(SWATCH_W), px(SWATCH_X)
    name_x, target_w, right_pad = px(NAME_X), px(TARGET_W), px(RIGHT_PAD)
    corner = px(CORNER)
    text_nudge_x, text_nudge_y = px(2.0), px(-1.0)        # optical centering of text
    swatch_rounding = px(4.0)
    name_target_gap = px(6.0)                             # gap between name and summon button
    manager_row_text_x, manager_row_text_y = px(8.0), px(7.0)  # "Window Manager" label offsets
    picker_width, picker_height = px(216), px(180 + 14 + 4 * 26 + 26)
    picker_gap_y = px(2.0)                                # popover offset below its trigger

    if style_manager is None:
        style_manager = Melty.style_manager
    dl = imgui.get_window_draw_list()
    x0, y0 = imgui.get_cursor_screen_pos()
    cw = draw_state.content_width or (draw_state.width or 200)

    # ---- collect + sort rows (same grouping as WINDOW_MANAGER_SORTED) ----
    rows = []
    for key, mw in list(input_value.items()):
        wds = mw.draw_state
        if wds is None:
            continue
        name = str(wds.name)
        if name in WindowManager.excluded_windows or str(key) in WindowManager.excluded_windows:
            continue
        if not wds.persistent and not wds.seen and wds.closed:
            Core.melty.delete_window(wds)
            continue
        rows.append((name, mw, wds))
    rows.sort(key=lambda r: r[0].lower())

    # Dummy rows set the full content height so the window scrolls normally.
    # Keep the trailing ROW_GAP as bottom padding so the last row isn't clipped.
    # The scroll clamp is content_height - clipped_height, but clipped_height
    # spans the WHOLE window (header included) while rows start below the
    # header - boost the content height by that top inset or the last row can
    # never scroll fully into view.
    top_inset = (y0 + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(cw, max(1.0, len(rows) * row_stride + max(0.0, top_inset)))

    mx, my = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    ev = left_mouse_down
    click = (ev.x, ev.y) if (ev and hasattr(ev, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)

    # ---- name loop-geometry (loop-invariant; only y varies per row) ----
    sw_x0, sw_x1 = x0 + swatch_x, x0 + swatch_x + swatch_w
    tg_x1 = x0 + cw - right_pad
    tg_x0 = tg_x1 - target_w
    nm_x0, nm_x1 = x0 + name_x, tg_x0 - name_target_gap

    # ---- local find-bar search ----
    # The window's find UI (searchable=True) counts matches by walking
    # draw_states and calling each node's _search_matcher (melty.search_walk)
    # - rows here aren't draw_states, so this view is its own single matcher
    # node claiming one slot per matching row name, in the same ordinal order
    # the row loop draws them, keeping count and current-index aligned.
    from src.lsd.gl_gui.melty import SearchTerm
    from src.lsd.gl_gui.view.core_views.new_core_view import _fuzzy_key_match
    from src.lsd.gl_gui.view.core_views.text_editor import _scroll_into_view

    _names_low = tuple(r[0].split("##")[0].lower() for r in rows)

    def _search_matcher(term, session, _names=_names_low):
        q = str(term).lower()
        if q:
            session.claim(sum(1 for n in _names if _fuzzy_key_match(q, n)))

    draw_state._search_matcher = _search_matcher

    # Session resolution mirrors draw_collection: a forwarded SearchTerm in
    # search_text, else our own session when this view hosts the find UI.
    _term = search_text or (draw_state.search_text if draw_state.search_active else "")
    if isinstance(_term, SearchTerm):
        _session = _term
    elif draw_state.search_active and draw_state._search_session is not None:
        _session = draw_state._search_session
    else:
        _session = None
    _q = str(_term).lower() if (_session is not None and _term) else ""
    _current_local = draw_state._search_active_local if _q else None
    _match_ord = 0
    # Required for Ctrl+Enter (search_activate_target): the current match's
    # row rect, so the injected "click the target" lands on the row instead of
    # the view's center.
    draw_state._search_current_rect = None

    edit_name = getattr(draw_state, "tint_edit_name", None)
    edit_row_top = None
    edit_row = None
    # Highlights are deferred to a second pass AFTER the row loop: the current
    # match's radial glow spills over neighbouring rows, so drawn in-row it gets
    # painted over by the later row's background rect. draw_search_highlight
    # clips its own font out of the glow, so drawing it on top stays legible.
    highlight_rects = []

    for i, (name, mw, wds) in enumerate(rows):
        ry0 = y0 + i * row_stride
        ry1 = ry0 + row_h
        if edit_name == name:
            edit_row_top = ry0
            edit_row = (name, mw, wds)

        # Match bookkeeping runs for EVERY row - clipped ones too - so the
        # ordinal sequence stays aligned with the matcher's count, and the
        # current match can scroll into view from off-screen.
        display = name.split("##")[0]
        is_match = bool(_q) and _fuzzy_key_match(_q, display.lower())
        is_current = is_match and _current_local is not None and _match_ord == _current_local
        if is_match:
            _match_ord += 1
        if is_current:
            draw_state._search_current_rect = (nm_x0, ry0, nm_x1 - nm_x0, row_h)
            if _session.scroll_to:
                _scroll_into_view(draw_state, ry0, ry1, center=True)

        if clip is not None and (ry1 < clip[1] or ry0 > clip[3]):
            continue

        tint = _row_tint(mw, wds)

        if name == "Window Manager":
            tx = _mix(style_manager, tint, target_text_value, 1.0, text_saturation)
            dl.add_text(x0 + name_x + manager_row_text_x, ry0 + manager_row_text_y,
                        imgui.get_color_u32_rgba(*tx[:3], 1.0), name)
            continue

        # ---- geometry (x is loop-invariant, hoisted above) ----
        sw_y0, sw_y1 = ry0 + (row_h - swatch_w) / 2.0, ry0 + (row_h + swatch_w) / 2.0

        in_swatch = sw_x0 <= mx <= sw_x1 and sw_y0 <= my <= sw_y1
        in_target = tg_x0 <= mx <= tg_x1 and ry0 <= my <= ry1
        in_name = nm_x0 <= mx <= nm_x1 and ry0 <= my <= ry1

        # ---- name button (open vs closed styling from the old dock) ----
        open_ = not wds.closed
        hov = hover_ok and in_name
        if open_:
            factor, bg_value, text_value, sat = open_factor, open_bg_value, open_text_value, open_saturation
        else:
            factor, bg_value, text_value, sat = closed_factor, closed_bg_value, closed_text_value, closed_saturation
        bg = _mix(style_manager, tint, bg_value + (hover_bg_boost if hov else 0.0), factor, sat)
        tx = _mix(style_manager, tint, text_value + (hover_text_boost if hov else 0.0), factor, text_saturation)
        dl.add_rect_filled(nm_x0, ry0, nm_x1, ry1,
                           imgui.get_color_u32_rgba(*bg[:3], 1.0), rounding=corner)

        if is_match:
            highlight_rects.append((ry0, ry1, is_current))

        ts = imgui.calc_text_size(display)
        dl.add_text(nm_x0 + (nm_x1 - nm_x0 - ts[0]) / 2.0 + text_nudge_x,
                    ry0 + (row_h - ts[1]) / 2.0 + text_nudge_y,
                    imgui.get_color_u32_rgba(*tx[:3], 1.0), display)

        # ---- tint swatch ----
        if isinstance(tint, tuple) and len(tint) >= 3:
            a = tint[3] if len(tint) > 3 else 1.0
            dl.add_rect_filled(sw_x0, sw_y0, sw_x1, sw_y1,
                               imgui.get_color_u32_rgba(tint[0], tint[1], tint[2], a),
                               rounding=swatch_rounding)
        else:
            dl.add_rect(sw_x0, sw_y0, sw_x1, sw_y1,
                        imgui.get_color_u32_rgba(*empty_swatch_color),
                        rounding=swatch_rounding)

        # ---- target (summon) button ----
        hov_t = hover_ok and in_target
        bg_t = _mix(style_manager, tint, target_bg_value + (hover_bg_boost if hov_t else 0.0),
                    target_factor, target_saturation)
        tx_t = _mix(style_manager, tint, target_text_value + (hover_text_boost if hov_t else 0.0),
                    target_factor, text_saturation)
        dl.add_rect_filled(tg_x0, ry0, tg_x1, ry1,
                           imgui.get_color_u32_rgba(*bg_t[:3], 1.0), rounding=corner)
        its = imgui.calc_text_size(TARGET_ICON)
        dl.add_text(tg_x0 + (target_w - its[0]) / 2.0 + text_nudge_x,
                    ry0 + (row_h - its[1]) / 2.0 + text_nudge_y,
                    imgui.get_color_u32_rgba(*tx_t[:3], 1.0), TARGET_ICON)

        # ---- live indicator ----
        if wds.live:
            ls = imgui.calc_text_size(LIVE_ICON)
            dl.add_text(x0 + (swatch_x - ls[0]) / 2.0,
                        ry0 + (row_h - ls[1]) / 2.0 + text_nudge_y,
                        imgui.get_color_u32_rgba(*LIVE_TINT, 1.0), LIVE_ICON)

        # ---- clicks ----
        if click is not None and ry0 <= click[1] <= ry1:
            cx = click[0]
            if nm_x0 <= cx <= nm_x1:
                if wds.closed:
                    wds.closed = False
                    _summon(wds, draw_state, ry0)
                else:
                    wds.closed = True
                Core.melty.cache.invalidate_up_by_obj(mw)
                request_render()
            elif tg_x0 <= cx <= tg_x1:
                _summon(wds, draw_state, ry0)
                Core.melty.cache.invalidate_up_by_obj(mw)
                request_render()
            elif sw_x0 <= cx <= sw_x1 and sw_y0 <= click[1] <= sw_y1:
                if not (isinstance(tint, tuple) and len(tint) >= 3):
                    _set_row_tint(mw, wds, (0.0, 0.0, 0.0, 1.0))
                was_open = (Melty.popover_focused_ds is draw_state and edit_name == name)
                if was_open:
                    Melty.popover_focused_ds = None
                    draw_state.tint_edit_name = None
                else:
                    Melty.popover_focused_ds = draw_state
                    Melty._popover_open_frame = Melty.frame_count
                    draw_state.tint_edit_name = name
                    edit_name = name
                    edit_row_top = ry0
                    edit_row = (name, mw, wds)
                request_render()

    # ---- search highlights (second pass, over every row's background) ----
    for hy0, hy1, hcur in highlight_rects:
        draw_search_highlight(dl, nm_x0, hy0, nm_x1, hy1,
                              current=hcur, rounding=corner)

    # ---- tint picker popover (only rendered while open - zero idle cost) ----
    if edit_name is not None:
        is_open = Melty.popover_focused_ds is draw_state and edit_row is not None
        if not is_open:
            draw_state.tint_edit_name = None
        else:
            from src.lsd.gl_gui.modes import Modes
            from src.lsd.gl_gui.view.core_views.new_core_view import draw_color_picker
            name, mw, wds = edit_row
            cur = _row_tint(mw, wds)
            changed, new_color = draw_color_picker(
                cur, name="fast_dock_picker", closed=False,
                window_pos=(swatch_x, (edit_row_top or y0) - y0 + row_h + picker_gap_y),
                parent_window=draw_state, width=picker_width, height=picker_height,
                mode=Modes.POPOVER)
            if changed and new_color is not None:
                _set_row_tint(mw, wds, tuple(new_color))
                Core.melty.cache.invalidate_up_by_obj(mw)
                request_render()

    return False, input_value
