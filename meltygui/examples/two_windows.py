#!/usr/bin/env python3
"""Two peer OS windows, one with a child window that follows it.

    PYTHONPATH=<latent-descent> python melty/examples/two_windows.py
"""
import melty
from melty import glfw_window, pressed

notes = "type here\n"
log = "child window: drag me, then drag the parent\n"


@glfw_window(name='Notes')
def notes_window():
    global notes, log
    changed, new = melty.draw_text(notes, name='notes', syntax_highlight=False)
    if changed:
        notes = new
    # A child OS window of this one: onable, follows it, closes
    # when this call stops (Ctrl+L toggles it).
    if not state['hide_log']:
        changed, new = melty.draw_text(log, name='log', glfw_window=True, window_size=(520, 300),
                                       syntax_highlight=False)
        if changed:
            log = new
    if pressed('ctrl+l'):
        state['hide_log'] = not state['hide_log']


state = {'hide_log': False}


@glfw_window(name='Scratch', width=600, height=400)
def scratch_window():
    melty.draw_text("a second peer window\n", name='scratch', syntax_highlight=False)
