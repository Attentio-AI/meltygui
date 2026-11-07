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
    monkeypatch.setattr(Melty, '_last_input_time', 0.0)
    monkeypatch.setattr(Melty, '_last_presence_time', 0.0)
    monkeypatch.setattr('meltygui.core.windowing.glfw_utils.request_render', lambda: None)
    set_button_probe(None)

    def send(kind, x=20.0, y=80.0, dt=1/60, velocity=None):
        clock[0] += dt
        handler.feed_move(x, y)
        handler.feed_touch(kind, x, y, t=clock[0], velocity=velocity)

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


def test_momentum_counts_as_activity_until_the_last_scroll_step(gesture, monkeypatch):
    handler, clock, send, text = gesture
    monkeypatch.setattr('meltygui.core.input.input_handler.time.monotonic', lambda: clock[0])
    monkeypatch.setattr(Melty, '_last_input_time', 0.0)
    monkeypatch.setattr(Melty, '_last_presence_time', 0.0)
    monkeypatch.setattr(Melty, '_last_key_time', 7.0)
    text()
    send('begin')
    handler.process_frame()
    handler.begin_frame()
    send('move', y=50)
    send('end', y=50, velocity=(0, -240))
    handler.process_frame()
    moving_frames = 0
    for _ in range(480):
        handler.begin_frame()
        clock[0] += 1 / 120
        events = handler.process_frame()[0]
        if 'scroll' not in events:
            break
        moving_frames += 1
        # Idle refresh/parsing must stay deferred beyond its 0.5 s quiet gate.
        assert Melty._last_input_time == clock[0]
        assert Melty._last_presence_time == clock[0]
        assert Melty._last_key_time == 7.0  # Scrolling is not typing.
    assert 60 < moving_frames < 479
    last_activity = Melty._last_input_time
    for _ in range(10):
        handler.begin_frame()
        clock[0] += .1
        assert not handler.process_frame()[0]
        assert Melty._last_input_time == last_activity


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


@pytest.mark.parametrize('after_scroll', [False, True])
def test_explicit_handle_keeps_mouse_drag_in_scroll_area(gesture, after_scroll):
    handler, _, send, text = gesture
    if after_scroll:
        flick(gesture)
    text()
    handler.register_hovered('handle', ['left_mouse_down', 'left_mouse_drag'], priority=-10)
    send('begin')
    assert 'left_mouse_down' in handler.process_frame()[0]['handle']
    handler.begin_frame()
    send('move', y=40)
    result = handler.process_frame()[0]
    assert 'left_mouse_drag' in result['handle']
    assert 'scroll' not in result


def test_scroll_priority_beats_background_row_drag(gesture):
    handler, _, send, _ = gesture
    handler.register_hovered('scroll', ['touch_scroll_changed'], priority=0)
    handler.register_hovered('row', ['left_mouse_drag', 'left_mouse_drag_released'], priority=10)
    send('begin')
    assert not handler.process_frame()[0]
    handler.begin_frame()
    send('move', y=40)
    result = handler.process_frame()[0]
    assert result['scroll']['touch_scroll_changed'].value == -40
    assert 'row' not in result
    assert not handler.is_down('left_mouse')


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


def test_diagonal_gesture_locks_vertical_and_removes_horizontal_jitter(gesture):
    handler, _, send, text = gesture
    text()
    handler.register_hovered('text', ['touch_scroll_x_changed'], priority=0, tile_id='text-tile')
    send('begin', x=100)
    handler.process_frame()
    handler.begin_frame()
    send('move', x=70.25, y=60.5)
    result = handler.process_frame()[0]
    assert set(result) == {'scroll'}
    assert result['scroll']['touch_scroll_changed'].value == -19.5


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


def flick(gesture, speed=-1500.0):
    handler, clock, send, text = gesture
    text()
    send('begin')
    send('move', y=40)
    send('end', y=40, velocity=(0.0, speed))
    result = handler.process_frame()[0]
    handler.begin_frame()
    return result


