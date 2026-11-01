"""Fractional split geometry survives tile layout, resize replay and storage."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from OpenGL import GL as gl

from meltygui.core.cache import tile_cache
from meltygui.core.layout import column_core, tile_manager_core
from meltygui.core.core_render import column_boundary, set_column_width
from meltygui.core.melty import Melty


def test_tile_box_and_split_divider_share_fractional_edges(monkeypatch):
    window = SimpleNamespace(abs_left=10.125, abs_top=20.375)
    state = SimpleNamespace(closable=False, abs_clip_rect=(-100, -100, 1000, 1000))
    monkeypatch.setattr(tile_manager_core, 'layout_window', lambda _: window)
    monkeypatch.setattr(Melty, 'get_clip_rect', lambda: None)
    dl = Mock()
    monkeypatch.setattr(tile_manager_core.imgui, 'get_window_draw_list', lambda: dl)
    monkeypatch.setattr(tile_manager_core, 'hover_shown', lambda *args, **kwargs: False)
    frame = ({'x': 0.}, {'x': 300.25}, {'y': 0.}, {'y': 200.5})
    edges = [frame[0], {'x': 100.375}, frame[1]]
    layout = SimpleNamespace(edges=edges, active_edge=None)
    for offset in (0, .125, .25, .375, -.125):
        window.abs_left, window.abs_top = 10.125 + offset, 20.375 + offset
        rect = tile_manager_core.tile_rect(frame, state, gap=2.25)
        assert rect == pytest.approx((11.25 + offset, 21.5 + offset, 309.25 + offset, 219.75 + offset))
        tile_manager_core.draw_split_dividers(layout, 'x', frame, state)
        args = dl.add_line.call_args.args
        assert args[0] == args[2] == pytest.approx(110.5 + offset)


def test_even_columns_and_width_override_do_not_drop_remainders():
    parent = SimpleNamespace(content_width=100.25, column_offsets=[])
    assert column_boundary(parent.content_width, 3, [], 1) == pytest.approx(100.25 / 3)
    set_column_width(parent, 3, 1, 40.125)
    edges = [column_boundary(parent.content_width, 3, parent.column_offsets, i) for i in range(4)]
    assert edges[2] - edges[1] == pytest.approx(40.125)
    assert edges[3] == 100.25


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_local_frame_tracks_fractional_resize_without_next_frame_snapback(axis):
    from test_column_edge_solve import make_window, run_pass
    from test_row_edge_solve import make_rows_window
    window, left, right, dividers = make_window() if axis == 'x' else make_rows_window()
    dimension = 'width' if axis == 'x' else 'height'
    initial = getattr(window, dimension)
    for offset in (.125, .25, .375, 0):
        pending = window._pending_drags if axis == 'x' else window._pending_row_drags
        pending.append((right, initial + offset, True))
        run_pass(window)
        assert getattr(window, dimension) == pytest.approx(initial + offset)
        assert right[axis] == pytest.approx(initial + offset)
        run_pass(window)
        assert getattr(window, dimension) == pytest.approx(initial + offset)
    setattr(window, dimension, initial + .125)  # externally supplied layout size
    run_pass(window)
    assert right[axis] == pytest.approx(initial + .125)


@pytest.mark.parametrize('size', [(.25, .375), (40.125, 32.375)])
@pytest.mark.parametrize('scale', [(1., 1.), (1.5, 2.), (3., 3.)])
def test_fractional_logical_size_has_nonzero_integer_storage(gl_context, monkeypatch, size, scale):
    cache = tile_cache.TileCacheMasked()
    monkeypatch.setattr(Melty, 'cache', cache)
    tile = tile_cache._ensure_tile(None, *size, pixel_scale=scale)
    try:
        assert tile.size == size
        aw, ah = tile_cache._tile_pixel_size(tile, allocated=True)
        assert isinstance(aw, int) and isinstance(ah, int)
        assert aw > size[0] * scale[0] and ah > size[1] * scale[1]
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, tile.fbo)
        assert gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE
        us, vs, _, _ = tile_cache._tile_uv_rect(tile)
        assert us * aw == pytest.approx(size[0] * scale[0])
        assert vs * ah == pytest.approx(size[1] * scale[1])
        grown_size = tuple(n + .125 for n in size)
        same = tile_cache._ensure_tile(tile, *grown_size, pixel_scale=scale)
        assert same is tile
        assert same.size == grown_size
    finally:
        cache._tiles['test'] = tile
        cache.cleanup()


def test_fractional_growth_clears_only_new_texels_in_capture_grid(gl_context, monkeypatch):
    cache = tile_cache.TileCacheMasked()
    monkeypatch.setattr(Melty, 'cache', cache)
    tile = tile_cache._ensure_tile(None, 10.125, 10.125, pixel_scale=(3, 3))
    tile.capture_offset = (.375, .75)
    aw, ah = tile_cache._tile_pixel_size(tile, allocated=True)
    # Simulate preserved pixels from an older, larger logical size.
    source = np.ones((ah, aw, 4), dtype='f')
    gl.glBindTexture(gl.GL_TEXTURE_2D, tile.tex)
    gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, aw, ah, gl.GL_RGBA, gl.GL_FLOAT, source)
    try:
        before_rect = tile_cache._tile_content_rect(tile, tile.size)
        same = tile_cache._ensure_tile(tile, 10.25, 10.25, pixel_scale=(3, 3))
        assert same is tile and tile.size == (10.25, 10.25)
        assert before_rect[2:] == (31, 32)
        assert tile_cache._tile_content_rect(tile, tile.size)[2:] == (32, 32)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tile.tex)
        pixels = np.asarray(gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_FLOAT)).reshape(ah, aw, 4)
        assert np.all(pixels[ah-32:ah, :31] == 1)  # no lost old edge row
        assert np.all(pixels[ah-32:ah, 31] == 0)  # newly revealed guard texel
        assert np.all(pixels[:ah-32] == 1)
    finally:
        cache._tiles['test'] = tile
        cache.cleanup()


def test_tile_content_wrapper_keeps_fractional_body_and_footer_geometry(gl_context, monkeypatch):
    import meltygui_imgui as imgui
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame
    from meltygui.core.core_render import render_func
    from meltygui.core.styling.style_core import ImGuiStyleManager
    from meltygui.model.tile_model import Tile
    from meltygui.view.tile_view import draw_tile_content

    import importlib
    core = importlib.import_module("meltygui.core.core_render")
    errors = []
    monkeypatch.setattr(core, "_report_draw_exception", lambda e, *args: errors.append(e))
    runtime = _init_melty()
    monkeypatch.setattr(runtime, 'style_manager', ImGuiStyleManager())
    model = "fractional-tile-probe"
    seen = []

    @render_func(multi_instance=True, tint=(.2, .4, .6))
    def body(input_value: object):
        ds = Melty.draw_state_stack[-1]
        seen.append((ds.width, ds.height, ds.abs_left, ds.abs_top))
        return False, input_value
    tile = Tile(render_func=body, input_value=model)

    @render_func(tint=(.2, .4, .6))
    def host(input_value: object):
        imgui.set_cursor_screen_pos((40.125, 60.375))
        draw_tile_content(tile, width=320.125, height=220.375, use_cache=True)
        return False, input_value

    _tick_frame(runtime)
    begin_frame()
    imgui.set_next_window_position(0, 0)
    imgui.set_next_window_size(800, 600)
    imgui.begin('Fractional tile content')
    host(None, width=750.25, height=500.375)
    imgui.end()
    end_frame()
    assert not errors, errors
    assert seen
    assert seen[-1] == pytest.approx((320.125, 192.375, 40.125, 60.375))


def test_fractional_capture_coverage_is_complete_without_repeated_redraw(monkeypatch):
    cache = tile_cache.TileCacheMasked()
    ds = SimpleNamespace(width=80.75, height=60.375, abs_left=10.125, abs_top=20.25,
                         clipped_by_rect=(0, 0, 0, 0))
    tile = SimpleNamespace(size=(ds.width, ds.height), draw_state=ds,
                           filled_bbox=None, last_clean_frame=1, pixel_scale=(3, 3))
    monkeypatch.setattr(cache, '_display_rect_screen', lambda: (0, 0, 300, 200))
    visible = cache._on_screen_tile_rect(ds.abs_left, ds.abs_top, *tile.size)
    cache._accumulate_filled(tile, ds, visible)
    assert tile.filled_bbox == (0, 0, 80.75, 60.375)
    assert cache._tile_fully_filled(tile)
    assert not cache._visible_unfilled(tile, ds)
    # A clipped fractional edge must stay incomplete until actually captured.
    tile.filled_bbox = None
    ds.clipped_by_rect = (.375, .25, .5, .125)
    cache._accumulate_filled(tile, ds, visible)
    assert tile.filled_bbox == (.375, .25, 80.25, 60.25)
    assert not cache._tile_fully_filled(tile)
    assert not cache._visible_unfilled(tile, ds)
    ds.clipped_by_rect = (0, 0, 0, 0)
    assert cache._visible_unfilled(tile, ds)
