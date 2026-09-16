"""Backend selection and native input contracts; no compositor required."""
import ctypes
from types import SimpleNamespace

import meltygui_imgui as imgui
import glfw
import pytest

import meltygui.core.window_api as window_api
import meltygui.core.window_constants as window_constants
from meltygui.core.backends.native_wayland import Backend
from meltygui.core.backends.native_wayland import NativeWindow
from meltygui.core.backends.imgui_renderer import WindowRenderer


@pytest.mark.parametrize('enabled,platform,environment,expected', [
    (True, 'linux', {'WAYLAND_DISPLAY': 'wayland-test'}, True),
    (True, 'linux', {'WAYLAND_SOCKET': '3'}, True),
    (True, 'linux', {'XDG_SESSION_TYPE': 'wayland'}, False),
    (True, 'linux', {'DISPLAY': ':0'}, False),
    (False, 'linux', {'WAYLAND_DISPLAY': 'wayland-test'}, False),
    (True, 'darwin', {'WAYLAND_DISPLAY': 'wayland-test'}, False),
    (True, 'win32', {}, False),
])
def test_selection(enabled, platform, environment, expected):
    assert window_api.use_native_windows(enabled, environment, platform) is expected


def test_window_constants_match_glfw():
    for name, value in vars(glfw).items():
        if name.isupper() and not name.startswith('_') and type(value) is int:
            assert getattr(window_constants, name) == value, name


def test_native_constants_do_not_import_glfw(monkeypatch):
    def unexpected_import(name):
        raise AssertionError(f'Unexpected import: {name}')
    monkeypatch.setattr(window_api.importlib, 'import_module', unexpected_import)
    assert window_api.KEY_A == glfw.KEY_A
    assert window_api.CONTEXT_VERSION_MAJOR == glfw.CONTEXT_VERSION_MAJOR
    assert getattr(window_api, '__module__', None) is None
    assert getattr(window_api, '__wrapped__', None) is None
    assert getattr(window_api, '__path__', None) is None


def test_importing_native_backend_does_not_load_glfw():
    import subprocess
    import sys
    subprocess.run([sys.executable, '-c',
                    'import sys; '
                    'from meltygui.core.backends.native_wayland import Backend; '
                    'from meltygui import window_api; '
                    'assert window_api.KEY_A == 65; '
                    'assert "glfw" not in sys.modules'], check=True)


def test_backend_is_fixed_for_the_share_group(monkeypatch):
    monkeypatch.setattr(window_api, '_state', {'backend': None, 'selected': False})
    monkeypatch.setattr(window_api, 'use_native_windows', lambda enabled: False)
    assert window_api.select_backend(True) == 'glfw'
    monkeypatch.setattr(window_api, 'use_native_windows', lambda enabled: True)
    assert window_api.select_backend(True) == 'glfw'


def test_native_handles_never_fall_through_to_glfw(monkeypatch):
    backend = SimpleNamespace(get_window_size=lambda window: (700, 500), terminate=lambda: None)
    monkeypatch.setattr(window_api, '_state', {'backend': backend, 'selected': True})
    assert window_api.__getattr__('get_window_size')(object()) == (700, 500)
    with pytest.raises(AttributeError):
        window_api.__getattr__('get_x11_window')
    assert window_api.KEY_A == glfw.KEY_A
    window_api.terminate()
    assert window_api._state == {'backend': None, 'selected': False}


@pytest.fixture
def backend():
    value = Backend()
    yield value
    value.terminate()


def test_callbacks_chain_and_are_window_local(backend):
    first = NativeWindow(backend, 700, 500, 'first', {})
    second = NativeWindow(backend, 700, 500, 'second', {})
    received = []
    def callback(window, character):
        received.append((window, character))
    assert backend.set_char_callback(first, callback) is None
    previous = backend.set_char_callback(first, lambda window, character: callback(window, character + 1))
    assert previous is callback
    first.emit('char', 65)
    second.emit('char', 65)
    assert received == [(first, 66)]


