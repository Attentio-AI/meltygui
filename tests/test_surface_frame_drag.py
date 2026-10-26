"""Native background resize must run even with OS-supplied decorations."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from meltygui.core.windowing import surface_frame
from meltygui.core.rendering import parameter_core


@pytest.mark.parametrize('chrome', [False, True])
def test_surface_polls_resize_before_solving_with_either_decoration(monkeypatch, chrome):
    calls = []
    fake_imgui = MagicMock()
    fake_imgui.get_io.return_value.display_size = (800, 600)
    monkeypatch.setattr(surface_frame, 'imgui', fake_imgui)
    monkeypatch.setattr(surface_frame, 'views', MagicMock())
    monkeypatch.setattr(surface_frame.Melty, 'begin_frame', lambda: None)
    monkeypatch.setattr(surface_frame.Melty, 'imgui_main_window_hovered', False)
    monkeypatch.setattr(parameter_core, 'flush_deferred_writes', lambda: None)
    monkeypatch.setattr(surface_frame.os_frame, 'begin_frame', lambda: calls.append('begin'))
    monkeypatch.setattr(surface_frame.titlebar, 'poll_os_window_drag', lambda: calls.append('drag'))

    class Solved(Exception):
        pass

    def solve():
        calls.append('solve')
        raise Solved

    monkeypatch.setattr(surface_frame.os_frame, 'solve', solve)
    with pytest.raises(Solved):
        surface_frame.draw_surface_frame(SimpleNamespace(), chrome=chrome)
    assert calls == ['begin', 'drag', 'solve']
