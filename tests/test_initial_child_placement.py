"""Initial placement snaps; later parent following keeps normal movement."""
from types import SimpleNamespace

from meltygui.core.runtime import app
from meltygui.core.windowing import geometry_feed as feed, window_api, titlebar


def test_initial_placement_requests_commit_and_later_move_does_not(monkeypatch):
    rects = {'parent': (100, 200, 900, 800), 'child': (0, 0, 720, 640)}
    calls, frames = [], []
    req = SimpleNamespace(window_pos=(30, 40), pinned=False)
    child = SimpleNamespace(request=req, window=object(), title='child',
                            toplevel=None, seen_rect=None, await_ack=False,
                            last_sent_rect=None, activate=lambda: None,
                            request_frame=lambda: frames.append(True))
    parent = SimpleNamespace(title='parent', children=[child], seen_rect=None)
    monkeypatch.setattr(feed, 'surface_rect', rects.get)
    monkeypatch.setattr(feed, 'hypr_honors_geometry', lambda: True)
    monkeypatch.setattr(titlebar, 'window_inset', lambda: 10)
    monkeypatch.setattr(window_api, 'get_window_size', lambda _: (740, 660))
    def place(title, target, **kwargs):
        calls.append((title, target, kwargs))
        return True
    monkeypatch.setattr(feed, 'place_window', place)
    app._present_children(parent)
    assert calls == [('child', (130, 240, 720, 640), {'resize': False, 'initial': True})]
    assert frames == [True]
    rects['child'] = (130, 240, 720, 640)
    app._present_children(parent)
    assert not child.await_ack
    assert len(calls) == 1
    rects['parent'] = (200, 300, 900, 800)
    app._present_children(parent)
    assert calls[-1] == ('child', (230, 340, 720, 640), {'resize': False, 'initial': False})
    assert frames == [True]


def test_initial_commit_capability_guard_and_normal_move(monkeypatch):
    scripts = []
    monkeypatch.setattr(feed, 'backend', lambda: 'hyprland')
    monkeypatch.setattr(feed, '_window_by_title', lambda _: {'address': '0x123'})
    monkeypatch.setattr(feed, 'hypr_config_is_lua', lambda: True)
    monkeypatch.setattr(feed, 'hypr_honors_geometry', lambda: True)
    monkeypatch.setattr(feed, '_hypr_eval', lambda script, _: scripts.append(script) or True)
    assert feed.place_window('child', (130, 240, 720, 640), resize=False, initial=True)
    assert 'if hl.dsp.window.resize_on_commit and not w.xwayland' in scripts[-1]
    assert 'width = 720, height = 640, dx = 130 - p.x, dy = 240 - p.y' in scripts[-1]
    assert feed.place_window('child', (230, 340, 720, 640), resize=False)
    assert 'resize_on_commit' not in scripts[-1]
    assert 'window.move' in scripts[-1]