def test_key_release_focus_and_repeat_are_window_local(backend):
    window = NativeWindow(backend, 700, 500, 'first', {})
    backend.by_surface[123] = window
    backend.keyboard_enter(9, 123, None)
    backend.key(10, 0, 30, 1)
    assert backend.get_key(window, glfw.KEY_A) == glfw.PRESS
    backend.repeat_key = (window, 30, glfw.KEY_A)
    backend.keyboard_leave(11, 123)
    assert not window.focused
    assert backend.get_key(window, glfw.KEY_A) == glfw.RELEASE
    assert backend.repeat_key is None


def test_pointer_coordinates_buttons_and_framebuffer_scale(backend):
    window = NativeWindow(backend, 700, 500, 'first', {})
    backend.pointer_window = window
    backend.pointer_motion(0, 123 * 256, 45 * 256)
    backend.pointer_button(1, 0, 273, 1)
    assert backend.get_cursor_pos(window) == (123, 45)
    assert backend.get_mouse_button(window, glfw.MOUSE_BUTTON_RIGHT) == glfw.PRESS
    window.scale = 2
    assert backend.get_framebuffer_size(window) == (1400, 1000)


def test_wakeup_interrupts_wait(backend):
    # A compositor FD with no pending events; only the wake socket is readable.
    backend.native = SimpleNamespace(pending=lambda display: 0, flush=lambda display: 0,
                                     prepare_read=lambda display: 0, cancel_read=lambda display: None,
                                     get_fd=lambda display: backend.wake_writer.fileno())
    backend.post_empty_event()
    backend.wait_events_timeout(0)
    with pytest.raises(BlockingIOError):
        backend.wake_reader.recv(1)


def test_imgui_char_callback_targets_receiving_context(monkeypatch):
    first_characters, second_characters = [], []
    first = object.__new__(WindowRenderer)
    second = object.__new__(WindowRenderer)
    first.io = SimpleNamespace(add_input_character=first_characters.append, keys_down=[False] * 512)
    second.io = SimpleNamespace(add_input_character=second_characters.append)
    # The compositor dispatches A's event while B's frame is current.
    monkeypatch.setattr(imgui, 'get_io', lambda: second.io)
    first.char_callback(None, ord('a'))
    assert first_characters == [ord('a')]
    assert second_characters == []
    first.keyboard_callback(None, -1, 0, glfw.PRESS, 0)
    assert not any(first.io.keys_down)


def test_native_handle_identifies_its_gl_context(backend):
    window = NativeWindow(backend, 1, 1, 'owner', {glfw.VISIBLE: False})
    window.context = 0x12345
    assert window_api.is_native_window(window)
    assert not window.visible
    assert ctypes.cast(window, ctypes.c_void_p).value == 0x12345


def test_native_frame_is_available_to_chrome_and_collision_flush(backend, monkeypatch):
    import meltygui.core.titlebar as titlebar
    import meltygui.core.os_frame as os_frame
    from meltygui.core.melty import Melty
    from meltygui.core.toggles import Toggles
    window = NativeWindow(backend, 800, 600, 'native', {glfw.TRANSPARENT_FRAMEBUFFER: True})
    monkeypatch.setattr(Melty, 'glfw_window', window)
    assert titlebar._studio_window() is window
    monkeypatch.setattr(window_api, '_state', {'backend': backend, 'selected': True})
    monkeypatch.setattr(Toggles.Melty, 'window_shadow_margin', 40)
    monkeypatch.setattr(titlebar, '_maximized', lambda handle: False)
    monkeypatch.setattr(titlebar, '_fullscreen', lambda handle: False)
    monkeypatch.setattr(titlebar, '_monitor_size', lambda handle: (1920, 1080))
    assert titlebar.window_inset() == max(40, titlebar.shadow_reach(1920, 1080))
    monkeypatch.setattr(Toggles.Melty, 'push_os_window_edges', True)
    monkeypatch.setattr(Melty, 'display_size', (800, 600))
    monkeypatch.setattr(titlebar, 'window_inset', lambda: 0)
    monkeypatch.setattr(os_frame, '_book_pin_rebases', lambda: None)
    state = dict(os_frame._STATE)
    state.update(mode='walls', edges={'x': [{'x': 0.}, {'x': 850.}], 'y': [{'y': 0.}, {'y': 600.}]},
                 size_requests={'x': [], 'y': []}, size_observed=[800., 600.],
                 size_expected=[800., 600.], size_request_frame=[0, 0])
    monkeypatch.setattr(os_frame, '_STATE', state)
    requests = []
    monkeypatch.setattr(titlebar, 'request_surface_size', lambda handle, *size, **kwargs: requests.append((handle, size)))
    assert os_frame.flush() == (850, 600)
    assert requests == [(window, (850, 600))]


