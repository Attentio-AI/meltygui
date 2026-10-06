"""The macOS capability boundary; the existing solver suites cover collision rules."""
import json
import socket
import threading
import tempfile

import pytest
from meltygui.core.windowing import melty_windows as bridge, titlebar, window_api


@pytest.fixture
def socket_directory():
    # Darwin's sockaddr_un path is only 104 bytes; pytest's default TMPDIR is longer.
    with tempfile.TemporaryDirectory(prefix='melty-', dir='/tmp') as directory:
        yield directory


def test_protocol_handshake_and_pause(socket_directory, monkeypatch):
    path = socket_directory + '/surface.sock'
    monkeypatch.setattr(bridge, 'PATH', path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        import os
        os.chmod(path, 0o600)
        server.listen()
        requests = []

        def serve():
            for active in (True, False):
                connection, _ = server.accept()
                with connection:
                    requests.append(json.loads(connection.recv(4096)))
                    payload = json.dumps(dict(version=1, capability='native-edges',
                                              enabled=active, lease_seconds=1)).encode() + b'\n'
                    connection.sendall(payload[:8])
                    connection.sendall(payload[8:])
        worker = threading.Thread(target=serve)
        worker.start()
        assert bridge._exchange({17, 9}) == (True, None)
        assert bridge._exchange({17}) == (False, None)
        worker.join(1)
        assert requests == [dict(version=1, operation='claim', windows=[9, 17]),
                            dict(version=1, operation='claim', windows=[17])]


def test_untrusted_or_absent_endpoint_cannot_enable_geometry(tmp_path, monkeypatch):
    path = tmp_path / 'surface.sock'
    monkeypatch.setattr(bridge, 'PATH', str(path))
    with pytest.raises(OSError):
        bridge._exchange({1})
    path.write_text('not a socket')
    with pytest.raises(OSError):
        bridge._exchange({1})


@pytest.mark.parametrize('active,move,expected', [(True, True, True), (True, False, False), (False, True, False)])
def test_native_move_advertisement_follows_service_and_setting(socket_directory, monkeypatch, active, move, expected):
    import os
    monkeypatch.setattr(bridge, '_STATE', dict(lock=threading.Lock(), move_enabled=False))
    path = socket_directory + '/move.sock'
    monkeypatch.setattr(bridge, 'PATH', path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        os.chmod(path, 0o600)
        server.listen()
        def serve():
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(json.dumps(dict(version=1, capability='native-edges', enabled=active,
                    lease_seconds=1, frame_api=1, frame_library='/helper/MeltySurfaceFrame.dylib',
                    move_api=1, move_enabled=move)).encode() + b'\n')
        worker = threading.Thread(target=serve)
        worker.start()
        result = bridge._exchange({7})
        worker.join(1)
    assert result == (active, '/helper/MeltySurfaceFrame.dylib' if active else None)
    assert bridge._STATE['move_enabled'] is expected


def test_lease_is_per_surface_and_expires(monkeypatch):
    state = dict(lock=threading.Lock(), windows=set(), accepted={7},
                 expires=20., thread=object())
    monkeypatch.setattr(bridge, '_STATE', state)
    monkeypatch.setattr(bridge.sys, 'platform', 'darwin')
    monkeypatch.setattr(bridge, '_window_number', lambda window: window)
    monkeypatch.setattr(bridge, '_native_value', lambda window, selector: 0)
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: 19.)
    assert bridge.available(7)
    assert not bridge.available(8)
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: 20.)
    assert not bridge.available(7)


def test_content_coordinates_reserve_decorations_without_retina_scaling(monkeypatch):
    monkeypatch.setitem(window_api.__dict__, 'get_window_pos', lambda w: (-1000, 80))
    monkeypatch.setitem(window_api.__dict__, 'get_window_size', lambda w: (800, 600))
    monkeypatch.setitem(window_api.__dict__, 'get_window_frame_size', lambda w: (1, 28, 1, 1))
    monkeypatch.setattr(titlebar, '_workarea_for', lambda w: (-1440, 25, 0, 900))
    monkeypatch.setattr(bridge, '_window_number', lambda w: 7)
    assert bridge.observe(object()) == ((-1000., 80.), (-1439, 53, 1438, 846),
                                         'cocoa', (-200., 680.), 7)


def test_position_and_size_apply_before_render_with_original_origin(monkeypatch):
    calls = []
    monkeypatch.setitem(window_api.__dict__, 'get_window_pos', lambda w: (100, 100))
    monkeypatch.setitem(window_api.__dict__, 'set_window_size',
                        lambda w, x, y: calls.append(('size', x, y)))
    monkeypatch.setitem(window_api.__dict__, 'set_window_pos',
                        lambda w, x, y: calls.append(('position', x, y)))
    bridge.apply(object(), 900, 700, (-100, -50))
    assert calls == [('size', 900, 700), ('position', 0, 50)]


