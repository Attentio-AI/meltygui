"""Native touch events focus text rows, not the unused area of a tall view."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("editable", [True, False])
def test_touch_focus_uses_text_rows(tmp_path, editable):
    program = r'''
from pathlib import Path
import sys
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
keyboard = []
clipboard = ['']
host = SimpleNamespace(request_frame=lambda: None, set_safe_zone=lambda top, bottom: None,
                       set_keyboard_visible=keyboard.append,
                       set_clipboard_text=lambda text: clipboard.__setitem__(0, text),
                       get_clipboard_text=lambda: clipboard[0])
native = NativeApplication({'app_id': 'text-focus-test'}, host, lambda: MetalRenderer(GPU()))
app.install_native_host(native)
from meltygui import glfw_window
from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty
from meltygui.view.text_view import draw_text
editable = sys.argv[2] == 'True'
state = dict(text='one\n\nthree')
@glfw_window(name='Focus test', app_id='text-focus-test')
@render_func(use_cache=False)
def body(input_value):
    state['changed'], state['result'], state['ds'] = draw_text(
        state['text'], name='text', width=380, height=360, editable=editable,
        show_header=False, with_header=None, with_footer=None, is_tree=False,
        show_widgets=False, autocomplete=False, syntax_highlight=False,
        show_file_header=False, show_jump_bar=False, return_extras=True)
    return False, input_value
app.run()
index = 0
def frame(events=()):
    global index
    index += 1
    native.frame(dict(width=440, height=600, scale=2, now=index/60,
                      presentation_time=(index+1)/60), list(events))
    native.presented()

def tap(row):
    ds = state['ds']
    x = ds.abs_left + 120
    y = ds.abs_top + ds._diff_top_inset - ds.scroll_offset[1] + row * ds._diff_line_px
    for kind in ('touch_begin', 'touch_end'):
        frame([dict(kind=kind, touch_id=1, x=x, y=y)])
    for _ in range(4): frame()
    assert state['ds']._stack_trace is None, state['ds']._stack_trace

for _ in range(8): frame()
assert state['ds']._stack_trace is None, state['ds']._stack_trace
tap(6.5)
assert Melty.text_focused_ds is None
assert not any(keyboard), keyboard
tap(2.5)
assert Melty.text_focused_ds is state['ds']
assert keyboard[-1] is editable, keyboard
if not editable:
    from meltygui.core.windowing import window_constants as keys
    from meltygui import imgui
    def key(code, mods=0):
        frame([dict(kind='key', key=code, action=keys.PRESS, modifiers=mods)])
        frame([dict(kind='key', key=code, action=keys.RELEASE, modifiers=mods)])
    key(keys.KEY_A, keys.MOD_CONTROL)
    ds = state['ds']
    assert (ds.text_selection_start, ds.text_selection_end) == (0, len(state['text']))
    key(keys.KEY_C, keys.MOD_CONTROL)
    assert imgui.get_clipboard_text() == state['text']
    for code, mods in [(keys.KEY_X, keys.MOD_CONTROL), (keys.KEY_V, keys.MOD_CONTROL),
                       (keys.KEY_BACKSPACE, 0), (keys.KEY_DELETE, 0),
                       (keys.KEY_ENTER, 0), (keys.KEY_TAB, 0),
                       (keys.KEY_SLASH, keys.MOD_CONTROL), (keys.KEY_I, keys.MOD_CONTROL)]:
        key(code, mods)
        assert state['result'] == state['text'] and not state['changed']
    frame([dict(kind='text', text='cannot edit')])
    assert state['result'] == state['text'] and not state['changed']
    assert not any(keyboard), keyboard
caret = state['ds'].text_cursor_pos
tap(6.5)
assert Melty.text_focused_ds is None
assert state['ds'].text_cursor_pos == caret
assert keyboard[-1] is False, keyboard
# A real blank line is editable, as is the first row of an empty document.
tap(1.5)
assert Melty.text_focused_ds is state['ds']
state['text'] = ''
state['ds'].invalidate()
for _ in range(4): frame()
tap(6.5)
assert Melty.text_focused_ds is None
tap(0.5)
assert Melty.text_focused_ds is state['ds']
native.close()
'''
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path), str(editable)],
                            close_fds=False, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
