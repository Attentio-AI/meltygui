"""Live backgrounds stay beneath body pixels, including ancestor replay."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meltygui.core.melty import Melty
from meltygui.core.rendering import overlay


@pytest.fixture
def scene(monkeypatch):
    body, foreground = Mock(vtx_buffer_size=0), Mock(vtx_buffer_size=0)
    monkeypatch.setattr(overlay.imgui, 'get_window_draw_list', lambda: body)
    monkeypatch.setattr(overlay.imgui, 'get_overlay_draw_list', lambda: foreground)
    monkeypatch.setattr(overlay, 'time', SimpleNamespace(thread_time=lambda: 0))
    monkeypatch.setattr(Melty, '_overlay_channels_active', False)
    monkeypatch.setattr(overlay.Tint, 'dd_text', lambda tint: (1, 1, 1))
    monkeypatch.setattr(overlay, 'draw_scrollbar', Mock())

    def view(callback=None):
        return SimpleNamespace(
            _kwargs={'draw_background': callback}, _raw_input_value='text',
            closed=False, just_shadow=False, misc={}, misc_used=set(),
            abs_left=10, abs_top=20, width=200, height=100,
            header_height=0, current_tint=(.2, .4, .6),
            abs_clip_rect=(10, 20, 210, 120), _body_actions=['saved input'])
    return body, foreground, view


def test_live_bounds_and_body_draw_list(scene):
    body, foreground, view = scene
    bounds = []
    def paint(input_value, draw_state, draw_list):
        assert input_value == 'text'
        assert draw_state._body_actions is None
        bounds.append((draw_state.abs_left, draw_state.width))
        draw_list.add_rect_filled(draw_state.abs_left, 20, draw_state.width, 100, 0)
    ds = view(paint)
    overlay.draw_background(ds)
    ds.abs_left, ds.width = 30, 400
    overlay.draw_background(ds)
    assert bounds == [(10, 200), (30, 400)]
    assert body.add_rect_filled.call_count == 2
    body.push_clip_rect.assert_called_with(*ds.abs_clip_rect, True)
    assert body.pop_clip_rect.call_count == 2
    assert not foreground.mock_calls
    assert ds._body_actions == ['saved input']


def test_ancestor_layout_precedes_background_and_resident_body(scene, monkeypatch):
    _, _, view = scene
    order = []
    child = view(lambda draw_state: order.append(('background', draw_state.width)))
    parent = view()
    parent.width = 500
    def layout():
        child.width = parent.width - 40
        order.append('layout')
    parent._kwargs['draw_overlay_background'] = layout
    child._kwargs['draw_overlay'] = lambda: order.append('foreground')
    monkeypatch.setattr(overlay, 'paint_cached_view', lambda ds: order.append(('body', ds.width)))
    monkeypatch.setattr(overlay, '_resident_tile', lambda ds: object())
    ancestor = SimpleNamespace(overlay_views=())
    cache = SimpleNamespace(enabled=True, _stack=[ancestor],
                            _tiles={'parent': SimpleNamespace(overlay_views=(child,))})
    ctx = SimpleNamespace(draw_state=parent, key='parent', drew_cached=True)
    for width in (500, 240, 600):
        parent.width = width
        order.clear()
        overlay.finish_cached_overlays(cache, ctx)
        assert order == ['layout', ('background', width - 40),
                         ('body', width - 40), 'foreground']


def test_background_only_view_is_retained_and_closed_views_do_not_replay(scene, monkeypatch):
    _, _, view = scene
    callback, pixels = Mock(), Mock()
    ds = view(callback)
    ancestor = SimpleNamespace(overlay_views=())
    overlay.finish_overlay(ds, SimpleNamespace(enabled=True, _stack=[ancestor]))
    assert ancestor.overlay_views == (ds,)
    callback.assert_not_called()  # the body pre-pass, never the foreground epilogue
    monkeypatch.setattr(overlay, 'paint_cached_view', pixels)
    ds.closed = True
    overlay.draw_background(ds, replay=True)
    callback.assert_not_called()
    pixels.assert_not_called()


def test_background_is_public_and_text_uses_the_standard_painter():
    from meltygui.core.core_render import render_func_kwarg_names
    from meltygui.core.rendering.fast_view import FAST_VIEW_WRAPPER_KWARGS
    from meltygui.view.text_view import draw_text
    assert 'draw_background' in render_func_kwarg_names()
    assert 'draw_background' in FAST_VIEW_WRAPPER_KWARGS
    assert draw_text.__header_defaults__['draw_background'] is overlay.paint_view_background


def test_uncached_descendant_never_erases_its_flattened_body(scene, monkeypatch):
    _, _, view = scene
    paint = Mock()
    ds = view(paint)
    ds._tile_id = 'uncached'
    monkeypatch.setattr(Melty, 'cache', SimpleNamespace(enabled=True, _tiles={}))
    overlay.draw_background(ds, replay=True)
    paint.assert_not_called()


@pytest.mark.parametrize('invalid_return', [False, True])
def test_background_failure_stays_visible_above_cached_pixels(scene, invalid_return):
    body, foreground, view = scene
    calls = []
    def broken():
        calls.append(1)
        if invalid_return:
            return True
        raise ValueError('broken background')
    ds = view(broken)
    overlay.draw_background(ds)
    overlay.draw_background(ds)
    assert calls == [1]
    assert 'Background disabled:' in foreground.add_text.call_args.args[-1]
    assert foreground.pop_clip_rect.call_count == 2
    body.add_text.assert_not_called()


def test_standard_background_uses_explicit_owner_during_replay(scene, monkeypatch):
    from meltygui.core.runtime.toggles import Toggles
    body, _, view = scene
    ds = view()
    ds._kwargs.update(show_bg=True, tint=(.2, .4, .6))
    paint = Mock()
    monkeypatch.setattr(Toggles, 'dynamic_styles', True)
    monkeypatch.setattr(Melty, 'add_background', paint)
    monkeypatch.setattr(Melty, 'cache', None)
    overlay.paint_view_background(ds, body)
    paint.assert_called_once_with((.2, .4, .6), draw_state=ds, draw_list=body)
    paint.reset_mock()
    ds._kwargs['show_bg'] = False
    overlay.paint_view_background(ds, body)
    paint.assert_not_called()


def test_background_definition_changes_invalidate_its_resident_tiles():
    from meltygui.core.cache.tile_cache import TileCacheMasked
    cache = object.__new__(TileCacheMasked)
    cache.func_id_to_keys = {}
    def background():
        pass
    ds = SimpleNamespace(_kwargs={'draw_background': background})
    cache._register_func_keys(ds, 'text-view')
    assert cache._keys_for_func(background) == {'text-view'}


def test_dynamic_background_shadow_uses_owner_and_replaces_prior_marks(scene, monkeypatch):
    from meltygui.core.cache import tile_marks
    from meltygui.core.graphics import gl_state
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.core.styling import style
    body, _, view = scene
    ds = view()
    ds._parent, ds.corner_radius, ds.depth_and_layer = None, 4, (3, 7)
    palette = SimpleNamespace(width=32, height=1, texture_id=123)
    monkeypatch.setattr(Toggles, 'dynamic_styles', True)
    monkeypatch.setattr(Melty, 'draw_state_stack', [view()])
    monkeypatch.setattr(Melty, 'dynamic_style_gl', SimpleNamespace(peek=lambda _: palette))
    for name, value in (('backgrounds', []), ('background_rects', []),
                        ('background_shadow_offsets', {})):
        monkeypatch.setattr(Melty, name, value)
    monkeypatch.setattr(gl_state, 'gl_limits', lambda: {'max_2d': 32})
    monkeypatch.setattr(style, 'resolve_shadow_offset', lambda *args: 2)
    shadow, clear = Mock(), Mock()
    monkeypatch.setattr(tile_marks, 'add_shadow', shadow)
    monkeypatch.setattr(tile_marks, 'clear_shadows', clear)
    for _ in range(2):
        Melty.add_background((.2, .4, .6), draw_state=ds, draw_list=body)
    assert all(owner is ds for owner, _ in Melty.backgrounds)
    assert clear.call_count == 2
    clear.assert_called_with(ds, 'live_background')
    assert shadow.call_args.kwargs == dict(offset=2, corner_radius=4,
        draw_state=ds, group='live_background', depth=3, layer=7, clip=ds.abs_clip_rect)
    assert body.add_image_rounded.call_count == 2