@pytest.mark.parametrize('fps', [60, 80, 120])
def test_native_flick_has_apple_projected_distance_at_every_frame_rate(gesture, fps):
    from meltygui.core.input.touch_input import flick_velocity, FLICK_PROJECTION
    handler, clock, _, _ = gesture
    flick(gesture, -2400.0)
    distance = 0.0
    for _ in range(10 * fps):
        clock[0] += 1 / fps
        result = handler.process_frame()[0]
        if 'scroll' in result:
            distance += result['scroll']['touch_scroll_changed'].value
        handler.begin_frame()
    assert distance == pytest.approx(flick_velocity(-2400) * FLICK_PROJECTION)
    assert not handler._touch_input.has_momentum(clock[0])


@pytest.mark.parametrize('speed,coasts', [(0, False), (99.999, False), (100, True),
                                         (-100, True), (float('nan'), False)])
def test_deliberate_native_scroll_release_threshold(gesture, speed, coasts):
    flick(gesture, speed)
    assert bool(gesture[0]._touch_input.velocity) is coasts


def test_native_velocity_ignores_noisy_lift_off_sample(gesture):
    handler, _, send, text = gesture
    text()
    send('begin')
    send('move', y=20)
    send('end', y=21, dt=.00001, velocity=(0, -500))
    handler.process_frame()
    assert handler._touch_input.velocity == -500


@pytest.mark.parametrize('delay', [0.0, 0.05, 0.15])
@pytest.mark.parametrize('fps', [60, 120])
def test_delayed_release_starts_coasting_without_a_catch_up_jump(gesture, delay, fps):
    import math
    from meltygui.core.input.touch_input import FLICK_DECAY, FLICK_PROJECTION

    handler, clock, send, text = gesture
    text()
    send('begin')
    send('move', y=40)
    handler.process_frame()
    handler.begin_frame()
    send('end', y=38, velocity=(0, -1000))
    clock[0] += delay
    released = handler.process_frame()[0]
    # Render the final finger movement, without replaying queued time as inertia.
    assert released['scroll']['touch_scroll_changed'].value == pytest.approx(-2)
    assert handler._touch_input.velocity == -1000
    handler.begin_frame()
    clock[0] += 1 / fps
    coast = handler.process_frame()[0]['scroll']['touch_scroll_changed'].value
    assert coast == pytest.approx(-1000 * (1 - math.exp(-FLICK_DECAY / fps)) * FLICK_PROJECTION)


def test_batched_second_flick_uses_native_gap_for_carried_velocity(gesture):
    import math
    from meltygui.core.input.touch_input import FLICK_DECAY, flick_velocity

    handler, clock, send, text = gesture
    text()
    send('begin')
    send('move', y=40)
    send('end', y=40, velocity=(0, -1800))
    send('begin', y=40, dt=.15)
    clock[0] += .05  # Both gestures arrive in one delayed frame.
    handler.process_frame()
    assert handler._touch_input.carry[1][1] == pytest.approx(
        flick_velocity(-1800) * math.exp(-FLICK_DECAY * .15))
    assert handler._touch_input.velocity == 0  # The new contact stops visible coasting.


def test_flick_gain_is_smooth_and_has_no_speed_ceiling(gesture):
    from meltygui.core.input.touch_input import flick_velocity
    assert flick_velocity(1200) == 1200
    assert flick_velocity(4000) == 9000
    assert flick_velocity(8000) == 18000
    assert flick_velocity(-8000) == -18000
    flick(gesture, -8000)
    assert gesture[0]._touch_input.velocity == -18000


def test_horizontal_intent_locks_without_later_axis_switch(gesture):
    handler, _, send, text = gesture
    text()
    handler.register_hovered('horizontal', ['touch_scroll_x_changed'], priority=0)
    send('begin', x=100)
    send('move', x=80, y=76)
    send('move', x=70, y=20)
    send('end', x=70, y=20, velocity=(-500, -5000))
    result = handler.process_frame()[0]
    assert set(result) == {'horizontal'}
    assert result['horizontal']['touch_scroll_x_changed'].value == -30
    assert handler._touch_input.velocity == 0
    assert handler._touch_input.velocity_x == -500


