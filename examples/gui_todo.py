"""A to-do list: the worked example of docs/GUI_VIEW_GUIDE.md.

    .venv/bin/python examples/gui_todo.py

One file: a model that is a value (`Todo`), plain views that return `(changed, value)`, and a root
that only composes. What to look at:
- `button`, `dropdown`: controls drawn with the draw list, which ask their `event` what happened.
- `options`: a temporary popup (`popup=`) that the pointer and the keyboard both drive, closed with
  `cache.focus.close()`.
- `row`, `items`: a scroll list of keyed child views that edit the dict they are given.
- `uptime`: a result from another thread, shown through `cache.wake`.
- `app`: `cache.invalidate` after the root changed the model in place.
"""
import threading
import time

import meltygui.core.windowing.window_constants as glfw
from meltygui import gui, os_window, imgui, persisted
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.input import mouse_cursor
from meltygui.hdr_color import pack_color
from meltygui.rust import draw_bool, draw_str

# Levers: sizes in logical pixels, colours.
PAD = 8.0
ROW = 30.0                          # height of one item
TOOLS = 30.0                        # height of the toolbar's views
FILTERS = ('All', 'Open', 'Done')   # one object: a new tuple each run would run the list again
BACK = pack_color(0.10, 0.12, 0.15)     # the list under the dropdown
FACE = pack_color(0.20, 0.24, 0.30)     # a control
HOT = pack_color(0.28, 0.36, 0.48)      # a control under the pointer
PICK = pack_color(0.20, 0.36, 0.52)     # the selected row of the list
TEXT = pack_color(0.92, 0.94, 0.96)
DIM = pack_color(0.60, 0.64, 0.70)


class Todo(DictConversion):
    """The app's model, kept between runs: `items` is {key: {'title': str, 'done': bool}} in order."""
    def __init__(self):
        super().__init__()
        self.items = {'1': {'title': 'Write the guide', 'done': True}, '2': {'title': 'Test it', 'done': False}}
        self.filter = 'All'
        self.next = 3


todo = persisted('todo', Todo, app_id='meltygui-todo')


# --- small controls ----------------------------------------------------------
@gui(cursor=mouse_cursor.HAND, height=TOOLS)
def button(input_value: str, width=0.0, height=0.0, event=None):
    """A label in a box. Returns (True, label) in the run of a left press."""
    pressed = event.left_mouse_down()
    face = HOT if event.is_hovered() else FACE
    draw_list = imgui.get_window_draw_list()
    draw_list.add_rect_filled(0.0, 0.0, width, height, face, 5.0)
    draw_list.add_text(PAD, (height - imgui.get_text_line_height()) * 0.5, TEXT, input_value)
    return pressed, input_value


