"""Dropdown keyboard policy through the actual native renderer and text view."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2] / "meltygui/platforms/ios"

PROGRAM = r'''
from pathlib import Path
import sys
sandbox, artifacts = map(Path, sys.argv[1:3])
touch = sys.argv[3] == 'touch'
Path.home = classmethod(lambda cls: sandbox)
import _melty_metal as gpu
gpu._test_initialize(str(artifacts / 'Melty.metallib'), str(artifacts / 'metal-programs.json'))
class Host:
    def __init__(self): self.keyboard = []
    def request_frame(self): pass
    def set_keyboard_visible(self, visible): self.keyboard.append(visible)
    def set_safe_zone(self, top, bottom): pass
    def get_clipboard_text(self): return ''
    def set_clipboard_text(self, text): pass

from meltygui.core.runtime import app
from meltygui.core.runtime.native_app import NativeApplication
from meltygui.core.graphics.metal_renderer import MetalRenderer
host = Host()
native = NativeApplication({'app_id': 'melty-ios-test'}, host, lambda: MetalRenderer(gpu))
app.install_native_host(native)
sys.platform = 'ios'
import meltygui_imgui as imgui
from meltygui import glfw_window
from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty
from meltygui.core.rendering.core_decoration import Core
from meltygui.view.dropdown_view import draw_dropdown
from meltygui.view.header_view import flat_button
captured = {}
options = {f'Item {i}': i for i in range(8)}
@glfw_window(name='Dropdown test', app_id='melty-ios-test')
@render_func(use_cache=False)
def body(input_value, draw_state):
    imgui.set_cursor_screen_pos((40, 100))
    if captured.get('mode') == 'button':
        captured['pressed'] = flat_button('Replacement', draw_state, 'replacement',
            width=220, height=25, event='left_mouse_down') or captured.get('pressed', False)
    else:
        _, _, captured['dropdown'] = draw_dropdown(
            0, collection=options, name='menu', width=220,
            show_header=False, with_header=None, return_extras=True)
    return False, input_value
app.run()
Melty.is_touch = touch
assert Core.melty.is_touch is touch

def frame(index, events=()):
    native.frame(dict(now=index/120, presentation_time=(index+1)/120,
                      width=440, height=850, scale=2), events)
    gpu._test_flush()
    native.presented()

for i in range(8): frame(i)
ds = captured['dropdown']
rect, = [action[3] for action in ds._body_actions[1] if '_dd_trigger' in str(action[0])]
x, y = ds.abs_left + (rect[0]+rect[2])/2, ds.abs_top + (rect[1]+rect[3])/2
frame(8, [dict(kind='touch_begin', touch_id=1, x=x, y=y)])
frame(9, [dict(kind='touch_end', touch_id=1, x=x, y=y)])
for i in range(10,20): frame(i)
assert Melty.popover_focused_ds is ds, 'Dropdown did not open'
state = ds.misc['drop_down_state']
assert state._search_box_tile is not None
if touch:
    assert Melty.text_focused_ds is None, 'Opening a mobile menu claimed text focus'
    assert True not in host.keyboard, host.keyboard
    assert state._focus_search == 0
    box, = [view for view in Melty._bvh_id_to_ds.values() if view._tile_id == state._search_box_tile]
    l, t, r, b = box.abs_clamped_rect
    x, y = (l+r)/2, (t+b)/2
    frame(20, [dict(kind='touch_begin', touch_id=2, x=x, y=y)])
    frame(21, [dict(kind='touch_end', touch_id=2, x=x, y=y)])
    frame(22)
    assert Melty.text_focused_ds is box, 'Tapping the search field did not focus it'
else:
    assert Melty.text_focused_ds is not None, 'Desktop dropdown lost autofocus'
assert host.keyboard[-1] is True, host.keyboard
frame(23, [dict(kind='text', text='Item 6')])
frame(24)
assert state.search_query == 'Item 6', state.search_query
assert ds._stack_trace is None, repr(ds._stack_trace)
Melty.text_focused_ds = Melty.popover_focused_ds = None
captured['mode'] = 'button'
for i in range(25, 31): frame(i)
frame(31, [dict(kind='touch_begin', touch_id=3, x=150, y=112)])
frame(32, [dict(kind='touch_end', touch_id=3, x=150, y=112)])
assert captured['pressed'], 'An old hidden menu stole its replacement control tap'
native.close()
'''


@unittest.skipUnless(os.environ.get('MELTY_METAL_TEST') == '1', 'requires compiled host Metal extension')
class DropdownMetal(unittest.TestCase):
    def test_touch_requires_search_tap_and_desktop_retains_autofocus(self):
        artifacts = build_directory() / 'metal-test'
        for mode in ('touch', 'desktop'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='melty-dropdown-') as sandbox:
                result = subprocess.run([sys.executable, '-c', PROGRAM, sandbox, str(artifacts), mode],
                                        env=dict(os.environ, PYTHONPATH=str(artifacts)),
                                        close_fds=False, text=True, capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
