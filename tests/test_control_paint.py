"""Overlay hosts can retain control input without baking duplicate pixels."""
from types import SimpleNamespace
from unittest.mock import Mock

from meltygui.core.melty import Melty
from meltygui.view import collection_view, header_view, color_view


def test_flat_button_without_paint_keeps_layout_and_click(monkeypatch):
    dl = Mock()
    monkeypatch.setattr(header_view.imgui, 'calc_text_size', lambda text: SimpleNamespace(x=30, y=16))
    monkeypatch.setattr(header_view.imgui, 'get_mouse_pos', lambda: (10, 20))
    dummy = Mock()
    monkeypatch.setattr(header_view.imgui, 'dummy', dummy)
    monkeypatch.setattr(Melty, 'effect_hook', None)
    ds = SimpleNamespace(on_action=Mock(return_value=object()))
    assert header_view.flat_button('tab', ds, 'tab', width=100, height=40,
                                   pos=(10, 20), hovered=True, draw_list=dl, paint=False)
    dl.assert_not_called()
    assert not dl.method_calls
    dummy.assert_called_once_with(100, 40)
    assert ds.on_action.call_args.kwargs['rect'] == (10, 20, 110, 60)


def test_tint_icon_without_paint_keeps_picker_input(monkeypatch):
    dl = Mock()
    monkeypatch.setattr(collection_view.imgui, 'get_window_draw_list', lambda: dl)
    monkeypatch.setattr(color_view, '_popover_anchor', lambda ds: ds)
    ds = SimpleNamespace(on_action=Mock(return_value=None))
    value = (.2, .3, .4, 1.)
    assert collection_view.draw_tuple_fast(value, ds, 'tint', x=10, y=20,
                                           icon=f'\uf15b', paint=False) == (False, value)
    assert not dl.method_calls
    assert ds.on_action.call_args.kwargs['rect'] == (10, 20, 27, 37)
