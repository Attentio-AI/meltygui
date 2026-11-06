"""Native single-view input contracts, without UIKit, GLFW or a GPU context."""
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import sys

import meltygui_imgui as imgui
import pytest

from meltygui.core.input.input_handler import InputHandler
from meltygui.core.input.ios_input import IOSInput
from meltygui.core.windowing import window_api, window_constants as codes
from meltygui.core.windowing.backends.native_ios import Backend


class Host:
    def __init__(self):
        self.frames = 0
        self.keyboard = []
        self.clipboard = ''

    def request_frame(self):
        self.frames += 1

    def set_keyboard_visible(self, visible):
        self.keyboard.append(visible)

    def get_clipboard_text(self):
        return self.clipboard

    def set_clipboard_text(self, text):
        self.clipboard = text


def frame_info(index=0):
    return dict(width=400, height=300, scale=3, now=10 + index / 60,
                presentation_time=10 + (index + 1) / 60)


def test_held_contact_keeps_pacing_until_release_or_cancel(native):
    adapter = native.adapter
    adapter.process_inputs(frame_info(), [touch('begin')])
    assert adapter.has_pending_events
    for index in range(1, 4):
        wakes = native.host.frames
        adapter.process_inputs(frame_info(index), [])
        assert adapter.has_pending_events and native.host.frames == wakes + 1
    adapter.process_inputs(frame_info(4), [touch('end')])
    adapter.process_inputs(frame_info(5), [])
    assert not adapter.has_pending_events
    adapter.process_inputs(frame_info(6), [touch('begin', identity=2)])
    adapter.process_inputs(frame_info(7), [dict(kind='cancel_all')])
    assert not adapter.has_pending_events


