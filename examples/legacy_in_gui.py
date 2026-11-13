"""Legacy @render_func views nested in native @gui views.

    MELTY_RUST=1 .venv/bin/python examples/legacy_in_gui.py

The path-finding bridge (meltygui/core/rendering/gui_compat.py) turns a legacy
view called from a native body into an island: its own retained node, drawn in
window coordinates, that the legacy rules run without its native caller. So
an app can be ported from the top down: the root and the shells go native
first and stop re-running, the views inside stay legacy until their turn.

What to try: type in the text editor, hover and click the legacy controls,
open the dropdown. The native run counters stay where they are; only a legacy
edit that a native body consumes moves that body's counter by one or two.
"""
from collections import Counter

import meltygui
from meltygui import gui, imgui, os_window
from meltygui.core.core_render import render_func

RUNS = Counter()
model = {'flag': True, 'level': 0.25, 'label': 'edit me', 'choice': 'beta',
         'text': 'def greet(name):\n    return f"hello {name}"\n\n\nprint(greet("melty"))\n',
         'settings': {'enabled': True, 'name': 'melty', 'retries': 3}}


@render_func(use_cache=True, show_bg=True)
def legacy_counter(input_value: int, draw_state):
    """A plain legacy view: its return value is an edit for its native caller."""
    RUNS['legacy_counter'] += 1
    imgui.text(f'legacy counter runs={RUNS["legacy_counter"]} value={input_value}')
    if imgui.button('+ 1'):
        return True, input_value + 1
    return False, input_value


@gui(width=150, height=30)
def badge(input_value, clicks=0, draw_state=None):
    """A native view inside a legacy view inside a native view."""
    RUNS['badge'] += 1
    if imgui.button(f'native {clicks}'):
        draw_state.clicks = clicks + 1


@render_func(use_cache=True, show_bg=True)
def legacy_group(input_value, draw_state):
    RUNS['legacy_group'] += 1
    imgui.text(f'legacy group runs={RUNS["legacy_group"]}')
    badge(None, name='badge')
    return False, input_value


@gui(width=440, height=None, tint=(0.10, 0.14, 0.20, 1.0))
def controls(input_value: dict, count=0, draw_state=None):
    """Library controls (draw_any picks the legacy view for each type)."""
    RUNS['controls'] += 1
    imgui.text(f'native controls runs={RUNS["controls"]}')
    edited = False
    for key in ('flag', 'level', 'label'):
        changed, value = meltygui.draw_any(input_value[key], name=key)
        if changed:
            input_value[key], edited = value, True
    changed, value = meltygui.draw_dropdown(input_value['choice'], name='choice', width=220, show_header=False,
                                            collection=['alpha', 'beta', 'gamma', 'delta', 'epsilon'])
    if changed:
        input_value['choice'], edited = value, True
    changed, value = legacy_counter(count, name='counter', width=300)
    if changed:
        draw_state.count = value
    legacy_group(None, name='group', width=300)
    imgui.text(f'the native body sees flag={input_value["flag"]} level={input_value["level"]:.2f} '
               f'label={input_value["label"]!r} choice={input_value["choice"]}')
    return edited, input_value


@gui(width=440, height=None, tint=(0.14, 0.11, 0.18, 1.0))
def settings(input_value: dict):
    """A legacy collection: nested rows, headers, drag and drop."""
    RUNS['settings'] += 1
    imgui.text(f'native settings runs={RUNS["settings"]}')
    changed, value = meltygui.draw_any(input_value, name='settings')
    return changed, input_value


@gui(width=620, height=560, tint=(0.07, 0.08, 0.10, 1.0))
def editor(input_value: dict):
    """The legacy text editor. A keystroke runs it alone; this shell runs to
    take the new text and once more to show it. The edit stops here: returning
    it would repaint every other view of the model, and the root."""
    RUNS['editor'] += 1
    imgui.text(f'native editor shell runs={RUNS["editor"]}   {len(input_value["text"])} characters')
    changed, value = meltygui.draw_text(input_value['text'], name='source', height=500)
    if changed:
        input_value['text'] = value


def pick(label):
    return lambda: model.__setitem__('picked', label)


# What draw_menu_bar draws: {title: menu}, callables at the leaves.
MENU = {'File': {'New': pick('File > New'), 'Open': pick('File > Open'), 'Save': pick('File > Save')},
        'Edit': {'Undo': pick('Edit > Undo'), 'Redo': pick('Edit > Redo')}}


@os_window(name='legacy in gui', app_id='legacy-in-gui', width=1140, height=900)
def app(input_value=None):
    RUNS['root'] += 1
    # The library's menu bar skips the @render_func wrapper where it can
    # (fast_view.py); called from a native body it takes the wrapper.
    meltygui.draw_menu_bar(MENU, name='menu')
    imgui.text(f'native root runs={RUNS["root"]}   last menu pick: {model.get("picked", "none")}')
    controls(model, name='controls', left=12, top=70)
    settings(model['settings'], name='settings', left=12, top=500)
    editor(model, name='editor', left=480, top=70)
