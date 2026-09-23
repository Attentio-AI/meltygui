"""Closed call sites sleep without dropping a window's final selection."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from meltygui.core.melty import Melty
from meltygui.core.windowing.window_visibility import WindowCallState


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(Melty, 'returned_values', {})
    monkeypatch.setattr(Melty, 'pending_return_values', {})
    return WindowCallState()


def test_unopened_and_closed_windows_skip_renderer(state):
    renderer = Mock(return_value=(False, None, SimpleNamespace(closed=True, _tile_id='dialog')))
    assert not state.needs_call()
    assert state.needs_call(True)
    state.draw(renderer, '/seed', open_requested=True)
    renderer.assert_called_once_with('/seed', open_requested=True, return_extras=True)
    assert not state.needs_call()
    assert state.needs_call(True)


def test_open_window_keeps_receiving_calls_until_closed(state):
    ds = SimpleNamespace(closed=False, _tile_id='dialog')
    renderer = Mock(return_value=(False, None, ds))
    state.draw(renderer)
    assert state.needs_call()
    ds.closed = True
    assert not state.needs_call()


@pytest.mark.parametrize('queue', ['returned_values', 'pending_return_values'])
def test_selection_on_close_is_consumed_before_sleeping(state, queue):
    ds = SimpleNamespace(closed=False, _tile_id='parent-call')
    state.draw(lambda *args, **kwargs: (False, None, ds))
    # The native surface draws with the same state and a different tile id.
    ds._tile_id = 'child-root'
    ds.closed = True
    getattr(Melty, queue)['parent-call'] = (True, '/chosen/file.py')
    assert state.needs_call()
    def deliver(*args, **kwargs):
        changed, value = getattr(Melty, queue).pop('parent-call')
        ds._tile_id = 'parent-call'
        return changed, value, ds
    assert state.draw(deliver) == (True, '/chosen/file.py')
    assert not state.needs_call()
    assert state.needs_call(True)
