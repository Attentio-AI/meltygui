"""Slice controls share one value contract across volume and line views."""
import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.view import control_view, tensor_view


@pytest.mark.parametrize(('inline', 'top'), [(True, 30), (False, 50)])
def test_inline_header_keeps_the_visible_value_row_in_the_input_rect(inline, top):
    from meltygui.state.new_core_model import DrawState
    state = SimpleNamespace(abs_left=5, abs_top=30, width=600, height=24,
                            header_height=20, footer_height=0,
                            _kwargs={'header_same_line': inline})
    assert DrawState.get_content_rect(state) == (5, top, 605, 54)


@pytest.mark.parametrize(('pointer', 'expected'), [(None, 3), (-10, 0), (50, 5), (120, 10)])
def test_integer_slider_uses_injected_events(pointer, expected, monkeypatch):
    monkeypatch.setattr(control_view.imgui, 'get_cursor_screen_pos', lambda: (0, 0))
    monkeypatch.setattr(control_view.imgui, 'get_window_draw_list', Mock())
    monkeypatch.setattr(control_view.imgui, 'calc_text_size', lambda text: (8, 14))
    monkeypatch.setattr(control_view.imgui, 'dummy', Mock())
    for name in ('checkbox_bg', 'checkbox_bg_selected', 'checkbox_text'):
        monkeypatch.setattr(control_view.Tint, name, lambda: (0.3, 0.4, 0.5))
    state = SimpleNamespace(content_width=100, event_rect=Mock())
    event = None if pointer is None else SimpleNamespace(x=pointer)
    changed, value = inspect.unwrap(control_view.draw_int_slider)(
        3, draw_state=state, min_value=0, max_value=10, left_mouse_drag=event)
    assert value == expected
    assert changed is (pointer is not None)
    assert isinstance(value, int)


def test_slice_edit_preserves_other_dimensions_and_returns_a_tuple(monkeypatch):
    edits = Mock(side_effect=[(False, 1), (True, 6)])
    monkeypatch.setattr(control_view, 'draw_int_slider', edits)
    original = (1, 2, 3, 4)
    changed, value = inspect.unwrap(tensor_view.draw_tensor_slices)(
        original, slider_dims=(0, 3), dim_names=('batch', 'y', 'x', 'channel'),
        source_shape=(2, 10, 10, 8), draw_state=SimpleNamespace(content_width=200))
    assert changed and value == (1, 2, 3, 6)
    assert original == (1, 2, 3, 4)
    assert edits.call_args_list[0].kwargs['max_value'] == 1
    assert edits.call_args_list[1].kwargs['max_value'] == 7


def test_slice_helper_writes_through_the_local_parameter_interface(monkeypatch):
    monkeypatch.setattr(tensor_view, 'draw_tensor_slices', Mock(return_value=(True, (0, 4))))
    parent = SimpleNamespace()
    tensor_view._draw_slice_sliders(parent, (1,), ('row', 'column'), (0, 2), (8, 8), 300)
    assert parent.locate_slices == (0, 4)


def test_account_field_edits_only_its_supplied_store(monkeypatch):
    from meltygui.view import account_view, text_view
    monkeypatch.setattr(account_view.imgui, 'set_cursor_screen_pos', Mock())
    monkeypatch.setattr(text_view, 'draw_text', Mock(return_value=(True, ' changed ')))
    store = SimpleNamespace(set_field=Mock())
    account = {'id': 'local', 'name': 'before'}
    assert account_view._draw_field(account, SimpleNamespace(name='name'), 0, 0, 100, 22, store=store)
    store.set_field.assert_called_once_with('local', 'name', 'changed')
