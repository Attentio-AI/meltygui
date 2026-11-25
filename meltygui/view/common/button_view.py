"""A button: a label in a box that says when it was pressed.

    pressed, _ = draw_button('Run', name='run', left=8.0, top=8.0, width=60.0)
    if pressed:
        ...

    draw_button('Run', name='run', width=None, height=None)     # as large as its label

The colours are `look(tint)` of the value views (value_view.py): pass `tint=` on the call for
another family. The sizes are that file's levers too.
"""
import meltygui_imgui as imgui

from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view.common.value_view import PAD, ROW, SLATE, box, look

TINT = SLATE                    # the tint a button's colours are made from


@gui(cursor=mouse_cursor.HAND, height=ROW)
def draw_button(input_value: str, width=0.0, height=0.0, canvas=None, event=None):
    """Returns (True, label) in the run of a left press. A wrapping size (`width=None`,
    `height=None`) is the label's with `PAD` either side, and `ROW` high."""
    pressed = event.left_mouse_down()
    label = imgui.calc_text_size(input_value).x
    # On a wrapping axis `width` / `height` are the room the button may take: it takes its own size of it.
    if canvas.width is None:
        width = min(width, label + 2.0 * PAD)
    if canvas.height is None:
        height = min(height, ROW)
    imgui.dummy(width, height)          # the box is a draw list shape: this is what a wrapping size measures
    table = look(TINT)
    fill = table.field
    if event.is_hovered():
        fill = table.on
    draw_list = imgui.get_window_draw_list()
    y = box(draw_list, table, width, height, fill)
    x = max((width - label) * 0.5, PAD)
    draw_list.push_clip_rect(0.0, 0.0, width, height, True)
    draw_list.add_text(x, y, table.text, input_value)
    draw_list.pop_clip_rect()
    return pressed, input_value
