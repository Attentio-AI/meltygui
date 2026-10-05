"""Native settings keep their controls without querying unrelated OS chrome."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui import imgui
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing import glfw_utils, titlebar


@pytest.mark.parametrize('platform', ['darwin', 'win32'])
def test_native_decoration_policy_needs_no_per_frame_platform_or_attribute_queries(monkeypatch, platform):
    monkeypatch.setattr(titlebar, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(Toggles.Melty, 'enhanced_titlebar', True)
    wayland, get_attribute, set_attribute = Mock(), Mock(), Mock()
    monkeypatch.setattr(titlebar, '_on_wayland', wayland)
    monkeypatch.setattr(titlebar.glfw, 'get_window_attrib', get_attribute)
    monkeypatch.setattr(titlebar.glfw, 'set_window_attrib', set_attribute)

    assert not titlebar.backend_supported()
    assert not titlebar.titlebar_enabled()
    assert titlebar.wants_os_decoration()
    assert not titlebar.sync_decoration(object())
    wayland.assert_not_called()
    get_attribute.assert_not_called()
    set_attribute.assert_not_called()


@pytest.mark.parametrize('wayland,native_frame', [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize('enhanced', [False, True])
@pytest.mark.parametrize('show_frame', [False, True])
def test_linux_decoration_still_tracks_live_settings(monkeypatch, wayland, native_frame, enhanced, show_frame):
    monkeypatch.setattr(titlebar, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(titlebar, '_on_wayland', lambda: wayland)
    monkeypatch.setattr(glfw_utils, 'wayland_native_frame_active', lambda: native_frame)
    monkeypatch.setattr(Toggles.Melty, 'enhanced_titlebar', enhanced)
    monkeypatch.setattr(Toggles.Melty, 'wayland_show_frame', show_frame)
    custom = native_frame if wayland else enhanced
    decorated = (show_frame if native_frame else True) if wayland else not enhanced
    get_attribute = Mock(side_effect=[not decorated, decorated])
    set_attribute = Mock()
    monkeypatch.setattr(titlebar.glfw, 'get_window_attrib', get_attribute)
    monkeypatch.setattr(titlebar.glfw, 'set_window_attrib', set_attribute)
    window = object()

    assert titlebar.backend_supported() == (not wayland or native_frame)
    assert titlebar.wants_os_decoration() == decorated
    assert titlebar.sync_decoration(window) == custom
    assert titlebar.sync_decoration(window) == custom
    assert get_attribute.call_count == 2
    set_attribute.assert_called_once_with(window, titlebar.glfw.DECORATED,
                                          titlebar.glfw.TRUE if decorated else titlebar.glfw.FALSE)


@pytest.mark.parametrize('kinds', [((), ('settings',)), (('close',), ()), ((), ('minimize',))])
def test_layout_without_maximize_does_not_read_native_maximized_state(monkeypatch, kinds):
    monkeypatch.setattr(titlebar, 'controls_enabled', lambda: True)
    monkeypatch.setattr(titlebar, 'control_kinds', lambda: kinds)
    maximized = Mock()
    monkeypatch.setattr(titlebar, '_maximized', maximized)
    imgui.new_frame()

    left, right = titlebar.chrome_insets()
    buttons = titlebar._button_layout(800)
    assert [button[0] for button in buttons] == list(kinds[0] + kinds[1])
    assert (left > 0, right > 0) == (bool(kinds[0]), bool(kinds[1]))
    maximized.assert_not_called()


def test_maximize_control_uses_live_restore_glyph(monkeypatch):
    monkeypatch.setattr(titlebar, 'controls_enabled', lambda: True)
    monkeypatch.setattr(titlebar, 'control_kinds', lambda: ((), ('maximize',)))
    maximized = Mock(side_effect=[False, True])
    monkeypatch.setattr(titlebar, '_maximized', maximized)
    imgui.new_frame()

    assert titlebar._button_layout(800)[0][1] == titlebar._ICON_MAXIMIZE
    assert titlebar._button_layout(800)[0][1] == titlebar._ICON_RESTORE
    assert maximized.call_count == 2


def test_hosted_settings_click_is_not_relaid_out_or_handled_twice(monkeypatch):
    monkeypatch.setattr(titlebar, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(titlebar, 'settings_available', lambda: True)
    monkeypatch.setattr(titlebar, '_hosted_frame', -1)
    monkeypatch.setattr(titlebar, '_pressed_button', 0)
    monkeypatch.setattr(titlebar, '_wm_move_started', True)
    monkeypatch.setattr(titlebar, '_rdrag', {'top_left': False})
    monkeypatch.setattr(Melty, 'frame_count', 42)
    monkeypatch.setattr(Melty, 'on_drag', False)
    window = object()
    monkeypatch.setattr(titlebar, '_studio_window', lambda: window)
    painted, activated = Mock(), Mock()
    monkeypatch.setattr(titlebar, '_paint_buttons', painted)
    monkeypatch.setattr(titlebar, '_activate_button', activated)
    native_platform, native_attribute = Mock(), Mock()
    monkeypatch.setattr(titlebar.glfw, 'get_platform', native_platform)
    monkeypatch.setattr(titlebar.glfw, 'get_window_attrib', native_attribute)
    layout = Mock(wraps=titlebar._button_layout)
    monkeypatch.setattr(titlebar, '_button_layout', layout)
    io = imgui.get_io()
    io.display_size = (800, 600)
    io.mouse_pos = (-100, -100)
    imgui.new_frame()
    state = SimpleNamespace(_bounding_hovered=True, on_action=Mock(return_value=object()))

    titlebar.draw_header_controls(state)
    titlebar.paint_window_controls(imgui.get_window_draw_list())
    titlebar.draw_titlebar(window)

    activated.assert_called_once_with('settings', window)
    assert painted.call_count == layout.call_count == 1
    state.on_action.assert_called_once()
    native_platform.assert_not_called()
    native_attribute.assert_not_called()
    assert titlebar._pressed_button is None
    assert not titlebar._wm_move_started and titlebar._rdrag is None


def test_hosted_linux_titlebar_keeps_geometry_and_resize_registration(monkeypatch):
    monkeypatch.setattr(titlebar, 'sync_decoration', lambda window: True)
    monkeypatch.setattr(titlebar, 'control_kinds', lambda: ((), ('close',)))
    monkeypatch.setattr(titlebar, '_wm_gestures_available', lambda: False)
    monkeypatch.setattr(titlebar, 'drag_anywhere_enabled', lambda: False)
    monkeypatch.setattr(titlebar, '_hosted_frame', 42)
    monkeypatch.setattr(titlebar, '_wm_move_started', False)
    monkeypatch.setattr(titlebar, '_rdrag', None)
    monkeypatch.setattr(titlebar, '_pressed_button', None)
    geometry, region, maximized = Mock(), Mock(), Mock(return_value=False)
    monkeypatch.setattr(titlebar, 'sync_window_geometry', geometry)
    monkeypatch.setattr(titlebar, 'sync_input_region', region)
    monkeypatch.setattr(titlebar, '_maximized', maximized)
    monkeypatch.setattr(titlebar.titlebar_buttons, 'refresh_if_stale', Mock())
    monkeypatch.setattr(titlebar.hypr_left_drag, 'refresh_if_stale', Mock())
    monkeypatch.setattr(Melty, 'frame_count', 42)
    monkeypatch.setattr(Melty, 'events', {})
    register = Mock()
    monkeypatch.setattr(Melty, 'event_handler', SimpleNamespace(register_hovered=register))
    io = imgui.get_io()
    io.display_size = (800, 600)
    io.mouse_pos = (400, 300)
    imgui.new_frame()
    window = object()

    titlebar.draw_titlebar(window)

    geometry.assert_called_once_with(window)
    region.assert_called_once_with(window)
    maximized.assert_called_once_with(window)
    register.assert_called_once_with(titlebar._RESIZE_ID,
        ['non_blocking_right_mouse_dragged', 'non_blocking_right_mouse_double_dragged'],
        priority=titlebar._STRIP_PRIORITY)
