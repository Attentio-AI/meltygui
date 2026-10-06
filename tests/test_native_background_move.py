"""Cocoa uses the app's drag arbitration and a leased, original-event handoff."""
import ctypes
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui import imgui
from meltygui.core.melty import Melty
from meltygui.core.input.input_handler import InputHandler
from meltygui.core.input.pynput_backend import GlfwQueueBackend
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing import melty_windows as bridge, titlebar, window_api as glfw


def test_cached_helper_gains_pointer_safe_move_exports_after_hotswap(monkeypatch):
    calls = []
    capture_callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)(
        lambda window, down: calls.append((window, down)))
    begin_callback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(lambda window: 1)
    # Like a previously loaded CDLL, these exports have no argument declarations.
    capture = ctypes.CFUNCTYPE(ctypes.c_int)(ctypes.cast(capture_callback, ctypes.c_void_p).value)
    begin = ctypes.CFUNCTYPE(ctypes.c_int)(ctypes.cast(begin_callback, ctypes.c_void_p).value)
    api = SimpleNamespace(MeltySurfaceMoveCapture=capture, MeltySurfaceMoveBegin=begin)
    monkeypatch.setattr(bridge, '_STATE', dict(frame_apis={'/already-loaded': api}))
    monkeypatch.setattr(bridge.ctypes, 'CDLL', lambda path: pytest.fail('cached helper reloaded'))
    assert bridge._frame_api('/already-loaded') is api
    pointer = 0x123456788765
    api.MeltySurfaceMoveCapture(pointer, 1)
    assert calls == [(pointer, 1)]
    assert api.MeltySurfaceMoveBegin(pointer) == 1
    assert bridge._frame_api('/already-loaded') is api


@pytest.fixture
def move_api(monkeypatch):
    api = SimpleNamespace(MeltySurfaceMoveCapture=Mock(), MeltySurfaceMoveBegin=Mock(return_value=1))
    state = dict(lock=threading.Lock(), accepted={7}, expires=20., move_enabled=True,
                 frame_library='/helper')
    monkeypatch.setattr(bridge, '_STATE', state)
    monkeypatch.setattr(bridge, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(bridge, '_window_number', lambda window: window)
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: 19.)
    monkeypatch.setattr(bridge, '_frame_api', lambda path: api)
    monkeypatch.setattr(glfw, 'get_cocoa_window', lambda window: 700 + window)
    return api, state


def test_press_capture_is_per_surface_and_only_left_button(move_api):
    api, _ = move_api
    bridge.capture_move_press(7, glfw.MOUSE_BUTTON_RIGHT, glfw.PRESS)
    bridge.capture_move_press(8, glfw.MOUSE_BUTTON_LEFT, glfw.PRESS)
    api.MeltySurfaceMoveCapture.assert_not_called()
    bridge.capture_move_press(7, glfw.MOUSE_BUTTON_LEFT, glfw.PRESS)
    bridge.capture_move_press(7, glfw.MOUSE_BUTTON_LEFT, glfw.RELEASE)
    assert [call.args for call in api.MeltySurfaceMoveCapture.call_args_list] == [(707, 1), (707, 0)]
    assert bridge.begin_move(7)
    api.MeltySurfaceMoveBegin.assert_called_once_with(707)


@pytest.mark.parametrize('change', ['expiry', 'disabled', 'removed', 'old_helper'])
def test_lost_permission_or_older_helper_cannot_start_move(move_api, change):
    api, state = move_api
    if change == 'expiry': state['expires'] = 19.
    elif change == 'disabled': state['move_enabled'] = False
    elif change == 'removed': state['accepted'].clear()
    else: del api.MeltySurfaceMoveCapture
    assert not bridge.move_available(7)
    assert not bridge.begin_move(7)
    api.MeltySurfaceMoveBegin.assert_not_called()


@pytest.mark.parametrize('control', [False, True])
def test_decorated_cocoa_background_uses_normal_down_capture(monkeypatch, control):
    handler = InputHandler()
    monkeypatch.setattr(Melty, 'event_handler', handler)
    monkeypatch.setattr(Melty, 'events', {})
    monkeypatch.setattr(titlebar, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(titlebar, 'sync_decoration', lambda window: False)
    monkeypatch.setattr(titlebar, 'settings_available', lambda: False)
    monkeypatch.setattr(titlebar, '_fullscreen', lambda window: False)
    monkeypatch.setattr(titlebar, '_maximized', lambda window: False)
    monkeypatch.setattr(titlebar, '_wm_move_started', False)
    monkeypatch.setattr(bridge, 'move_available', lambda window: True)
    monkeypatch.setattr(Toggles.Melty, 'move_drag_anywhere', True)
    move = Mock(return_value=True)
    monkeypatch.setattr(bridge, 'begin_move', move)
    window = object()
    handler.register_hovered('workspace', [], priority=0, blocker=True)
    if control:
        handler.register_hovered('text/divider', ['left_mouse_drag'], priority=-2)
    titlebar.draw_titlebar(window)
    handler.feed_down('left_mouse', x=200, y=200)
    handler.process_frame()
    expected = 'text/divider' if control else titlebar._STRIP_ID
    assert handler._drag_capture['left_mouse'][0] == expected
    move.assert_not_called()  # a click alone never hands off to Cocoa
    if not control:
        monkeypatch.setattr(Melty, 'events', {titlebar._STRIP_ID: {'non_blocking_left_mouse_dragged': object()}})
        titlebar.draw_titlebar(window)
        titlebar.draw_titlebar(window)
        move.assert_called_once_with(window)


@pytest.mark.parametrize('reason', ['fullscreen', 'maximized', 'disabled', 'no_service'])
def test_ineligible_background_does_not_register(monkeypatch, reason):
    handler = SimpleNamespace(is_down=lambda name: False, register_hovered=Mock())
    monkeypatch.setattr(Melty, 'event_handler', handler)
    monkeypatch.setattr(titlebar, '_fullscreen', lambda window: reason == 'fullscreen')
    monkeypatch.setattr(titlebar, '_maximized', lambda window: reason == 'maximized')
    monkeypatch.setattr(bridge, 'move_available', lambda window: reason != 'no_service')
    monkeypatch.setattr(Toggles.Melty, 'move_drag_anywhere', reason != 'disabled')
    titlebar._draw_native_background_move(object())
    handler.register_hovered.assert_not_called()


def test_failed_optional_capture_preserves_the_original_input(monkeypatch):
    from meltygui.core.windowing import glfw_utils
    backend = object.__new__(GlfwQueueBackend)
    backend.handler = SimpleNamespace(feed_down=Mock(), feed_up=Mock(), set_modifiers=Mock())
    backend._prev_button = Mock()
    monkeypatch.setattr(backend, '_content_cursor', lambda window: (30, 40))
    monkeypatch.setattr(bridge, 'capture_move_press', Mock(side_effect=OSError('helper unavailable')))
    monkeypatch.setattr(glfw_utils, 'request_render', Mock())
    window = object()
    backend._on_button(window, glfw.MOUSE_BUTTON_LEFT, glfw.PRESS, 0)
    backend.handler.feed_down.assert_called_once_with('left_mouse', 30, 40)
    backend._prev_button.assert_called_once_with(window, glfw.MOUSE_BUTTON_LEFT, glfw.PRESS, 0)
