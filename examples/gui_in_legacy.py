"""Native @gui views nested in legacy @render_func views.

    MELTY_RUST=1 .venv/bin/python examples/gui_in_legacy.py

The path-finding bridge (meltygui/core/rendering/gui_compat.py) serves every
cached legacy view from a retained Rust node and hosts @gui views in the same
graph, so a legacy app can be ported one view at a time. Without MELTY_RUST a
@gui call outside an @os_window raises.

What to try: the run counters only move for the view under the pointer; an
edit returned by a native view reaches its legacy caller; the dropdown's
popover covers a native button without clicking it.
"""
from collections import Counter

import meltygui
from meltygui import glfw_window, gui, imgui
from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty

RUNS = Counter()
model = {'count': 0, 'text': 'edit me', 'level': 0.25, 'flag': True, 'choice': 'beta'}


@gui(width=260, height=64, tint=(0.10, 0.16, 0.24, 1.0))
def counter(input_value, clicks=0, draw_state=None):
    """State lives on the native draw_state; the tint is the view's background."""
    RUNS[input_value] += 1
    imgui.text(f'{input_value}: runs={RUNS[input_value]}')
    if imgui.button(f'clicked {clicks}'):
        draw_state.clicks = clicks + 1


@gui(width=300, height=None)
def stepper(input_value: int):
    """Returns (changed, value): the legacy caller receives it from the call."""
    RUNS['stepper'] += 1
    imgui.text(f'native value {input_value} runs={RUNS["stepper"]}')
    if imgui.button('+ 1'):
        return True, input_value + 1
    imgui.same_line()
    if imgui.button('- 1'):
        return True, input_value - 1


@gui(width=320, height=70)
def fields(input_value: dict):
    changed, input_value['text'] = imgui.input_text('text', input_value['text'], 64)
    moved, input_value['level'] = imgui.slider_float('level', input_value['level'], 0.0, 1.0)
    return changed or moved, input_value


@gui(width=290, height=None, tint=(0.16, 0.10, 0.20, 1.0))
def nest(input_value):
    """A native view declaring native views: plain @gui, Rust runs each alone."""
    RUNS['nest'] += 1
    imgui.text(f'native parent runs={RUNS["nest"]}')
    counter('inner A', name='a')
    counter('inner B', name='b')


@gui(use_cache=False)
def immediate(input_value):
    """use_cache=False: drawn with its caller, every time the caller runs."""
    imgui.text(f'immediate native, frame {Melty.frame_count}')


@render_func(use_cache=True, show_bg=True, shadow=True)
def cached_panel(input_value, draw_state):
    RUNS['cached_panel'] += 1
    imgui.text(f'cached legacy panel runs={RUNS["cached_panel"]}')
    counter('in cached legacy', name='c')
    changed, value = stepper(model['count'], name='step')
    if changed:
        model['count'] = value
    imgui.text(f'legacy caller sees count={model["count"]}')
    changed, value = meltygui.draw_any(model['flag'], name='legacy flag')
    if changed:
        model['flag'] = value
    return False, input_value


@render_func(use_cache=False)
def uncached_panel(input_value, draw_state):
    RUNS['uncached_panel'] += 1
    imgui.text(f'uncached legacy panel runs={RUNS["uncached_panel"]}')
    counter('in uncached legacy', name='u')
    fields(model, name='fields')
    imgui.text(f'legacy caller sees text={model["text"]!r} level={model["level"]:.2f}')
    nest(None, name='nest')
    immediate(None)
    return False, input_value


@glfw_window(name='gui in legacy', app_id='gui-in-legacy', width=940, height=900)
@render_func(use_cache=True)
def app(input_value, draw_state):
    RUNS['root'] += 1
    imgui.text(f'legacy root runs={RUNS["root"]} frame {Melty.frame_count}')
    changed, value = meltygui.draw_dropdown(model['choice'], collection=['alpha', 'beta', 'gamma', 'delta', 'epsilon'],
                                            name='choice', width=220, show_header=False)
    if changed:
        model['choice'] = value
    counter('at root', name='root')
    cached_panel(1, name='cached', width=440, height=250)
    uncached_panel(2, name='uncached', width=440, height=330)

