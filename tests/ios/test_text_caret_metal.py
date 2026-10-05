"""Touch/caret positions must match glyph vertices, including Retina fractions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2] / "meltygui/platforms/ios"

PROGRAM = r'''
import ctypes
from pathlib import Path
import struct
import sys
import time

sandbox, artifacts = map(Path, sys.argv[1:3])
density = int(sys.argv[3])
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
from meltygui import glfw_window, imgui
from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty
from meltygui.core.styling.fonts import Font
from meltygui.view.text_view import draw_text
state = dict(text='/private/var/mobile/Containers/Data/Application/' + '1234ABCD-'*4
             + '/Documents/Projects/MyProject/untitled.py', focus=True)

@glfw_window(name='Caret check', app_id='melty-ios-test')
@render_func(use_cache=False)
def body(input_value):
    _, state['text'], state['field'] = draw_text(
        state['text'], name='new-file', single_line=True,
        syntax_highlight=False, autocomplete=False, wrap=False,
        request_focus=state.pop('focus', False), select_all_on_focus=True,
        width=380, height=30, show_header=False, disable_scroll=True,
        return_extras=True)
    # Read the real glyph geometry sent to Metal, independently of the
    # editor's measurement or ImGui's rounded CalcTextSize result.
    imgui.push_font(Melty.font_mgr.get(Font.FONTAWESOME_MONO_19))
    dl = imgui.get_window_draw_list()
    start = dl.vtx_buffer_size
    dl.add_text(20, 120, 0xFFFFFFFF, '00')
    assert dl.vtx_buffer_size == start + 8
    vertices = ctypes.string_at(dl.vtx_buffer_data + start*20, 8*20)
    state['advance'] = struct.unpack_from('f', vertices, 4*20)[0] - struct.unpack_from('f', vertices)[0]
    imgui.pop_font()
    return False, input_value

app.run()
index = 0
def frame(events=(), height=700):
    global index
    native.frame(dict(now=index/120, presentation_time=(index+1)/120,
                      width=420, height=height, scale=density), events)
    gpu._test_flush()
    native.presented()
    index += 1

for _ in range(8): frame()
field = state['field']
assert field._stack_trace is None, repr(field._stack_trace)
assert abs(field._diff_char_w - state['advance']) < .0001, (field._diff_char_w, state['advance'])
assert field.text_h_scroll > 0
# Keyboard appearance, then taps across the scrolled filename. Use actual
# glyph advance for hit positions so a shared hit-test/caret bug cannot pass.
for _ in range(4): frame(height=400)
for tap_x in (90, 250, 150):
    time.sleep(.31)  # distinguish taps from the editor's word-select gesture
    before = state['text']
    position = round((tap_x-field.abs_left+field.text_h_scroll)/state['advance'])
    x = field.abs_left - field.text_h_scroll + position*state['advance']
    y = field.abs_top + field._diff_top_inset + field._diff_line_px/2
    frame([dict(kind='touch_begin', touch_id=index, x=x, y=y)], height=400)
    frame([dict(kind='touch_end', touch_id=index-1, x=x, y=y)], height=400)
    assert field.text_cursor_pos == position, (position, field.text_cursor_pos)
    frame([dict(kind='text', text='Z')], height=400)
    frame(height=400)
    assert state['text'] == before[:position]+'Z'+before[position:], (position, state['text'])
    assert abs(field._diff_char_w - state['advance']) < .0001
assert field._stack_trace is None, repr(field._stack_trace)
native.close()
'''


@unittest.skipUnless(os.environ.get('MELTY_METAL_TEST') == '1', 'requires compiled host Metal extension')
class TextCaretMetal(unittest.TestCase):
    def test_caret_and_touch_match_rendered_glyphs_at_desktop_and_retina_densities(self):
        artifacts = build_directory() / 'metal-test'
        for density in (1, 2, 3):
            with self.subTest(density=density), tempfile.TemporaryDirectory(prefix='melty-caret-') as sandbox:
                result = subprocess.run(
                    [sys.executable, '-c', PROGRAM, sandbox, str(artifacts), str(density)],
                    env=dict(os.environ, PYTHONPATH=str(artifacts)),
                    close_fds=False, text=True, capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
