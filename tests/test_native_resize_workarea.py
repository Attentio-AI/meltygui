"""Native decoration limits belong to the content resize box, not the monitor."""
import json

import pytest

from meltygui.core.windowing import geometry_feed as feed


@pytest.fixture
def native(monkeypatch):
    window = feed.hyprland_window_info(dict(address='0x123', pid=7, floating=True))
    clock = [100.]
    settings = dict(mode='top', border_size=1, decorate=True)
    calls = []

    def request(command, path):
        calls.append(command)
        if command.startswith('j/getoption'):
            return json.dumps({'str': settings['mode']})
        prop = command.split()[-1]
        return json.dumps({prop: settings[prop]})

    monkeypatch.setitem(feed._STATE, 'hypr_resize_constraints', {})
    monkeypatch.setattr(feed, 'hypr_request', request)
    monkeypatch.setattr(feed.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(feed, 'workarea', lambda: (100., 200., 3840., 2160.))
    monkeypatch.setattr(feed, '_current_frame', lambda: window)
    return window, settings, clock, calls


@pytest.mark.parametrize('mode,border,decorated,insets', [
    ('top', 1, True, (0, 1, 0, 0)),
    ('top', 2, True, (0, 2, 0, 0)),
    ('all', 2, True, (2, 2, 2, 2)),
    ('none', 2, True, (0, 0, 0, 0)),
    ('unsupported', 2, True, (0, 0, 0, 0)),
    ('top', 2, False, (0, 0, 0, 0)),
    ('top', 0, True, (0, 0, 0, 0)),
])
def test_resize_limits_use_reported_border_and_clamp_policy(native, mode, border, decorated, insets):
    window, settings, _, _ = native
    settings.update(mode=mode, border_size=border, decorate=decorated)
    feed._hypr_resize_constraints([window], '/socket')
    left, top, right, bottom = insets
    assert feed.resize_workarea() == (100 + left, 200 + top, 3840 - left - right, 2160 - top - bottom)
    assert feed.workarea() == (100., 200., 3840., 2160.)


@pytest.mark.parametrize('field,value', [('floating', False), ('fullscreen', True),
                                      ('maximized', True), ('xwayland', True)])
def test_unaffected_native_windows_keep_monitor_limits(native, field, value):
    window, _, _, _ = native
    window[field] = value
    feed._hypr_resize_constraints([window], '/socket')
    assert feed.resize_workarea() == feed.workarea()


def test_constraint_queries_are_cached_and_settings_changes_refresh(native):
    window, settings, clock, calls = native
    feed._hypr_resize_constraints([window], '/socket')
    initial = list(calls)
    for _ in range(20):
        feed._hypr_resize_constraints([window], '/socket')
        assert feed.resize_workarea()[1] == 201.
    assert calls == initial
    settings['border_size'] = 2
    clock[0] += 1.
    feed._hypr_resize_constraints([window], '/socket')
    assert feed.resize_workarea()[1] == 202.
    settings['mode'] = 'none'
    clock[0] += 1.
    feed._hypr_resize_constraints([window], '/socket')
    assert feed.resize_workarea() == feed.workarea()


def test_unavailable_optional_query_does_not_invent_a_border(native, monkeypatch):
    window, _, _, _ = native
    def unavailable(command, path):
        raise OSError('unsupported query')
    monkeypatch.setattr(feed, 'hypr_request', unavailable)
    feed._hypr_resize_constraints([window], '/socket')
    assert feed.resize_workarea() == feed.workarea()


def test_resize_limits_are_local_to_each_native_window(native, monkeypatch):
    window, _, _, _ = native
    feed._hypr_resize_constraints([window], '/socket')
    assert feed.resize_workarea()[1] == 201.
    monkeypatch.setattr(feed, '_current_frame', lambda: {'resize_insets': (0, 3, 0, 0)})
    assert feed.resize_workarea()[1] == 203.
    monkeypatch.setattr(feed, '_current_frame', lambda: None)
    assert feed.resize_workarea() == feed.workarea()


@pytest.mark.parametrize('failed_query', ['getoption', 'getprop'])
def test_temporary_metadata_failure_keeps_confirmed_border(native, monkeypatch, failed_query):
    window, _, clock, _ = native
    feed._hypr_resize_constraints([window], '/socket')
    request = feed.hypr_request
    def intermittent(command, path):
        if failed_query in command:
            raise OSError('temporary metadata failure')
        return request(command, path)
    monkeypatch.setattr(feed, 'hypr_request', intermittent)
    clock[0] += 1.
    feed._hypr_resize_constraints([window], '/socket')
    assert feed.resize_workarea()[1] == 201.


def test_poll_publishes_complete_constraints_even_when_geometry_is_unchanged(native, monkeypatch):
    _, settings, clock, _ = native
    monkeypatch.setitem(feed._STATE, 'windows', [])
    monkeypatch.setitem(feed._STATE, 'frame', None)
    monkeypatch.setitem(feed._STATE, 'updates', 0)
    monkeypatch.setattr(feed, '_refresh_workarea', lambda: None)
    request = feed.hypr_request
    previous = feed._STATE['windows']
    def inspect_publication(command, path):
        assert feed._STATE['windows'] is previous
        if command == 'j/clients':
            return json.dumps([dict(address='0x123', pid=7, floating=True, at=[100, 200], size=[800, 600])])
        return request(command, path)
    monkeypatch.setattr(feed, 'hypr_request', inspect_publication)
    feed._hypr_poll_windows(7, '/socket')
    assert feed._STATE['frame']['resize_insets'] == (0, 1, 0, 0)
    previous = feed._STATE['windows']
    settings['border_size'] = 2
    clock[0] += 1.
    feed._hypr_poll_windows(7, '/socket')
    assert feed._STATE['frame']['resize_insets'] == (0, 2, 0, 0)
    assert feed._STATE['updates'] == 2
