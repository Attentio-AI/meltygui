"""Compact codec badges shared by file lists and editor tabs."""
import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color


def draw_file_badge(draw_list, x, y, size, color, badge, alpha=1.0):
    """Tinted page with a small codec accent; geometry stays inside the tint chip."""
    label, accent, mark = badge
    unit = size / 28.0
    # Change these proportions to adjust the page/badge balance.
    left, top = x + 5 * unit, y + unit
    right, bottom = x + 23 * unit, y + 26 * unit
    fold = 5 * unit
    ink = pack_color(0.08, 0.10, 0.13, alpha)
    if mark == "image":
        # The same white-to-metadata-tint blend as the other file bases.
        # A dark inset makes the landscape distinct even with no assigned tint.
        draw_list.add_rect_filled(x + unit, y + 2 * unit, x + 27 * unit,
                                  y + 19 * unit, color, rounding=unit)
        draw_list.add_rect_filled(x + 3 * unit, y + 4 * unit, x + 25 * unit,
                                  y + 17 * unit, ink)
        draw_list.add_triangle_filled(x + 4 * unit, y + 16 * unit,
                                      x + 10 * unit, y + 9 * unit,
                                      x + 16 * unit, y + 16 * unit, color)
        draw_list.add_triangle_filled(x + 12 * unit, y + 16 * unit,
                                      x + 18 * unit, y + 11 * unit,
                                      x + 24 * unit, y + 16 * unit, color)
        draw_list.add_circle_filled(x + 20 * unit, y + 7 * unit, 1.5 * unit, color)
    else:
        draw_list.add_rect_filled(left, top, right - fold, bottom, color, rounding=unit)
        draw_list.add_rect_filled(right - fold, top + fold, right, bottom, color)
        draw_list.add_triangle_filled(right - fold, top, right, top + fold,
                                      right - fold, top + fold, color)
        draw_list.add_line(right - fold, top, right - fold, top + fold, ink, unit)
        draw_list.add_line(right - fold, top + fold, right, top + fold, ink, unit)
    if mark == "python":
        # Interlocking blue/yellow snakes, drawn as vectors so no brand font is needed.
        blue = pack_color(0.18, 0.43, 0.65, alpha)
        yellow = pack_color(1.0, 0.82, 0.28, alpha)
        sx, sy = x + 8 * unit, y + 4 * unit
        for dx, dy, width, height, paint in (
                (3, 0, 7, 5, blue), (0, 4, 7, 5, blue),
                (5, 5, 7, 5, yellow), (2, 9, 7, 4, yellow)):
            draw_list.add_rect_filled(sx + dx * unit, sy + dy * unit,
                                      sx + (dx + width) * unit, sy + (dy + height) * unit,
                                      paint, rounding=2 * unit)
        draw_list.add_circle_filled(sx + 5 * unit, sy + 2 * unit, 0.7 * unit, ink)
        draw_list.add_circle_filled(sx + 7 * unit, sy + 11 * unit, 0.7 * unit, ink)
    # A narrow colored footer leaves the page itself predominantly file-tinted.
    draw_list.add_rect_filled(x, y + 18 * unit, x + size, y + 27 * unit,
                              pack_color(*accent, alpha), rounding=unit)
    imgui.set_window_font_scale(0.36)
    try:
        extent = imgui.calc_text_size(label)
        # Dark red needs light lettering; the brighter badges use dark lettering.
        lettering = pack_color(1.0, 0.93, 0.93, alpha) if label == "MD" else ink
        draw_list.add_text(x + (size - extent.x) * 0.5,
                           y + 22.5 * unit - extent.y * 0.5, lettering, label)
    finally:
        imgui.set_window_font_scale(1.0)
