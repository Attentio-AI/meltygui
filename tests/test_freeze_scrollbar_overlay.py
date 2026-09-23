"""Freeze-resize scrollbars never enter window pixels captured by blit."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.core import core_render
from meltygui.core.cache.tile_cache import TileCacheMasked
from meltygui.core.melty import Melty


@pytest.mark.parametrize('width', [300, 300, 240, 360, 300])
def test_freeze_scrollbar_uses_deferred_window_masked_overlay(monkeypatch, width):
    overlay = Mock()
    monkeypatch.setattr(core_render.imgui, 'get_overlay_draw_list', lambda: overlay)
    window = Mock(side_effect=AssertionError('bar entered captured window list'))
    monkeypatch.setattr(core_render.imgui, 'get_window_draw_list', window)
    monkeypatch.setattr(core_render, 'clear_shadows', Mock())
    monkeypatch.setattr(core_render, 'add_shadow', Mock())
    monkeypatch.setattr(Melty, '_overlay_channels_active', True)
    monkeypatch.setattr(Melty, 'overlay_channel_for', lambda ds: 7)
    ds = SimpleNamespace(
        freeze_resize=True, scroll_visible=True, closed=False, just_shadow=False,
        height=200, width=width, footer_height=0, header_height=0,
        abs_content_height=1000, abs_clipped_height=200,
        _kwargs={}, _abs_left=lambda: 20, _abs_top=lambda: 30,
        abs_clip_rect=(20, 30, 20 + width, 230),
        scroll_offset=(0, 100), current_tint=(0.3, 0.5, 0.7),
        on_action=Mock(return_value=None),
    )
    TileCacheMasked.draw_freeze_scrollbar(SimpleNamespace(clear_shadows=Mock()), ds)
    overlay.add_rect_filled.assert_called_once()
    x1, y1, x2, y2, _ = overlay.add_rect_filled.call_args.args
    assert 20 < x1 < x2 < 20 + width
    assert 30 < y1 < y2 < 230
    overlay.push_clip_rect.assert_called_once_with(*ds.abs_clip_rect, True)
    overlay.pop_clip_rect.assert_called_once()
    assert overlay.channels_set_current.call_args_list[0].args == (7,)
    assert overlay.channels_set_current.call_args_list[-1].args == (Melty.max_layer - 1,)
    assert any(call.args[0] == 'left_mouse_drag' for call in ds.on_action.call_args_list)


def test_hidden_freeze_scrollbar_clears_retained_shadow(monkeypatch):
    paint = Mock()
    monkeypatch.setattr(core_render, 'draw_overlay_scrollbar', paint)
    cache = SimpleNamespace(clear_shadows=Mock())
    ds = SimpleNamespace(freeze_resize=True, scroll_visible=False)
    TileCacheMasked.draw_freeze_scrollbar(cache, ds)
    cache.clear_shadows.assert_called_once_with(ds, core_render.SCROLLBAR_SHADOW_GROUP)
    paint.assert_not_called()


def test_cached_parent_replays_and_retains_descendant_scrollbars():
    from meltygui.core.cache.tile_cache import Tile, _Ctx

    child = SimpleNamespace(freeze_resize=True)
    parent = SimpleNamespace(freeze_resize=False)
    ancestor = SimpleNamespace(freeze_scrollbars=())
    tile = Tile(parent, 0, 0, 0, None, (300, 200), freeze_scrollbars=(child,))
    cache = SimpleNamespace(_tiles={'parent': tile}, _stack=[ancestor],
                            draw_freeze_scrollbar=Mock())
    ctx = _Ctx(parent, 'parent', (0, 0), (300, 200), 0, 0, True, False)
    TileCacheMasked._draw_freeze_scrollbars(cache, ctx)
    assert [call.args[0] for call in cache.draw_freeze_scrollbar.call_args_list] == [child, parent]
    assert ctx.freeze_scrollbars == ancestor.freeze_scrollbars == (child,)


def test_live_parent_does_not_draw_descendant_scrollbar_twice():
    from meltygui.core.cache.tile_cache import _Ctx

    child = SimpleNamespace(freeze_resize=True)
    parent = SimpleNamespace(freeze_resize=True)
    ancestor = SimpleNamespace(freeze_scrollbars=())
    cache = SimpleNamespace(_tiles={}, _stack=[ancestor], draw_freeze_scrollbar=Mock())
    ctx = _Ctx(parent, 'parent', (0, 0), (300, 200), 0, 0, False, False,
               freeze_scrollbars=(child,))
    TileCacheMasked._draw_freeze_scrollbars(cache, ctx)
    cache.draw_freeze_scrollbar.assert_called_once_with(parent)
    assert ancestor.freeze_scrollbars == (child, parent)


@pytest.mark.parametrize("existing_size", [None, (300, 200)])
def test_first_pane_during_click_uses_live_size_until_cache_tile_exists(monkeypatch, existing_size):
    from meltygui.core.cache import tile_cache
    from meltygui.core.rendering import overlay
    ds = SimpleNamespace(abs_left=0, abs_top=0, width=400, height=240,
                         min_width=None, min_height=None, clipped_by_rect=None,
                         frame_count=2, freeze_resize=True)
    ctx = tile_cache._Ctx(ds, "pane", (0, 0), (400, 240), 0, 0, False, False)
    tile = SimpleNamespace(size=existing_size) if existing_size else None
    cache = SimpleNamespace(
        enabled=True, _stack=[ctx], _key_to_ctx={},
        _tiles={"pane": tile} if tile else {}, _dummy_vao=1,
        _draw_freeze_scrollbars=Mock(), _get_current_clip_rect_screen=lambda: None,
        _clip_rect=lambda *args: (0, 0, 400, 240), mask_mark_view=Mock(),
        _oversized=lambda size: False, _scrub_stale_content=Mock(),
        _is_dirty=lambda tile: True, _enq_copy_keys=set(), _pending=[])
    monkeypatch.setattr(Melty, "tile_id_stack", ["pane"])
    monkeypatch.setattr(Melty, "resize_gesture_live", lambda: True)
    monkeypatch.setattr(tile_cache.imgui, "pop_id", lambda: None)
    monkeypatch.setattr(tile_cache.imgui, "end_group", lambda: None)
    monkeypatch.setattr(tile_cache.imgui, "is_mouse_down", lambda button: button == 0)
    monkeypatch.setattr(tile_cache.gl, "glBindVertexArray", lambda vao: None)
    monkeypatch.setattr(overlay, "finish_cached_overlays", lambda *args: None)
    TileCacheMasked.mark_end_offscreen(cache)
    assert len(cache._pending) == 1
    assert cache._pending[0].tile is tile
    assert cache._pending[0].size == (existing_size or (400, 240))
