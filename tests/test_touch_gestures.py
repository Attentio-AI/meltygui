"""Touch arbitration through the real cached-view dispatcher, without a GPU."""
import pytest

from meltygui.core.input.input_handler import InputHandler, set_button_probe
from meltygui.core.melty import Melty


@pytest.fixture
def gesture(monkeypatch):
    handler = InputHandler()
    clock = [10.0]
    monkeypatch.setattr('meltygui.core.input.input_handler.time.perf_counter', lambda: clock[0])
    monkeypatch.setattr(Melty, 'get_latest_mouse', lambda: handler.cursor())
    monkeypatch.setattr('meltygui.core.windowing.glfw_utils.request_render', lambda: None)
    set_button_probe(None)

    def send(kind, x=20.0, y=80.0, dt=1/60):
        clock[0] += dt
        handler.feed_move(x, y)
        handler.feed_touch(kind, x, y, t=clock[0])

    def text():
        handler.register_hovered('text', ['left_mouse_down', 'left_mouse_drag', 'touch_clicked'],
                                 priority=0, tile_id='text-tile')
        handler.register_hovered('scroll', ['touch_scroll_changed', 'scroll_y_changed'],
                                 priority=10, tile_id='text-tile')

    return handler, clock, send, text


def test_text_waits_for_completed_tap_with_finger_jitter(gesture):
    handler, _, send, text = gesture
    text()
    send('begin')
    assert not handler.process_frame()[0]
    handler.begin_frame()
    send('move', y=76)
    assert not handler.process_frame()[0]
    handler.begin_frame()
    send('end', y=76)
    event = handler.process_frame()[0]['text']['touch_clicked']
    assert event.tile_id == 'text-tile'
    assert event.y == 76


def test_scroll_captures_starting_pane_and_preserves_fractional_motion(gesture):
    handler, _, send, text = gesture
    text()
    send('begin')
    handler.process_frame()
    handler.begin_frame()
    handler.register_hovered('other', ['touch_scroll_changed'], priority=-20)
    send('move', y=65.25)
    send('move', y=50.5)
    result = handler.process_frame()[0]
    assert set(result) == {'scroll'}
    assert result['scroll']['touch_scroll_changed'].value == -29.5
    handler.begin_frame()
    send('end', y=45.25)
    result = handler.process_frame()[0]
    assert result['scroll']['touch_scroll_changed'].value == -5.25
    assert 'text' not in result


def test_out_and_back_swipe_in_one_frame_never_becomes_tap(gesture):
    handler, _, send, text = gesture
    text()
    send('begin')
    send('move', y=20)
    send('end', y=80)
    result = handler.process_frame()[0]
    assert 'text' not in result
    assert not handler.is_down('left_mouse')


def test_flick_coasts_and_new_contact_stops_it(gesture):
    handler, clock, send, text = gesture
    text()
    send('begin')
    handler.process_frame()
    handler.begin_frame()
    send('move', y=50)
    send('end', y=50)
    handler.process_frame()
    handler.begin_frame()
    clock[0] += 1/60
    assert handler.process_frame()[0]['scroll']['touch_scroll_changed'].value < 0
    handler.begin_frame()
    text()
    send('begin', y=50)
    assert not handler.process_frame()[0]
    handler.begin_frame()
    clock[0] += 1/60
    assert not handler.process_frame()[0]


def test_pause_before_lift_and_cancel_stop_momentum(gesture):
    handler, clock, send, text = gesture
    text()
    send('begin')
    send('move', y=50)
    send('end', y=50, dt=.2)
    handler.process_frame()
    handler.begin_frame()
    clock[0] += 1/60
    assert not handler.process_frame()[0]
    text()
    send('begin')
    send('move', y=50)
    send('end', y=50)
    handler.process_frame()
    handler.begin_frame()
    handler.feed_touch('cancel')
    clock[0] += 1/60
    assert not handler.process_frame()[0]


def test_explicit_handle_keeps_mouse_drag_in_scroll_area(gesture):
    handler, _, send, text = gesture
    text()
    handler.register_hovered('handle', ['left_mouse_down', 'left_mouse_drag'], priority=-10)
    send('begin')
    assert 'left_mouse_down' in handler.process_frame()[0]['handle']
    handler.begin_frame()
    send('move', y=40)
    result = handler.process_frame()[0]
    assert 'left_mouse_drag' in result['handle']
    assert 'scroll' not in result


def test_front_window_blocks_touch_scroll_behind_it(gesture):
    handler, _, send, text = gesture
    text()
    handler.register_hovered('front', [], priority=-20, tile_id='front', blocker=True)
    send('begin')
    send('move', y=30)
    send('end', y=30)
    assert not handler.process_frame()[0]


def test_mouse_still_focuses_on_press_and_wheel_stays_in_notches(gesture):
    handler, _, _, text = gesture
    text()
    handler.feed_down('left_mouse', 20, 80)
    handler.feed_change('scroll_y', 1)
    result = handler.process_frame()[0]
    assert 'left_mouse_down' in result['text']
    assert result['scroll']['scroll_y_changed'].value == 1


def test_diagonal_gesture_sends_each_axis_to_its_captured_consumer(gesture):
    handler, _, send, text = gesture
    text()
    handler.register_hovered('text', ['touch_scroll_x_changed'], priority=0, tile_id='text-tile')
    send('begin', x=100)
    handler.process_frame()
    handler.begin_frame()
    send('move', x=70.25, y=60.5)
    result = handler.process_frame()[0]
    assert result['text']['touch_scroll_x_changed'].value == -29.75
    assert result['scroll']['touch_scroll_changed'].value == -19.5
    assert 'left_mouse_drag' not in result['text']


def test_focus_observers_wait_for_tap_and_do_not_block_scroll(gesture):
    handler, _, send, text = gesture
    text()
    handler.register_hovered('clear_focus', ['non_blocking_left_mouse_down'], priority=-512)
    send('begin')
    assert not handler.process_frame()[0]
    handler.begin_frame()
    send('end')
    result = handler.process_frame()[0]
    assert 'touch_clicked' in result['text']
    assert 'non_blocking_left_mouse_down' in result['clear_focus']


def test_nested_scroll_uses_innermost_registered_pane(gesture):
    handler, _, send, text = gesture
    text()
    handler.register_hovered('inner', ['touch_scroll_changed'], priority=-10, tile_id='inner')
    send('begin')
    handler.process_frame()
    handler.begin_frame()
    send('move', y=40)
    result = handler.process_frame()[0]
    assert set(result) == {'inner'}
    assert result['inner']['touch_scroll_changed'].value == -40


def test_swipe_over_a_text_button_scrolls_instead_of_selecting_text(gesture):
    handler, _, send, text = gesture
    text()
    handler.register_hovered('button', ['left_mouse_down', 'left_mouse_clicked'], priority=-5)
    send('begin')
    assert not handler.process_frame()[0]
    handler.begin_frame()
    send('move', y=40)
    result = handler.process_frame()[0]
    assert set(result) == {'scroll'}
    assert result['scroll']['touch_scroll_changed'].value == -40
