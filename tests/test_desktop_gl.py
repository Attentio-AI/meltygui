"""Lazy GL bindings resolve once without copying or wrapping their values."""
import subprocess
import sys


def test_lazy_binding_identity_and_consumer_patching():
    # A fresh interpreter proves importing the bridge does not load a driver,
    # independent of GL contexts or bindings created by other test modules.
    program = '''
import importlib.abc
import sys
import types

attempts = []
class NoDriver(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == 'OpenGL' or name.startswith('OpenGL.'):
            attempts.append(name)
            raise AssertionError('Unexpected driver import: ' + name)

sys.meta_path.insert(0, NoDriver())
from meltygui.core.graphics import desktop_gl
assert 'OpenGL' not in sys.modules
assert getattr(desktop_gl, '__module__', None) is None
assert getattr(desktop_gl, '__wrapped__', None) is None
assert not attempts

checker = types.SimpleNamespace(enabled=True)
def glProbe():
    return checker.enabled

reads = []
class Binding(types.ModuleType):
    def __getattribute__(self, name):
        if name in ('glProbe', 'GL_TEXTURE_2D', 'glMissing'):
            reads.append(name)
        return super().__getattribute__(name)

binding = Binding('OpenGL.GL')
binding.glProbe = glProbe
binding.GL_TEXTURE_2D = 0x0DE1
package = types.ModuleType('OpenGL')
package.GL = binding
sys.modules['OpenGL'] = package

for _ in range(5):
    assert desktop_gl.glProbe is glProbe
    assert desktop_gl.GL_TEXTURE_2D == 0x0DE1
assert reads == ['glProbe', 'GL_TEXTURE_2D']
assert vars(desktop_gl)['glProbe'] is glProbe

# The error checker changes in place; a cached wrapper must keep seeing it.
assert desktop_gl.glProbe() is True
checker.enabled = False
assert desktop_gl.glProbe() is False

# Patching the consumed module, then restoring its binding, stays supported.
original = desktop_gl.glProbe
desktop_gl.glProbe = lambda: 'patched'
assert desktop_gl.glProbe() == 'patched'
desktop_gl.glProbe = original
assert desktop_gl.glProbe is binding.glProbe

# Hotswap updates code on the same function object rather than wrapping it.
def replacement():
    return 'hotswapped'
glProbe.__code__ = replacement.__code__
assert desktop_gl.glProbe() == 'hotswapped'

try:
    desktop_gl.glMissing
except AttributeError:
    pass
else:
    raise AssertionError('Missing GL names must retain AttributeError')
assert 'glMissing' not in vars(desktop_gl)
binding.glMissing = 7
assert desktop_gl.glMissing == 7
assert not attempts
'''
    result = subprocess.run([sys.executable, '-c', program], text=True,
                            capture_output=True, close_fds=False, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
