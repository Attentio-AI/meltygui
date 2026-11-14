"""Run with .venv/bin/python examples/gui_scroll_lab.py after building the native prototype.

Wheel over either panel. The right panel nests eight independent viewports.
Hide/show preserves state; row bodies run only for new tiles and real input.
"""
from collections import Counter

from meltygui import gui, os_window, imgui

CALLS = Counter()
ROW_COUNT = 10_000
DEPTH = 8


@gui(width=260, height=130, tint=(.055, .075, .095, 1.))
def detail(input_value, count=0, draw_state=None):
    CALLS['detail'] += 1
    imgui.text('Drag the title, then hide/show the panel.')
    if imgui.button('Increment detail'):
        draw_state.count = count + 1
    imgui.text(f'Count: {draw_state.count}')


@gui(width=480, height=28, tint=(.055, .075, .095, 1.))
def row(input_value, selected=False, draw_state=None):
    CALLS['rows'] += 1
    changed, selected = imgui.checkbox(f'Row {input_value:05d}', selected)
    if changed:
        draw_state.selected = selected


@gui(width=500, height=560, scroll=True, tint=(.025, .035, .045, 1.))
def rows(input_value, width=None):
    CALLS['lists'] += 1
    for index in range(input_value):
        row(index, key=index, width=width-16, left=0, top=index*28)


@gui(width=560, height=560, scroll=True, tint=(.035, .045, .055, 1.))
def nested(input_value, width=None, height=None):
    CALLS['containers'] += 1
    if input_value:
        nested(input_value-1, width=width-8, height=height-12, left=4, top=28)
    else:
        rows(ROW_COUNT, width=width, height=height)
        detail(None, name='detail', melty_window=True,
               initial={'left': 80, 'top': 80, 'width': 260, 'height': 130})


@os_window(name='Relative geometry and scrolling', app_id='meltygui-scroll-lab',
           width=1200, height=760, use_cache=False, live=True)
def app(show_nested=True, draw_state=None):
    imgui.text('Cached scroll viewports: 10,000 rows on each side')
    changed, show_nested = imgui.checkbox('Show nested panel', show_nested)
    if changed:
        draw_state.show_nested = show_nested
    imgui.text(f"Body calls: rows {CALLS['rows']}, lists {CALLS['lists']}, containers {CALLS['containers']}")
    rows(ROW_COUNT, name='flat', left=16, top=80)
    if show_nested:
        nested(DEPTH, name='nested', left=570, top=80)
