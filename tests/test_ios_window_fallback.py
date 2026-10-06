"""Native dialog requests fall back even before the iOS renderer exists."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.core import core_render
from meltygui.core.melty import Melty
from meltygui.state.new_core_model import DrawState


@pytest.mark.parametrize('platform,renderer,managed', [
    ('ios', None, True),
    ('ios', object(), True),
    ('darwin', None, False),
    ('linux', None, False),
    ('darwin', object(), True),
])
def test_native_dialog_backend_selection(monkeypatch, platform, renderer, managed):
    monkeypatch.setattr(core_render, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(Melty, 'graphics_backend', renderer)
    monkeypatch.setattr(Melty, 'root_draw_states', {})
    monkeypatch.setattr(Melty, 'returned_values', {})
    monkeypatch.setattr(Melty, 'silence_invalidate', False)
    request = Mock()
    monkeypatch.setattr(Melty, 'surface_window_request', request)
    monkeypatch.setattr(Melty, 'record_surface_request', Mock())

    @core_render.render_func(glfw_window=True)
    def dialog(input_value):
        pytest.fail('A closed dialog must not draw its body')

    state = DrawState()
    result = dialog('value', draw_state=state, _converter_mode=True,
                    unmanaged=True, open_requested=False, return_extras=True)
    assert result == (False, None, state)
    assert state._window_visibility_initialized is managed
    if managed:
        assert state.closed
        request.assert_not_called()
    else:
        request.assert_called_once()
