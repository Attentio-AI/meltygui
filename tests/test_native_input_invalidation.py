"""Real cached render traversal with recorded Metal calls (no GPU required)."""
import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize('device', ['touch', 'mouse'])
@pytest.mark.parametrize('press_listener', [True, False])
@pytest.mark.parametrize('after_scroll', [False, True])
def test_contact_wakes_cached_receiver_without_repainting_sibling_rows(
        tmp_path, device, press_listener, after_scroll):
    program = r'''
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import time
import meltygui
from meltygui.platforms.ios.compile_shaders import generate

sandbox = Path(sys.argv[1])
Path.home = classmethod(lambda cls: sandbox)
generate(Path(meltygui.__file__).parent, sandbox / 'shaders')
manifest = (sandbox / 'shaders/metal-programs.json').read_text()
sys.platform = 'ios'
from meltygui.core.runtime import app
from meltygui.core.runtime.native_app import NativeApplication
from meltygui.core.graphics.metal_renderer import MetalRenderer

class GPU:
    next_texture = 0
    def program_manifest(self): return manifest
    def create_texture(self, *args):
        self.next_texture += 1
        return self.next_texture
    def delete_texture(self, *args): pass
    def clear_texture(self, *args): pass
    def upload_texture(self, *args): pass
    def quad(self, *args): pass
    def shape(self, *args): pass
    def set_scene(self, *args): pass
    def mesh(self, *args): pass

host = SimpleNamespace(request_frame=lambda: None, set_safe_zone=lambda *args: None,
    set_keyboard_visible=lambda visible: None, set_clipboard_text=lambda text: None,
    get_clipboard_text=lambda: '')
native = NativeApplication({'app_id': 'input-cache-test'}, host, lambda: MetalRenderer(GPU()))
app.install_native_host(native)
from meltygui import glfw_window, render_func, imgui
from meltygui.core.melty import Melty

runs, presses, clicks = Counter(), Counter(), Counter()
rows = {}

press_listener, after_scroll = (arg == 'True' for arg in sys.argv[3:5])
def render_row(input_value, draw_state, left_mouse_down, left_mouse_clicked):
    runs[input_value] += 1
    presses[input_value] += left_mouse_down is not None
    clicks[input_value] += left_mouse_clicked is not None
    rows[input_value] = draw_state
    imgui.text('Row ' + str(input_value))
    return False, input_value

if press_listener:
    @render_func(use_cache=True)
    def row(input_value: int, draw_state, left_mouse_down=None, left_mouse_clicked=None):
        return render_row(input_value, draw_state, left_mouse_down, left_mouse_clicked)
else:
    @render_func(use_cache=True)
    def row(input_value: int, draw_state, left_mouse_clicked=None):
        return render_row(input_value, draw_state, None, left_mouse_clicked)

@render_func(use_cache=True)
def listing(input_value: list):
    for value in input_value:
        row(value, name='Row ' + str(value), width=340, height=26, use_cache=True,
            show_header=False, with_header=None, with_footer=None,
            is_tree=False, show_widgets=False)
    return False, input_value

values = list(range(60))
@glfw_window(name='Input cache test', app_id='input-cache-test')
@render_func(use_cache=False)
def body(input_value):
    listing(values, name='list', width=380, height=360, use_cache=True,
        show_header=False, with_header=None, with_footer=None,
        is_tree=False, show_widgets=False)
    return False, input_value

app.run()
def frame(events=()):
    now = time.perf_counter()
    native.frame(dict(width=440, height=600, scale=2, now=now,
        presentation_time=now+1/120), list(events))
    native.presented()
    assert all(ds._stack_trace is None for ds in rows.values())

for _ in range(20): frame()
assert native.cache.enabled and len(rows) > 8
runs.clear()
target = rows[7]
x, y = target.abs_left+40, target.abs_top+10
def contact(kind):
    return dict(kind='touch_'+kind, touch_id=1, x=x, y=y, timestamp=time.perf_counter())

if after_scroll:
    frame([contact('begin')])
    frame()
    y -= 50
    frame([contact('move')])
    frame([dict(contact('end'), velocity=(0, -200))])
    while native.input._touch_pending:
        frame()
    assert Melty.on_scroll and native.input.handler._touch_input.velocity
    x, y = target.abs_left+40, target.abs_top+10
    runs.clear()
    assert not any(clicks.values())

if sys.argv[2] == 'touch':
    frame([contact('begin')])
    frame()  # Present touchdown before pressing.
else:
    native.input._pointer_cancelled = False
    native.input._move(dict(x=x, y=y, _time=time.perf_counter()))
    native.window.hovered = Melty._pointer_inside = True
    frame()
    if after_scroll:
        # Positioning a mouse permits another coast step before its press;
        # newly revealed rows from that step are not click invalidations.
        runs.clear()
    native.window.buttons[0] = 1
    native.input.handler.feed_down('left_mouse', x, y)
    frame()
for _ in range(0 if after_scroll else 4): frame()
if sys.argv[2] == 'touch':
    frame([contact('end')])
else:
    native.window.buttons[0] = 0
    native.input.handler.feed_up('left_mouse', x, y)
    frame()
for _ in range(4): frame()
assert presses[7] == int(press_listener) and clicks[7] == 1, (presses, clicks)
assert set(runs) == {7}, runs  # Focus observers must not cascade through the list.
print('RESULT ' + json.dumps(dict(runs=dict(runs), presses=dict(presses), clicks=dict(clicks))))
'''
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path), device,
                             str(press_listener), str(after_scroll)],
                            close_fds=False, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(next(line[7:] for line in result.stdout.splitlines() if line.startswith('RESULT ')))
    assert data['runs']['7'] > 0
    assert data['presses']['7'] == int(press_listener)
    assert data['clicks']['7'] == 1
