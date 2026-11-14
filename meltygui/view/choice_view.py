"""A dropdown: the current choice in a box; a press opens the list of `choices` under it.

    picked, size = draw_dropdown(size, name='size', choices=SIZES, left=8.0, top=8.0, width=160.0)

- `draw_dropdown`: the box. Input first, then the list, then the drawing.
- `options`: the list, a temporary popup (`popup=`). The pointer or the arrows and Enter pick, a
  press outside or Escape closes it. Nobody keeps an open flag.
Keep `choices` as a module constant: a tuple built in the caller's body is a new argument on every
run. The colours are `look(tint)` of the value views (value_view.py).
"""
import meltygui_imgui as imgui

import meltygui.core.windowing.window_constants as glfw
from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view.value_view import PAD, ROUND, ROW, SLATE, box, look

TINT = SLATE                    # the tint the box's and the list's colours are made from
GAP = 2.0                       # between the box and the list
ARROW = ''                # Font Awesome's chevron, at the box's right end


@gui(cursor=mouse_cursor.HAND, no_save=('index',))
def options(input_value, choices=(), index=None, width=0.0, height=0.0, draw_state=None, cache=None, event=None):
    """The list under a dropdown. Returns (True, choice) in the run one is picked."""
    if index is None:               # just opened: `index` is not saved
        index = 0
        if input_value in choices:
            index = choices.index(input_value)
    picked = False
    # The keyboard: the arrows move the selection around, Enter picks it. Asking for the keys is
    # what gives the open list the keyboard.
    for key, mods in event.keys():
        if key == glfw.KEY_DOWN:
            index = (index + 1) % len(choices)
        elif key == glfw.KEY_UP:
            index = (index - 1) % len(choices)
        elif key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
            picked = True
    # The pointer: the row it moves over is selected, a press picks it.
    pressed = event.left_mouse_down()
    if pressed or event.mouse_moved():
        index = min(max(int(event.y // ROW), 0), len(choices) - 1)
    if pressed:
        picked = True
    draw_state.index = index
    table = look(TINT)
    draw_list = imgui.get_window_draw_list()
    box(draw_list, table, width, height)
    y = (ROW - imgui.get_text_line_height()) * 0.5
    for at, choice in enumerate(choices):
        if at == index:
            draw_list.add_rect_filled(0.0, at * ROW, width, (at + 1) * ROW, table.on, ROUND)
        draw_list.add_text(PAD, at * ROW + y, table.text, str(choice))
    if picked:
        cache.focus.close()         # the keyboard returns to whoever had it
        return True, choices[index]
    return False, input_value


@gui(cursor=mouse_cursor.HAND, height=ROW)
def draw_dropdown(input_value, choices=(), width=0.0, height=0.0, event=None):
    """Returns (True, choice) in the run another one is picked. The box shows what the list
    returned, so a pick is seen in the run it is made in."""
    pressed = event.left_mouse_down()
    hot = event.is_hovered()
    picked, value = options(input_value, name='options', popup=pressed, choices=choices,
                            left=0.0, top=height + GAP, width=width, height=len(choices) * ROW)
    table = look(TINT)
    fill = table.field
    if hot:
        fill = table.on
    draw_list = imgui.get_window_draw_list()
    y = box(draw_list, table, width, height, fill)
    arrow = width - PAD - imgui.calc_text_size(ARROW).x
    draw_list.push_clip_rect(0.0, 0.0, arrow - PAD, height, True)
    draw_list.add_text(PAD, y, table.text, str(value))
    draw_list.pop_clip_rect()
    draw_list.add_text(arrow, y, table.dim, ARROW)
    return picked, value
