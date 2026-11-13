"""Entry Points, a multi-file GLFW + ImGui app, hosted as a @gui view.

    .venv/bin/python examples/gui_module_entry_points.py

~/Desktop/entry-points/main.py is run unmodified by gui_module, with the files
beside it (its renderer, scanner, process runner). It is the same app with the
same state: its tasks, favorites and running processes are the ones the
standalone dock shows, and a click on a repo runs its task. Lever: APP.
"""
from pathlib import Path

from meltygui import gui_module, imgui, os_window

APP = Path.home() / 'Desktop/entry-points/main.py'
draw_entry_points = gui_module(APP, width=420, height=760)


@os_window(name='Entry Points in @gui', app_id='gui-module-entry-points', width=460, height=830)
def app(input_value=None):
    imgui.text(f'hosting {APP}')
    draw_entry_points(name='entry points', left=20, top=50)
