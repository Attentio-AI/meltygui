"""Background parsing must respect a native render owner without OpenGL."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize('owner', ['native', 'opengl', 'render', 'no_owner'])
def test_background_parse_respects_render_owner_and_activity(owner):
    # Conversion imports have background services. Keep their clocks/workers
    # separate from other tests while exercising the real cooperative yield.
    program = r'''
import sys
import threading
from meltygui.code import libcst_conversion
from meltygui.core.graphics import gl_state
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing import glfw_utils

owner = sys.argv[1]
clock = [100.0]
libcst_conversion.time.monotonic = lambda: clock[0]
Toggles.yield_to_ui = True
Melty.frame_count = 10
Melty._frame_draw_start = 0.0  # Between rendered frames.
Melty._last_input_time = clock[0]
glfw_utils._render_thread_id = threading.get_ident() if owner == 'native' else None
gl_state._gl_thread = threading.current_thread() if owner == 'opengl' else None
waiting, resume, done = threading.Event(), threading.Event(), threading.Event()
errors = []
original_sleep = libcst_conversion.time.sleep

def pause(seconds):
    if threading.current_thread().name != 'coast-parse-test':
        return original_sleep(seconds)
    waiting.set()
    if not resume.wait(3):
        raise TimeoutError('test did not release the parser')

def parse():
    if owner == 'render':
        glfw_utils._render_thread_id = threading.get_ident()
    try:
        libcst_conversion._yield_to_ui()
    except Exception as error:
        errors.append(error)
    finally:
        done.set()

libcst_conversion.time.sleep = pause
worker = threading.Thread(target=parse, name='coast-parse-test')
worker.start()
try:
    if owner in ('render', 'no_owner'):
        assert done.wait(1) and not waiting.is_set(), 'UI/startup work must never park'
    else:
        assert waiting.wait(1), ('parser ignored ongoing native UI activity', errors)
        assert not done.is_set()
finally:
    clock[0] += libcst_conversion._YIELD_QUIET_S + .01
    resume.set()
    worker.join(2)
assert done.is_set() and not worker.is_alive()
assert not errors, errors
'''
    result = subprocess.run([sys.executable, '-c', program, owner],
                            capture_output=True, text=True, close_fds=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
