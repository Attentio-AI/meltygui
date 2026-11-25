"""`draw_function`, and views that build the parameters nobody passes them.

    .venv/bin/python examples/gui_function.py

What to look at:
- `app`: `draw_function(add)` shows the parameters of `add` and a button that calls it.
- `counter`: `count: int` has no default and `app` passes none, so its type builds the 0 it
  starts at. It is the view's state from then on, saved with the session.
- `shot`: `camera: Camera` needs arguments. Until its button has built one, the view shows
  `draw_function(Camera)` in its own place.
"""
from meltygui import gui, os_window, imgui
from meltygui.core.input import mouse_cursor
from meltygui.rust import draw_function

# Levers: sizes in logical pixels, colours.
PAD = 8.0
ROW = 26.0                              # height of a line of text
FORM = 86.0                             # room for the two parameters of `add` and its button
FACE = (0.20, 0.24, 0.30)     # the counter
HOT = (0.28, 0.36, 0.48)      # the counter under the pointer
TEXT = (0.92, 0.94, 0.96)
DIM = (0.60, 0.64, 0.70)
NOTES = []                              # what `add` appends to


def add(title: str = 'Buy milk', times: int = 1):
    """Any function: its parameters are the form."""
    for _ in range(times):
        NOTES.append(title)


class Camera:
    """A type that cannot be built without arguments."""
    def __init__(self, zoom: float, name: str = 'main'):
        self.zoom = zoom
        self.name = name


@gui(listen=())
def notes(input_value: list, width=0.0, height=0.0):
    draw_list = imgui.get_window_draw_list()
    draw_list.push_clip_rect(0.0, 0.0, width, height, True)
    draw_list.add_text(0.0, 4.0, DIM, f'{len(input_value)} notes: {", ".join(input_value[-4:])}')
    draw_list.pop_clip_rect()


@gui(cursor=mouse_cursor.HAND, height=ROW)
def counter(count: int, width=0.0, height=0.0, event=None):
    """Nobody passes `count`: `int()` built it, and a press writes the next one to the view's state."""
    pressed = event.left_mouse_down()
    face = HOT if event.is_hovered() else FACE
    if pressed:
        count += 1
    draw_list = imgui.get_window_draw_list()
    draw_list.add_rect_filled(0.0, 0.0, width, height, face, 5.0)
    draw_list.add_text(PAD, (height - imgui.get_text_line_height()) * 0.5, TEXT, f'Pressed {count} times')
    return pressed, count


@gui(listen=())
def shot(camera: Camera, width=0.0, height=0.0):
    """Runs once a `Camera` is there: passed by the caller, or built with the form in this view's place."""
    imgui.get_window_draw_list().add_text(0.0, 4.0, TEXT, f'Camera {camera.name} at zoom {camera.zoom:g}')


@os_window(name='Function', app_id='meltygui-function', width=420, height=360, min_width=300, min_height=240,
           use_cache=False)
def app(width=None, height=None, cache=None):
    wide = width - 2.0 * PAD
    top = PAD
    ran, _ = draw_function(add, name='add', left=PAD, top=top, width=wide)
    if ran:
        cache.invalidate(NOTES)         # changed in place, outside the view that shows it
    top += FORM
    notes(NOTES, name='notes', left=PAD, top=top, width=wide, height=ROW)
    top += ROW + PAD
    counter(name='counter', left=PAD, top=top, width=180.0)
    top += ROW + PAD
    shot(name='shot', left=PAD, top=top, width=wide, height=height - top)
