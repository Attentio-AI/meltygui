"""Run on a reserved desktop: real GL capture/replay and ownership cleanup."""
import struct
import numpy as np
import pytest
from OpenGL import GL as gl
import meltygui_imgui as imgui
from conftest import _ensure_gl_context
from meltygui.core.rendering._gui_native import TextureCache
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


@pytest.fixture
def gl_context():
    _ensure_gl_context()


def pixels(texture, width, height):
    previous = int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    try:
        return np.frombuffer(gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE),
                             dtype=np.uint8).reshape(height, width, 4).copy()
    finally:
        gl.glBindTexture(gl.GL_TEXTURE_2D, previous)


def test_native_packets_alpha_clipping_and_gl_state(gl_context):
    white = int(gl.glGenTextures(1))
    gl.glBindTexture(gl.GL_TEXTURE_2D, white)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 1, 1, 0, gl.GL_RGBA,
                    gl.GL_UNSIGNED_BYTE, bytes([255, 255, 255, 255]))
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    cache = TextureCache()
    try:
        target = cache.target(1, 16, 16)
        vertices = b''.join(struct.pack('4f4B', x, y, .5, .5, 255, 0, 0, 128)
                            for x, y in [(0, 0), (16, 0), (16, 16), (0, 16)])
        indices = struct.pack('6I', 0, 1, 2, 0, 2, 3)
        cache.begin_packet(1)
        cache.add_packet(1, vertices, indices, [(white, 6, (0, 0, 8, 16), 0, 0)])
        gl.glViewport(3, 4, 50, 60)
        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glScissor(5, 6, 30, 40)
        gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ZERO, gl.GL_ZERO, gl.GL_ONE)
        gl.glClearColor(.1, .2, .3, .4)
        previous = {enum: tuple(np.atleast_1d(gl.glGetIntegerv(enum))) for enum in (
            gl.GL_VIEWPORT, gl.GL_SCISSOR_BOX, gl.GL_CURRENT_PROGRAM, gl.GL_DRAW_FRAMEBUFFER_BINDING,
            gl.GL_VERTEX_ARRAY_BINDING, gl.GL_BLEND_SRC_RGB, gl.GL_BLEND_DST_ALPHA)}
        cache.render(1)
        assert all(tuple(np.atleast_1d(gl.glGetIntegerv(enum))) == value for enum, value in previous.items())
        result = pixels(target, 16, 16)
        assert np.all(result[:, :8, 0] >= 253)  # straight-alpha red, not twice multiplied
        assert np.all(abs(result[:, :8, 3].astype(int) - 128) <= 1)
        assert np.all(result[:, 8:] == 0)
        assert cache.target(1, 24, 24) == target  # retained parent references remain valid
    finally:
        cache.close()
        gl.glDeleteTextures([white])


def test_child_texture_updates_without_ancestor_execution_and_window_retires(gl_context):
    cache = RetainedGui()
    color = [0xFF0000FF]  # packed RGBA: red
    closed = [False]
    counts = [0, 0, 0]
    ids = {}

    @cache.gui(width=30, height=30)
    def child(input_value: object, cache=None):
        counts[1] += 1
        ids['child'] = cache.current
        imgui.get_window_draw_list().add_rect_filled(0, 0, 30, 30, color[0])
        return False, input_value

    @cache.gui(width=20, height=20)
    def portal(input_value: object, cache=None):
        counts[2] += 1
        ids['portal'] = cache.current
        imgui.get_window_draw_list().add_rect_filled(0, 0, 20, 20, 0xFFFFFFFF)
        cache.region('click', (0, 0, 20, 20))
        return False, input_value

    @cache.gui(width=60, height=60)
    def root(input_value: object, cache=None):
        counts[0] += 1
        ids['root'] = cache.current
        imgui.set_cursor_screen_pos((5, 5))
        child(None)
        if not closed[0]:
            portal(None, melty_window=True)
        return False, input_value

    try:
        root(None)
        cache.flush()
        before = pixels(cache.gpu.texture(ids['root']), 60, 60)
        assert before[40, 15, 0] > 250 and before[40, 15, 1] < 3
        start = cache.gpu.stats()
        color[0] = 0xFF00FF00
        cache.invalidate_id(ids['child'])
        cache.flush()
        after = pixels(cache.gpu.texture(ids['root']), 60, 60)
        assert after[40, 15, 1] > 250 and after[40, 15, 0] < 3
        assert counts == [1, 2, 1]
        assert cache.gpu.stats()[0] - start[0] == 2  # child raster + parent command replay
        stable = cache.gpu.stats()
        for _ in range(10):
            root(None)
            cache.flush()
        assert cache.gpu.stats() == stable
        old_texture = cache.gpu.texture(ids['portal'])
        closed[0] = True
        cache.invalidate_id(ids['root'])
        cache.flush()
        assert cache.graph.windows() == []
        assert not gl.glIsTexture(old_texture)
        assert ids['portal'] not in cache.regions
    finally:
        cache.close()


def test_gui_window_host_owns_capture_position_and_cleanup(gl_context, monkeypatch):
    from types import SimpleNamespace
    from conftest import begin_frame, end_frame
    from meltygui.core.melty import Melty
    from meltygui.core.runtime.app import _root_body
    from meltygui.core.rendering.gui_prototype import gui

    monkeypatch.setattr(Melty, 'root_fill', (600., 400., 30.), raising=False)
    calls = []

    @gui(width=30, height=30)
    def view(input_value: object):
        calls.append(1)
        imgui.get_window_draw_list().add_rect_filled(0, 0, 30, 30, 0xFF00FF00)
        return False, input_value

    @gui(use_cache=False)
    def app(input_value: object):
        imgui.set_cursor_screen_pos((45, 70))
        view(None)
        return False, input_value

    backend = SimpleNamespace(gui_character_callback=None,
                              _get_clipboard_text=lambda: '',
                              _set_clipboard_text=lambda text: None)
    surface = SimpleNamespace(_gui_prototype=None, request_frame=lambda: None, impl=backend)
    body = _root_body(app, 'native root')
    try:
        for _ in range(2):
            begin_frame()
            imgui.set_next_window_size(600, 400)
            imgui.begin('window-owned gui')
            body(surface)
            imgui.end()
            end_frame()
        assert len(calls) == 1
        owner = surface._gui_prototype
        assert backend.gui_character_callback == owner.cache.imgui_input.character
        node = owner.cache.graph.nodes()[0]
        assert owner.cache.graph.info(node)['rect'] == (45., 70., 30., 30.)
        texture = owner.cache.gpu.texture(node)
        assert pixels(texture, 30, 30)[15, 15, 1] > 250
        owner.close()
        assert backend.gui_character_callback is None
        assert not gl.glIsTexture(texture)
        assert owner.cache.contexts == {} and owner.cache.records == {}
    finally:
        if surface._gui_prototype:
            surface._gui_prototype.close()
