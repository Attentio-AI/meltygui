"""Window operations shared by native Wayland and GLFW surfaces.

GLFW's constants and callback signatures remain the compatibility vocabulary.
The selected backend owns all windows in an app's GL share group. Merely
importing this module does not initialize GLFW or connect to a display.
"""
import importlib
import os
import sys
import meltygui.core.windowing.window_constants as window_constants

_state = globals().get('_state') or {'backend': None, 'selected': False}


def use_native_windows(enabled, environment=None, platform=None):
    environment = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    return bool(enabled and platform.startswith('linux') and
                (environment.get('WAYLAND_DISPLAY') or environment.get('WAYLAND_SOCKET')))


def select_backend(enabled):
    if _state['selected']:
        return backend_name()
    if sys.platform == 'ios':
        # Host setup is a runtime responsibility, including ordinary desktop
        # entry points and a fresh boot after terminate() cleared the backend.
        if '_hub_graphics' in sys.builtin_module_names:
            from hub_ios_glfw import _bind_melty
            _bind_melty()
        else:
            from melty_ios_bootstrap import get_host
            select_ios_backend(get_host())
        return backend_name()
    if use_native_windows(enabled):
        from meltygui.core.windowing.backends.native_wayland import Backend
        _state['backend'] = Backend()
    _state['selected'] = True
    return backend_name()


def backend_name():
    backend = _state['backend']
    return getattr(backend, 'name', 'wayland') if backend is not None else 'glfw'


def opengl_context_version():
    """Respect the selected host's GL subset before creating shared contexts."""
    default = (4, 1) if sys.platform == 'darwin' else (4, 3)
    return getattr(_state['backend'], 'opengl_version', default)


def supports_dual_source_blending():
    """Whether the host supports LCD text's two fragment outputs."""
    return getattr(_state['backend'], 'dual_source_blending', True)


def owns_touch_input():
    """External hosts can own gestures while Melty consumes their mouse events."""
    backend = _state['backend']
    return backend is None or getattr(backend, 'owns_touch_input', True)


def select_ios_backend(host):
    """Bind the UIKit host explicitly, before importing/booting the app."""
    if _state['selected']:
        backend = _state['backend']
        if backend_name() != 'ios' or backend.host is not host:
            raise RuntimeError('a different window backend or iOS host is already active')
        return backend
    from meltygui.core.windowing.backends.native_ios import Backend
    backend = Backend(host)
    _state.update(backend=backend, selected=True)
    return backend


def is_native_window(window):
    # Inspect the class marker, not a truthy instance attribute: MagicMock
    # manufactures attributes and must never pass as a real window handle.
    return (getattr(type(window), 'native_wayland', False) is True or
            getattr(type(window), 'native_ios', False) is True)


def destroy_window(window):
    if sys.platform == 'darwin':
        from meltygui.core.windowing import melty_windows
        melty_windows.forget(window)
    backend = _state['backend']
    if backend is not None:
        return backend.destroy_window(window)
    return importlib.import_module('glfw').destroy_window(window)


def terminate():
    if sys.platform == 'darwin':
        from meltygui.core.windowing import melty_windows
        melty_windows.stop()
    backend = _state['backend']
    try:
        if backend is not None:
            backend.terminate()
        elif sys.platform != 'ios':
            importlib.import_module('glfw').terminate()
    finally:
        _state.update(backend=None, selected=False)


def poke():
    """How a thread that holds no interpreter rouses the event wait (the
    frame loop then finds what it queued: `glfw_utils.drain_wakes`):
    ('fd', descriptor) to write a byte to, ('call', address) of a C function
    that takes nothing, or (None, 0) when the host has neither: the loop
    notices at its next turn. `post_empty_event` is the same wake for a
    thread that does hold the interpreter."""
    backend = _state['backend']
    if backend is not None:
        return backend.poke() if hasattr(backend, 'poke') else (None, 0)
    if sys.platform == 'ios':
        return None, 0
    import ctypes
    library = importlib.import_module('glfw')._glfw
    return 'call', ctypes.cast(library.glfwPostEmptyEvent, ctypes.c_void_p).value


def pace(drawing=lambda: True):
    """Before the input of a frame is read: let the backend wait for the moment that leaves the
    input freshest when the frame reaches the screen (`native_wayland.Backend.pace`). Backends
    without pacing return at once."""
    backend = _state['backend']
    wait = getattr(backend, 'pace', None)
    if wait is not None:
        wait(drawing)


def presentation_latency_ms():
    """Optional host presentation timing; None means this backend has no measurement."""
    backend = _state['backend']
    provider = getattr(backend, 'get_presentation_latency_ms', None)
    return provider() if provider is not None else None


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
    if sys.platform == 'ios' or backend_name() == 'ios':
        raise AttributeError(f'the native iOS window API does not provide {name!r}')
    return getattr(importlib.import_module('glfw'), name)
