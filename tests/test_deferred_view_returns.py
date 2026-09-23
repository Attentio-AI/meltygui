"""Deferred editors must wake the cached view that consumes their result."""
from types import SimpleNamespace

import pytest

import meltygui.core.melty as runtime


@pytest.mark.parametrize('result', [(True, None), (True, (0.2, 0.4, 0.6, 1.0)), (False, None)])
def test_deferred_result_wakes_its_inline_caller(monkeypatch, result):
    calls = []
    caller = SimpleNamespace(_tile_id='cached-file-tabs')
    window = SimpleNamespace(_tile_id='enclosing-window')
    picker = SimpleNamespace(
        closed=False, _bg_depth=0, _bg_stack=None,
        _wrapper=lambda **kwargs: result, _raw_input_value=(0, 0, 0, 1),
        _kwargs={}, unique=1, abs_left=0, abs_top=0,
        _tile_id='picker', name='picker', _parent=caller, parent_window=window,
    )
    monkeypatch.setattr(runtime, 'imgui', SimpleNamespace(set_cursor_screen_pos=lambda pos: None))
    monkeypatch.setattr(runtime, 'request_render', lambda: None)
    monkeypatch.setattr(runtime.Toggles, 'debug_z_depth', False)
    monkeypatch.setattr(runtime.Melty, 'bg_stack', [])
    monkeypatch.setattr(runtime.Melty, 'bg_depth', 0)
    monkeypatch.setattr(runtime.Melty, 'pending_return_values', {})
    monkeypatch.setattr(runtime.Melty, 'cache', SimpleNamespace(
        invalidate_up=lambda key, **kwargs: calls.append((key, kwargs))))

    runtime.Melty.draw(picker)

    assert runtime.Melty.pending_return_values['picker'] == result
    if result[0]:
        assert len(calls) == 1
        assert calls[0][0] == caller._tile_id
        assert calls[0][1]['force'] is True
    else:
        assert not calls