def test_cocoa_capability_requires_service_and_eligible_window(monkeypatch):
    monkeypatch.setattr(titlebar.sys, 'platform', 'darwin')
    monkeypatch.setattr(titlebar, '_fullscreen', lambda w: False)
    monkeypatch.setitem(window_api.__dict__, 'get_window_attrib',
                        lambda w, attr: attr == window_api.RESIZABLE)
    monkeypatch.setattr(bridge, 'available', lambda w: True)
    assert titlebar.can_adjust_window_edges(object())
    monkeypatch.setattr(bridge, 'available', lambda w: False)
    assert not titlebar.can_adjust_window_edges(object())
    monkeypatch.setattr(bridge, 'available', lambda w: True)
    monkeypatch.setattr(titlebar, '_fullscreen', lambda w: True)
    assert not titlebar.can_adjust_window_edges(object())


def test_display_selection_uses_logical_workareas_on_mixed_scale_screens(monkeypatch):
    monkeypatch.setitem(window_api.__dict__, 'get_window_pos', lambda w: (1500, 100))
    monkeypatch.setitem(window_api.__dict__, 'get_window_size', lambda w: (500, 400))
    monkeypatch.setitem(window_api.__dict__, 'get_monitors', lambda: [0, 1])
    monkeypatch.setitem(window_api.__dict__, 'get_monitor_workarea',
                        lambda m: [(0, 25, 1440, 875), (1440, 25, 1920, 1055)][m])
    # A 2880-pixel mode on the first Retina display must not make it cover
    # the neighboring display's logical coordinates.
    monkeypatch.setitem(window_api.__dict__, 'get_video_mode',
                        lambda m: pytest.fail('physical video mode used for screen geometry'))
    assert bridge.workarea(object()) == (1440., 25., 3360., 1080.)


def test_native_fullscreen_space_does_not_enable_adjustment(monkeypatch):
    monkeypatch.setattr(bridge.sys, 'platform', 'darwin')
    monkeypatch.setattr(bridge, '_native_value', lambda window, selector: 1 << 14)
    monkeypatch.setattr(bridge, '_window_number', lambda window: pytest.fail('fullscreen window registered'))
    assert not bridge.available(object())


@pytest.mark.parametrize('owned,live,expected', [(True,False,True),(True,True,False),(False,False,False)])
def test_only_cooperative_nonmodal_resizes_defer_refresh(monkeypatch, owned, live, expected):
    monkeypatch.setattr(bridge.sys, 'platform', 'darwin')
    monkeypatch.setattr(bridge, '_window_number', lambda window: 7)
    monkeypatch.setattr(bridge, '_in_live_resize', lambda window: live)
    monkeypatch.setattr(bridge, '_STATE', dict(lock=threading.Lock(), accepted={7} if owned else set(), expires=float('inf')))
    assert bridge.defer_refresh(object()) is expected


def test_native_frame_groups_geometry_until_matching_frame_finishes(monkeypatch):
    from types import SimpleNamespace
    calls = []
    api = SimpleNamespace(
        MeltySurfaceFrameBegin=lambda native: calls.append(('begin', native)) or 99,
        MeltySurfaceFrameSet=lambda *args: calls.append(('set', *args)) or 1,
        MeltySurfaceFrameEnd=lambda token: calls.append(('end', token)))
    monkeypatch.setattr(bridge, 'defer_refresh', lambda window: True)
    monkeypatch.setattr(bridge, '_frame_api', lambda path: api)
    monkeypatch.setattr(bridge, '_STATE', dict(lock=threading.Lock(), frame_library='/helper', frame_tokens={}))
    monkeypatch.setitem(window_api.__dict__, 'get_cocoa_window', lambda window: 7)
    monkeypatch.setitem(window_api.__dict__, 'set_window_size', lambda *args: pytest.fail('intermediate GLFW resize'))
    monkeypatch.setitem(window_api.__dict__, 'set_window_pos', lambda *args: pytest.fail('intermediate GLFW move'))
    frame = bridge.begin_frame(object())
    assert bridge.begin_frame(object()) is None
    bridge.apply(object(), 900, 700, (-100, -50))
    calls.append(('present',))
    bridge.end_frame(frame)
    assert calls == [('begin', 7), ('set', 99, 900., 700., -100., -50.), ('present',), ('end', 99)]
    assert not bridge._STATE['frame_tokens']


def test_missing_native_helper_keeps_older_service_compatible(monkeypatch):
    monkeypatch.setattr(bridge, 'defer_refresh', lambda window: True)
    monkeypatch.setattr(bridge, '_STATE', dict(lock=threading.Lock(), frame_library=None))
    assert bridge.begin_frame(object()) is None
