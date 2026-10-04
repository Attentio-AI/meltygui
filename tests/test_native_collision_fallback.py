"""Unsupported native adjustment leaves real window bounds as fixed walls."""
import pytest

from meltygui.core.windowing import geometry_feed, wayland_move
from test_os_frame import studio, hand, app_root, root, os_frame, tb
from test_surface_body_resize import body_frame


@pytest.mark.parametrize('platform,wayland,feed,offset,floating,expected', [
    ('darwin', False, None, False, True, False),
    ('linux', False, None, False, True, True),
    ('linux', True, None, True, True, False),
    ('linux', True, 'gnome', False, True, False),
    ('linux', True, 'gnome', True, True, True),
    ('linux', True, 'hyprland', False, True, True),
    ('linux', True, 'hyprland', True, False, False),
])
def test_native_adjustment_requires_a_supported_control_path(
        monkeypatch, platform, wayland, feed, offset, floating, expected):
    monkeypatch.setattr(tb.sys, 'platform', platform)
    monkeypatch.setattr(tb, '_on_wayland', lambda: wayland)
    monkeypatch.setitem(tb.glfw.__dict__, 'get_platform',
                        lambda: tb.glfw.PLATFORM_WAYLAND if wayland else tb.glfw.PLATFORM_X11)
    monkeypatch.setitem(tb.glfw.__dict__, 'get_window_attrib', lambda w, attr: attr == tb.glfw.RESIZABLE)
    monkeypatch.setattr(tb, '_fullscreen', lambda w: False)
    monkeypatch.setattr(geometry_feed, 'backend', lambda: feed)
    monkeypatch.setattr(geometry_feed, 'ensure_started', lambda: None)
    monkeypatch.setitem(geometry_feed._STATE, 'available', True)
    monkeypatch.setattr(geometry_feed, '_current_frame', lambda: {
        'address': '0x123', 'floating': floating, 'fullscreen': False, 'maximized': False})
    monkeypatch.setattr(wayland_move, 'offset_available', lambda: offset)
    assert tb.can_adjust_window_edges(object()) is expected


def test_a_readable_position_is_not_permission_to_adjust_the_frame(monkeypatch):
    monkeypatch.setattr(tb, '_studio_window', lambda: object())
    monkeypatch.setattr(tb, 'can_adjust_window_edges', lambda w: False)
    monkeypatch.setattr(tb, '_on_wayland', lambda: False)
    monkeypatch.setitem(tb.glfw.__dict__, 'get_window_pos', lambda w: (200, 100))
    monkeypatch.setitem(tb.glfw.__dict__, 'get_window_size', lambda w: (800, 600))
    monkeypatch.setattr(tb, '_workarea_for', lambda w: (0, 0, 1920, 1080))
    assert os_frame._observe() is None


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_fixed_native_bounds_keep_divider_limits_sticky_and_stable(studio, hand, axis):
    studio.mode = None
    studio.size = [1000., 800.]
    window = app_root(studio)
    body_frame(studio, window, 30.)
    pair = window._frame_edges if axis == 'x' else window._frame_rows
    views = window._edge_views if axis == 'x' else window._row_views
    cells = window._edge_cells if axis == 'x' else window._row_cells
    divider = {axis: 300.}
    views[('fallback', axis)] = (window, [pair[0], divider, pair[1]])
    cells[('fallback', axis)] = ([100., 200.], [500., None])
    body_frame(studio, window, 30.)
    previous = 0.
    for travel in (-500., -500., 0., 800., 800., 0.):
        pending = window._pending_drags if axis == 'x' else window._pending_row_drags
        pending.append((divider, divider[axis] + travel - previous, True))
        previous = travel
        body_frame(studio, window, 30.)
        expected = max(100., min(500., 300. + travel))
        assert divider[axis] == pytest.approx(expected)
        assert (window.window_pos, window.width, window.height) == ((0., 30.), 1000., 770.)
        assert studio.requests == []
        for _ in range(3):
            body_frame(studio, window, 30.)
            assert divider[axis] == pytest.approx(expected)
            assert studio.requests == []


@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('index', [0, 1])
def test_native_background_drag_cannot_move_fixed_walls(studio, hand, axis, index):
    studio.mode = None
    window = root(studio, x=100., width=400.)
    before = tuple(e[axis] for e in os_frame.edges(axis))
    for _ in range(4):
        os_frame.queue_drag(axis, index, -100. if index == 0 else 100.)
        studio.frame(window)
        assert tuple(e[axis] for e in os_frame.edges(axis)) == before
        assert studio.requests == []
        assert os_frame._STATE['unapplied'] == [0., 0.]


def test_native_shrink_still_compresses_contained_windows_without_adjustment(studio):
    studio.mode = None
    studio.size = [1000., 800.]
    window = root(studio, x=400., width=400., min_width=200.)
    studio.size[0] = 600.
    studio.frame(window)
    assert window.window_pos[0] + window.width <= 600.
    assert window.width >= 200.
    assert studio.requests == []


@pytest.mark.parametrize('collision', [True, False])
def test_lost_capability_cancels_collision_requests_but_allows_explicit_sizes(
        studio, monkeypatch, collision):
    window = root(studio, x=100., width=400.)
    native = object()
    monkeypatch.setattr(tb, 'can_adjust_window_edges', lambda w: False)
    monkeypatch.setattr(wayland_move, 'clear_surface_offset', lambda: None)
    monkeypatch.setattr(wayland_move, 'set_surface_offset', lambda *args: pytest.fail('unsupported move'))
    monkeypatch.setattr(geometry_feed, 'backend', lambda: None)
    monkeypatch.setattr(tb, '_on_hyprland', lambda: False)
    monkeypatch.setattr(tb, '_pending_surface_size', (900, 700))
    monkeypatch.setattr(tb, '_pending_surface_offset', (-80, -40) if collision else None)
    monkeypatch.setitem(os_frame._STATE, 'pending_surface_collision', collision)
    monkeypatch.setattr(tb, '_pending_surface_fit', False)
    os_frame._STATE['unapplied'] = [-80., -40.]
    os_frame._STATE['unapplied_far'] = [80., 40.]
    sizes = []
    monkeypatch.setattr(tb, 'set_surface_size', lambda w, *args, **kw: sizes.append(args))
    before = window.window_pos
    tb.apply_pending_surface_size(native)
    assert sizes == ([] if collision else [(900, 700)])
    assert window.window_pos == before
    assert os_frame.mode() == 'walls'
    assert os_frame._STATE['unapplied'] == [0., 0.]
    assert os_frame._STATE['unapplied_far'] == [0., 0.]
    assert os_frame._STATE['pin_rebases'] == {}
    assert tb._pending_surface_size is None
