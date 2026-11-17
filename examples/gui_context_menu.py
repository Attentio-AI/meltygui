"""Context menus: `@context_menu({label: function})` above `@gui`.

    .venv/bin/python examples/gui_context_menu.py

Right click a note. Its own options come first, then the list's, then the window's, with a line
between each: a menu is made of the options of every view around the pointer, innermost first.

An option that is a dict of options is a submenu (`Move` on a note): it opens beside its row, by
the pointer or Right, and nests to any depth.

An option's function is given the parameters of its view that it names. `delete(input_value, items)`
gets the note's key and the dict the list passed it; `add(input_value)` gets the list's dict. One
that takes nothing is just called. After a pick the view and the ones around it run again, so an
option may change the model in place.

The notes are `Notes` (Notes.pkl beside this file: try it on a copy in a folder of its own).
"""
from meltygui import context_menu, gui, os_window, imgui, save
from meltygui.hdr_color import pack_color

# Levers: sizes in logical pixels, colours as r, g, b.
PAD = 8.0
ROW = 26.0                              # height of one note
HEAD = 30.0                             # the hint above the list
FACE = pack_color(0.17, 0.20, 0.25)     # a note
HOT = pack_color(0.24, 0.29, 0.37)      # the note under the pointer
TEXT = pack_color(0.92, 0.94, 0.96)


@save
class Notes:
    """The app's notes, by key."""
    items = {str(at): f'Note {at}' for at in range(1, 13)}


def delete(input_value, items):
    del items[input_value]


def upper(input_value, items):
    items[input_value] = items[input_value].upper()


def first(input_value, items):
    rest = dict(items)
    items.clear()
    items[input_value] = rest.pop(input_value)
    items.update(rest)


def last(input_value, items):
    items[input_value] = items.pop(input_value)


def add(input_value):
    key = str(max((int(key) for key in input_value), default=0) + 1)
    input_value[key] = f'Note {key}'


def clear(input_value):
    input_value.clear()


@context_menu({'Delete': delete, 'Upper case': upper, 'Move': {'To the top': first, 'To the bottom': last}})
@gui(height=ROW)
def note(input_value: str, items: dict = None, width=0.0, height=0.0, event=None):
    """One note: `input_value` is its key in `items`."""
    face = FACE
    if event.is_hovered():
        face = HOT
    draw_list = imgui.get_window_draw_list()
    draw_list.add_rect_filled(0.0, 1.0, width, height - 1.0, face, 4.0)
    draw_list.add_text(PAD, (height - imgui.get_text_line_height()) * 0.5, TEXT, items[input_value])


@context_menu({'Add a note': add, 'Remove all': clear})
@gui(scroll=True)
def notes(input_value: dict, width=0.0, height=0.0):
    """The notes, one view each, so that each has its own menu."""
    for at, key in enumerate(input_value):
        note(key, key=key, items=input_value, left=0.0, top=at * ROW, width=width, height=ROW)


@context_menu({'Print the notes': lambda: print(Notes.items)})
@os_window(name='Context menus', app_id='meltygui-gui-context-menu', width=360, height=330, min_width=240,
           min_height=160, use_cache=False)
def app(width=None, height=None):
    imgui.set_cursor_pos((PAD, imgui.get_cursor_pos_y() + PAD))
    imgui.text('Right click a note, the list, or up here')
    notes(Notes.items, name='notes', left=PAD, top=HEAD, width=width - 2.0 * PAD, height=height - HEAD - PAD)

