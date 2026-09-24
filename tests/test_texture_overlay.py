"""Texture overlays refit cached images without rerunning preparation."""
from types import SimpleNamespace
from unittest.mock import Mock
import inspect

import numpy as np
import OpenGL.GL as gl
import pytest
import meltygui_imgui as imgui

from meltygui.core.graphics.gl_state import GLState
from meltygui.core.rendering.core_decoration import Core
from meltygui.state.new_core_model import ZoomState
from meltygui.state.texture_state import TextureViewState
from meltygui.view.texture_view import draw_texture, draw_texture_overlay


def test_prepared_outputs_cannot_be_linked_between_tiles():
    from meltygui.core.rendering.injected_state import state_parameters
    parameters = state_parameters(draw_texture)
    assert parameters == {'zoom_state': ZoomState}
    assert '_texture_state' not in draw_texture.__auto_state_params__()


def view(width=400, height=300, flip_y=False, crop=None):
    state = SimpleNamespace(texture_id=12, bright_texture_id=13, source_id=11,
                            width=200, height=100, color=0xffffffff, show_info=False,
                            dim_outside=crop, flip_y=flip_y,
                            zoom_state=SimpleNamespace(zoom=1., center_u=.5, center_v=.5))
    return SimpleNamespace(width=width, height=height, abs_left=10, abs_top=20,
                           misc={'_texture_state': state}, get_action=lambda name: None)


def test_frozen_image_refits_and_moves_without_gl_or_body(monkeypatch):
    ds, dl = view(), Mock()
    # An overlay must never query GL, filter, or poll imgui input.
    monkeypatch.setattr(gl, 'glGetIntegerv', Mock(side_effect=AssertionError('GL in overlay')))
    monkeypatch.setattr(imgui, 'get_io', Mock(side_effect=AssertionError('input in overlay')))
    draw_texture_overlay(ds, dl)
    original_uv = dl.add_image_rounded.call_args.args[3:5]
    assert original_uv == ((0., 1.25), (1., -.25))
    assert dl.add_rect.call_args.args[:4] == pytest.approx((12, 72, 413, 273))
    ds.width, ds.height, ds.abs_left = 800, 200, 40
    dl.reset_mock()
    draw_texture_overlay(ds, dl)
    assert dl.add_image_rounded.call_args.args[3:5] == ((-.5, 1.), (1.5, 0.))
    assert dl.add_rect.call_args.args[:4] == pytest.approx((242, 22, 643, 223))
    assert ds.misc['_texture_state'].zoom_state.center_u == .5
    assert dl.push_clip_rect.call_count == dl.pop_clip_rect.call_count == 1


@pytest.mark.parametrize('flip_y', [False, True])
def test_crop_uses_same_live_projection_and_orientation(flip_y):
    ds, dl = view(flip_y=flip_y, crop=(50, 25, 150, 75)), Mock()
    draw_texture_overlay(ds, dl)
    args = dl.add_image.call_args.args
    assert args[:3] == (13, (112., 122.), (312., 222.))
    expected = ((.25, .25), (.75, .75)) if flip_y else ((.25, .75), (.75, .25))
    assert args[3:] == expected


def test_cached_ancestor_replays_texture_overlay_at_current_bounds(monkeypatch):
    from meltygui.core.rendering import overlay
    from meltygui.core.melty import Melty
    ds, dl = view(), Mock(vtx_buffer_size=0)
    ds._kwargs = {'draw_overlay': draw_texture_overlay}
    ds.closed = ds.just_shadow = False
    ds.misc_used = set()
    ds._raw_input_value = 11
    ds.abs_clip_rect = (0, 0, 900, 900)
    ds._abs_left, ds._abs_top = lambda: ds.abs_left, lambda: ds.abs_top
    ds.header_height, ds.current_tint = 0, (0, 0, 0)
    parent = SimpleNamespace(_kwargs={}, misc={})
    tile = SimpleNamespace(overlay_views=(ds,))
    cache = SimpleNamespace(_tiles={'parent': tile}, _stack=[], enabled=True)
    ctx = SimpleNamespace(drew_cached=True, key='parent', draw_state=parent)
    monkeypatch.setattr(imgui, 'get_overlay_draw_list', lambda: dl)
    monkeypatch.setattr(Melty, '_overlay_channels_active', False)
    monkeypatch.setattr(overlay, 'time', SimpleNamespace(perf_counter=lambda: 0))
    overlay.finish_cached_overlays(cache, ctx)
    ds.width = 800
    overlay.finish_cached_overlays(cache, ctx)
    assert dl.add_image_rounded.call_count == 2
    assert dl.add_image_rounded.call_args.args[2][0] == 810
    assert ds.misc['render_overlay'].error is None


def test_views_of_same_input_keep_independent_filtered_pixels(gl_context, monkeypatch):
    from meltygui.graphics.filter import Filter
    from meltygui.model.texture_model import ImageTexture
    texture = ImageTexture('shared', 2, 2, gl.GL_RGBA, bytes([128, 64, 32, 255] * 4))
    filters = Filter()
    monkeypatch.setattr(Core, 'melty', SimpleNamespace(filter=filters, silence_invalidate=True))
    resources = [GLState(), GLState()]
    states = [TextureViewState(), TextureViewState()]
    ds = SimpleNamespace(width=400, height=300, abs_left=0, abs_top=0, on_action=lambda name: None)
    imgui.new_frame()
    imgui.begin('Prepared images')
    try:
        before = imgui.get_window_draw_list().vtx_buffer_size
        snapshots = []
        for i in range(2):
            zoom = ZoomState()
            zoom.brightness = i * .5
            inspect.unwrap(draw_texture)(texture, False, None, None, None, zoom, .3,
                                         draw_state=ds, _texture_state=states[i], gl_state=resources[i],
                                         nearest=bool(i), dim_outside=(0, 0, 1, 1))
            assert imgui.get_window_draw_list().vtx_buffer_size == before
            gl.glBindTexture(gl.GL_TEXTURE_2D, states[i].texture_id)
            snapshots.append(np.array(gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_FLOAT)))
        assert states[0].texture_id != states[1].texture_id
        assert not np.allclose(*snapshots)
        gl.glBindTexture(gl.GL_TEXTURE_2D, states[0].texture_id)
        assert np.array_equal(snapshots[0], gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_FLOAT))
        assert gl.glGetTexParameteriv(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER) == gl.GL_LINEAR
        gl.glBindTexture(gl.GL_TEXTURE_2D, states[1].texture_id)
        assert gl.glGetTexParameteriv(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER) == gl.GL_NEAREST
    finally:
        imgui.end()
        imgui.end_frame()
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        for resource in resources:
            resource.release()
        texture.release()
        filters.cleanup()
        GLState.flush_deletes()
