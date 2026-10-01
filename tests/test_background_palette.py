"""Overlay backgrounds reuse the resident palette without synchronous GL reads."""
from types import SimpleNamespace
from unittest.mock import Mock

from OpenGL import GL as gl
import pytest

from meltygui.core.graphics import gl_state
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles


@pytest.fixture
def palette(gl_context, monkeypatch):
    resources = gl_state.GLState()
    limits = {'max_2d': 32}
    draw_list = SimpleNamespace(get_clip_rect_min=lambda: (0, 0),
                                get_clip_rect_max=lambda: (400, 300),
                                add_image_rounded=Mock())
    monkeypatch.setattr(Toggles, 'dynamic_styles', True)
    monkeypatch.setattr(gl_state, 'gl_limits', lambda: limits)
    monkeypatch.setattr(Melty, 'dynamic_style_gl', resources)
    monkeypatch.setattr(Melty, 'draw_state_stack', [])
    monkeypatch.setattr(Melty, 'backgrounds', [])
    monkeypatch.setattr(Melty, 'background_rects', [])
    monkeypatch.setattr(Melty, 'background_inline_indices', set())
    texture = int(gl.glGenTextures(1))
    previous = int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    try:
        yield resources, limits, draw_list, texture
    finally:
        gl.glBindTexture(gl.GL_TEXTURE_2D, previous)
        gl.glDeleteTextures([texture])
        resources.release()
        gl_state.GLState.flush_deletes()


def paint(draw_list):
    Melty.add_background((.1, .2, .3, 1), rect=(10, 20, 80, 30), draw_list=draw_list)


def test_warm_first_background_of_next_frame_never_queries_gl(palette, monkeypatch):
    resources, limits, draw_list, texture = palette
    paint(draw_list)
    first = resources.peek('background_palette')
    assert (first.width, first.height) == (32, 1)
    assert int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)) == texture
    Melty.backgrounds.clear()
    Melty.background_rects.clear()
    Melty.background_inline_indices.clear()
    with monkeypatch.context() as patch:
        patch.setattr(gl, 'glGetIntegerv', Mock(side_effect=AssertionError('warm GL query')))
        patch.setattr(gl, 'glBindTexture', Mock(side_effect=AssertionError('warm texture binding')))
        paint(draw_list)
    assert resources.peek('background_palette') is first
    assert draw_list.add_image_rounded.call_count == 2
    assert draw_list.add_image_rounded.call_args.args[0] == first.texture_id


def test_palette_recreation_restores_binding_even_mid_frame(palette):
    resources, limits, draw_list, texture = palette
    paint(draw_list)
    first = resources.peek('background_palette')
    limits['max_2d'] = 64
    paint(draw_list)
    replacement = resources.peek('background_palette')
    assert replacement is not first
    assert replacement.width == 64
    assert int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)) == texture


def test_failed_palette_allocation_restores_binding(palette, monkeypatch):
    resources, limits, draw_list, texture = palette
    def fail(key, width, height):
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        raise RuntimeError('palette allocation failed')
    monkeypatch.setattr(resources, 'fbo', fail)
    with pytest.raises(RuntimeError, match='palette allocation failed'):
        paint(draw_list)
    assert int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)) == texture
    assert not Melty.backgrounds
    draw_list.add_image_rounded.assert_not_called()
