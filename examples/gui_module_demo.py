"""A standalone ImGui app hosted as a @gui view, twice.

    .venv/bin/python examples/gui_module_demo.py

standalone_counter.py is an ordinary GLFW + ImGui program. gui_module runs it
in a namespace per call site: the left one owns its text, the right one is
given this window's text and its title on every frame.
"""
from pathlib import Path

from meltygui import gui_module, imgui, os_window

draw_counter = gui_module(Path(__file__).with_name('standalone_counter.py'), width=440, height=150,
                          tint=(0.10, 0.14, 0.20, 1.0))
model = {'text': 'Not hello'}


@os_window(name='gui_module', app_id='gui-module-demo', width=960, height=320)
def app(input_value=None):
    imgui.text(f'host text: {model["text"]}')
    draw_counter(name='own state', left=12, top=50)
    changed, text = draw_counter(model['text'], title='Passed', name='passed', left=480, top=50) or (False, None)
    if changed:
        model['text'] = text
