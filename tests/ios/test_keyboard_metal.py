"""Real text-view caret scrolling when UIKit reduces the native viewport."""
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

sandbox, artifacts = map(Path, sys.argv[1:])
Path.home = classmethod(lambda cls: sandbox)
import _melty_metal as gpu
gpu._test_initialize(str(artifacts / 'Melty.metallib'), str(artifacts / 'metal-programs.json'))
class Host:
    def request_frame(self): pass
    def set_keyboard_visible(self, visible): pass
    def set_safe_zone(self, inset): pass
    def get_clipboard_text(self): return ''
    def set_clipboard_text(self, text): pass

from meltygui.core.runtime import app
from meltygui.core.runtime.native_app import NativeApplication
from meltygui.core.graphics.metal_renderer import MetalRenderer
native = NativeApplication({'app_id': 'melty-ios-test'}, Host(), lambda: MetalRenderer(gpu))
app.install_native_host(native)
sys.platform = 'ios'
import meltygui_imgui as imgui
from meltygui import glfw_window
from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty
from meltygui.view.text_view import draw_text
captured = {}
text = ''.join(f'line {i:03d}\n' for i in range(100))
@glfw_window(name='Keyboard test', app_id='melty-ios-test')
@render_func(use_cache=False)
def body(input_value, draw_state):
    changed, value, ds = draw_text(
        text, name='text', width=400, height=imgui.get_io().display_size.y-80,
        show_header=False, with_header=None, with_footer=None, is_tree=False,
        show_widgets=False, autocomplete=False,
        show_file_header=False, show_jump_bar=False, return_extras=True)
    captured['text'] = ds
    return False, input_value
app.run()

def frame(index, height=850):
    native.frame(dict(now=index/60, presentation_time=(index+1)/60,
                      width=440, height=height, scale=3), [])
    gpu._test_flush()
    native.presented()

for i in range(8): frame(i)
ds = captured['text']
assert ds._stack_trace is None, repr(ds._stack_trace)
Melty.text_focused_ds = ds
# The caret stays on the same line throughout the keyboard transition.
position = text.index('line 025')
ds.text_cursor_pos = ds.text_prev_cursor_pos = position
ds.scroll_offset = (0, 0)
ds.invalidate()
frame(8)
before = ds.scroll_offset[1]
index = 9
# Follow the intermediate presentation sizes, including fractional UI points,
# instead of jumping straight to UIKit's final model bounds.
for height in (820, 775+2/3, 701, 640+1/3, 578+2/3, 511+1/3, 440):
    frame(index, height)
    index += 1
    assert ds.text_cursor_pos == position
    line_top = ds.abs_top + ds._diff_top_inset + 25*ds._diff_line_px - ds.scroll_offset[1]
    assert ds.abs_top <= line_top and line_top + ds._diff_line_px <= height, (height, line_top, ds.scroll_offset)
assert ds.text_cursor_pos == position
assert ds.scroll_offset[1] > before, ('Stationary caret stayed under keyboard', before, ds.scroll_offset)
line_top = ds.abs_top + ds._diff_top_inset + 25*ds._diff_line_px - ds.scroll_offset[1]
assert ds.abs_top <= line_top and line_top + ds._diff_line_px <= 440, (line_top, ds.scroll_offset)
assert 'reveal_text_cursor' not in ds.misc
# A subsequent manual pan is not pulled back to the stationary caret.
ds.scroll_offset = (0, 0)
ds.invalidate()
frame(index, 440)
frame(index+1, 440)
assert ds.scroll_offset[1] == 0, ds.scroll_offset
frame(index+2, 850)
assert ds.scroll_offset[1] == 0, ds.scroll_offset
assert ds._stack_trace is None, repr(ds._stack_trace)
native.close()
'''


@unittest.skipUnless(os.environ.get('MELTY_METAL_TEST') == '1', 'requires compiled host Metal extension')
class KeyboardMetal(unittest.TestCase):
    def test_viewport_shrink_reveals_stationary_caret_once(self):
        artifacts = build_directory() / 'metal-test'
        with tempfile.TemporaryDirectory(prefix='melty-keyboard-') as sandbox:
            result = subprocess.run(
                [sys.executable, '-c', PROGRAM, sandbox, str(artifacts)],
                env=dict(os.environ, PYTHONPATH=str(artifacts)),
                close_fds=False, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
