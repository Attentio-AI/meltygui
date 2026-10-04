"""Retina font atlases keep native pixels and stable point-sized layout."""
import ctypes
import struct

import pytest

from meltygui import imgui
from meltygui.core.styling import fonts


@pytest.fixture
def font_context():
    previous = imgui.get_current_context()
    context = imgui.create_context()
    imgui.set_current_context(context)
    io = imgui.get_io()
    io.display_size, io.delta_time = (300, 200), 1 / 60
    try:
        yield io
    finally:
        imgui.destroy_context(context)
        imgui.set_current_context(previous)


def glyph_metrics(manager):
    io = imgui.get_io()
    width, height, _ = io.fonts.get_tex_data_as_rgba32()
    imgui.new_frame()
    imgui.push_font(manager.get(fonts.Font.DEJAVU_SANS_18))
    point_size = imgui.get_font_size()
    draw_list = imgui.get_background_draw_list()
    draw_list.add_text(20, 20, 0xffffffff, 'H')
    imgui.pop_font()
    imgui.render()
    data = ctypes.string_at(draw_list.vtx_buffer_data, 4 * imgui.VERTEX_SIZE)
    left, top, u0, v0 = struct.unpack_from('ffff', data)
    right, bottom, u1, v1 = struct.unpack_from('ffff', data, 2 * imgui.VERTEX_SIZE)
    return point_size, (right - left, bottom - top), ((u1 - u0) * width, (v1 - v0) * height)


@pytest.mark.parametrize('density', [1., 1.5, 2.])
def test_font_texels_follow_backing_density_without_enlarging_layout(font_context, density):
    manager = fonts.FontManager(font_context, pixel_scale=density)
    manager.prewarm()
    point_size, quad, texels = glyph_metrics(manager)
    assert point_size == pytest.approx(18)
    assert texels[1] / quad[1] == pytest.approx(density, abs=.001)
    assert texels[0] / quad[0] == pytest.approx(3 * density, abs=.001)


def test_backing_density_rebuilds_only_when_it_changes(font_context):
    manager = fonts.FontManager(font_context)
    manager.prewarm()
    original_size, _, original_texels = glyph_metrics(manager)
    assert manager.rebuild(1, pixel_scale=2)
    size, _, texels = glyph_metrics(manager)
    assert size == original_size
    assert texels[1] > original_texels[1] * 1.5
    assert not manager.rebuild(1, pixel_scale=2)
    # A hint probe works in physical glyph units, then restores point layout.
    width, height, _ = font_context.fonts.get_tex_data_as_rgba32()
    rect = manager._probe_glyph_rects(manager.get(fonts.Font.DEJAVU_SANS_18), [ord('H')], width, height)[ord('H')]
    assert rect[3] - rect[1] == pytest.approx(rect[7] - rect[5])
    assert font_context.font_global_scale == .5


def test_macos_uses_cocoa_points_for_automatic_ui_size(monkeypatch):
    monkeypatch.setattr(fonts.sys, 'platform', 'darwin')
    assert fonts.detect_auto_scale() == 1
