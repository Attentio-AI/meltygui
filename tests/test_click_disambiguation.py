"""Context clicks wait only for competing double gestures, not resize fallbacks."""
import pytest

from meltygui.core.input import input_handler
from meltygui.core.input.input_handler import InputHandler, DOUBLE_CLICK_WINDOW
from meltygui.core.melty import Melty


@pytest.fixture
def pointer(monkeypatch):
    clock = [10.0]
    position = [100.0, 100.0]
    monkeypatch.setattr(input_handler.time, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(Melty, 'get_latest_mouse', lambda: tuple(position))
    handler = InputHandler()
    return handler, clock, position


def frame(pointer, double_priority):
    handler, _, _ = pointer
    handler.begin_frame()
    handler.register_hovered('menu', ['right_mouse_clicked'], priority=0)
    if double_priority is not None:
        handler.register_hovered('gesture', ['double_right_mouse_drag'], priority=double_priority)


def click(pointer):
    handler, clock, _ = pointer
    handler.feed_down('right_mouse', 100, 100)
    handler.process_frame()[0]
    handler._pending.clear()
    clock[0] += .03
    handler.feed_up('right_mouse', 100, 100)
    return handler.process_frame()[0]


@pytest.mark.parametrize('priority', [None, 1, 100])
def test_context_click_is_immediate_without_competing_double(pointer, priority):
    frame(pointer, priority)
    result = click(pointer)
    assert 'right_mouse_clicked' in result['menu']
    assert not pointer[0]._pending_clicks


@pytest.mark.parametrize('priority', [0, -3])
def test_single_click_waits_only_until_double_deadline(pointer, priority):
    handler, clock, _ = pointer
    frame(pointer, priority)
    assert 'menu' not in click(pointer)
    frame(pointer, priority)
    clock[0] += DOUBLE_CLICK_WINDOW - .001
    assert 'menu' not in handler.process_frame()[0]
    clock[0] += .002
    assert 'right_mouse_clicked' in handler.process_frame()[0]['menu']
    assert not handler._pending_clicks


def test_double_drag_cancels_menu_even_when_second_press_is_held(pointer):
    handler, clock, position = pointer
    frame(pointer, -3)
    assert 'menu' not in click(pointer)
    frame(pointer, -3)
    clock[0] += .05
    handler.feed_down('right_mouse', 100, 100)
    assert 'menu' not in handler.process_frame()[0]
    frame(pointer, -3)
    clock[0] += DOUBLE_CLICK_WINDOW + .1
    assert 'menu' not in handler.process_frame()[0]
    position[:] = [130, 120]
    handler.feed_move(*position)
    result = handler.process_frame()[0]
    assert 'double_right_mouse_drag' in result['gesture']
    assert 'menu' not in result
    assert not handler._pending_clicks
    frame(pointer, -3)
    handler.feed_up('right_mouse', *position)
    assert 'menu' not in handler.process_frame()[0]


def test_double_click_without_drag_delivers_one_menu_click(pointer):
    handler, clock, _ = pointer
    frame(pointer, -3)
    assert 'menu' not in click(pointer)
    frame(pointer, -3)
    clock[0] += .05
    assert 'right_mouse_clicked' in click(pointer)['menu']
    frame(pointer, -3)
    clock[0] += DOUBLE_CLICK_WINDOW + .1
    assert 'menu' not in handler.process_frame()[0]


def test_declared_double_drag_and_menu_on_same_view(pointer):
    handler, _, _ = pointer
    frame(pointer, None)
    handler.register_hovered('menu', ['double_right_mouse_drag'], priority=-3)
    assert 'menu' not in click(pointer)
    assert 'right_mouse' in handler._pending_clicks
