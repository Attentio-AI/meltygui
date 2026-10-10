"""Cached ImGui widgets hosted by the throwaway Rust @gui implementation.

Run with .venv/bin/python examples/gui_imgui_widgets.py after building the
prototype. Text/input state stays local to this view through normal injection.
"""
from meltygui import os_window
import imgui
from meltygui.core.conversion.dict_conversion import DictConversion


class Controls(DictConversion):
    def __init__(self):
        super().__init__()
        self.clicks = 0
        self.enabled = False
        self.amount = .25
        self.text = ''


@os_window(name='ImGui widgets in @gui', width=700, height=420,
           app_id='meltygui-imgui-prototype')
def app(state: Controls = None):
    imgui.text('hello')
    if imgui.button('Click me', 120, 30):
        state.clicks += 1
    imgui.text(f'Clicks: {state.clicks}')
    _, state.enabled = imgui.checkbox('Enabled', state.enabled)
    _, state.amount = imgui.slider_float('Amount', state.amount, 0., 1.)
    _, state.text = imgui.input_text('Text', state.text, 256)
    imgui.text(f'You typed: {state.text}')