@pytest.mark.parametrize('speed', [-1500, -1])
@pytest.mark.parametrize('target', ['text', 'button', 'handle', 'background'])
def test_first_tap_after_scroll_activates_and_stops_momentum(gesture, target, speed):
    handler, _, send, text = gesture
    flick(gesture)
    handler._touch_input.velocity = speed  # Include a barely visible coast tail.
    if target in ('text', 'button', 'handle'):
        text()
    if target == 'button':
        handler.register_hovered(target, ['left_mouse_down', 'left_mouse_clicked'], priority=-10)
    elif target == 'handle':
        handler.register_hovered(target, ['left_mouse_down', 'left_mouse_drag'], priority=-10)
    elif target == 'background':
        handler.register_hovered(target, ['left_mouse_down'], priority=-20, blocker=True)
    send('begin')
    send('end')
    result = handler.process_frame()[0]
    expected = 'touch_clicked' if target == 'text' else 'left_mouse_down'
    assert expected in result[target]
    if target == 'button':
        assert 'left_mouse_clicked' in result[target]
    assert 'scroll' not in result
    assert handler._touch_input.velocity == handler._touch_input.velocity_x == 0
    assert not handler.is_down('left_mouse')
    handler.begin_frame()
    assert not handler.process_frame()[0]  # No delayed or duplicate activation.


@pytest.mark.parametrize('hold,speed,other_pane,accelerates', [
    (.1, -1000, False, True), (.36, -1000, False, False),
    (.1, 1000, False, False), (.1, -799, False, False),
    (.1, -99, False, False), (.1, -1000, True, False)])
def test_repeat_flick_carry_requires_quick_same_direction_same_pane(gesture, hold, speed, other_pane, accelerates):
    from meltygui.core.input.touch_input import flick_velocity
    handler, clock, send, text = gesture
    flick(gesture, -2000)
    text()
    if other_pane:
        handler.register_hovered('other', ['touch_scroll_changed'], priority=-10)
    send('begin')
    assert not handler.process_frame()[0]  # remaining coast stops under the finger
    handler.begin_frame()
    send('move', y=40, dt=hold / 2)
    send('end', y=40, dt=hold / 2, velocity=(0, speed))
    handler.process_frame()
    baseline = flick_velocity(speed) if abs(speed) >= 100 else 0
    if accelerates:
        assert abs(handler._touch_input.velocity) > abs(baseline)
    else:
        assert handler._touch_input.velocity == baseline


def test_expired_coast_does_not_swallow_next_tap(gesture):
    handler, _, send, text = gesture
    flick(gesture)
    text()
    send('begin', dt=10)
    send('end')
    assert 'touch_clicked' in handler.process_frame()[0]['text']


def test_flick_and_activation_tap_batched_before_dispatch(gesture):
    handler, _, send, text = gesture
    text()
    send('begin')
    send('move', y=40)
    send('end', y=40, velocity=(0, -1800))
    send('begin', y=40)
    send('end', y=40)
    result = handler.process_frame()[0]
    assert set(result) == {'scroll', 'text'}
    assert 'touch_clicked' in result['text']
    assert handler._touch_input.velocity == 0


def test_external_host_owns_touch_without_disabling_mouse(monkeypatch):
    from types import SimpleNamespace
    from meltygui.core.windowing import window_api
    handler = InputHandler()
    monkeypatch.setitem(window_api._state, 'backend', SimpleNamespace(owns_touch_input=False))
    handler.feed_touch('begin', 10, 20)
    assert handler._touch_input is None
    handler.feed_move(30, 40)
    assert handler.cursor() == (30, 40)
    monkeypatch.setitem(window_api._state, 'backend', None)
    handler.feed_touch('begin', 10, 20)
    assert handler._touch_input is not None
    monkeypatch.setitem(window_api._state, 'backend', SimpleNamespace(owns_touch_input=False))
    handler.feed_touch('move', 15, 25)
    assert handler._touch_input is None


def test_external_host_supplies_opengl_context_version(monkeypatch):
    from types import SimpleNamespace
    from meltygui.core.windowing import window_api, glfw_utils
    from unittest.mock import Mock
    monkeypatch.setitem(window_api._state, 'backend', SimpleNamespace(opengl_version=(3, 3)))
    hint = Mock()
    monkeypatch.setattr(window_api, 'window_hint', hint, raising=False)
    glfw_utils.apply_opengl_context_hints()
    hint.assert_any_call(window_api.CONTEXT_VERSION_MAJOR, 3)
    hint.assert_any_call(window_api.CONTEXT_VERSION_MINOR, 3)
