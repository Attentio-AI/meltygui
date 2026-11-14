"""A button: a label in a box that says when it was pressed.

    pressed, _ = draw_button('Run', name='run', left=8.0, top=8.0, width=60.0)
    if pressed:
        ...

The colours are `look(tint)` of the value views (value_view.py): pass `tint=` on the call for
another family. The sizes are that file's levers too.
"""
import meltygui_imgui as imgui

from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view.value_view import PAD, ROW, SLATE, box, look

TINT = SLATE                    # the tint a button's colours are made from


@gui(cursor=mouse_cursor.HAND, height=ROW)
def draw_button(input_value: str, width=0.0, height=0.0, event=None):
    """Returns (True, label) in the run of a left press."""
    pressed = event.left_mouse_down()
    table = look(TINT)
    fill = table.field
    if event.is_hovered():
        fill = table.on
    draw_list = imgui.get_window_draw_list()
    y = box(draw_list, table, width, height, fill)
    x = max((width - imgui.calc_text_size(input_value).x) * 0.5, PAD)
    draw_list.push_clip_rect(0.0, 0.0, width, height, True)
    draw_list.add_text(x, y, table.text, input_value)
    draw_list.pop_clip_rect()
    return pressed, input_value
