"""View registration and code inspection must not initialize desktop graphics."""
import subprocess
import sys


def test_shader_metadata_and_image_view_registration_without_opengl():
    program = '''
import importlib.abc
import sys
attempts = []
class NoDesktop(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('OpenGL', 'glfw'):
            attempts.append(name)
            raise AssertionError('Desktop graphics import: ' + name)
sys.meta_path.insert(0, NoDesktop())
import meltygui.core.rendering.mode
from meltygui.core.graphics import desktop_gl
from meltygui.core.graphics.shader_func import shader_func
assert getattr(desktop_gl, '__module__', None) is None
@shader_func(fragment='out vec4 color; void main() { color = vec4(1); }')
def example(gl_state=None): pass
assert example.stages[0].enum == 0x8B31
assert example.stages[1].enum == 0x8B30
assert not attempts, attempts
'''
    result = subprocess.run([sys.executable, '-c', program], text=True,
                            capture_output=True, close_fds=False, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
