"""Fast Dock: the Dock's window list drawn directly to the draw list.

One @render_func body replaces draw_collection + the per-row
draw_managed_window / button / draw_tuple widgets. Rows are plain draw-list
rects/text with manual hit-testing, so a frame costs a handful of draw calls
instead of a render_func wrapper per widget. It still lives inside a normal
meltygui window (Mode.WINDOW chrome: drag, header, scroll, blit cache). Open
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

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.runtime.toggles import WindowManager
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.styling.fonts import Font
from meltygui.core.cache.tile_cache import add_glow
from meltygui.core.cache.tile_cache import add_shadow
from meltygui.core.cache.tile_cache import clear_glows
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import Core

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
    return pack_color(color[0], color[1], color[2], alpha)


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


from meltygui.core.layout.layout_core import dock_header