@gui(no_save=('index',))
def options(input_value, choices=(), index=None, width=0.0, height=0.0, draw_state=None, cache=None, event=None):
    """The list under a dropdown. Returns (True, choice) when one is picked."""
    if index is None:               # just opened: `index` is not saved
        index = choices.index(input_value) if input_value in choices else 0
    # The keyboard: the arrows move the selection, Enter picks it. Asking for keys is what gives
    # the open list the keyboard.
    if event.key_down(glfw.KEY_DOWN):
        index = min(index + 1, len(choices) - 1)
    if event.key_down(glfw.KEY_UP):
        index = max(index - 1, 0)
    picked = event.key_down(glfw.KEY_ENTER)
    # The pointer: the row it moves over is selected, a press picks it.
    pressed = event.left_mouse_down()
    if pressed or event.mouse_moved():
        index = min(max(int(event.y // ROW), 0), len(choices) - 1)
    if pressed:
        picked = True
    draw_state.index = index
    draw_list = imgui.get_window_draw_list()
    draw_list.add_rect_filled(0.0, 0.0, width, height, BACK, 5.0)
    for at, choice in enumerate(choices):
        if at == index:
            draw_list.add_rect_filled(0.0, at * ROW, width, (at + 1) * ROW, PICK, 4.0)
        draw_list.add_text(PAD, at * ROW + (ROW - imgui.get_text_line_height()) * 0.5, TEXT, str(choice))
    if picked:
        cache.focus.close()
        return True, choices[index]
    return False, input_value


@gui(cursor=mouse_cursor.HAND, height=TOOLS)
def dropdown(input_value, choices=(), width=0.0, height=0.0, event=None):
    """The current choice; a press opens the list under it. Input first, then the list, then the
    drawing: the label is drawn from what the list returned, so a pick shows in the run it is made in."""
    pressed = event.left_mouse_down()
    face = HOT if event.is_hovered() else FACE
    picked, value = options(input_value, name='options', popup=pressed, choices=choices,
                            left=0.0, top=height + 2.0, width=width, height=len(choices) * ROW)
    draw_list = imgui.get_window_draw_list()
    draw_list.add_rect_filled(0.0, 0.0, width, height, face, 5.0)
    draw_list.add_text(PAD, (height - imgui.get_text_line_height()) * 0.5, TEXT, f'{value}  ')
    return picked, value


# --- the list ----------------------------------------------------------------
@gui(height=ROW)
def row(input_value: dict, width=0.0, height=0.0):
    """One item: its tick and its title, edited in the dict it came from."""
    ticked, input_value['done'] = draw_bool(input_value['done'], name='done', left=PAD, top=4.0, width=70.0,
                                            height=height - 8.0)
    typed, input_value['title'] = draw_str(input_value['title'], name='title', left=PAD + 78.0, top=4.0,
                                           width=width - 2.0 * PAD - 78.0, height=height - 8.0)
    return ticked or typed, input_value


@gui(scroll=True)
def items(input_value: dict, show='All', width=0.0, height=0.0):
    """The items `show` lets through, one `row` each."""
    shown = [key for key, item in input_value.items() if show == 'All' or item['done'] == (show == 'Done')]
    changed = False
    for at, key in enumerate(shown):
        edited, _ = row(input_value[key], key=key, left=0.0, top=at * ROW, width=width, height=ROW)
        if edited:
            changed = True
    return changed, input_value


# --- a result from another thread -------------------------------------------
class Clock:
    """Seconds since launch, counted on a thread: a stand-in for any background result."""
    seconds = 0

    def start(self, wake):
        def count():
            while True:
                time.sleep(1.0)
                self.seconds += 1
                wake()                          # thread safe: runs the view that showed the old value
        threading.Thread(target=count, daemon=True).start()


clock = Clock()


@gui(listen=(), no_save=('started',), height=TOOLS)
def uptime(input_value: Clock, started=False, width=0.0, height=0.0, draw_state=None, cache=None):
    if not started:
        node = cache.current
        input_value.start(lambda: cache.wake(node))
        draw_state.started = True
    text = f'{input_value.seconds} s'
    left = width - imgui.calc_text_size(text).x
    top = (height - imgui.get_text_line_height()) * 0.5
    imgui.get_window_draw_list().add_text(left, top, DIM, text)


@os_window(name='To do', app_id='meltygui-todo', width=520, height=420, min_width=320, min_height=200, use_cache=False)
def app(width=None, height=None, cache=None):
    top = PAD
    added, _ = button('Add', name='add', left=PAD, top=top, width=60.0)
    if added:
        todo.items[str(todo.next)] = {'title': f'Item {todo.next}', 'done': False}
        todo.next += 1
        cache.invalidate(todo.items)        # changed in place, outside the views that show it
    picked, todo.filter = dropdown(todo.filter, name='filter', choices=FILTERS, left=PAD + 68.0, top=top, width=110.0)
    uptime(clock, name='uptime', left=width - 80.0 - PAD, top=top, width=80.0)
    top += TOOLS + PAD
    items(todo.items, name='items', show=todo.filter, left=0.0, top=top, width=width, height=height - top)
