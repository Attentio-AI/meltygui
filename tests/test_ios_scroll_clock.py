"""Display timing survives the full cached text/settings render path.

Metal operations are recorded without a GPU. Compare scroll positions and the
actual submitted vertex positions under steady versus jittery CPU dispatch.
"""
import json
import math
import subprocess
import sys

import pytest

from meltygui.core.input.touch_input import FLICK_DECAY, FLICK_PROJECTION


@pytest.mark.parametrize('view', ['text', 'settings'])
def test_cached_scroll_geometry_uses_presentation_time(tmp_path, view):
    program = r'''
from pathlib import Path
import hashlib
import json
import sys
import time
from types import SimpleNamespace
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
    positions = b''
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
    def mesh(self, target, vertices, *args):
        self.positions += b''.join(vertices[i:i+8] for i in range(0, len(vertices), 20))
gpu = GPU()
host = SimpleNamespace(request_frame=lambda: None, set_safe_zone=lambda *args: None,
                       set_keyboard_visible=lambda visible: None,
                       set_clipboard_text=lambda text: None, get_clipboard_text=lambda: '')
native = NativeApplication({'app_id': 'scroll-clock-test'}, host, lambda: MetalRenderer(gpu))
app.install_native_host(native)
from meltygui import glfw_window, draw_any
from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty
from meltygui.view.text_view import draw_text
state = dict(text='\n'.join(f'line {i}' for i in range(150)),
             settings={f'Setting {i}': i for i in range(60)})
@glfw_window(name='Scroll clock test', app_id='scroll-clock-test')
@render_func(use_cache=False)
def body(input_value):
    options = dict(name='pane', width=380, height=360, use_cache=True,
                   show_header=False, with_header=None, with_footer=None,
                   is_tree=False, show_widgets=False, return_extras=True)
    if sys.argv[2] == 'text':
        result = draw_text(state['text'], editable=True, autocomplete=False,
                           syntax_highlight=False, show_file_header=False,
                           show_jump_bar=False, **options)
    else:
        result = draw_any(state['settings'], **options)
    state['ds'] = result[2]
    return False, input_value
app.run()
clock = [1000.]
time.perf_counter = time.monotonic = lambda: clock[0]
index = 0
def frame(events=(), delay=0):
    global index
    index += 1
    tick = 1000 + index / 120
    clock[0] = tick + delay
    gpu.positions = b''
    again = native.frame(dict(width=440, height=600, scale=2, now=tick,
                             presentation_time=tick + 2 / 120), list(events))
    native.presented()
    assert state['ds']._stack_trace is None, state['ds']._stack_trace
    if index >= 11:  # Release and subsequent momentum frames, without contacts.
        assert again, 'momentum lost its next frame request'
        assert Melty._last_input_time == clock[0], 'momentum was treated as idle'
for _ in range(8): frame()
ds = state['ds']
x, y = ds.abs_left + 120, ds.abs_top + 200
for kind, movement in [('begin', 0), ('move', 20), ('end', 24)]:
    frame([dict(kind='touch_' + kind, touch_id=1, x=x, y=y-movement,
                timestamp=1000+(index+1)/120, velocity=(0, -240))])
positions, geometry = [ds.scroll_offset[1]], []
for i in range(90):  # Cross the parser's half-second input-quiet threshold.
    frame(delay=.005 if sys.argv[3] == 'jitter' and i % 2 else 0)
    positions.append(ds.scroll_offset[1])
    geometry.append(hashlib.sha256(gpu.positions).hexdigest())
assert native.cache.enabled
print('RESULT ' + json.dumps(dict(positions=positions, geometry=geometry)))
'''
    outputs = []
    for timing in ('steady', 'jitter'):
        result = subprocess.run(
            [sys.executable, '-c', program, str(tmp_path / timing), view, timing],
            capture_output=True, text=True, close_fds=False, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(json.loads(next(line[7:] for line in result.stdout.splitlines()
                                       if line.startswith('RESULT '))))
    expected = [24 + 240 * FLICK_PROJECTION * (1 - math.exp(-FLICK_DECAY * i / 120))
                for i in range(91)]
    for output in outputs:
        assert output['positions'] == pytest.approx(expected)
    assert outputs[0]['geometry'] == outputs[1]['geometry']
