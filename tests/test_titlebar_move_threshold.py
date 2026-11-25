"""The OS window move starts on the first pointer motion of a press the strip owns."""
from meltygui.core.input.input_handler import InputHandler
from meltygui.core.windowing import titlebar
from meltygui.core.windowing.titlebar import _STRIP_ID, _STRIP_PRIORITY, _move_dragged


def press(handler, claimed=False):
    handler.register_hovered(_STRIP_ID, ['non_blocking_left_mouse_dragged'], priority=_STRIP_PRIORITY)
    if claimed:
        handler.register_hovered('view', ['left_mouse_drag'], priority=0)
    handler.feed_move(200, 200)
    handler.feed_down('left_mouse', x=200, y=200)
    handler.process_frame()


def test_still_press_is_not_a_move():
    handler = InputHandler()
    press(handler)
    assert not _move_dragged(handler, {})


def test_sub_pixel_motion_moves():
    handler = InputHandler()
    press(handler)
    handler.feed_move(200.5, 200)
    assert titlebar._MOVE_THRESHOLD == 0.0
    assert _move_dragged(handler, {})


def test_view_that_claims_the_drag_keeps_it():
    handler = InputHandler()
    press(handler, claimed=True)
    handler.feed_move(260, 200)
    assert not _move_dragged(handler, {})


def test_released_press_does_not_move():
    handler = InputHandler()
    press(handler)
    handler.feed_move(201, 200)
    handler.feed_up('left_mouse', x=201, y=200)
    handler.process_frame()
    assert not _move_dragged(handler, {})
