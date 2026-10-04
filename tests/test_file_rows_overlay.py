"""File row feedback uses live geometry and never enters the captured body."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from OpenGL import GL as gl

from meltygui import imgui
from meltygui.core.melty import Melty
from meltygui.hdr_color import pack_color
from meltygui.state.file_state import FileExplorerState
from meltygui.view.file_view import paint_file_rows_overlay


def row_layout():
    return dict(left_offset=4, top_offset=10, width_reserve=12,
                row_height=20, row_count=20, selected_index=2,
                hover_alpha=.05, rounding=3)


def owner():
    return SimpleNamespace(abs_left=40, abs_top=40, width=200, height=220,
                           scroll_offset=(0, 0), abs_clip_rect=(40, 40, 240, 260),
                           _bounding_hovered=False, current_tint=(.3, .4, .5),
                           corner_radius=3)


def test_row_feedback_tracks_live_size_move_and_scroll(monkeypatch):
    ds, layout, draw_list = owner(), row_layout(), Mock()
    paint = Mock()
    monkeypatch.setattr(Melty, 'paint_selection', paint)
    paint_file_rows_overlay(ds, draw_list, layout)
    paint.assert_called_with(ds, draw_list, (44, 90, 188, 20))
    ds.abs_left, ds.abs_top, ds.width = 60, 70, 340
    ds.scroll_offset = (3, 30)
    ds.abs_clip_rect = (60, 70, 400, 290)
    paint_file_rows_overlay(ds, draw_list, layout)
    paint.assert_called_with(ds, draw_list, (61, 90, 328, 20))
    draw_list.push_clip_rect.assert_called_with(60, 70, 400, 290, True)


@pytest.mark.parametrize('height,filled', [(20, True), (80, False)])
def test_row_and_whole_view_share_selection_style(monkeypatch, height, filled):
    ds, whole, row = owner(), Mock(), Mock()
    ds.height = height
    monkeypatch.setattr(Melty, '_highlight_rgb', lambda tint: (.4, .7, 1.0))
    Melty.paint_selection(ds, whole)
    Melty.paint_selection(ds, row, (ds.abs_left, ds.abs_top, ds.width, ds.height))
    assert whole.mock_calls == row.mock_calls
    assert whole.add_rect_filled.called is filled
    assert whole.add_rect.call_count == 1


def test_row_layout_is_not_saved():
    state = FileExplorerState()
    state.selected = '/tmp/example.py'
    state._row_overlay = row_layout()
    saved = state.to_dict()
    assert saved['selected'] == '/tmp/example.py'
    assert '_row_overlay' not in saved


def test_gpu_selection_leaves_no_body_pixels_after_resize_and_scroll(gl_context, monkeypatch):
    from conftest import begin_frame
    _, renderer = gl_context
    ds, layout = owner(), row_layout()
    monkeypatch.setattr(Melty, '_highlight_rgb', lambda tint: (.4, .7, 1.0))
    monkeypatch.setattr(Melty, '_overlay_channels_active', False)
    monkeypatch.setattr(Melty, 'on_drag', False)

    def frame():
        begin_frame()
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(500, 350)
        imgui.begin('File row overlay', flags=imgui.WINDOW_NO_TITLE_BAR)
        body = imgui.get_window_draw_list()
        body.add_rect_filled(20, 20, 480, 320, pack_color(.1, .1, .1, 1))
        vertices = body.vtx_buffer_size
        paint_file_rows_overlay(ds, imgui.get_overlay_draw_list(), layout)
        assert body.vtx_buffer_size == vertices
        imgui.end()
        imgui.render()
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        renderer.render(imgui.get_draw_data())
        io = imgui.get_io()
        scale_x, scale_y = io.display_fb_scale
        framebuffer_height = int(io.display_size.y * scale_y)
        def pixel(x, y):
            return np.asarray(gl.glReadPixels(int(x * scale_x), framebuffer_height - int(y * scale_y), 1, 1,
                                              gl.GL_RGBA, gl.GL_FLOAT)).reshape(4)[:3]
        return pixel

    first = frame()
    selected_before = first(100, 100)
    background = first(100, 140)
    assert np.max(np.abs(selected_before - background)) > .01
    ds.width = 340
    ds.abs_clip_rect = (40, 40, 380, 260)
    ds.scroll_offset = (0, 20)
    layout['selected_index'] = 6
    second = frame()
    np.testing.assert_allclose(second(100, 100), background, atol=.005)
    assert np.max(np.abs(second(340, 160) - background)) > .01
    np.testing.assert_allclose(second(380, 160), background, atol=.005)
