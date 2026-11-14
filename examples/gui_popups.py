"""Run with .venv/bin/python examples/gui_popups.py after building the native prototype.

Temporary popups: a view called with `popup=<open event>` (core/input/focus.py).
Click the swatch for a colour picker, the button for a list. A click elsewhere
or Escape closes either, a click on its anchor toggles it. The list takes the
keyboard (arrows, Enter) and gives it back when it closes; the picker takes
none. A click in the note field closes an open popup and types there. No view
here keeps an open flag.
"""
from meltygui import gui, os_window, imgui
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.hdr_color import pack_color
from meltygui.view.editor_view import draw_editor
import meltygui.core.windowing.window_constants as glfw

# Levers: a list row's height, the popups' colours.
ROW = 24.
BACK, PICK, TEXT = pack_color(.10, .12, .15), pack_color(.20, .36, .52), pack_color(.92, .94, .96)
MODES = ('Draw', 'Erase', 'Fill', 'Select')     # one object: a new tuple each run would run the list again


class Settings(DictConversion):
    def __init__(self):
        super().__init__()
        self.color = [.20, .55, .80]
        self.mode = 'Draw'
        self.note = 'type here, then open the picker'


@gui(width=230, height=84)
def picker(input_value, width=0., height=0.):
    """Channel sliders beside a preview of the colour."""
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled(0, 0, width, height, BACK, 4.)
    dl.add_rect_filled(width - 34, 6, width - 6, height - 6, pack_color(*input_value[:3]), 3.)
    value, changed = list(input_value), False
    imgui.push_item_width(width - 70)
    for at, label in enumerate('rgb'):
        imgui.set_cursor_pos((6, 6 + at * 24))
        moved, value[at] = imgui.slider_float(label, value[at], 0., 1.)
        changed |= moved
    imgui.pop_item_width()
    return changed, value


@gui(width=36, height=22)
def swatch(input_value, width=0., height=0.):
    """A colour chip: a click opens the picker under it."""
    pressed = imgui.invisible_button('##swatch', width, height)
    imgui.get_window_draw_list().add_rect_filled(0, 0, width, height, pack_color(*input_value[:3]), 4.)
    return picker(input_value, name='picker', popup=pressed, left=0., top=height + 2)


@gui(no_save=('index',))
def options(input_value, choices=(), index=None, width=0.0, height=0.0, draw_state=None, cache=None, event=None):
    """The list under a dropdown. Returns (True, choice) when one is picked."""
    if index is None:       # just opened (`index` is not saved): start on the current choice
        index = choices.index(input_value) if input_value in choices else 0
    picked = False
    # The arrows move the selection and Enter picks it: asking for the keys is what gives the open list the keyboard.
    for key, mods in event.keys():
        if key == glfw.KEY_DOWN:
            index = (index + 1) % len(choices)
        elif key == glfw.KEY_UP:
            index = (index - 1) % len(choices)
        elif key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
            picked = True
    # The pointer selects the row it moves over; a press picks it.
    pressed = event.left_mouse_down()
    if pressed or event.mouse_moved():
        index = min(max(int(event.y // ROW), 0), len(choices) - 1)
    if pressed:
        picked = True
    draw_state.index = index
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled(0.0, 0.0, width, height, BACK, 4.0)
    for at, choice in enumerate(choices):
        if at == index:
            dl.add_rect_filled(0.0, at * ROW, width, (at + 1) * ROW, PICK, 3.0)
        dl.add_text(8.0, at * ROW + 4.0, TEXT, str(choice))
    if picked:
        cache.focus.close()     # the keyboard returns to whoever had it
        return True, choices[index]
    return False, input_value


@gui(width=160, height=24)
def dropdown(input_value, choices=(), width=0., height=0.):
    """The current choice: a click opens the list under it."""
    pressed = imgui.button(str(input_value), width, height)
    return options(input_value, name='options', popup=pressed, choices=choices,
                   left=0., top=height + 2, width=width, height=len(choices) * ROW)


@gui(listen=(), width=70, height=22)
def caption(input_value: str):
    imgui.text(input_value)


@os_window(name='Temporary popups', app_id='meltygui-popups', width=560, height=300, use_cache=False)
def app(state: Settings = None):
    for at, label in enumerate(('Colour', 'Mode', 'Note')):
        caption(label, key=label, left=16, top=14 + at * 40)
    changed, state.color = swatch(state.color, name='color', left=90, top=10)
    changed, state.mode = dropdown(state.mode, name='mode', choices=MODES, left=90, top=50)
    changed, state.note = draw_editor(state.note, name='note', single_line=True, language=None,
                                      left=90, top=90, width=380, height=26)
    caption(f'{state.mode}  ({", ".join(f"{part:.2f}" for part in state.color)})', name='status', left=16, top=140, width=400)
