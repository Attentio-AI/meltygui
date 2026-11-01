"""Moving a retained capture must not bilinearly filter already hinted text."""
from math import ceil, floor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.core.cache import tile_cache
from meltygui.core.melty import Melty


@pytest.mark.parametrize('scale', [(1., 1.), (1.25, 1.5), (2., 2.), (3., 3.)])
@pytest.mark.parametrize('move_pixels', [(0., 0.), (1., -2.), (.375, 0.), (0., .375)])
def test_cache_repaints_only_when_capture_pixel_phase_changes(monkeypatch, scale, move_pixels):
    display_pos = (4.25, 7.125)
    captured = (20.125, 60.375)
    size = (200., 100.)
    destination = tuple(p + d / s for p, d, s in zip(captured, move_pixels, scale))
    ds = SimpleNamespace(
        use_cache=True, _input_value=None, _collection=None, _tile_id='file', name='file',
        width=size[0], height=size[1], abs_left=destination[0], abs_top=destination[1],
        z_pos=1, shadow_depth=1, _bypass_cache=False, just_shadow=False, frame_count=10,
        size_change=False, freeze_resize=False, _external_change=False, multi_line=False,
        auto_resize=False, _kwargs={})
    tile = tile_cache.Tile(ds, 1, 2, None, None, size, alloc_size=(224, 128),
                           pixel_scale=scale, last_clean_frame=4, last_invalidated_frame=4,
                           dirty=False)
    px = (captured[0] - display_pos[0]) * scale[0]
    py = (captured[1] - display_pos[1]) * scale[1]
    # Capture uses framebuffer bottom-up bounds, including an integer shadow inset.
    bounds = (128 + px, 1000 - 128 - py - size[1] * scale[1],
              128 + px + size[0] * scale[0], 1000 - 128 - py)
    tile_cache._tile_capture_rect(tile, bounds)
    assert tile.capture_offset == pytest.approx((px - floor(px), ceil(-py) + py))

    cache = tile_cache.TileCacheMasked()
    cache.enabled = True
    cache.gpu = object()  # This test exercises the shared GL/Metal cache decision.
    cache._pixel_scale = scale
    cache._frame_id = 5
    cache._tiles['file'] = tile
    monkeypatch.setattr(cache, '_visible_unfilled', lambda *args: False)
    draw_list = Mock()
    monkeypatch.setattr(tile_cache, 'imgui', SimpleNamespace(
        get_cursor_screen_pos=lambda: destination,
        get_main_viewport=lambda: SimpleNamespace(pos=display_pos),
        get_window_draw_list=lambda: draw_list,
        push_id=Mock(), begin_group=Mock(), set_cursor_screen_pos=Mock(), dummy=Mock()))
    from meltygui.core.rendering import overlay
    monkeypatch.setattr(overlay, 'draw_background', Mock())
    monkeypatch.setattr(Melty, 'tile_id_stack', [])
    monkeypatch.setattr(Melty, 'frame_count', 5)
    monkeypatch.setattr(Melty, 'dnd_home_rect', None)
    monkeypatch.setattr(Melty, 'resize_gesture_live', lambda: False)

    should_repaint = any(d != round(d) for d in move_pixels)
    assert cache.mark_start_offscreen(ds) is should_repaint
    assert cache._is_dirty(tile) is should_repaint
    assert cache._stack[-1].drew_cached is not should_repaint
    assert draw_list.add_image_rounded.call_count == (0 if should_repaint else 1)
    assert (ds.abs_left, ds.abs_top) == destination  # Never round layout or input geometry.

    if should_repaint:
        # Simulate completing that capture: the unchanged next frame must hit,
        # rather than keeping the view permanently dirty after a fractional move.
        dx, dy = move_pixels
        tile_cache._tile_capture_rect(tile, (bounds[0] + dx, bounds[1] - dy,
                                            bounds[2] + dx, bounds[3] - dy))
        tile.last_clean_frame = cache._frame_id
        cache._frame_id += 1
        cache._stack.clear()
        assert cache.mark_start_offscreen(ds) is False
        assert not cache._is_dirty(tile)
