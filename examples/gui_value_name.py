"""One counter written twice: its value as `input_value`, and under a name of its own.

    .venv/bin/python examples/gui_value_name.py

Each is shown twice: with nothing passed (the count is the view's own state) and with the app's
count passed (`Counts`, kept in Counts.pkl beside this file: try it on a copy in a folder of its
own). Press each row, press Reset, relaunch: the rows differ where `input_value` is still special.
"""
from meltygui import gui, os_window, imgui, save

# Levers: sizes in logical pixels.
PAD = 8.0
ROW = 30.0      # height of one row
WIDE = 320.0    # width of one row


@save
class Counts:
    """The app's own counts: what the `passed` rows are given."""
    position = 0
    name = 0


@gui(width=WIDE, height=ROW)
def count(input_value: int = 0, label='', draw_state=None):
    input_value = input_value or 0      # None when nothing is passed: this default is never used
    if imgui.button(f'{label}: {input_value}'):
        draw_state.input_value = input_value + 1
        return True, input_value + 1
    return False, input_value


@gui(width=WIDE, height=ROW)
def count_named(not_input_value: int = 0, label='', draw_state=None):
    if imgui.button(f'{label}: {not_input_value}'):
        draw_state.not_input_value = not_input_value + 1
        return True, not_input_value + 1
    return False, not_input_value


@gui(width=WIDE, height=ROW)
def press(input_value: str = ''):
    return imgui.button(input_value), input_value


@os_window(name='Value name', app_id='meltygui-gui-value-name', width=WIDE + 2 * PAD, height=6 * (ROW + PAD),
           use_cache=False)
def app():
    step = ROW + PAD
    count(name='own', label='input_value, nothing passed', left=PAD, top=PAD)
    changed, Counts.position = count(Counts.position, name='passed', label='input_value, passed',
                                     left=PAD, top=PAD + step)
    count_named(name='own', label='not_input_value, nothing passed', left=PAD, top=PAD + 2 * step)
    changed, Counts.name = count_named(not_input_value=Counts.name, name='passed', label='not_input_value, passed',
                                       left=PAD, top=PAD + 3 * step)
    reset, _ = press('Reset the passed counts', name='reset', left=PAD, top=PAD + 4 * step)
    if reset:
        Counts.position = Counts.name = 0

