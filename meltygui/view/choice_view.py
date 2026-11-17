"""A dropdown: the current choice in a box; a press opens the list of `choices` under it.

    picked, size = draw_dropdown(size, name='size', choices=SIZES, left=8.0, top=8.0, width=160.0)

`choices` is one of two things:

    SIZES = ('Small', 'Medium', 'Large')                            # the choices themselves
    SIZES = {'Small': 1, 'Large': 3, 'More': {'Huge': 9}}           # {label: choice}

In a dict a value that is a dict (and not empty) is a submenu: its row has a chevron and opens
beside the list, to any depth. The value is always a leaf, whichever list it was picked from.

- `draw_dropdown`: the box. Input first, then the list, then the drawing.
- `options`: the list, a temporary popup (`popup=`). The pointer or the arrows and Enter pick, a
  press outside or Escape closes it. Nobody keeps an open flag. A submenu is the same view, called
  by the list for the row that is selected: Right or the pointer opens it, Left closes it, and it
  goes when the selection moves on, since the list then no longer calls it.
- `entries`, `submenu`, `extent`, `shown`: what a list's rows are, which of them open one, how
  large it is, and the label of a value. The context menu uses them too (context_menu_view.py).
Keep `choices` as a module constant: a tuple built in the caller's body is a new argument on every
run. Choices in a sequence that are not text (functions, objects) are named by `label=`, a function
of one choice that is a module constant too. The colours are `look(tint)` of the value views
(value_view.py).
"""
import meltygui_imgui as imgui

import meltygui.core.windowing.window_constants as glfw
from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view.value_view import PAD, ROUND, ROW, SLATE, box, look

TINT = SLATE                    # the tint the box's and the list's colours are made from
GAP = 2.0                       # between the box and the list
ARROW = ''                     # Font Awesome's chevron, at the box's right end
MORE = ''                      # and the one at the right end of a row that opens a submenu
NARROWEST = 120.0               # a submenu is at least this wide
WIDEST = 420.0                  # and at most this: a longer label is cut


def entries(choices, label=str):
    """The rows of `choices` as [(text, choice)]: a dict's keys name its values, `label` the rest."""
    if isinstance(choices, dict):
        return [(str(text), choice) for text, choice in choices.items()]
    return [(label(choice), choice) for choice in choices]


def submenu(choices, choice):
    """Whether the row of `choice` opens a list of its own: a dict in a dict. To nest other
    containers (a tuple in a dict), this is the test to widen."""
    return isinstance(choices, dict) and isinstance(choice, dict) and bool(choice)


def extent(choices, label=str):
    """(width, height) of the list of `choices`: as wide as its longest label, and a chevron's
    room more when a row opens a submenu."""
    found = entries(choices, label)
    width = 2.0 * PAD
    if found:
        width += max(imgui.calc_text_size(text).x for text, _ in found)
    if any(submenu(choices, choice) for _, choice in found):
        width += PAD + imgui.calc_text_size(MORE).x
    return min(max(width, NARROWEST), WIDEST), len(found) * ROW


def shown(choices, value, label=str):
    """The text of `value`: its key in a dict of choices, in whichever submenu it is; what `label`
    says of it in a sequence, or when the dict does not have it."""
    found = None
    if isinstance(choices, dict):
        found = named(choices, value)
    if found is None:
        return label(value)
    return found


def named(choices, value):
    """The label of `value` in the dict `choices` or a submenu of it; None when it is in none."""
    for text, choice in choices.items():
        if submenu(choices, choice):
            found = named(choice, value)
            if found is not None:
                return found
        elif choice == value:
            return str(text)
    return None


@gui(cursor=mouse_cursor.HAND, no_save=('index',))
def options(input_value, choices=(), label=str, index=None, width=0.0, height=0.0, draw_state=None, cache=None, event=None):
    """The list under a dropdown, and each submenu of it. Returns (True, choice) in the run one is
    picked, here or in a submenu."""
    found = entries(choices, label)
    if index is None:               # just opened: `index` is not saved. -1: no row is selected
        index = -1
        for at, (_, choice) in enumerate(found):
            if not submenu(choices, choice) and choice == input_value:
                index = at
    chosen = False                  # Enter or a press: the selected row is picked, or its submenu opens
    opened = False                  # Right, or the pointer came to the row: only its submenu opens
    back = False                    # Left: this list closes
    # The keyboard. Asking for the keys is what gives the open list the keyboard.
    for key, mods in event.keys():
        if key == glfw.KEY_DOWN:
            index = (index + 1) % len(found)
        elif key == glfw.KEY_UP:
            index = (max(index, 0) - 1) % len(found)
        elif key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
            chosen = True
        elif key == glfw.KEY_RIGHT:
            opened = True
        elif key == glfw.KEY_LEFT:
            back = True
    # The pointer: the row it moves over is selected, a press picks it.
    pressed = event.left_mouse_down()
    if pressed or event.mouse_moved():
        over = min(max(int(event.y // ROW), 0), len(found) - 1)
        if over != index:
            opened = True
        index = over
    if pressed:
        chosen = True
    draw_state.index = index
    picked = False
    value = input_value
    if index >= 0:
        text, choice = found[index]
        if submenu(choices, choice):
            # The selected row's own list, beside it. Another row selected: this call is not made, and it closes.
            across, down = extent(choice, label)
            picked, value = options(input_value, key=text, popup=chosen or opened, choices=choice, label=label,
                                    left=width, top=index * ROW, width=across, height=down)
        elif chosen:
            picked = True
            value = choice
    table = look(TINT)
    draw_list = imgui.get_window_draw_list()
    box(draw_list, table, width, height)
    y = (ROW - imgui.get_text_line_height()) * 0.5
    more = width - PAD - imgui.calc_text_size(MORE).x
    for at, (text, choice) in enumerate(found):
        if at == index:
            draw_list.add_rect_filled(0.0, at * ROW, width, (at + 1) * ROW, table.select, ROUND)
        draw_list.add_text(PAD, at * ROW + y, table.text, text)
        if submenu(choices, choice):
            draw_list.add_text(more, at * ROW + y, table.dim, MORE)
    if picked or back:
        cache.focus.close()         # the keyboard returns to whoever had it
    return picked, value


@gui(cursor=mouse_cursor.HAND, height=ROW)
def draw_dropdown(input_value, choices=(), label=str, width=0.0, height=0.0, event=None):
    """Returns (True, choice) in the run another one is picked. The box shows what the list
    returned, so a pick is seen in the run it is made in."""
    pressed = event.left_mouse_down()
    hot = event.is_hovered()
    picked, value = options(input_value, name='options', popup=pressed, choices=choices, label=label,
                            left=0.0, top=height + GAP, width=width, height=len(choices) * ROW)
    table = look(TINT)
    fill = table.field
    if hot:
        fill = table.on
    draw_list = imgui.get_window_draw_list()
    y = box(draw_list, table, width, height, fill)
    arrow = width - PAD - imgui.calc_text_size(ARROW).x
    draw_list.push_clip_rect(0.0, 0.0, arrow - PAD, height, True)
    draw_list.add_text(PAD, y, table.text, shown(choices, value, label))
    draw_list.pop_clip_rect()
    draw_list.add_text(arrow, y, table.dim, ARROW)
    return picked, value
