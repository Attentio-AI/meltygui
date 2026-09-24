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
    monkeypatch.setattr(Melty, '_overlay_channels_active', True)
    monkeypatch.setattr(Melty, 'overlay_channel_for', lambda ds: 7)
    monkeypatch.setattr(overlay.Tint, 'dd_text', lambda tint: (1, 1, 1))
    monkeypatch.setattr(overlay, 'time', SimpleNamespace(perf_counter=lambda: 0))
    def view(callback):
        return SimpleNamespace(_kwargs={'draw_overlay': callback}, closed=False,
                               just_shadow=False, misc={}, misc_used=set(),
                               _raw_input_value={'count': 1}, abs_clip_rect=(10, 20, 90, 100),
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


def test_slow_callback_disabled_per_view_and_hotswap_retries(setup, monkeypatch):
    draw_list, view = setup
    calls = []
    def callback():
        calls.append(1)
    ds = view(callback)
    clock = iter([0, .0006])
    monkeypatch.setattr(overlay.time, 'perf_counter', lambda: next(clock))
    overlay.draw_overlay(ds)
    overlay.draw_overlay(ds)
    assert calls == [1]
    assert '0.5 ms' in draw_list.add_text.call_args.args[-1]
    def replacement():
        calls.append(2)
    callback.__code__ = replacement.__code__
    monkeypatch.setattr(overlay, 'time', SimpleNamespace(perf_counter=lambda: 0))
    overlay.draw_overlay(ds)
    assert ds.misc['render_overlay'].error is None
    other = view(callback)
    overlay.draw_overlay(other)
    assert other.misc['render_overlay'] is not ds.misc['render_overlay']


@pytest.mark.parametrize('elapsed,disabled', [(0.0005, False), (0.000501, True)])
def test_budget_boundary(setup, monkeypatch, elapsed, disabled):
    _, view = setup
    ds = view(lambda: None)
    clock = iter([0, elapsed])
    monkeypatch.setattr(overlay.time, 'perf_counter', lambda: next(clock))
    overlay.draw_overlay(ds)
    assert (ds.misc['render_overlay'].error is not None) is disabled


def test_exception_draws_error_and_restores_clip(setup):
    draw_list, view = setup
    def broken():
        raise ValueError('broken overlay')
    ds = view(broken)
    overlay.draw_overlay(ds)
    overlay.draw_overlay(ds)
    assert 'ValueError: broken overlay' in draw_list.add_text.call_args.args[-1]
    assert draw_list.pop_clip_rect.call_count == 2


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


def test_cached_child_overlay_uses_preserved_pixels_beyond_old_viewport(monkeypatch):
    from unittest.mock import Mock
    from meltygui.core.cache.tile_cache import Tile
    ds = SimpleNamespace(_tile_id='text', width=260, height=180,
                         abs_left=10, abs_top=20, abs_clip_rect=(10, 20, 270, 200))
    tile = Tile(ds, 1, 2, None, None, (200, 100), alloc_size=(512, 256),
                content_size=(300, 200), last_clean_frame=1)
    cache = SimpleNamespace(enabled=True, _tiles={'text': tile})
    monkeypatch.setattr(Melty, 'cache', cache)
    dl = Mock()
    assert overlay.paint_cached_view(ds, dl)
    dl.add_image.assert_called_once_with(2, (10, 20), (270, 200),
                                         (0, 1), (260 / 512, 1 - 180 / 256))
    dl.push_clip_rect.assert_called_once_with(10, 20, 270, 200, True)
    dl.pop_clip_rect.assert_called_once()
    # Painting must never resize or overwrite the resident cache.
    assert tile.size == (200, 100) and tile.content_size == (300, 200)
    cache._tiles.clear()
    assert not overlay.paint_cached_view(ds, dl)
