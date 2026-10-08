"""Overlay execution is independent of tile body execution and view identity."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.core.rendering import overlay
from meltygui.core.melty import Melty


@pytest.fixture
def setup(monkeypatch):
    draw_list = Mock(vtx_buffer_size=0)
    monkeypatch.setattr(overlay.imgui, 'get_overlay_draw_list', lambda: draw_list)
    monkeypatch.setattr(overlay.imgui, 'get_window_draw_list', lambda: draw_list)
    monkeypatch.setattr(overlay.imgui, 'calc_text_size', lambda text: (60, 12))
    monkeypatch.setattr(Melty, '_overlay_channels_active', True)
    monkeypatch.setattr(Melty, 'overlay_channel_for', lambda ds: 7)
    monkeypatch.setattr(overlay.Tint, 'dd_text', lambda tint: (1, 1, 1))
    monkeypatch.setattr(overlay, 'time', SimpleNamespace(thread_time=lambda: 0, monotonic=lambda: 0,
                                                                   strftime=lambda fmt: '01:08:00'))
    def view(callback):
        return SimpleNamespace(_kwargs={'draw_overlay': callback}, closed=False,
                               just_shadow=False, misc={}, misc_used=set(),
                               _raw_input_value={'count': 1}, abs_clip_rect=(10, 20, 90, 100),
                               abs_left=10, abs_top=20, width=80, height=80,
                               _abs_left=lambda: 10, _abs_top=lambda: 20,
                               header_height=5, current_tint=(0, 0, 0))
    return draw_list, view


def test_live_inputs_and_restored_routing(setup):
    draw_list, view = setup
    seen = []
    def callback(input_value, draw_state, draw_list):
        seen.append((input_value['count'], draw_state._abs_left()))
        draw_list.add_text(10, 20, 0, 'overlay')
    ds = view(callback)
    overlay.draw_overlay(ds)
    ds._raw_input_value['count'] = 2
    ds._abs_left = lambda: 30
    overlay.draw_overlay(ds)
    assert seen == [(1, 10), (2, 30)]
    assert draw_list.pop_clip_rect.call_count == 2
    assert draw_list.channels_set_current.call_args.args == (Melty.max_layer - 1,)


@pytest.mark.parametrize('option,state_key', [
    ('draw_overlay', 'render_overlay'),
    ('draw_overlay_background', 'render_overlay_background'),
    ('draw_background', 'render_background'),
])
def test_expensive_callback_keeps_rendering_with_red_warning(setup, monkeypatch, option, state_key):
    draw_list, view = setup
    discard = Mock()
    monkeypatch.setattr(overlay, 'discard_geometry', discard)
    callback = Mock(return_value=None)
    ds = view(callback)
    ds._kwargs = {option: callback}
    clock = iter([0, .0006, 0, .0006, 0, .0001])
    monkeypatch.setattr(overlay.time, 'thread_time', lambda: next(clock))
    for _ in range(2):
        getattr(overlay, option)(ds)
        assert ds.misc[state_key].error is None
        assert draw_list.add_text.call_args.args == (
            26, 84, overlay.pack_color(1.0, 0.0, 0.0, 1.0),
            f'[01:08:00] {option}: 0.600 ms CPU exceeds 0.5 ms CPU budget')
    draw_list.add_text.reset_mock()
    monkeypatch.setattr(overlay.time, 'monotonic', lambda: 5.0)
    getattr(overlay, option)(ds)
    assert callback.call_count == 3
    draw_list.add_text.assert_not_called()
    discard.assert_not_called()


@pytest.mark.parametrize('elapsed,warned', [(0.0005, False), (0.000501, True)])
def test_budget_boundary(setup, monkeypatch, elapsed, warned):
    draw_list, view = setup
    ds = view(lambda: None)
    clock = iter([0, elapsed])
    monkeypatch.setattr(overlay.time, 'thread_time', lambda: next(clock))
    overlay.draw_overlay(ds)
    assert ds.misc['render_overlay'].error is None
    assert draw_list.add_text.called is warned


def test_slow_warning_keeps_its_own_phase_timings(setup, monkeypatch):
    draw_list, view = setup
    def callback(draw_state):
        overlay.overlay_checkpoint(draw_state, 'Navigation')
        overlay.overlay_checkpoint(draw_state, 'Tabs')
        overlay.overlay_checkpoint(draw_state, 'Tabs')
    ds = view(callback)
    clock = iter([0, .0001, .0004, .00055, .0006,
                  1, 1.00001, 1.00002, 1.00003, 1.00004])
    monkeypatch.setattr(overlay.time, 'thread_time', lambda: next(clock))
    overlay.draw_overlay(ds)
    state = ds.misc['render_overlay']
    assert dict(state.warning_timings) == pytest.approx(
        {'Navigation': .0001, 'Tabs': .00045, 'Other': .00005})
    warning = state.budget_warning
    assert 'Tabs: 0.450 ms' in warning
    overlay.draw_overlay(ds)
    assert state.budget_warning == warning
    assert dict(state.warning_timings)['Tabs'] == pytest.approx(.00045)
    assert state.phase_started is None
    overlay.overlay_checkpoint(ds, 'Outside callback')
    assert 'Outside callback' not in state.timings


@pytest.mark.parametrize('width', [72, 200, 390])
def test_warning_wraps_inside_visible_tile_and_reflows(setup, monkeypatch, width):
    draw_list, view = setup
    def measure(text):
        lines = text.split('\n')
        return max(map(len, lines)) * 6, len(lines) * 12
    monkeypatch.setattr(overlay.imgui, 'calc_text_size', measure)
    monkeypatch.setattr(overlay.imgui, 'get_font_size', lambda: 12)
    ds = view(lambda: None)
    ds.width, ds.height = 600, 600
    ds.abs_clip_rect = (20, 30, 20 + width, 610)
    clock = iter([0, .0006, 0, .0001])
    monkeypatch.setattr(overlay.time, 'thread_time', lambda: next(clock))
    overlay.draw_overlay(ds)
    x, y, _, wrapped = draw_list.add_text.call_args.args
    assert ('\n' in wrapped) == (measure(ds.misc['render_overlay'].budget_warning)[0] > width - 8)
    assert all(measure(line)[0] <= width - 8 for line in wrapped.split('\n'))
    assert 24 <= x and x + measure(wrapped)[0] <= 16 + width
    assert 34 <= y and y + measure(wrapped)[1] <= 606
    # A cached view can change width without a new slow callback.
    ds.abs_clip_rect = (20, 30, 600, 610)
    overlay.draw_overlay(ds)
    if '\n' in wrapped:
        assert draw_list.add_text.call_args.args[-1] != wrapped


def test_warning_wraps_long_tokens_and_keeps_explicit_lines(monkeypatch):
    monkeypatch.setattr(overlay.imgui, 'calc_text_size', lambda text: (len(text), 1))
    assert overlay._wrap_warning('abcdefghij\nnext line', 4) == 'abcd\nefgh\nij\nnext\nline'


def test_old_budget_failure_recovers_without_changing_callback(setup):
    _, view = setup
    callback = Mock()
    ds = view(callback)
    overlay.draw_overlay(ds)
    ds.misc['render_overlay'].error = 'draw_overlay: 0.600 ms CPU exceeds 0.5 ms CPU budget'
    overlay.draw_overlay(ds)
    assert callback.call_count == 2
    assert ds.misc['render_overlay'].error is None


def test_scheduler_pause_does_not_disable_a_cheap_callback(setup, monkeypatch):
    _, view = setup
    clock = {'cpu': 0.0, 'wall': 0.0}
    monkeypatch.setattr(overlay, 'time', SimpleNamespace(
        thread_time=lambda: clock['cpu'], perf_counter=lambda: clock['wall']))
    calls = []
    def callback():
        calls.append(True)
        # A scheduler pause advances wall time, while this callback does only
        # 0.1 ms of actual work on the render thread each time it runs.
        clock['cpu'] += 0.0001
        clock['wall'] += 0.020
    ds = view(callback)
    overlay.draw_overlay(ds)
    overlay.draw_overlay(ds)
    assert calls == [True, True]
    assert ds.misc['render_overlay'].error is None


def test_exception_draws_error_and_restores_clip(setup):
    draw_list, view = setup
    def broken():
        raise ValueError('broken overlay')
    ds = view(broken)
    overlay.draw_overlay(ds)
    overlay.draw_overlay(ds)
    assert 'ValueError: broken overlay' in draw_list.add_text.call_args.args[-1]
    assert draw_list.pop_clip_rect.call_count == 2


@pytest.mark.parametrize('option', ['draw_overlay', 'draw_overlay_background'])
@pytest.mark.parametrize('raises', [False, True])
def test_overlay_input_is_live_and_body_recording_restored_after_errors(setup, monkeypatch, option, raises):
    from meltygui.state.new_core_model import DrawState
    ds = DrawState()
    ds.width = ds.height = 100
    ds.header_height = 0
    ds._tile_id = 'view'
    record = (Melty.frame_count, [('body_button', ('left_mouse_down',), 0, None, None, None)])
    ds._body_actions = record
    register = Mock()
    monkeypatch.setattr(Melty, 'inside_clip', lambda **kwargs: True)
    monkeypatch.setattr(DrawState, '_register_action', register)
    monkeypatch.setattr(DrawState, 'get_action', lambda *args, **kwargs: None)
    def callback(draw_state):
        draw_state.on_action('left_mouse_down', view_id='live_overlay', rect=(1, 2, 3, 4))
        if raises:
            raise ValueError('after subscribing')
    ds._kwargs = {option: callback}
    getattr(overlay, option)(ds)
    register.assert_called_once()
    assert register.call_args.args[0] == 'view_live_overlay'
    assert ds._body_actions is record
    assert [action[0] for action in record[1]] == ['body_button']
    state_key = 'render_overlay' if option == 'draw_overlay' else 'render_overlay_background'
    assert (ds.misc[state_key].error is not None) is raises


@pytest.mark.parametrize('kind', ['decorated', 'not_callable', 'wrong_signature'])
def test_invalid_callback_is_a_visible_error(setup, kind):
    draw_list, view = setup
    def callback():
        pytest.fail('invalid callback must not run')
    if kind == 'decorated':
        callback.__render_func__ = True
    elif kind == 'not_callable':
        callback = 42
    else:
        callback = lambda unsupported: None
    overlay.draw_overlay(view(callback))
    assert 'Overlay disabled:' in draw_list.add_text.call_args.args[-1]


def test_uncached_child_registered_for_cached_ancestor(setup):
    _, view = setup
    ds = view(lambda: None)
    ancestor = SimpleNamespace(overlay_views=())
    cache = SimpleNamespace(enabled=True, _stack=[ancestor])
    overlay.finish_overlay(ds, cache)
    assert ancestor.overlay_views == (ds,)


def test_hidden_overlay_does_not_run(setup):
    _, view = setup
    callback = Mock()
    ds = view(callback)
    ds.closed = True
    overlay.draw_overlay(ds)
    ds.closed, ds.just_shadow = False, True
    overlay.draw_overlay(ds)
    callback.assert_not_called()


def test_cached_parent_replays_descendants_once_and_retains_them(setup, monkeypatch):
    _, view = setup
    from meltygui.core.cache.tile_cache import _Ctx
    child, parent = view(lambda: None), view(lambda: None)
    ancestor = SimpleNamespace(overlay_views=())
    tile = SimpleNamespace(overlay_views=(child,))
    cache = SimpleNamespace(enabled=True, _tiles={'parent': tile}, _stack=[ancestor])
    ctx = _Ctx(parent, 'parent', (0, 0), (100, 100), 0, 0, True, False)
    paint = Mock()
    monkeypatch.setattr(overlay, 'draw_overlay', paint)
    overlay.finish_cached_overlays(cache, ctx)
    assert [c.args[0] for c in paint.call_args_list] == [child, parent]
    assert ancestor.overlay_views == (child, parent)
    ancestor.overlay_views = ()
    ctx.drew_cached = False
    paint.reset_mock()
    overlay.finish_cached_overlays(cache, ctx)
    paint.assert_called_once_with(parent)
    assert ancestor.overlay_views == (child, parent)


def test_failed_overlay_discards_only_its_geometry(monkeypatch):
    import ctypes
    import meltygui_imgui as imgui
    # The real binding's buffers validate stride/address arithmetic, including
    # HDR vertex formats; no GL/window is needed for draw-list generation.
    imgui.new_frame()
    try:
        draw_list = imgui.get_overlay_draw_list()
        draw_list.add_rect_filled(10, 10, 30, 30, 0xffffffff)
        start = draw_list.vtx_buffer_size
        before = ctypes.string_at(draw_list.vtx_buffer_data, start * imgui.VERTEX_SIZE)
        ds = SimpleNamespace(_kwargs={}, closed=False, just_shadow=False,
                             misc={}, misc_used=set(), _raw_input_value=None,
                             abs_clip_rect=(0, 0, 800, 600), _abs_left=lambda: 0,
                             _abs_top=lambda: 0, header_height=0, current_tint=(0, 0, 0))
        end = []
        def broken(draw_list):
            draw_list.add_rect_filled(50, 50, 70, 70, 0xffffffff)
            end.append(draw_list.vtx_buffer_size)
            raise ValueError('discard me')
        ds._kwargs['draw_overlay'] = broken
        monkeypatch.setattr(Melty, '_overlay_channels_active', False)
        monkeypatch.setattr(overlay.Tint, 'dd_text', lambda tint: (1, 1, 1))
        overlay.draw_overlay(ds)
        assert ctypes.string_at(draw_list.vtx_buffer_data, len(before)) == before
        emitted = ctypes.string_at(draw_list.vtx_buffer_data + len(before),
                                   (end[0] - start) * imgui.VERTEX_SIZE)
        assert emitted and not any(emitted)
        assert draw_list.vtx_buffer_size > end[0]  # visible error text survives
    finally:
        imgui.end_frame()


def test_bound_method_failure_stays_disabled_across_attribute_reads(setup):
    _, view = setup
    class Painter:
        def __init__(self):
            self.calls = 0
        def draw(self):
            self.calls += 1
            raise ValueError('disabled')
    painter = Painter()
    ds = view(painter.draw)
    overlay.draw_overlay(ds)
    ds._kwargs['draw_overlay'] = painter.draw
    overlay.draw_overlay(ds)
    assert painter.calls == 1
    ds._kwargs['draw_overlay'] = None
    overlay.draw_overlay(ds)
    assert 'render_overlay' not in ds.misc


def test_draw_overlay_is_public_wrapper_option():
    from meltygui.core.core_render import render_func_kwarg_names
    from meltygui.core.rendering.fast_view import FAST_VIEW_WRAPPER_KWARGS
    assert 'draw_overlay' in render_func_kwarg_names()
    assert 'draw_overlay' in FAST_VIEW_WRAPPER_KWARGS
    assert 'draw_overlay_background' in render_func_kwarg_names()
    assert 'draw_overlay_background' in FAST_VIEW_WRAPPER_KWARGS


def test_cached_layout_and_backing_run_parent_first_before_child_overlays(setup, monkeypatch):
    _, view = setup
    order = []
    child = view(lambda draw_state: order.append(('image', draw_state.width)))
    parent = view(lambda: order.append('parent chrome'))
    root = view(lambda: order.append('root chrome'))
    monkeypatch.setattr(Melty, 'frame_count', 44)
    for descendant in (root, parent, child):
        descendant._blit_served_frame = 42
        descendant.last_seen = 43
    root.width = 800
    def root_background():
        assert all(descendant._blit_served_frame == 44 for descendant in (root, parent, child))
        parent.width = root.width - 20
        order.append('root backing')
    def parent_background():
        child.width = parent.width - 40
        order.append('parent backing')
    root._kwargs['draw_overlay_background'] = root_background
    parent._kwargs['draw_overlay_background'] = parent_background
    cache = SimpleNamespace(_tiles={'root': SimpleNamespace(overlay_views=(child, parent))},
                            enabled=True, _stack=[])
    ctx = SimpleNamespace(draw_state=root, key='root', drew_cached=True)
    overlay.finish_cached_overlays(cache, ctx)
    assert order == ['root backing', 'parent backing', ('image', 740), 'parent chrome', 'root chrome']
    root.width = 400
    order.clear()
    overlay.finish_cached_overlays(cache, ctx)
    assert order == ['root backing', 'parent backing', ('image', 340), 'parent chrome', 'root chrome']
    assert all(descendant.last_seen == 43 for descendant in (root, parent, child))


def test_fresh_body_does_not_claim_descendant_pixels_were_preserved(setup, monkeypatch):
    from meltygui.core.cache.tile_cache import TileCacheMasked
    _, view = setup
    child, parent = view(None), view(None)
    child._parent = parent
    parent._parent = None
    child.last_seen = 40
    parent.last_seen = 41
    cache = TileCacheMasked()
    cache._tiles['parent'] = SimpleNamespace(overlay_views=(child,))
    monkeypatch.setattr(Melty, 'frame_count', 41)
    ctx = SimpleNamespace(draw_state=parent, key='parent', drew_cached=True)
    overlay.finish_cached_overlays(cache, ctx)
    assert cache._pixels_preserved(child)
    assert child.last_seen == 40

    # On the next frame a fresh parent body drops the old branch. Replaying
    # only the parent's own overlay must not keep that child's shadows alive.
    monkeypatch.setattr(Melty, 'frame_count', 42)
    parent.last_seen = 42
    ctx.drew_cached, ctx.overlay_views = False, ()
    overlay.finish_cached_overlays(cache, ctx)
    assert parent._blit_served_frame == child._blit_served_frame == 41
    assert not cache._pixels_preserved(child)


def test_cached_ancestor_restores_descendant_pixels_after_shrink_and_grow(setup, monkeypatch):
    from meltygui.core.cache.tile_cache import Tile
    _, view = setup
    root, editor = view(None), view(None)
    pane = SimpleNamespace(_tile_id='text', width=160, height=80,
                           abs_left=10, abs_top=20, abs_clip_rect=(10, 20, 170, 300))
    # The shrunk editor snapshot has an empty strip below its old pane. The
    # pane's own texture still holds the text from the earlier tall viewport.
    text = Tile(pane, 1, 2, None, None, (160, 80), alloc_size=(256, 512),
                content_size=(160, 260), last_clean_frame=1)
    editor._blit_served_frame = 40
    editor.last_seen = 40
    def background(draw_state):
        if draw_state._blit_served_frame == Melty.frame_count:
            overlay.paint_cached_view(pane)
    editor._kwargs['draw_overlay_background'] = background
    cache = SimpleNamespace(_tiles={'root': SimpleNamespace(overlay_views=(editor,)),
                                    'text': text}, enabled=True, _stack=[])
    monkeypatch.setattr(Melty, 'cache', cache)
    main_list = Mock()
    monkeypatch.setattr(overlay.imgui, 'get_window_draw_list', lambda: main_list)
    ctx = SimpleNamespace(draw_state=root, key='root', drew_cached=True)
    for frame, height in ((41, 80), (42, 260)):
        monkeypatch.setattr(Melty, 'frame_count', frame)
        pane.height = height
        overlay.finish_cached_overlays(cache, ctx)
    assert main_list.add_image.call_count == 2
    main_list.add_image.assert_called_with(2, (10, 20), (170, 280),
                                           (0, 1), (160 / 256, 1 - 260 / 512))
    assert editor.last_seen == 40
    assert text.size == (160, 80)


def test_background_only_view_registers_for_ancestor_replay(setup):
    _, view = setup
    ds = view(None)
    background = Mock()
    ds._kwargs['draw_overlay_background'] = background
    ancestor = SimpleNamespace(overlay_views=())
    cache = SimpleNamespace(enabled=True, _stack=[ancestor])
    overlay.finish_overlay(ds, cache)
    background.assert_called_once()
    assert ancestor.overlay_views == (ds,)


def test_overlay_child_placement_preserves_cursor_and_refreshes_live_geometry(monkeypatch):
    from meltygui.state.new_core_model import DrawState
    parent, child = DrawState(), DrawState()
    parent.window_pos = (10, 20)
    parent.width, parent.height = 500, 400
    child.parent_window = child._parent = parent
    child.width, child.height = 100, 100
    child.left_offset = child.top_offset = 0
    child.window_pos = (0, 0)
    monkeypatch.setattr(DrawState, '_cap_to_display', lambda self, pos, axis: pos)
    monkeypatch.setattr(Melty, 'frame_count', 999)
    monkeypatch.setattr(Melty, 'silence_invalidate', False)
    cursor = [(7, 9)]
    monkeypatch.setattr(overlay.imgui, 'get_cursor_screen_pos', lambda: cursor[0])
    monkeypatch.setattr(overlay.imgui, 'set_cursor_screen_pos', lambda pos: cursor.__setitem__(0, pos))
    _ = child.abs_left, child.abs_top
    overlay.place_overlay_view(child, (40, 60, 200, 150), (20, 30, 210, 180))
    assert (child.abs_left, child.abs_top, child.width, child.height) == (40, 60, 200, 150)
    assert child.abs_clip_rect == (40, 60, 210, 180)
    assert cursor[0] == (7, 9)
    assert Melty.silence_invalidate is False


@pytest.mark.parametrize('changed_definition', [False, True])
def test_cached_child_replay_precedes_shadows_and_preserves_pixels_beyond_old_viewport(monkeypatch, changed_definition):
    from unittest.mock import Mock
    from meltygui.core.cache.tile_cache import Tile
    ds = SimpleNamespace(_tile_id='text', width=260, height=180,
                         abs_left=10, abs_top=20, abs_clip_rect=(10, 20, 270, 200))
    tile = Tile(ds, 1, 2, None, None, (200, 100), alloc_size=(512, 256),
                content_size=(300, 200), last_clean_frame=1,
                content_stale=changed_definition)
    cache = SimpleNamespace(enabled=True, _tiles={'text': tile},
                            _scrub_stale_content=Mock(side_effect=AssertionError('overlay must not do GL cleanup')))
    monkeypatch.setattr(Melty, 'cache', cache)
    dl = Mock()
    foreground = Mock()
    monkeypatch.setattr(overlay.imgui, 'get_window_draw_list', lambda: dl)
    monkeypatch.setattr(overlay.imgui, 'get_overlay_draw_list', lambda: foreground)
    assert overlay.paint_cached_view(ds)
    cache._scrub_stale_content.assert_not_called()
    width, height = (200, 100) if changed_definition else (260, 180)
    dl.add_image.assert_called_once_with(2, (10, 20), (10 + width, 20 + height),
                                         (0, 1), (width / 512, 1 - height / 256))
    dl.push_clip_rect.assert_called_once_with(10, 20, 270, 200, True)
    dl.pop_clip_rect.assert_called_once()
    assert not foreground.mock_calls
    # Painting must never resize or overwrite the resident cache.
    assert tile.size == (200, 100) and tile.content_size == (300, 200)
    assert tile.content_stale is changed_definition
    cache._tiles.clear()
    assert not overlay.paint_cached_view(ds)


def test_budget_warning_fades_and_new_overrun_resets_age(setup, monkeypatch):
    draw_list, view = setup
    ds = view(lambda: None)
    clock = iter([0, .0006, 0, .0001, 0, .0006, 0, .0001])
    monkeypatch.setattr(overlay.time, 'thread_time', lambda: next(clock))
    overlay.draw_overlay(ds)
    monkeypatch.setattr(overlay.time, 'monotonic', lambda: 2.5)
    overlay.draw_overlay(ds)
    assert draw_list.add_text.call_args.args[2] == overlay.pack_color(1, 0, 0, .5)
    assert draw_list.add_text.call_args.args[-1].startswith('[01:08:00]')
    monkeypatch.setattr(overlay.time, 'strftime', lambda fmt: '01:08:03')
    overlay.draw_overlay(ds)
    assert draw_list.add_text.call_args.args[2] == overlay.pack_color(1, 0, 0, 1)
    assert draw_list.add_text.call_args.args[-1].startswith('[01:08:03]')
    monkeypatch.setattr(overlay.time, 'monotonic', lambda: 7.5)
    draw_list.add_text.reset_mock()
    overlay.draw_overlay(ds)
    draw_list.add_text.assert_not_called()
    assert ds.misc['render_overlay'].budget_warning is None
