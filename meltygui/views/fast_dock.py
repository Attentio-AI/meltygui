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
import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import WindowManager
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.core_undo import NavUndo
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
NAME_X = 20.0
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


def _row_icon(name, mw, wds):
    """The icon shown for a row: the window draw_state's kwargs win over the
    @window registration's — same fallback order as the tint lookup in
    window_index."""
    icon = (wds._kwargs or {}).get("icon") if wds is not None else None
    if not icon:
        label = str(name).split("##")[0].strip()
        reg = (Melty.annotated_window_classes.get(label)
               or Melty.annotated_window_classes.get(str(name)))
        if reg is not None:
            w_cls, w_kwargs = reg
            icon = w_kwargs.get("icon") or getattr(w_cls, "icon", None)
    return icon


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
    the same math as the old dock's name/target buttons, now via
    Melty.summon_window so the placement is bounded to the display (a row low
    in a long dock would otherwise open the window with its bottom off the
    bottom of the screen)."""
    this_window_right = dock_ds.abs_left + dock_ds.width
    Core.melty.summon_window(wds, this_window_right + 10, row_top)


def _mix(style_manager, tint, value, factor, saturation):
    c = tint if (isinstance(tint, tuple) and len(tint) >= 3) else DEFAULT_TINT
    return style_manager.make_color_rgb(c[0], c[1], c[2], value=value,
                                        factor=factor, saturation_scale=saturation)


@render_func(use_cache=True, selectable=False, show_add_delete=False, is_tree=False,
             show_name=False, searchable=True, shadow=True)
def draw_fast_dock(input_value, draw_state, style_manager=None, hide_internal=False,
                   left_mouse_down=False, search_text="", **kwargs):
    # ---- styling ----
    open_bg_value, open_text_value = 0.16, 1.357          # name button, window open
    open_factor, open_saturation = 0.659, 1.315
    closed_bg_value, closed_text_value = 0.045, 0.341     # name button, window closed
    closed_factor, closed_saturation = 0.90, 1.091
    target_bg_value, target_text_value = 0.103, 1.269     # summon button
    target_factor, target_saturation = 0.799, 0.764
    hover_bg_boost, hover_text_boost = 0.05, 1.5
    text_saturation = 0.8

    # ---- geometry, authored at ui_scale 1.0 and scaled once per frame ----
    px = Melty.px
    row_h, row_stride = px(ROW_H), px(ROW_STRIDE)

    name_x, target_w, right_pad = px(NAME_X), px(TARGET_W), px(RIGHT_PAD)
    corner = px(CORNER)
    text_nudge_x, text_nudge_y = px(2.0), px(-1.0)        # optical centering of text
    name_target_gap = px(6.0)                             # gap between name and summon button
    name_pad_x = px(10.9)                                  # left padding for icon/name text
    icon_gap = px(6.0)                                    # gap between icon and name
    manager_row_text_x, manager_row_text_y = px(8.0), px(7.0)  # "Window Manager" label offsets

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

        if hide_internal and not wds._kwargs.get("icon", None):
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

    # Highlights are deferred to a second pass AFTER the row loop: the current
    # match's radial glow spills over neighbouring rows, so drawn in-row it gets
    # painted over by the later row's background rect. draw_search_highlight
    # clips its own font out of the glow, so drawing it on top stays legible.
    highlight_rects = []

    for i, (name, mw, wds) in enumerate(rows):
        ry0 = y0 + i * row_stride
        ry1 = ry0 + row_h

        # [tint=(0.85, 0.75, 0.05), show_tint=True]
        icon = _row_icon(name, mw, wds)

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

        tint = wds.locate_tint


        if name == "Window Manager":
            tx = _mix(style_manager, tint, target_text_value, 1.0, text_saturation)
            dl.add_text(x0 + name_x + manager_row_text_x, ry0 + manager_row_text_y,
                        imgui.get_color_u32_rgba(*tx[:3], 1.0), name)
            continue

        # ---- hit-testing (x is loop-invariant, hoisted above) ----
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
        if open_:
            # Shadow under the open row's name button - a standalone depth
            # mark (rows aren't draw_states the compositor can see). Clipped
            # to the dock's rect: partially scrolled rows still draw here.
            add_shadow((nm_x0, ry0, nm_x1 - nm_x0, row_h), offset=11,
                       corner_radius=corner, clip=clip)
        dl.add_rect_filled(nm_x0, ry0, nm_x1, ry1,
                           imgui.get_color_u32_rgba(*bg[:3], 1.0), rounding=corner)

        if is_match:
            highlight_rects.append((ry0, ry1, is_current))

        text_x = nm_x0 + name_pad_x + text_nudge_x


        if icon:
            ics = imgui.calc_text_size(icon)
            dl.add_text(text_x, ry0 + (row_h - ics[1]) / 2.0 + text_nudge_y,
                        imgui.get_color_u32_rgba(*tx[:3], 1.0), icon)
            text_x += ics[0] + icon_gap



        ts = imgui.calc_text_size(display)
        dl.add_text(text_x, ry0 + (row_h - ts[1]) / 2.0 + text_nudge_y,
                    imgui.get_color_u32_rgba(*tx[:3], 1.0), display)

        # ---- target (summon) button - only for open windows ----
        if open_:
            hov_t = hover_ok and in_target
            bg_t = _mix(style_manager, tint, target_bg_value + (hover_bg_boost if hov_t else 0.0),
                        target_factor, target_saturation)
            tx_t = _mix(style_manager, tint, target_text_value + (hover_text_boost if hov_t else 0.0),
                        target_factor, text_saturation)
            add_shadow((tg_x0, ry0, tg_x1 - tg_x0, row_h),
                       corner_radius=corner, clip=clip)
            dl.add_rect_filled(tg_x0, ry0, tg_x1, ry1,
                               imgui.get_color_u32_rgba(*bg_t[:3], 1.0), rounding=corner)
            its = imgui.calc_text_size(TARGET_ICON)
            dl.add_text(tg_x0 + (target_w - its[0]) / 2.0 + text_nudge_x,
                        ry0 + (row_h - its[1]) / 2.0 + text_nudge_y,
                        imgui.get_color_u32_rgba(*tx_t[:3], 1.0), TARGET_ICON)

        # ---- live indicator ----
        if wds.live:
            ls = imgui.calc_text_size(LIVE_ICON)
            dl.add_text(x0 + (name_x - ls[0]) / 2.0,
                        ry0 + (row_h - ls[1]) / 2.0 + text_nudge_y,
                        imgui.get_color_u32_rgba(*LIVE_TINT, 1.0), LIVE_ICON)

        # ---- clicks ----
        if click is not None and ry0 <= click[1] <= ry1:
            cx = click[0]
            if nm_x0 <= cx <= nm_x1:
                was_closed = wds.closed
                if wds.closed:
                    wds.closed = False
                    _summon(wds, draw_state, ry0)
                else:
                    wds.closed = True
                NavUndo.record_window(wds, was_closed, wds.closed)
                Core.melty.cache.invalidate_up_by_obj(mw)
                request_render()
            elif open_ and tg_x0 <= cx <= tg_x1:
                _summon(wds, draw_state, ry0)
                Core.melty.cache.invalidate_up_by_obj(mw)
                request_render()

    # ---- search highlights (second pass, over every row's background) ----
    for hy0, hy1, hcur in highlight_rects:
        draw_search_highlight(dl, nm_x0, hy0, nm_x1, hy1,
                              current=hcur, rounding=corner)

    return False, input_value