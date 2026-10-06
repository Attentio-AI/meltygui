"""A caught view exception must leave the real ImGui/Metal frame usable."""
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
    def set_safe_zone(self, top, bottom): pass
    def get_clipboard_text(self): return ''
    def set_clipboard_text(self, text): pass

from meltygui.core.runtime import app
from meltygui.core.runtime.native_app import NativeApplication
from meltygui.core.graphics.metal_renderer import MetalRenderer
native = NativeApplication({'app_id': 'melty-ios-test'}, Host(), lambda: MetalRenderer(gpu))
app.install_native_host(native)
sys.platform = 'ios'
from meltygui import glfw_window, imgui
from meltygui.core.core_render import render_func, id_stack
from meltygui.core.melty import Melty
state = dict(fail=False, siblings=0)
value = {'value': 1}

@render_func(use_cache=True)
def child(input_value: dict, draw_state):
    state['child'] = draw_state
    imgui.text('Before the exception')
    if state['fail']:
        raise NameError('intentional view failure')
    return False, input_value

@render_func(use_cache=False)
def sibling(input_value):
    state['siblings'] += 1
    imgui.text('Still drawing')
    return False, input_value

@glfw_window(name='Exception check', app_id='melty-ios-test')
@render_func(use_cache=False)
def body(input_value):
    changed, result = child(value, name='failing child', width=300, height=80)
    assert not changed and result is value
    sibling(None, name='sibling', width=300, height=80)
    return False, input_value

app.run()
def frame(index):
    native.frame(dict(now=index/120, presentation_time=(index+1)/120,
                      width=420, height=600, scale=2), [])
    gpu._test_flush()
    native.presented()

for i in range(6): frame(i)
stacks = (len(id_stack), len(Melty.seen_values), len(Melty.clip_stack))
assert native.cache.enabled
before = state['siblings']
state['fail'] = True
state['child'].invalidate_up()
frame(6)
error = state['child']._stack_trace
assert isinstance(error, NameError) and str(error) == 'intentional view failure', repr(error)
assert state['siblings'] > before
assert (len(id_stack), len(Melty.seen_values), len(Melty.clip_stack)) == stacks
state['fail'] = False
state['child'].invalidate_up()
for i in range(7, 10): frame(i)
assert not app._state.get('failed')
assert state['siblings'] >= before + 4
assert (len(id_stack), len(Melty.seen_values), len(Melty.clip_stack)) == stacks
native.suspend()
native.close()
'''


@unittest.skipUnless(os.environ.get('MELTY_METAL_TEST') == '1', 'requires compiled host Metal extension')
class RenderErrorsMetal(unittest.TestCase):
    def test_failed_cached_view_preserves_frame_siblings_and_next_frame(self):
        artifacts = build_directory() / 'metal-test'
        with tempfile.TemporaryDirectory(prefix='melty-view-error-') as sandbox:
            result = subprocess.run(
                [sys.executable, '-c', PROGRAM, sandbox, str(artifacts)],
                env=dict(os.environ, PYTHONPATH=str(artifacts)),
                close_fds=False, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
