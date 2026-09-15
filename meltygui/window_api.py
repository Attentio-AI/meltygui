"""Window operations shared by native Wayland and GLFW surfaces.

GLFW's constants and callback signatures remain the compatibility vocabulary.
The selected backend owns all windows in an app's GL share group. Merely
importing this module does not initialize GLFW or connect to a display.
"""
import importlib
import os
import sys
from src.lsd.gl_gui import window_constants

_state = globals().get('_state') or {'backend': None, 'selected': False}


def use_native_windows(enabled, environment=None, platform=None):
    environment = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    return bool(enabled and platform.startswith('linux') and
                (environment.get('WAYLAND_DISPLAY') or environment.get('WAYLAND_SOCKET')))


def select_backend(enabled):
    if _state['selected']:
        return backend_name()
    if use_native_windows(enabled):
        from src.lsd.gl_gui.window_backends.native_wayland import Backend
        _state['backend'] = Backend()
    _state['selected'] = True
    return backend_name()


def backend_name():
    return 'wayland' if _state['backend'] is not None else 'glfw'


def is_native_window(window):
    # Inspect the class marker, not a truthy instance attribute: MagicMock
    # manufactures attributes and must never pass as a real window handle.
    return getattr(type(window), 'native_wayland', False) is True


def terminate():
    backend = _state['backend']
    try:
        (backend or importlib.import_module('glfw')).terminate()
    finally:
        _state.update(backend=None, selected=False)


def __getattr__(name):
    # Source analysis and intros probe module metadata. Forwarding a missing
    # __module__/__wrapped__/__path__ to GLFW loads its shared library merely
    # to discover that GLFW does not supply such metadata either.
    if name.startswith('__'):
        raise AttributeError(name)
    if name.isupper() and hasattr(window_constants, name):
        return getattr(window_constants, name)
    backend = _state['backend']
    if backend is not None and not name.isupper() and not name.startswith('_'):
        # Never send native handles to GLFW's C functions by accident.
        return getattr(backend, name)
    return getattr(importlib.import_module('glfw'), name)
