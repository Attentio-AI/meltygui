"""Layout core functions and supporting definitions."""
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
import meltygui_imgui as imgui


def dock_header(draw_state=None, style_manager=None, **kwargs):
    """The Fast Dock's window header: the standard chrome plus the All /
    Important tab strip drawn IN the header band. Drawn from the body the
    strip was clipped whenever tab_pad_y lifted it above the content rect;
    as header content it owns the band legitimately. This function is
    draw-only: it stamps each tab's screen rect on
    draw_state._dock_tab_rects and the BODY resolves clicks there, where
    the left_mouse_down event reliably arrives."""
    from meltygui.view.header_view import draw_header
    from meltygui.core.cache.tile_cache import add_shadow
    from meltygui.core.windowing.dock_core import _color_u32
    from meltygui.core.windowing.dock_core import _floor_value

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