def test_touch_timestamps_do_not_include_variable_python_processing_delay(native, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('meltygui.core.input.ios_input.time.perf_counter', lambda: clock[0])
    native.adapter.process_inputs(frame_info(), [])  # Establish the native clock mapping.
    delivered = []
    for index, delay in enumerate((.002, .014, .001, .008), start=1):
        timestamp = 10 + index / 120
        clock[0] = timestamp + 90 + delay
        event = touch('begin' if index == 1 else 'move', y=30 - index)
        event['timestamp'] = timestamp
        native.adapter.process_inputs(dict(frame_info(), now=timestamp,
                                          presentation_time=timestamp + 1 / 120), [event])
        if index == 1:
            native.adapter.process_inputs(frame_info(1), [])
        delivered.append(native.handler._touch_input.events[-1][3])
    assert delivered == pytest.approx([100 + index / 120 for index in range(1, 5)])


def test_resume_recalibrates_clock_after_cancelling_contacts(native, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('meltygui.core.input.ios_input.time.perf_counter', lambda: clock[0])
    native.adapter.process_inputs(frame_info(), [touch('begin')])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    native.adapter.suspend()
    assert not native.adapter.has_pending_events
    native.adapter.resume()
    clock[0] = 200.0
    event = dict(touch('begin', identity=2), timestamp=10.1)
    native.adapter.process_inputs(dict(frame_info(), now=10.1), [event])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    assert native.handler._touch_input.events[-1][3] == pytest.approx(200.0)


def touch(kind, identity=1, x=20, y=30):
    return dict(kind='touch_' + kind, touch_id=identity, x=x, y=y,
                timestamp=10.0, pressure=.5, pencil=False)


@pytest.fixture
def native(monkeypatch):
    host = Host()
    backend = Backend(host)
    chars = []
    io = SimpleNamespace(key_map={}, config_flags=0, keys_down=[False] * 512,
                         mouse_down=[False] * 5, want_text_input=False,
                         add_input_characters_utf8=chars.append)
    melty = SimpleNamespace(frame_key_events=[], frame_text_events=[], _keys_down=set(),
                            text_focused_ds=None)
    handler = InputHandler()
    adapter = IOSInput(handler, backend, io, melty)
    from meltygui.core.melty import Melty
    monkeypatch.setattr(Melty, 'get_latest_mouse', lambda: backend.window.cursor_pos)
    yield SimpleNamespace(host=host, backend=backend, io=io, melty=melty,
                          handler=handler, adapter=adapter, chars=chars)
    adapter.close()


@pytest.fixture
def body_input(native, monkeypatch):
    """Actual cached-action replay and dispatch, without drawing GPU pixels."""
    from meltygui.core.melty import Melty
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.state.new_core_model import DrawState
    hits = []
    native.melty.bvh_query = lambda x, y: hits
    native.melty.cache = SimpleNamespace(_pixels_preserved=lambda view: True)
    native.melty.max_layer = Melty.max_layer
    overlay = Mock()
    monkeypatch.setattr(imgui, 'get_overlay_draw_list', lambda: overlay)
    monkeypatch.setattr(imgui, 'get_mouse_pos', lambda: native.io.mouse_pos)
    monkeypatch.setattr(Melty, 'event_handler', native.handler)
    monkeypatch.setattr(Melty, 'imgui_main_window_hovered', True)
    monkeypatch.setattr(Melty, 'inside_clip', lambda **kwargs: True)
    monkeypatch.setattr(Toggles.InputHandlerToggles, 'show_debug', False)

    def view(identity, x=0, suffix='press', events=('left_mouse_down',), fast=False):
        state = DrawState()
        state._tile_id = identity
        state.window_pos = x, 0
        state.width = state.height = 60
        state._wrapper = SimpleNamespace(fast_host=fast)
        state._body_actions = (0, [(suffix, events, 0, (0, 0, 60, 60),
                                    imgui.MOUSE_CURSOR_HAND, None)])
        return state

    return SimpleNamespace(native=native, hits=hits, view=view, overlay=overlay)


def test_first_contact_rehits_body_after_priming(body_input):
    native, hits = body_input.native, body_input.hits
    target = body_input.view('fresh', x=100)
    hits.append(target)
    native.adapter.process_inputs(frame_info(), [touch('begin', x=120, y=30)])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    native.adapter.pump()
    result = native.handler.process_frame()[0]
    assert result['fresh_press']['left_mouse_down'].tile_id == 'fresh'
    body_input.overlay.channels_split.assert_called_once_with(native.melty.max_layer)
    body_input.overlay.channels_merge.assert_called_once()


def test_fast_swipe_rehits_origin_even_when_it_leaves_before_the_frame(body_input):
    native, hits = body_input.native, body_input.hits
    target = body_input.view('pane', x=0, suffix='scroll', events=('touch_scroll_changed',))
    hits.append(target)
    native.adapter.process_inputs(frame_info(), [touch('begin', x=20, y=30),
                                                touch('move', x=220, y=10),
                                                touch('end', x=220, y=10)])
    assert native.io.mouse_pos == (20, 30)
    assert not native.io.mouse_down[0]
    native.adapter.process_inputs(frame_info(1), [])
    native.adapter.pump()
    native.handler.process_frame()
    native.adapter.process_inputs(frame_info(2), [])
    native.adapter.pump()
    event = native.handler.process_frame()[0]['pane_scroll']['touch_scroll_changed']
    assert event.value == -20
    assert native.io.mouse_pos == (220, 10)


def test_new_contact_removes_old_named_body_target_and_keeps_wrapper(body_input):
    native, hits = body_input.native, body_input.hits
    old = body_input.view('old', fast=True)
    parent, target = body_input.view('parent'), body_input.view('fresh', x=100)
    parent._body_actions = None
    parent._children['row'] = old
    hits.append(parent)
    native.adapter.process_inputs(frame_info(), [])
    native.io.mouse_pos = (20, 30)
    native.adapter.pump()  # remember the previous render's hit states
    old.replay_body_actions()
    native.handler.register_hovered('old', ['other_down'], priority=100000,
                                    tile_id='old', blocker=True)
    native.handler.feed_down('other', 20, 30)
    hits[:] = [target]
    native.adapter.process_inputs(frame_info(1), [touch('begin', x=120, y=30)])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    native.adapter.pump()
    assert 'old_press' not in native.handler._view_cursor
    assert native.handler._blocker_views == {'old'}
    result = native.handler.process_frame()[0]
    assert 'old_press' not in result
    assert result['fresh_press']['left_mouse_down'].tile_id == 'fresh'
    assert result['old']['other_down'].tile_id == 'old'


def test_short_new_contact_keeps_both_edges_across_presented_frames(body_input):
    native, hits = body_input.native, body_input.hits
    target = body_input.view('short', events=('left_mouse_down', 'left_mouse_up',
                                              'left_mouse_clicked'))
    hits.append(target)
    native.adapter.process_inputs(frame_info(), [touch('begin'), touch('end')])
    native.adapter.pump()
    assert not native.handler.process_frame()[0]
    native.adapter.process_inputs(frame_info(1), [])
    native.adapter.pump()
    assert set(native.handler.process_frame()[0]['short_press']) == {'left_mouse_down'}
    native.handler.begin_frame()
    target.replay_body_actions()
    native.adapter.process_inputs(frame_info(2), [])
    native.adapter.pump()
    result = native.handler.process_frame()[0]
    assert set(result['short_press']) == {'left_mouse_up', 'left_mouse_clicked'}
    assert all(event.tile_id == 'short' for event in result['short_press'].values())


def test_first_contact_rehits_declared_parameters_and_removes_old_target(body_input):
    native, hits = body_input.native, body_input.hits
    old, target = body_input.view('old'), body_input.view('text', x=100)
    for view in (old, target):
        view._body_actions = None
        view._wrapper.__params__ = {'left_mouse_down': None}
        view._kwargs = {'left_mouse_down': None}
        view.header_height = view.footer_height = 0
    hits.append(old)
    native.adapter.process_inputs(frame_info(), [])
    native.adapter.pump()
    native.handler.register_hovered('old', ['left_mouse_down', 'other_down'], tile_id='old')
    hits[:] = [target]
    native.adapter.process_inputs(frame_info(1), [touch('begin', x=120, y=30)])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    native.adapter.pump()
    native.handler.feed_down('other', 120, 30)
    events = native.handler.process_frame()[0]
    assert 'left_mouse_down' not in events['old']
    assert events['old']['other_down'].tile_id == 'old'
    assert events['text']['left_mouse_down'].tile_id == 'text'


def test_first_contact_respects_declared_parameter_rect(body_input):
    native, hits = body_input.native, body_input.hits
    view = body_input.view('text', x=100)
    view._body_actions = None
    view._wrapper.__params__ = {'left_mouse_down': None}
    view._kwargs = {'left_mouse_down': None}
    view._event_rects = {'left_mouse_down': [(0, 0, 10, 60)]}
    view.header_height = view.footer_height = 0
    hits.append(view)
    native.adapter.process_inputs(frame_info(), [touch('begin', x=120, y=30)])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    native.adapter.pump()
    assert 'text' not in native.handler.process_frame()[0]


def test_first_contact_does_not_revive_a_hidden_branch(body_input):
    native, hits = body_input.native, body_input.hits
    stale = body_input.view('hidden')
    target = body_input.view('visible')
    native.melty.cache._pixels_preserved = lambda view: view is target
    hits[:] = [stale, target]
    native.adapter.process_inputs(frame_info(), [touch('begin')])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    native.adapter.pump()
    events = native.handler.process_frame()[0]
    assert 'hidden_press' not in events
    assert events['visible_press']['left_mouse_down'].tile_id == 'visible'


def test_new_contact_replays_shared_fast_host_child_once(body_input, monkeypatch):
    from meltygui.state.new_core_model import DrawState
    native, hits = body_input.native, body_input.hits
    parent = body_input.view('parent', x=100)
    child = body_input.view('fast-row', x=100, fast=True)
    parent._body_actions = None
    parent._view_children['row'] = child
    hits[:] = [child, parent]  # both are indexed, but the parent's replay visits the row
    calls = []
    replay = DrawState.replay_body_actions
    def observe(state):
        calls.append(state)
        return replay(state)
    monkeypatch.setattr(DrawState, 'replay_body_actions', observe)
    native.adapter.process_inputs(frame_info(), [touch('begin', x=120, y=30)])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    native.adapter.pump()
    result = native.handler.process_frame()[0]
    assert calls == [parent, child]
    assert result['fast-row_press']['left_mouse_down'].tile_id == 'fast-row'


def test_touch_rehit_preserves_captured_drag_after_leaving_the_body(body_input):
    native, hits = body_input.native, body_input.hits
    target = body_input.view('drag', events=('left_mouse_down', 'left_mouse_drag',
                                             'left_mouse_drag_released'))
    hits.append(target)
    native.adapter.process_inputs(frame_info(), [touch('begin', x=20, y=30)])
    native.adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    native.adapter.pump()
    assert 'left_mouse_down' in native.handler.process_frame()[0]['drag_press']
    native.handler.begin_frame()
    hits.clear()
    native.adapter.process_inputs(frame_info(1), [touch('move', x=220, y=30)])
    native.adapter.pump()
    result = native.handler.process_frame()[0]
    assert result['drag_press']['left_mouse_drag'].total_dx == 200
    native.handler.begin_frame()
    native.adapter.process_inputs(frame_info(2), [touch('end', x=220, y=30)])
    native.adapter.pump()
    result = native.handler.process_frame()[0]
    assert 'left_mouse_drag_released' in result['drag_press']
    assert not native.handler._drag_capture


def test_no_new_contact_does_not_walk_or_replay_body_children(body_input):
    native, hits = body_input.native, body_input.hits
    # A normal keyboard/idle frame records only the BVH hits. This sentinel
    # has no child/record attributes, so an unnecessary tree walk fails.
    hits.append(object())
    native.adapter.process_inputs(frame_info(), [dict(kind='text', text='x')])
    native.adapter.pump()
    body_input.overlay.channels_split.assert_not_called()
    assert native.melty.frame_text_events == ['x']


def test_geometry_has_point_coordinates_and_pixel_framebuffer(native):
    native.adapter.process_inputs(frame_info(), [touch('begin', x=37, y=52)])
    assert native.io.display_size == (400, 300)
    assert native.io.display_fb_scale == (3, 3)
    assert native.io.mouse_pos == (37, 52)
    assert native.backend.get_framebuffer_size(native.backend.window) == (1200, 900)
    assert native.io.key_map[imgui.KEY_BACKSPACE] == codes.KEY_BACKSPACE
    native.adapter.process_inputs(frame_info(1), [])
    assert native.io.delta_time == pytest.approx(1 / 60)


def test_120hz_presentation_timestamps_are_not_clamped_to_60hz(native):
    for index in range(3):
        info = frame_info(index) | dict(presentation_time=10 + index / 120)
        native.adapter.process_inputs(info, [])
        if index:
            assert native.io.delta_time == pytest.approx(1 / 120)


def test_primary_touch_keeps_cached_drag_owner_and_aggregates_motion(native):
    handler, adapter = native.handler, native.adapter
    handler.register_hovered('editor', ['left_mouse_down', 'left_mouse_drag'], tile_id='editor-tile')
    adapter.process_inputs(frame_info(), [touch('begin')])
    adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    assert 'left_mouse_down' in handler.process_frame()[0]['editor']
    handler.begin_frame()
    handler.register_hovered('other', ['left_mouse_drag'], tile_id='other-tile')
    adapter.process_inputs(frame_info(1), [touch('begin', 2, 200, 220),
                                           touch('move', 1, 30, 40), touch('move', 1, 45, 50)])
    adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    result = handler.process_frame()[0]
    drag = result['editor']['left_mouse_drag']
    assert drag.tile_id == 'editor-tile'
    assert (drag.dx, drag.dy) == (25, 20)
    assert (drag.total_dx, drag.total_dy) == (25, 20)
    assert native.backend.window.primary_touch == 1
    assert 2 in native.backend.window.touches
    assert 'other' not in result


def test_stationary_touch_edges_do_not_emit_cursor_motion(native):
    adapter, handler = native.adapter, native.handler
    generation = native.backend.window.cursor_motion_generation
    batches = [[touch('begin')], [], [touch('move')],
               [touch('move', x=40)], [touch('end', x=40)], []]
    moved = []
    for index, batch in enumerate(batches):
        handler.register_hovered('motion', ['cursor_moved'])
        adapter.process_inputs(frame_info(index), batch)
        moved.append('motion' in handler.process_frame()[0])
        handler.begin_frame()
    assert moved == [True, False, False, True, False, False]
    assert native.backend.window.cursor_motion_generation == generation + 2


def test_secondary_contacts_do_not_take_over_after_primary_release(native):
    adapter = native.adapter
    adapter.process_inputs(frame_info(), [touch('begin'), touch('begin', 2)])
    adapter.process_inputs(frame_info(), [])
    adapter.process_inputs(frame_info(1), [touch('end'), touch('move', 2, 99, 99), touch('begin', 3)])
    assert native.backend.window.primary_touch is None
    assert native.backend.window.cursor_pos == (20, 30)
    assert not native.handler.is_down('left_mouse')
    adapter.process_inputs(frame_info(2), [touch('end', 2), touch('cancel', 3), touch('begin', 4)])
    adapter.process_inputs(frame_info(3), [])
    assert native.backend.window.primary_touch == 4


def test_cancel_clears_capture_without_click_release_or_drop(native):
    handler, adapter = native.handler, native.adapter
    subscriptions = ['left_mouse_down', 'left_mouse_drag', 'left_mouse_up',
                     'left_mouse_clicked', 'left_mouse_drag_released']
    handler.register_hovered('editor', subscriptions, tile_id='cached')
    adapter.process_inputs(frame_info(), [touch('begin')])
    adapter.process_inputs(frame_info(1), [])  # Present the primed contact.
    handler.process_frame()
    handler.begin_frame()
    handler.register_hovered('editor', subscriptions, tile_id='cached')
    adapter.process_inputs(frame_info(1), [touch('move', x=45, y=50)])
    assert 'left_mouse_drag' in handler.process_frame()[0]['editor']
    handler.begin_frame()
    handler.register_hovered('editor', subscriptions, tile_id='cached')
    adapter.process_inputs(frame_info(2), [touch('cancel', x=45, y=50)])
    assert not handler.process_frame()[0]
    assert not handler.is_down('left_mouse')
    assert not handler._drag_capture
    assert not native.io.mouse_down[0]
    assert native.io.mouse_pos[0] < 0  # raw ImGui widgets cannot accept the cancelled release


def test_cancel_does_not_clear_another_input_or_cached_subscription(native):
    handler = native.handler
    handler.register_hovered('button', ['left_mouse_clicked', 'other_down'], tile_id='cached')
    handler.feed_down('other', 20, 30)
    native.adapter.process_inputs(frame_info(), [touch('begin'), touch('cancel')])
    assert handler.is_down('other')
    assert handler.process_frame()[0]['button']['other_down'].tile_id == 'cached'
    # Cancellation removes the gesture, not its view's subscriptions.
    handler._pending.clear()
    native.adapter.process_inputs(frame_info(1), [touch('begin', 2), touch('end', 2)])
    native.adapter.process_inputs(frame_info(2), [])
    handler.process_frame()
    native.adapter.process_inputs(frame_info(3), [])
    assert handler.process_frame()[0]['button']['left_mouse_clicked'].tile_id == 'cached'


def test_short_tap_primes_then_presses_and_releases_once(native):
    native.handler.register_hovered('button', ['left_mouse_clicked'], tile_id='button-tile')
    native.adapter.process_inputs(frame_info(), [touch('begin'), touch('end')])
    assert not native.io.mouse_down[0] and native.adapter.has_pending_events
    assert not native.handler.process_frame()[0]
    native.adapter.process_inputs(frame_info(1), [])
    assert native.io.mouse_down[0] and native.adapter.has_pending_events
    assert not native.handler.process_frame()[0]
    native.adapter.process_inputs(frame_info(2), [])
    clicked = native.handler.process_frame()[0]['button']['left_mouse_clicked']
    assert clicked.tile_id == 'button-tile'
    assert not native.handler.is_down('left_mouse')
    assert not native.io.mouse_down[0] and not native.adapter.has_pending_events
    native.handler.begin_frame()
    native.adapter.process_inputs(frame_info(3), [])
    assert not native.io.mouse_down[0] and not native.adapter.has_pending_events


def test_batched_contacts_each_reach_their_newly_rendered_target(native):
    # Window chrome discovers its subscriptions during drawing, after dispatch.
    # Neither target has ever been hovered when UIKit delivers these two taps.
    batches = [[touch('begin', 1, 20, 30), touch('end', 1, 20, 30),
                touch('begin', 2, 220, 30), touch('end', 2, 220, 30)]] + [[]] * 6
    clicks, positions, down = [], [], []
    for index, batch in enumerate(batches):
        native.adapter.process_inputs(frame_info(index), batch)
        events = native.handler.process_frame()[0]
        clicks.extend(view for view, actions in events.items() if 'left_mouse_clicked' in actions)
        positions.append(native.io.mouse_pos)
        down.append(native.io.mouse_down[0])
        native.handler.begin_frame()
        target = 'first' if native.io.mouse_pos[0] < 100 else 'second'
        native.handler.register_hovered(target, ['left_mouse_clicked'], tile_id=target)
    assert clicks == ['first', 'second']
    assert positions == [(20, 30)] * 3 + [(220, 30)] * 4
    assert down == [False, True, False, False, True, False, False]
    assert not native.adapter.has_pending_events


def test_fast_drag_presents_origin_and_held_motion_before_drop(native):
    events = [touch('begin', x=20), touch('end', x=220)]
    phases, positions = [], []
    for index in range(4):
        native.adapter.process_inputs(frame_info(index), events if index == 0 else [])
        dispatched = native.handler.process_frame()[0]
        phases.append(set(dispatched.get('handle', {})))
        positions.append((native.io.mouse_pos, native.io.mouse_down[0]))
        native.handler.begin_frame()
        if native.io.mouse_pos[0] < 100:
            native.handler.register_hovered('handle', ['left_mouse_down', 'left_mouse_drag',
                'left_mouse_drag_released', 'left_mouse_clicked'], tile_id='handle')
    assert phases == [set(), {'left_mouse_down'}, {'left_mouse_drag'}, {'left_mouse_drag_released'}]
    assert positions == [((20, 30), False), ((20, 30), True),
                         ((220, 30), True), ((220, 30), False)]
    assert not native.handler._drag_capture
    assert not native.adapter.has_pending_events


@pytest.mark.parametrize('frames_before_cancel', [1, 2, 3])
def test_cancel_discards_staged_press_motion_and_release(native, frames_before_cancel):
    batch = [touch('begin'), touch('move', x=80), touch('end', x=80)]
    native.handler.register_hovered('button', ['left_mouse_clicked', 'left_mouse_drag_released'])
    for index in range(frames_before_cancel):
        native.adapter.process_inputs(frame_info(index), batch if index == 0 else [])
        native.handler.process_frame()
        native.handler.begin_frame()
    native.adapter.process_inputs(frame_info(3), [touch('cancel')])
    for index in range(4, 7):
        native.adapter.process_inputs(frame_info(index), [])
        assert not native.handler.process_frame()[0]
        assert not native.io.mouse_down[0]
        assert native.io.mouse_pos[0] < 0
        assert not native.adapter.has_pending_events
        native.handler.begin_frame()


def test_priming_freezes_momentum_then_delivers_the_same_contact(native):
    import time
    now = time.perf_counter()
    native.handler.feed_touch('begin', t=now)
    state = native.handler._touch_input
    state.events.clear()
    state.velocity, state.coast_time, state.scroll_target = -1000, now, 'list'
    native.adapter.process_inputs(frame_info(), [touch('begin')])
    assert not native.handler.process_frame(animation_time=now + .1)[0]
    assert state.velocity == -1000
    native.adapter.process_inputs(frame_info(1), [touch('end')])
    native.handler.register_hovered('control', ['left_mouse_down', 'left_mouse_drag', 'left_mouse_clicked'])
    assert 'left_mouse_down' in native.handler.process_frame(animation_time=now + .2)[0]['control']
    assert native.io.mouse_down[0]
    assert state.velocity == 0 and not state.contact_pending
    native.handler.begin_frame()
    native.handler.register_hovered('control', ['left_mouse_clicked'])
    native.adapter.process_inputs(frame_info(2), [])
    assert 'left_mouse_clicked' in native.handler.process_frame()[0]['control']
    assert not native.io.mouse_down[0]


def test_unicode_text_and_backspace_keep_batch_order_and_deliver_once(native):
    adapter, melty = native.adapter, native.melty
    adapter.process_inputs(frame_info(), [dict(kind='text', text='caf'), dict(kind='text', text='é🙂'),
                                           dict(kind='backspace'), dict(kind='text', text='!')])
    assert melty.frame_text_events == ['café🙂']
    assert not melty.frame_key_events
    assert native.chars == ['café🙂'] and adapter.has_pending_events
    adapter.process_inputs(frame_info(1), [])
    assert not melty.frame_text_events
    assert melty.frame_key_events == [(codes.KEY_BACKSPACE, 0)]
    assert native.io.keys_down[codes.KEY_BACKSPACE]
    adapter.process_inputs(frame_info(2), [])
    assert melty.frame_text_events == ['!'] and not melty.frame_key_events
    assert not native.io.keys_down[codes.KEY_BACKSPACE]
    adapter.process_inputs(frame_info(3), [])
    assert not melty.frame_key_events and not melty.frame_text_events
    assert native.chars == ['café🙂', '!'] and not adapter.has_pending_events


def test_enter_tab_are_keys_but_multiline_committed_text_stays_text(native):
    adapter, melty = native.adapter, native.melty
    adapter.process_inputs(frame_info(), [dict(kind='text', text='\n'), dict(kind='text', text='\t'),
                                           dict(kind='text', text='def f():\n    return 42\n')])
    assert melty.frame_key_events == [(codes.KEY_ENTER, 0)]
    adapter.process_inputs(frame_info(1), [])
    assert melty.frame_key_events == [(codes.KEY_TAB, 0)]
    adapter.process_inputs(frame_info(2), [])
    assert melty.frame_text_events == ['def f():\n    return 42\n']
    assert native.chars == melty.frame_text_events


def test_hardware_shortcut_and_suspend_reset_held_state(native):
    native.adapter.process_inputs(frame_info(), [dict(kind='key', key=codes.KEY_S,
                                                     action=codes.PRESS, modifiers=codes.MOD_SUPER)])
    assert native.melty.frame_key_events == [(codes.KEY_S, codes.MOD_SUPER)]
    assert native.backend.get_key(native.backend.window, codes.KEY_S) == codes.PRESS
    assert native.io.key_super and codes.KEY_S in native.melty._keys_down
    native.adapter.update_keyboard(True)
    native.adapter.suspend()
    assert not native.io.key_super and not any(native.io.keys_down)
    assert not native.melty._keys_down and not native.backend.window.focused
    assert native.host.keyboard == [True, False]


def test_native_clipboard_callbacks_and_keyboard_service(native):
    native.io.set_clipboard_text_fn('λ = 3')
    assert native.io.get_clipboard_text_fn() == 'λ = 3'
    native.melty.text_focused_ds = SimpleNamespace(_kwargs={'editable': True})
    native.adapter.update_keyboard()
    native.adapter.update_keyboard()
    assert native.host.keyboard == [True]


def test_backend_selection_never_imports_glfw_or_accepts_a_second_host(monkeypatch):
    monkeypatch.setattr(window_api, '_state', dict(backend=None, selected=False))
    monkeypatch.setattr(window_api, 'sys', SimpleNamespace(platform='ios', builtin_module_names=()))
    host = Host()
    get_host = Mock(return_value=host)
    monkeypatch.setitem(sys.modules, 'melty_ios_bootstrap', SimpleNamespace(get_host=get_host))
    assert window_api.select_backend(True) == 'ios'
    get_host.assert_called_once_with()
    backend = window_api.select_ios_backend(host)
    assert window_api.select_ios_backend(host) is backend
    assert window_api.select_backend(False) == 'ios'
    assert window_api.is_native_window(backend.window)
    assert window_api.KEY_A == 65
    with pytest.raises(RuntimeError, match='already active'):
        window_api.select_ios_backend(Host())
    with pytest.raises(AttributeError):
        window_api.get_x11_window
    with pytest.raises(NotImplementedError):
        window_api.create_window(100, 100, 'child')
    with pytest.raises(NotImplementedError):
        window_api.set_window_size(backend.window, 100, 100)
    window_api.terminate()
    assert window_api.select_backend(False) == 'ios'
    assert window_api._state['backend'].host is host
    window_api.terminate()


def test_ios_selection_automatically_binds_hub_graphics(monkeypatch):
    monkeypatch.setattr(window_api, '_state', dict(backend=None, selected=False))
    monkeypatch.setattr(window_api, 'sys', SimpleNamespace(
        platform='ios', builtin_module_names=('_hub_graphics',)))
    backend = SimpleNamespace(name='glfw')
    bind = Mock(side_effect=lambda: window_api._state.update(backend=backend, selected=True))
    monkeypatch.setitem(sys.modules, 'hub_ios_glfw', SimpleNamespace(_bind_melty=bind))
    assert window_api.select_backend(True) == 'glfw'
    assert window_api.select_backend(False) == 'glfw'
    assert window_api._state['backend'] is backend
    bind.assert_called_once_with()


def test_backend_callbacks_and_missing_services_are_explicit(native):
    sizes = []
    callback = lambda window, w, h: sizes.append((w, h))
    assert native.backend.set_window_size_callback(native.backend.window, callback) is None
    native.backend.update_frame(frame_info())
    assert sizes == [(400, 300)]
    bare = Backend(SimpleNamespace(request_frame=lambda: None))
    with pytest.raises(NotImplementedError, match='get_clipboard_text'):
        bare.get_clipboard_string(bare.window)


def test_adapter_accepts_the_actual_imgui_binding():
    previous = imgui.get_current_context()
    context = imgui.create_context()
    backend = Backend(Host())
    melty = SimpleNamespace(frame_key_events=[], frame_text_events=[], _keys_down=set(),
                            text_focused_ds=None)
    adapter = IOSInput(InputHandler(), backend, imgui.get_io(), melty)
    try:
        adapter.process_inputs(frame_info(), [dict(kind='text', text='print("é")')])
        assert tuple(imgui.get_io().display_size) == (400, 300)
        assert melty.frame_text_events == ['print("é")']
    finally:
        adapter.close()
        imgui.destroy_context(context)
        imgui.set_current_context(previous)


@pytest.mark.parametrize('release_x, expected_clicks', [(20, 1), (220, 0)])
@pytest.mark.parametrize('after_scroll', [False, True])
def test_single_contact_with_real_imgui_button(release_x, expected_clicks, after_scroll):
    previous = imgui.get_current_context()
    context = imgui.create_context()
    io = imgui.get_io()
    io.ini_file_name = None
    io.fonts.get_tex_data_as_rgba32()
    backend = Backend(Host())
    melty = SimpleNamespace(frame_key_events=[], frame_text_events=[], _keys_down=set(),
                            text_focused_ds=None)
    adapter = IOSInput(InputHandler(), backend, io, melty)
    clicks = 0
    try:
        # Build the window with no pointer, then deliver a whole contact between
        # frames. ImGui sees the same staged IO as the injected Melty controls.
        for index in range(7):
            if index == 2 and after_scroll:
                import time
                adapter.handler.feed_touch('begin')
                state = adapter.handler._touch_input
                state.events.clear()
                state.velocity, state.coast_time = -1000, time.perf_counter()
            batch = ([touch('begin', x=20, y=20), touch('end', x=release_x, y=20)]
                     if index == 2 else [])
            adapter.process_inputs(frame_info(index), batch)
            imgui.new_frame()
            imgui.set_next_window_position(0, 0)
            imgui.set_next_window_size(400, 300)
            imgui.begin('Touch test', flags=imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE)
            clicks += bool(imgui.button('Tap once', 100, 50))
            imgui.end()
            imgui.render()
        assert clicks == expected_clicks
        assert not adapter.has_pending_events
    finally:
        adapter.close()
        imgui.destroy_context(context)
        imgui.set_current_context(previous)


def test_native_adapter_import_graph_is_free_of_glfw_and_opengl():
    source = ('import sys; from meltygui.core.windowing.backends.native_ios import Backend; '
              'from meltygui.core.input.ios_input import IOSInput; '
              'assert "glfw" not in sys.modules; assert "OpenGL" not in sys.modules')
    subprocess.run([sys.executable, '-c', source], check=True, close_fds=False)


def test_native_release_velocity_reaches_shared_gesture_handler(native):
    end = touch('end', y=10)
    end['velocity'] = (12.5, -1800.25)
    native.adapter.process_inputs(frame_info(), [touch('begin'), touch('move', y=10), end])
    for index in range(1, 4):
        native.adapter.process_inputs(frame_info(index), [])
    assert native.handler._touch_input.events[-1][-1] == (12.5, -1800.25)


def test_first_contact_after_scroll_presses_raw_imgui_control(native):
    import time
    native.handler.feed_touch('begin', t=time.perf_counter())
    state = native.handler._touch_input
    state.events.clear()
    state.velocity = -1000
    state.coast_time = time.perf_counter()
    native.adapter.process_inputs(frame_info(), [touch('begin')])
    assert not native.io.mouse_down[0]
    native.adapter.process_inputs(frame_info(1), [touch('end')])
    assert native.io.mouse_down[0]
    native.adapter.process_inputs(frame_info(2), [])
    assert not native.io.mouse_down[0]
    native.adapter.process_inputs(frame_info(3), [])
    assert not native.io.mouse_down[0] and not native.adapter.has_pending_events
