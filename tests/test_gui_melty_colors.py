"""Run on a reserved desktop: the Rust raster with Melty's colour pipeline.

`TextureCache.melty_colors` makes the replay decode packed vertex colours
(hdr_color.pack_color) into linear light on half-float targets, as the host
draw-list renderer does. Without it the bytes are read as plain RGBA8.
"""
import numpy as np
import pytest
from OpenGL import GL as gl
import meltygui_imgui as imgui
from conftest import _ensure_gl_context
from meltygui import hdr_color
from meltygui.core.rendering.retained_gui_prototype import RetainedGui

# Lever: half-float targets and one 7-bit alpha step bound the comparison.
TOLERANCE = 0.02


@pytest.fixture
def gl_context(monkeypatch):
    from meltygui.core.runtime.toggles import Toggles
    monkeypatch.setattr(Toggles.Rust, 'auto_cache', False)
    _ensure_gl_context()


def linear_pixels(texture, width, height):
    previous = int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    try:
        data = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_FLOAT)
        return np.flipud(np.frombuffer(data, dtype=np.float32).reshape(height, width, 4))
    finally:
        gl.glBindTexture(gl.GL_TEXTURE_2D, previous)


def melty_cache():
    cache = RetainedGui(calibration=False)
    hdr_range, octaves = hdr_color._curve_params()
    matrix = hdr_color.P3_TO_SRGB
    cache._ensure_gpu().melty_colors(
        float(hdr_range), float(octaves), [float(matrix[row][column]) for column in range(3) for row in range(3)],
        1., int(imgui.get_io().fonts.texture_id))
    return cache


def test_packed_colors_decode_to_linear_light(gl_context):
    cache, ids = melty_cache(), {}
    opaque, translucent, bright = (0.5, 0.25, 1.0), (0.8, 0.4, 0.2), hdr_color.white(2.0)[:3]

    @cache.gui(width=60, height=20)
    def swatches():
        ids['node'] = cache.current
        draw_list = imgui.get_window_draw_list()
        draw_list.add_rect_filled(0, 0, 20, 20, hdr_color.pack_color(*opaque, 1.0))
        draw_list.add_rect_filled(20, 0, 40, 20, hdr_color.pack_color(*translucent, 0.5))
        draw_list.add_rect_filled(40, 0, 60, 20, hdr_color.pack_color(*bright, 1.0))
    try:
        swatches()
        image = linear_pixels(cache.gpu.texture(ids['node']), 60, 20)
        expected = [hdr_color.srgb_to_linear(channel) for channel in opaque]
        np.testing.assert_allclose(image[10, 10], [*expected, 1.0], atol=TOLERANCE)
        # Straight alpha after the resolve: the colour is not darkened by its coverage.
        expected = [hdr_color.srgb_to_linear(channel) for channel in translucent]
        np.testing.assert_allclose(image[10, 30], [*expected, 0.5], atol=TOLERANCE)
        # Above reference white: the half-float target keeps it.
        expected = [hdr_color.srgb_to_linear(channel) for channel in bright]
        assert expected[0] > 1.5
        np.testing.assert_allclose(image[10, 50, :3], expected, rtol=0.05)
    finally:
        cache.close()


def test_plain_colors_stay_the_default(gl_context):
    cache, ids = RetainedGui(calibration=False), {}

    @cache.gui(width=20, height=20)
    def swatch():
        ids['node'] = cache.current
        imgui.get_window_draw_list().add_rect_filled(0, 0, 20, 20, 0xFF4080C0)
    try:
        swatch()
        image = linear_pixels(cache.gpu.texture(ids['node']), 20, 20)
        np.testing.assert_allclose(image[10, 10], [0xC0 / 255, 0x80 / 255, 0x40 / 255, 1.0], atol=0.004)
    finally:
        cache.close()


def test_view_tints_are_srgb_colors_in_the_linear_target(gl_context):
    cache, ids = melty_cache(), {}
    opaque, translucent = (0.10, 0.16, 0.24, 1.0), (0.8, 0.4, 0.2, 0.5)

    @cache.gui(width=20, height=20, tint=opaque)
    def solid():
        ids['solid'] = cache.current

    @cache.gui(width=20, height=20, tint=translucent)
    def veil():
        ids['veil'] = cache.current
    try:
        solid()
        veil()
        image = linear_pixels(cache.gpu.texture(ids['solid']), 20, 20)
        expected = [hdr_color.srgb_to_linear(channel) for channel in opaque[:3]]
        np.testing.assert_allclose(image[10, 10], [*expected, 1.0], atol=TOLERANCE)
        image = linear_pixels(cache.gpu.texture(ids['veil']), 20, 20)
        expected = [hdr_color.srgb_to_linear(channel) for channel in translucent[:3]]
        np.testing.assert_allclose(image[10, 10], [*expected, 0.5], atol=TOLERANCE)
    finally:
        cache.close()
