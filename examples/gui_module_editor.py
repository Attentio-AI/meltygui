"""A small meltygui app, cleanslate's editor demo, hosted as a @gui view, twice.

    .venv/bin/python examples/gui_module_editor.py

~/Desktop/cleanslate/editor_demo.py is an `@os_window` around `draw_editor`
with its text in a module global. gui_module runs it unmodified: its window
function becomes a view. The left one keeps the demo's own text (the editor's
source); the right one is given this window's text, and its edits come back.
Lever: APP.
"""
from pathlib import Path

from meltygui import gui_module, imgui, os_window

APP = Path.home() / 'Desktop/cleanslate/editor_demo.py'
draw_demo = gui_module(APP, width=560, height=560)
model = {'text': 'def greet(name):\n    return f"hello {name}"\n\n\nprint(greet("host"))\n'}


@os_window(name='cleanslate editor in @gui', app_id='gui-module-editor', width=1180, height=640)
def app(input_value=None):
    imgui.text(f'host text: {len(model["text"])} characters, first line: {model["text"].splitlines()[0] if model["text"] else ""}')
    draw_demo(name='own text', left=12, top=60)
    changed, text = draw_demo(model['text'], name='host text', left=600, top=60) or (False, None)
    if changed:
        model['text'] = text
