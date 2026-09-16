"""Reusable palette selection and preview functions."""
import meltygui_imgui as imgui
from meltygui.core.core_render import render_func
from meltygui.hdr_color import pack_color
from meltygui.model.lut_model import Lut
from meltygui.view.header_view import draw_header


@render_func(is_default_for="Lut", show_bg=False, is_tree=False,
             header_same_line=True, with_header=draw_header)
def draw_lut(input_value: Lut = None, draw_state=None, unique=0, luts=None, **kwargs):
    """Choose a palette from supplied data and preserve the Lut string type."""
    from meltygui.view.dropdown_view import draw_dropdown

    names = [str(k) for k in luts]
    current = str(input_value) if input_value else "jet"
    changed, picked = draw_dropdown(
        current, collection={n: n for n in names},
        name=f"lut##{unique}", show_header=False, width=140)
    if changed and picked:
        return True, Lut(picked)
    return False, input_value


@render_func(show_bg=False, tint=(0.45, 0.55, 0.7))
def draw_luts(input_value: dict, draw_state=None):
    """Preview supplied flat RGB lists; edits use the ordinary collection views."""
    luts = input_value if isinstance(input_value, dict) else {}
    draw_list = imgui.get_window_draw_list()
    bar_w, bar_h, segs = 160.0, 13.0, 48
    for name, lut in luts.items():
        if not isinstance(lut, (list, tuple)) or len(lut) < 6 or len(lut) % 3:
            imgui.text(f"{name}: not a flat [r,g,b,...] list")
            continue
        n = len(lut) // 3
        x, y = imgui.get_cursor_screen_pos()
        for s in range(segs):
            i = min(n - 1, int(s * (n - 1) / max(1, segs - 1))) * 3
            col = pack_color(lut[i], lut[i + 1], lut[i + 2], 1.0)
            draw_list.add_rect_filled(x + bar_w * s / segs, y,
                                      x + bar_w * (s + 1) / segs, y + bar_h, col)
        imgui.dummy(bar_w, bar_h)
        imgui.same_line()
        imgui.text(f"{name} ({n})")
    return False, input_value
