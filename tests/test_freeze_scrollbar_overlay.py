"""All framework scrollbars stay live outside captured tile pixels."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.core import core_render
from meltygui.core.rendering import overlay as overlays
from meltygui.core.cache.tile_cache import TileCacheMasked
from meltygui.core.melty import Melty


@pytest.mark.parametrize('freeze_resize', [False, True])
@pytest.mark.parametrize('width', [300, 300, 240, 360, 300])
def test_scrollbar_uses_deferred_window_masked_overlay(monkeypatch, width, freeze_resize):
    overlay = Mock()
    monkeypatch.setattr(core_render.imgui, 'get_overlay_draw_list', lambda: overlay)
    window = Mock(side_effect=AssertionError('bar entered captured window list'))
    monkeypatch.setattr(core_render.imgui, 'get_window_draw_list', window)
    monkeypatch.setattr(core_render, 'clear_shadows', Mock())
    monkeypatch.setattr(core_render, 'add_shadow', Mock())
    monkeypatch.setattr(Melty, '_overlay_channels_active', True)
    monkeypatch.setattr(Melty, 'overlay_channel_for', lambda ds: 7)
    ds = SimpleNamespace(
        freeze_resize=freeze_resize, scroll_visible=True, closed=False, just_shadow=False,
        height=200, width=width, footer_height=0, header_height=0,
        abs_content_height=1000, abs_clipped_height=200,
        _kwargs={}, abs_left=20, abs_top=30,
        abs_clip_rect=(20, 30, 20 + width, 230),
        scroll_offset=(0, 100), current_tint=(0.3, 0.5, 0.7),
        on_action=Mock(return_value=None),
    )
    overlays.draw_scrollbar(ds)
    overlay.add_rect_filled.assert_called_once()
    x1, y1, x2, y2, _ = overlay.add_rect_filled.call_args.args
    assert 20 < x1 < x2 < 20 + width
    assert 30 < y1 < y2 < 230
    overlay.push_clip_rect.assert_called_once_with(*ds.abs_clip_rect, True)
    overlay.pop_clip_rect.assert_called_once()
    assert overlay.channels_set_current.call_args_list[0].args == (7,)
    assert overlay.channels_set_current.call_args_list[-1].args == (Melty.max_layer - 1,)
    assert any(call.args[0] == 'left_mouse_drag' for call in ds.on_action.call_args_list)


@pytest.mark.parametrize('visible,closed', [(True, True), (False, False)])
def test_hidden_scrollbar_clears_retained_shadow(monkeypatch, visible, closed):
    paint, clear = Mock(), Mock()
    monkeypatch.setattr(core_render, 'draw_overlay_scrollbar', paint)
    monkeypatch.setattr(core_render, 'clear_shadows', clear)
    ds = SimpleNamespace(scroll_visible=visible, closed=closed)
    overlays.draw_scrollbar(ds)
    clear.assert_called_once_with(ds, core_render.SCROLLBAR_SHADOW_GROUP)
    paint.assert_not_called()


@pytest.mark.parametrize('cached', [False, True])
def test_parent_replays_scrollbars_once_and_retains_uncached_children(monkeypatch, cached):
    from meltygui.core.cache.tile_cache import _Ctx
    child = SimpleNamespace(_kwargs={}, scroll_visible=True, use_cache=False)
    parent = SimpleNamespace(_kwargs={}, scroll_visible=True)
    ancestor = SimpleNamespace(overlay_views=())
    cache = SimpleNamespace(enabled=True, _tiles={'parent': SimpleNamespace(overlay_views=(child,))},
                            _stack=[ancestor])
    paint = Mock()
    monkeypatch.setattr(overlays, 'draw_scrollbar', paint)
    monkeypatch.setattr(overlays, 'draw_overlay', Mock())
    ctx = _Ctx(parent, 'parent', (0, 0), (300, 200), 0, 0, cached, False)
    if not cached:
        cache._stack = [ctx]
        overlays.finish_overlay(child, cache)
        cache._stack = [ancestor]
    overlays.finish_cached_overlays(cache, ctx)
    assert [call.args[0] for call in paint.call_args_list] == [child, parent]
    assert ancestor.overlay_views == (child, parent)


def test_nested_scrollbar_reflows_before_paint_and_never_replays_old_input(monkeypatch):
    from meltygui.state.new_core_model import DrawState
    from meltygui.core.input.input_handler import InputHandler
    from meltygui.core.runtime.toggles import Toggles
    parent, child = DrawState(), DrawState()
    parent.window_pos = (20, 30)
    parent.width, parent.height = 520, 260
    parent._kwargs = {}
    parent.header_height = parent.footer_height = 0
    child._parent = child.parent_window = parent
    child.window_pos = (0, 0)
    child.left_offset = child.top_offset = 0
    child.width, child.height = 504, 222
    child._tile_id = 'files'
    child._kwargs = {'disable_scroll': False}
    child.scroll_visible = True
    child.header_height = child.footer_height = 0
    child.frame_count = 1
    child.observed_content_height = 1000
    child.current_tint = (.3, .5, .7)
    child.scroll_offset = (0, 0)
    dl, retained = Mock(vtx_buffer_size=0), {}
    cursor, pointer = [(0, 0)], [(0, 0)]
    monkeypatch.setattr(DrawState, '_cap_to_display', lambda self, pos, axis: pos)
    monkeypatch.setattr(DrawState, 'hover_eligible', lambda *args, **kwargs: True)
    monkeypatch.setattr(Melty, 'inside_clip', lambda **kwargs: True)
    monkeypatch.setattr(Melty, 'events', {})
    monkeypatch.setattr(Melty, '_overlay_channels_active', False)
    monkeypatch.setattr(Melty, 'frame_count', 10)
    monkeypatch.setattr(Toggles.InputHandlerToggles, 'show_debug', False)
    monkeypatch.setattr(Toggles.ScrollSettings, 'scrollbar_shadow_offset', 1)
    monkeypatch.setattr(overlays, 'time', SimpleNamespace(thread_time=lambda: 0))
    monkeypatch.setattr(core_render.imgui, 'get_overlay_draw_list', lambda: dl)
    monkeypatch.setattr(core_render.imgui, 'get_window_draw_list',
                        Mock(side_effect=AssertionError('scrollbar entered cached pixels')))
    monkeypatch.setattr(core_render.imgui, 'get_cursor_screen_pos', lambda: cursor[0])
    monkeypatch.setattr(core_render.imgui, 'set_cursor_screen_pos', lambda pos: cursor.__setitem__(0, pos))
    monkeypatch.setattr(core_render.imgui, 'get_mouse_pos', lambda: pointer[0])
    monkeypatch.setattr(core_render, 'clear_shadows',
                        lambda ds, group: retained.pop(id(ds), None))
    monkeypatch.setattr(core_render, 'add_shadow',
                        lambda rect, draw_state, **kwargs: retained.__setitem__(id(draw_state), rect))
    def place_files(draw_state):
        overlays.place_overlay_view(child,
            (draw_state.abs_left + 8, draw_state.abs_top + 34,
             draw_state.width - 16, draw_state.height - 38), draw_state.abs_clip_rect)
    parent._kwargs['draw_overlay_background'] = place_files
    cache = SimpleNamespace(enabled=True, _stack=[],
                            _tiles={'parent': SimpleNamespace(overlay_views=(child,))})
    ctx = SimpleNamespace(draw_state=parent, key='parent', drew_cached=True)
    body_action = ('file_row', ('left_mouse_down',), 0, (0, 0, 100, 20), None, None)
    child._body_actions = (10, [body_action])
    handler = InputHandler()
    monkeypatch.setattr(Melty, 'event_handler', handler)
    overlays.finish_cached_overlays(cache, ctx)
    old_rect = dl.add_rect_filled.call_args.args[:4]
    assert child._body_actions == (10, [body_action])

    # Simulate a retained scrollbar record from before the overlay migration.
    child._body_actions[1].append(('scrollbar_grab', ('left_mouse_down',), 0,
        tuple(value - (child.abs_left if index % 2 == 0 else child.abs_top)
              for index, value in enumerate(old_rect)), None, None))
    pointer[0] = ((old_rect[0] + old_rect[2]) / 2, (old_rect[1] + old_rect[3]) / 2)
    parent.width = 1000
    monkeypatch.setattr(Melty, 'frame_count', 11)
    # Cache body input is replayed before the overlay has placed the child.
    child.replay_body_actions()
    assert not any(view_id == 'files_scrollbar_grab' for view_id, *_ in handler._hovered)
    overlays.finish_cached_overlays(cache, ctx)
    new_rect = dl.add_rect_filled.call_args.args[:4]
    assert new_rect[2] - old_rect[2] == 480
    assert new_rect[2] == child.abs_left + child.width - 3
    assert not any(view_id == 'files_scrollbar_grab' for view_id, *_ in handler._hovered)
    assert retained[id(child)] == (new_rect[0], new_rect[1],
                                    new_rect[2] - new_rect[0], new_rect[3] - new_rect[1])
    assert child._body_actions == (10, [body_action])

    pointer[0] = ((new_rect[0] + new_rect[2]) / 2, (new_rect[1] + new_rect[3]) / 2)
    overlays.finish_cached_overlays(cache, ctx)
    assert any(view_id == 'files_scrollbar_grab' for view_id, *_ in handler._hovered)
    # Growing until content fits removes the previous retained grab shadow.
    parent.height = 1300
    overlays.finish_cached_overlays(cache, ctx)
    assert id(child) not in retained


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
        _get_current_clip_rect_screen=lambda: None,
        _clip_rect=lambda *args: (0, 0, 400, 240), mask_mark_view=Mock(),
        _oversized=lambda size: False, _scrub_stale_content=Mock(),
        _is_dirty=lambda tile: True, _enq_copy_keys=set(), _pending=[])
    cache.retain_input_view = lambda ds: TileCacheMasked.retain_input_view(cache, ds)
    cache.finish_cached_input = lambda ctx: TileCacheMasked.finish_cached_input(cache, ctx)
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


@pytest.mark.parametrize('dy', [0, 50, 10000, -10000])
def test_overlay_grab_drag_clamps_without_forcing_body_repaint(monkeypatch, dy):
    class State(SimpleNamespace):
        __hash__ = object.__hash__
    dl = Mock()
    cache = SimpleNamespace(invalidate=Mock(side_effect=AssertionError('unconditional repaint')))
    monkeypatch.setattr(Melty, 'cache', cache)
    monkeypatch.setattr(Melty, 'selected', set())
    monkeypatch.setattr(Melty, 'frame_count', 10)
    monkeypatch.setattr(Melty, '_overlay_channels_active', False)
    monkeypatch.setattr(core_render, 'clear_shadows', Mock())
    monkeypatch.setattr(core_render, 'add_shadow', Mock())
    wake = Mock()
    monkeypatch.setattr(core_render, 'request_render', wake)
    monkeypatch.setattr(core_render.imgui, 'get_overlay_draw_list', lambda: dl)
    ds = State(scroll_visible=True, closed=False, just_shadow=False,
               height=200, width=300, footer_height=0, header_height=0,
               abs_content_height=1000, abs_clipped_height=200,
               _kwargs={}, abs_left=20, abs_top=30, abs_clip_rect=(20,30,320,230),
               scroll_offset=(0,100), current_tint=(.3,.5,.7),
               on_action=lambda action, **kwargs: SimpleNamespace(dy=dy)
                   if action == 'left_mouse_drag' else None)
    overlays.draw_scrollbar(ds)
    travel = 196 - 196 * .2
    assert ds.scroll_offset[1] == pytest.approx(max(0, min(100 + dy * 801 / travel, 801)))
    assert ds in Melty.selected
    wake.assert_called_once()
    cache.invalidate.assert_not_called()
    assert dl.add_rect_filled.call_args.args[1] >= 32
    assert dl.add_rect_filled.call_args.args[3] <= 228


def test_live_resize_hides_grab_and_clears_shadow_when_content_fits(monkeypatch):
    clear = Mock()
    monkeypatch.setattr(core_render, 'clear_shadows', clear)
    dl = Mock()
    monkeypatch.setattr(core_render.imgui, 'get_overlay_draw_list', lambda: dl)
    ds = SimpleNamespace(scroll_visible=True, closed=False, just_shadow=False,
                         height=1200, footer_height=0, abs_content_height=1000,
                         abs_clipped_height=1200, _kwargs={})
    overlays.draw_scrollbar(ds)
    clear.assert_called_once_with(ds, core_render.SCROLLBAR_SHADOW_GROUP)
    dl.add_rect_filled.assert_not_called()