def test_mock_window_is_not_a_native_handle(monkeypatch):
    from unittest.mock import MagicMock
    import meltygui.core.titlebar as titlebar
    from meltygui.core.melty import Melty
    window = MagicMock()
    monkeypatch.setattr(Melty, 'glfw_window', window)
    assert not window_api.is_native_window(window)
    assert titlebar._studio_window() is None


def test_native_decoration_protocol_metadata():
    from meltygui.core.backends.wayland_protocol import Native
    native = Native()
    manager = native.interfaces['zxdg_decoration_manager_v1']
    decoration = native.interfaces['zxdg_toplevel_decoration_v1']
    assert manager.methods[1].name == b'get_toplevel_decoration'
    assert manager.methods[1].signature == b'no'
    assert decoration.methods[1].name == b'set_mode'
    assert decoration.events[0].name == b'configure'


@pytest.mark.parametrize('decorated,mode', [(False, 1), (True, 2)])
def test_native_decoration_request_and_live_toggle(backend, decorated, mode):
    calls, listeners = [], []
    backend.decoration_manager = 10
    backend.native = SimpleNamespace(request=lambda proxy, opcode, *args, **kwargs:
                                    calls.append((proxy, opcode, args, kwargs)) or 30)
    backend.listen = lambda proxy, events: listeners.append((proxy, events))
    window = NativeWindow(backend, 700, 500, 'custom chrome', {glfw.DECORATED: decorated})
    window.toplevel = 20
    backend.sync_decoration(window)
    assert calls[0][0:2] == (10, 1)
    assert calls[0][2][1].value == 20
    assert calls[0][3]['interface'] == 'zxdg_toplevel_decoration_v1'
    assert calls[1][0:2] == (30, 1)
    assert calls[1][2][0].value == mode
    listeners[0][1][0][1](mode)
    assert window.decoration_mode == mode
    backend.set_window_attrib(window, glfw.DECORATED, not decorated)
    assert len(calls) == 3  # reuse the existing decoration object
    assert calls[-1][2][0].value == 3 - mode


def test_native_decoration_is_optional_and_skips_hidden_owner(backend):
    window = NativeWindow(backend, 1, 1, 'owner', {})
    backend.sync_decoration(window)
    backend.decoration_manager = 10
    backend.sync_decoration(window)
    assert window.decoration is None


def test_native_decoration_destroyed_before_toplevel(backend):
    destroyed = []
    backend.native = SimpleNamespace(request=lambda proxy, opcode, **kwargs: destroyed.append(proxy))
    window = NativeWindow(backend, 700, 500, 'custom chrome', {})
    window.decoration, window.toplevel, window.xdg_surface, window.surface = 10, 20, 30, 40
    backend.destroy_window(window)
    assert destroyed == [10, 20, 30, 40]
    backend.destroy_window(window)
    assert len(destroyed) == 4


def test_pointer_motion_generation_includes_identical_local_coordinates(backend):
    window = NativeWindow(backend, 700, 500, 'moving', {})
    backend.pointer_window = window
    for expected in (1, 2):
        backend.pointer_motion(0, 100 * 256, 200 * 256)
        assert window.cursor_pos == (100, 200)
        assert window.cursor_motion_generation == expected


def test_destroyed_child_in_shutdown_snapshot_is_not_destroyed_again(monkeypatch):
    from meltygui.core.surface import Surface
    # Parent teardown already removed this child; app.run's shutdown snapshot
    # still contains it. No context or GLFW handle may be touched a second time.
    child = object.__new__(Surface)
    monkeypatch.setattr(Surface, 'all', [])
    monkeypatch.setattr(child, 'activate', lambda: pytest.fail('destroyed context reactivated'))
    child.destroy()
