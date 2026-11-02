"""Fractional positions survive model geometry, ImGui and GPU tile replay."""
import ctypes
from types import SimpleNamespace

import meltygui_imgui as imgui
import numpy as np
import pytest
from OpenGL import GL as gl

from meltygui.core.cache import tile_cache
from meltygui.core.melty import Melty
from meltygui.state.new_core_model import DrawState


def test_nested_scroll_and_drag_keep_fractional_geometry_and_clip(monkeypatch):
    monkeypatch.setattr(DrawState, '_cap_to_display', lambda self, pos, axis: pos)
    parent, scroller, child = DrawState(), DrawState(), DrawState()
    for node in (parent, scroller, child):
        node.width, node.height = 100, 100
        node.left_offset = node.top_offset = 0
        node.window_pos = (0., 0.)
    scroller.parent_window = scroller._parent = parent
    child.parent_window, child._parent = parent, scroller
    child.clip_rect = (0., 0., 100., 100.)
    parent.window_pos = (10.125, 20.25)
    scroller.scroll_offset = (.25, .375)
    assert child.abs_left == 9.875
    assert child.abs_top == 19.875
    assert child.abs_clip_rect[:2] == (9.875, 19.875)
    before = (child.abs_left, child.abs_top)
    parent.window_pos = (10.25, 20.5)
    assert (child.abs_left, child.abs_top) == (before[0] + .125, before[1] + .25)
    scroller.scroll_offset = (.375, .625)
    assert (child.abs_left, child.abs_top) == before


def test_imgui_text_vertices_and_item_layout_follow_fractional_origin():
    previous = imgui.get_current_context()
    context = imgui.create_context()
    io = imgui.get_io()
    io.ini_file_name = None
    io.display_size = (300, 200)
    io.fonts.get_tex_data_as_rgba32()
    def frame(offset):
        imgui.new_frame()
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(300, 200)
        imgui.begin('fractional', flags=imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SAVED_SETTINGS)
        imgui.set_cursor_screen_pos((40.125 + offset, 50.25 + offset))
        dl = imgui.get_window_draw_list()
        start = dl.vtx_buffer_size
        imgui.text('retina text')
        cursor = tuple(imgui.get_cursor_screen_pos())
        vertices = np.frombuffer(ctypes.string_at(dl.vtx_buffer_data, dl.vtx_buffer_size * imgui.VERTEX_SIZE),
                                dtype=np.dtype([('pos', '2f4'), ('uv', '2f4'), ('color', 'u4')]))['pos'][start:].copy()
        imgui.end()
        imgui.render()
        return vertices, cursor
    try:
        frame(0)  # initialize the window
        a, ca = frame(0)
        b, cb = frame(.125)
        assert len(a) > 0
        np.testing.assert_allclose(b - a, .125, atol=1e-5)
        assert cb[1] - ca[1] == pytest.approx(.125)
    finally:
        imgui.destroy_context(context)
        imgui.set_current_context(previous)


@pytest.mark.parametrize('scale', [1., 2., 3.])
def test_cached_image_moves_in_quarter_physical_pixels(gl_context, monkeypatch, scale):
    _, renderer = gl_context
    io = imgui.get_io()
    previous_size, previous_scale = tuple(io.display_size), tuple(io.display_fb_scale)
    cache = tile_cache.TileCacheMasked()
    monkeypatch.setattr(Melty, 'cache', cache)
    tile = tile_cache._ensure_tile(None, 8, 8, pixel_scale=(scale, scale))
    tile.capture_offset = (.375, .75)
    aw, ah = tile_cache._tile_pixel_size(tile, allocated=True)
    yy, xx = np.indices((ah, aw))
    source = np.stack((xx % 2, xx * 0, xx * 0, np.ones_like(xx)), axis=-1).astype('f')
    gl.glBindTexture(gl.GL_TEXTURE_2D, tile.tex)
    gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, aw, ah, gl.GL_RGBA, gl.GL_FLOAT, source)
    fw = fh = int(48 * scale)
    output = tile_cache._create_color_tex(fw, fh)
    fbo, _ = tile_cache._create_fbo_with_tex(output, False, fw, fh)
    rows = []
    try:
        io.display_size, io.display_fb_scale = (48, 48), (scale, scale)
        for physical_offset in (0, .25, .5, .75, 1):
            x, y = 16 + (.375 + physical_offset) / scale, 16 + .75 / scale
            imgui.new_frame()
            uv_a, uv_b = tile_cache._tile_image_uv(tile, (x, y), align_to_pixels=False)
            imgui.get_background_draw_list().add_image(tile.tex, (x, y), (x + 8, y + 8),
                                                       uv_a, uv_b)
            imgui.render()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glClearColor(0, 0, 0, 0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            renderer.render(imgui.get_draw_data())
            pixels = np.array(gl.glReadPixels(0, 0, fw, fh, gl.GL_RGBA, gl.GL_FLOAT)).reshape(fh, fw, 4)
            xs = np.arange(int(16 * scale) + 2, int(24 * scale) - 2)
            row = pixels[fh - int(20 * scale), xs, 0]
            sample_x = xs - 16 * scale - physical_offset
            expected = np.interp(sample_x, np.arange(aw), np.arange(aw) % 2)
            np.testing.assert_allclose(row, expected, atol=.002)
            rows.append(row)
        assert all(not np.array_equal(a, b) for a, b in zip(rows, rows[1:]))
    finally:
        io.display_size, io.display_fb_scale = previous_size, previous_scale
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glDeleteFramebuffers(1, [fbo])
        gl.glDeleteTextures(1, [output])
        cache._tiles['test'] = tile
        cache.cleanup()
