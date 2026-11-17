"""The menu of `@context_menu` (core/input/context_menu.py): the options of the view that was right
clicked, then those of each view around it, a line between two views' options.

- `draw_context_menu`: the popup. The pointer or the arrows and Enter pick; a press outside or
  Escape closes it (the popup rules, core/input/focus.py).
- `call`: runs a picked option, and is where its arguments are found.
- `rows`, `extent`: the layout and the size of the menu.
The levers are the constants below. The colours are `look(tint)` of the value views (value_view.py).
"""
import inspect
import traceback

import meltygui_imgui as imgui

import meltygui.core.windowing.window_constants as glfw
from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view.value_view import PAD, ROUND, ROW, SLATE, box, look

TINT = SLATE                    # the tint the menu's colours are made from
GAP = 7.0                       # height of the line between two views' options
NARROWEST = 120.0               # the menu is at least this wide
WIDEST = 420.0                  # and at most this: a longer label is cut


def rows(sections):
    """The menu's rows from the top, as (y, view, label, function), and its height. `sections` is
    [(view, {label: function})]; a GAP separates two of them."""
    found = []
    y = 0.0
    for at, (node, options) in enumerate(sections):
        if at:
            y += GAP
        for label, action in options.items():
            found.append((y, node, label, action))
            y += ROW
    return found, y


def extent(sections):
    """(width, height) of the menu of `sections`: as wide as its longest label."""
    found, height = rows(sections)
    width = max(imgui.calc_text_size(label).x for _, _, label, _ in found) + 2.0 * PAD
    return min(max(width, NARROWEST), WIDEST), height


def row_at(found, y):
    """The index of the row at height `y`; -1 on the line between two sections or outside."""
    for at, (top, _, _, _) in enumerate(found):
        if top <= y < top + ROW:
            return at
    return -1


def call(action, cache, node):
    """Run a picked option for the view `node`. A function that takes nothing is just called. One
    that names parameters is given, by name: `draw_state` (the view's state), `cache`, and whatever
    else the view's state reads under that name, which is every parameter of the view, a value its
    caller passed included. A parameter the view does not have keeps its default, or is an error."""
    try:
        parameters = inspect.signature(action).parameters
    except (TypeError, ValueError):
        return action()                 # a builtin that keeps its signature to itself
    state = cache.states.get(node)      # None for the immediate root
    arguments = {}
    for name, parameter in parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if name == 'cache':
            arguments[name] = cache
        elif name == 'draw_state' and state is not None:
            arguments[name] = state
        else:
            try:
                arguments[name] = getattr(state, name)
            except AttributeError:
                if parameter.default is parameter.empty:
                    raise TypeError(f'{action.__name__}() asks for {name!r}, which the view of its menu does not have') from None
    return action(**arguments)


@gui(cursor=mouse_cursor.HAND, no_save=('index',))
def draw_context_menu(input_value: list, index=None, width=0.0, height=0.0, draw_state=None, cache=None, event=None):
    """The menu of `input_value`, the sections [(view, {label: function})], innermost view first.
    A pick runs its function for its view (`call`), then that view and the ones around it run again
    (the function may have changed what they show), and the menu closes."""
    found, _ = rows(input_value)
    if index is None:               # just opened: `index` is not saved, and nothing is selected yet
        index = -1
    picked = False
    # The keyboard: the arrows move the selection around, Enter picks it. Asking for the keys is
    # what gives the open menu the keyboard.
    for key, mods in event.keys():
        if key == glfw.KEY_DOWN:
            index = (index + 1) % len(found)
        elif key == glfw.KEY_UP:
            index = (max(index, 0) - 1) % len(found)
        elif key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
            picked = index >= 0
    # The pointer: the row it moves over is selected, a press picks it.
    pressed = event.left_mouse_down()
    if pressed or event.mouse_moved():
        index = row_at(found, event.y)
    if pressed:
        picked = index >= 0
    draw_state.index = index
    table = look(TINT)
    draw_list = imgui.get_window_draw_list()
    box(draw_list, table, width, height)
    text_y = (ROW - imgui.get_text_line_height()) * 0.5
    last = None
    for at, (y, node, _, _) in enumerate(found):
        if last is not None and node != last:
            line = y - GAP * 0.5
            draw_list.add_rect_filled(PAD, line, width - PAD, line + 1.0, table.dim, 0.0)
        last = node
        if at == index:
            draw_list.add_rect_filled(0.0, y, width, y + ROW, table.select, ROUND)
    draw_list.push_clip_rect(0.0, 0.0, width - PAD, height, True)
    for y, _, label, _ in found:
        draw_list.add_text(PAD, y + text_y, table.text, label)
    draw_list.pop_clip_rect()
    if not picked:
        return
    _, node, _, action = found[index]
    cache.focus.close()             # the keyboard returns to whoever had it
    try:
        call(action, cache, node)
    except Exception:
        traceback.print_exc()       # an option that fails is reported, and the app goes on
    # What the option changed in place is shown by the view it belongs to and the views around it.
    while node:
        cache.invalidate_id(node)
        node = cache.graph.parent(node)
