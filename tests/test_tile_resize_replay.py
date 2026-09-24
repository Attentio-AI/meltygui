"""Layout-owned cache replay: native-scale pixels, live bounds and safe fallbacks."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.core.cache.tile_cache import TileCacheMasked, Tile
from meltygui.core.layout.tile_resize import TileResizeRecord, renderer_version
from meltygui.core.melty import Melty
from meltygui.model.tile_model import Tile as LayoutTile


def resident():
    ds = SimpleNamespace(use_cache=True, closed=False, _external_change=False,
                         _tile_id='child', width=300, height=200, abs_left=40, abs_top=60,
                         parent_window=None, depth=4, z_pos=100, shadow_depth=2,
                         auto_resize=False, corner_radius=0, replay_surface_requests=Mock(),
                         _kwargs={}, misc={}, misc_used=set())
    texture = Tile(ds, 1, 2, None, None, (300, 200), alloc_size=(512, 256),
                   last_clean_frame=4, filled_bbox=(0, 0, 300, 200))
    cache = object.__new__(TileCacheMasked)
    cache.enabled = True
    cache._tiles = {'child': texture}
    cache._frozen_served = {}
    cache._stack = []
    cache._key_to_ctx = {}
    cache.key_to_parent_key = {}
    cache.key_to_draw_state = {}
    cache._frame_cache_hits = 0
    cache.draw_freeze_bg = Mock()
    cache.mask_mark_view = Mock()
    return cache, ds, texture


def test_replay_requires_resident_complete_capture_and_honors_external_changes():
    cache, ds, tex = resident()
    assert cache.can_replay_resize(ds)
    tex.last_clean_frame = -1
    assert not cache.can_replay_resize(ds)
    tex.last_clean_frame = 4
    tex.filled_bbox = (20, 0, 300, 200)
    assert not cache.can_replay_resize(ds)
    cache._frozen_served['child'] = ds
    assert cache.can_replay_resize(ds)
    ds._external_change = True
    assert not cache.can_replay_resize(ds)
    ds._external_change = False
    cache.enabled = False
    assert not cache.can_replay_resize(ds)


@pytest.mark.parametrize('size', [(180, 120), (460, 340)])
def test_pixels_do_not_stretch_and_footer_tracks_bottom(monkeypatch, size):
    from meltygui.core.cache import tile_cache
    from meltygui.core.rendering import view_identity
    cache, ds, tex = resident()
    dl = Mock()
    monkeypatch.setattr(tile_cache.imgui, 'get_window_draw_list', lambda: dl)
    monkeypatch.setattr(tile_cache.imgui, 'set_cursor_screen_pos', Mock())
    monkeypatch.setattr(view_identity, 'place_in_parent_window', Mock())
    monkeypatch.setattr(Melty, 'get_clip_rect', lambda: (40, 60, 500, 400))
    monkeypatch.setattr(Melty, 'channels_split', True)
    monkeypatch.setattr(Melty, 'frame_count', 12)
    monkeypatch.setattr(Melty, 'silence_invalidate', False)
    placement = Mock(side_effect=lambda _: pytest.fail('geometry must be silenced')
                     if not Melty.silence_invalidate else None)
    monkeypatch.setattr(view_identity, 'place_in_parent_window', placement)
    # Direct replay paints descendant and own overlays, then retains both
    # for a cached parent on the next frame.
    from meltygui.core.rendering import overlay
    descendant = SimpleNamespace(_kwargs={})
    tex.overlay_views = (descendant,)
    parent = SimpleNamespace(key="parent", overlay_views=())
    cache._stack = [parent]
    ds._kwargs = {"draw_overlay": lambda: None}
    painted = []
    monkeypatch.setattr(overlay, "draw_overlay", lambda view: painted.append(view))
    width, height = size
    assert cache.replay_resize(ds, (40, 60, width, height), footer_height=28)
    assert painted == [descendant, ds]
    assert parent.overlay_views == (descendant, ds)
    assert Melty.silence_invalidate is False
    body, footer = dl.add_image.call_args_list
    visible_w, visible_h = min(width, 300), min(height - 28, 172)
    assert body.args == (2, (40, 60), (40 + visible_w, 60 + visible_h),
                         (0, 1), (visible_w / 512, 1 - visible_h / 256))
    assert footer.args == (2, (40, 60 + height - 28), (40 + visible_w, 60 + height),
                           (0, 1 - 172 / 256), (visible_w / 512, 1 - 200 / 256))
    dl.push_clip_rect.assert_not_called()
    dl.pop_clip_rect.assert_not_called()
    assert ds.width == width and ds.height == height
    assert ds.last_seen == ds._blit_served_frame == 12
    ds.replay_surface_requests.assert_called_once()
    assert cache._frozen_served['child'] is ds
    assert cache._key_to_ctx['child'].size == size
    dl.channels_set_current.assert_called_once_with(Melty.get_channel(ds.depth))


def test_tile_replay_is_atomic_and_rejects_renderer_or_input_replacement(monkeypatch):
    def renderer(value):
        raise AssertionError('renderer must not run during replay')
    tile = LayoutTile(render_func=renderer, input_value={})
    record = TileResizeRecord(renderer_version(renderer), tile.input_value, (0, 0, 300, 200),
                              body='body', controls=['picker', 'link'], toolbar=True, link_parameter_count=2)
    cache = SimpleNamespace(replay_resize_batch=Mock(return_value=False))
    monkeypatch.setattr(Melty, 'push_clip', Mock())
    monkeypatch.setattr(Melty, 'pop_clip', Mock())
    monkeypatch.setattr(Melty, 'channels_split', False)
    assert not record.replay(tile, (10, 20, 240, 180), cache)
    cache.replay_resize_batch.return_value = True
    assert record.replay(tile, (10, 20, 240, 180), cache)
    assert cache.replay_resize_batch.call_args.args == ([
        ('body', (10, 20, 240, 180), 28),
        ('picker', (10, 172, 164, 28), 0),
        ('link', (178, 172, 72, 28), 0)],)
    tile.input_value = {}
    assert not record.replay(tile, (10, 20, 240, 180), cache)
    tile.input_value = record.input_value
    renderer.__code__ = (lambda value: (False, value)).__code__
    assert not record.replay(tile, (10, 20, 240, 180), cache)


def test_geometry_guard_is_restored_on_failed_placement(monkeypatch):
    from meltygui.core.cache import tile_cache
    from meltygui.core.rendering import view_identity
    cache, ds, _ = resident()
    monkeypatch.setattr(tile_cache.imgui, 'get_window_draw_list', Mock())
    monkeypatch.setattr(Melty, 'get_clip_rect', lambda: (0, 0, 500, 400))
    monkeypatch.setattr(tile_cache.imgui, 'set_cursor_screen_pos', Mock())
    monkeypatch.setattr(view_identity, 'place_in_parent_window',
                        Mock(side_effect=RuntimeError('placement failed')))
    monkeypatch.setattr(Melty, 'silence_invalidate', False)
    with pytest.raises(RuntimeError, match='placement failed'):
        cache.replay_resize(ds, (40, 60, 180, 120))
    assert Melty.silence_invalidate is False
    assert not cache._frozen_served


def test_drag_receiver_ancestors_stay_live_but_siblings_can_freeze(monkeypatch):
    from meltygui.core.input.input_handler import EventAction
    cache, ds, _ = resident()
    cache.key_to_parent_key = {'inner': 'child', 'child': 'root', 'root': None}
    event = SimpleNamespace(action=EventAction.DRAGGED, tile_id='inner')
    monkeypatch.setattr(Melty, 'events', {'inner_handle': {'left_mouse_drag': event}})
    cache._refresh_resize_input_keys()
    assert cache._resize_input_keys == {'inner', 'child', 'root'}
    assert not cache.can_replay_resize(ds)

    # The layout host owns a tile-divider drag: its children are still frozen.
    event.tile_id = 'root'
    cache._refresh_resize_input_keys()
    assert cache._resize_input_keys == {'root'}
    assert cache.can_replay_resize(ds)

    # Hovering a child is not a reason to render it during somebody else's drag.
    event.tile_id, event.action = 'inner', EventAction.HOVERED
    cache._refresh_resize_input_keys()
    assert not cache._resize_input_keys
    assert cache.can_replay_resize(ds)


def test_drag_receiver_ancestry_tolerates_a_stale_cycle(monkeypatch):
    from meltygui.core.input.input_handler import EventAction
    cache, _, _ = resident()
    cache.key_to_parent_key = {'inner': 'child', 'child': 'inner'}
    event = SimpleNamespace(action=EventAction.DOUBLE_DRAGGED, tile_id='inner')
    monkeypatch.setattr(Melty, 'events', {'inner_handle': {'double_left_mouse_drag': event}})
    cache._refresh_resize_input_keys()
    assert cache._resize_input_keys == {'inner', 'child'}


def test_batch_preflight_does_not_paint_or_stamp_any_member_on_miss(monkeypatch):
    cache, ds, _ = resident()
    cache._replay_resize_prepared = Mock()
    bad = SimpleNamespace(use_cache=False)
    assert not cache.replay_resize_batch([(ds, (0, 0, 100, 100), 0),
                                          (bad, (100, 0, 100, 100), 0)])
    cache._replay_resize_prepared.assert_not_called()
    assert not cache._frozen_served
