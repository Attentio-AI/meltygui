"""Exercise the real color-chip popover using the native Metal owner."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2] / "meltygui/platforms/ios"

PROGRAM = r'''
import importlib.abc
import os
from pathlib import Path
import sys
import time

sandbox, artifacts = map(Path, sys.argv[1:])
Path.home = classmethod(lambda cls: sandbox)
attempts = []
class NoDesktop(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('OpenGL', 'glfw'):
            attempts.append(name)
            raise AssertionError('Desktop graphics import: ' + name)
sys.meta_path.insert(0, NoDesktop())
import _melty_metal as gpu
gpu._test_initialize(str(artifacts / 'Melty.metallib'), str(artifacts / 'metal-programs.json'))
class Host:
    def request_frame(self): pass
    def set_keyboard_visible(self, visible): pass
    def set_safe_zone(self, top, bottom): pass
    def get_clipboard_text(self): return ''
    def set_clipboard_text(self, text): pass

from meltygui.core.runtime import app
from meltygui.core.runtime.native_app import NativeApplication
from meltygui.core.graphics.metal_renderer import MetalRenderer
native = NativeApplication({'app_id': 'melty-ios-test'}, Host(), lambda: MetalRenderer(gpu))
app.install_native_host(native)
sys.platform = 'ios'
from meltygui import glfw_window
from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty
from meltygui.view.collection_view import draw_tuple_fast
from meltygui.view import color_view
from meltygui.state.new_core_model import ColorPickerState
captured = {}
import meltygui_imgui as imgui
wide_texture = color_view._wide_square_texture
def observe_square(*args):
    captured['square'] = tuple(imgui.get_cursor_screen_pos())
    return wide_texture(*args)
color_view._wide_square_texture = observe_square
@glfw_window(name='Picker test', app_id='melty-ios-test')
@render_func(use_cache=False)
def body(input_value, draw_state):
    captured['root'] = draw_state
    changed, value = draw_tuple_fast(captured.get('value', (0.3, 0.5, 0.8, 1.0)), draw_state,
                                    view_id='probe', x=40, y=100, size=30, view_owner=draw_state)
    captured['value'] = value
    return changed, input_value
app.run()

def frame(index, events=()):
    native.frame({'now': index / 60, 'presentation_time': (index + 1) / 60,
                  'deadline': (index + 1) / 60, 'width': 440, 'height': 850, 'scale': 2}, events)
    gpu._test_flush()
    native.presented()

for i in range(4): frame(i)
frame(4, [{'kind':'touch_begin', 'touch_id':1, 'timestamp':4/60, 'x':52, 'y':112}])
frame(5, [{'kind':'touch_end', 'touch_id':1, 'timestamp':5/60, 'x':52, 'y':112}])
for i in range(6,10): frame(i)
picker = Melty.popover_focused_ds
assert picker is not None and picker is not captured['root'], 'Color-chip touch did not open its popover'
assert picker._stack_trace is None, repr(picker._stack_trace)
assert not attempts, attempts
states = [state for state in picker.misc.values() if isinstance(state, ColorPickerState)]
assert len(states) == 1, picker.misc
for i, tab in enumerate(('srgb', 'extended', 'wide'), 10):
    states[0].tab = tab
    picker.invalidate()
    frame(i)
    assert picker._stack_trace is None, repr(picker._stack_trace)
# ImGui's actual picking square receives UIKit-style touch edges and motion.
before = captured['value']
x, y = captured['square']
frame(13, [dict(kind='touch_begin', touch_id=3, x=x+25, y=y+100)])
frame(14, [dict(kind='touch_move', touch_id=3, x=x+90, y=y+140)])
frame(15, [dict(kind='touch_end', touch_id=3, x=x+90, y=y+140)])
frame(16)
assert captured['value'] != before, 'Dragging the picker did not change the color'
assert picker._stack_trace is None, repr(picker._stack_trace)

# Verify signed/HDR pixels on the real GPU and deferred resource ownership.
import numpy as np
from meltygui import hdr_color
from meltygui.core.graphics.gl_state import GLState
state = GLState()
for factory, args, expected in (
    (wide_texture, (0.0, 16, 0.2, 4.0), hdr_color.wide_square_linear(0.0, 16, 0.2, 4.0)),
    (color_view._srgb_plus_texture, (0.0, 16, 4, 4, 4.0), hdr_color.srgb_plus_linear(0.0, 16, 4, 4, 4.0)),
):
    texture = factory(state, *args)
    assert factory(state, *args) is texture, 'Unchanged hue recreated its texture'
    pixels = np.frombuffer(gpu._test_pixels(texture.texture_id), dtype=np.float16).reshape(expected.shape)
    assert np.array_equal(pixels, expected.astype(np.float16)), 'GPU pixels differ from the HDR gradient'
    assert pixels.min() < 0 and pixels.max() > 1, 'P3/HDR channels were clipped'
    replacement = factory(state, 1/3, *args[1:])
    assert replacement.texture_id != texture.texture_id
    GLState.flush_deletes()
    assert texture.texture_id not in native.renderer._textures
    state.release()
    GLState.flush_deletes()
    assert replacement.texture_id not in native.renderer._textures

Melty.popover_focused_ds = None
frame(17)
frame(18)
assert not attempts, attempts
native.close()
'''


@unittest.skipUnless(os.environ.get('MELTY_METAL_TEST') == '1', 'requires compiled host Metal extension')
class PickerMetal(unittest.TestCase):
    def test_touch_opens_color_popover_and_all_tabs_without_desktop_graphics(self):
        artifacts = build_directory() / 'metal-test'
        with tempfile.TemporaryDirectory(prefix='melty-picker-') as sandbox:
            env = dict(os.environ, PYTHONPATH=str(artifacts))
            result = subprocess.run([sys.executable, '-c', PROGRAM, sandbox, str(artifacts)],
                                    env=env, close_fds=False, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
