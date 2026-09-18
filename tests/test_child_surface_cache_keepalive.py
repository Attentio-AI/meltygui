"""A child OS window (draw_x(glfw_window=True)) requested from a view body
survives blit-cache hits of that body: the app loop closes a child whose
request was not refreshed in a tick, and a cache hit skips the body that
refreshes it. The wrapper records the request on the enclosing views'
per-frame record; the cache-hit path replays it (the project card's
environment picker flickered open/closed with every hover frame)."""
from types import SimpleNamespace

import pytest

from meltygui.core.melty import Melty
from meltygui.core.runtime import app
from meltygui.state.new_core_model import DrawState


@pytest.fixture
def surfaces(monkeypatch):
    monkeypatch.setattr(Melty, 'surface_windows', {})
    monkeypatch.setattr(Melty, 'surface_requests', [])
    monkeypatch.setattr(Melty, 'draw_state_stack', [])
    monkeypatch.setattr(Melty, 'app_tick', 10)
    monkeypatch.setattr(Melty, 'frame_count', 100)


def _request(ds, name='picker'):
    kwargs = {'window_size': (720, 640), 'open_requested': True}
    return Melty.surface_window_request(name, name, None, kwargs, ds)


def test_request_recorded_on_enclosing_body_records(surfaces):
    outer, inner = DrawState(), DrawState()
    outer._body_surface_requests = (Melty.frame_count, [])
    inner._body_surface_requests = (Melty.frame_count - 1, [])   # not this frame's run
    Melty.draw_state_stack[:] = [outer, inner]
    req = _request(DrawState())
    Melty.record_surface_request(req)
    Melty.record_surface_request(req)
    assert outer._body_surface_requests[1] == [req]
    assert inner._body_surface_requests[1] == []


def test_cache_hit_replay_keeps_child_alive(surfaces):
    card = DrawState()
    card._body_surface_requests = (Melty.frame_count, [])
    Melty.draw_state_stack[:] = [card]
    req = _request(DrawState())
    Melty.record_surface_request(req)
    child = SimpleNamespace(closed=False, stale=False, request=req)
    req.surface = child
    # Next tick: the card's body is a blit-cache hit, only the replay runs.
    Melty.app_tick += 1
    card.replay_surface_requests()
    app._close_stale_children()
    assert child.closed is False
    assert req.tick == Melty.app_tick


def test_no_replay_closes_stale_child(surfaces):
    card = DrawState()
    card._body_surface_requests = (Melty.frame_count, [])
    Melty.draw_state_stack[:] = [card]
    req = _request(DrawState())
    Melty.record_surface_request(req)
    child = SimpleNamespace(closed=False, stale=False, request=req)
    req.surface = child
    Melty.app_tick += 1
    app._close_stale_children()          # the card stopped drawing entirely
    assert child.closed is True and child.stale is True


def test_replay_skips_closed_or_replaced_requests(surfaces):
    card = DrawState()
    card._body_surface_requests = (Melty.frame_count, [])
    Melty.draw_state_stack[:] = [card]
    req = _request(DrawState())
    Melty.record_surface_request(req)
    req.closed = True
    Melty.app_tick += 1
    card.replay_surface_requests()
    assert req.tick == Melty.app_tick - 1
    req.closed = False
    Melty.surface_windows['picker'] = SimpleNamespace(tile_id='picker', closed=False, tick=-1)
    card.replay_surface_requests()
    assert req.tick == Melty.app_tick - 1
