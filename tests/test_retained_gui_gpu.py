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


@pytest.mark.parametrize('position',[(2.2,3.2),(2.7,3.7),(8.5,5.5)])
def test_fractional_child_composition_preserves_text_and_single_pixel_detail(gl_context,position):
    import math
    cache=RetainedGui();ids={}
    @cache.gui(width=62.4,height=32.6,tint=(0.,0.,0.,1.))
    def child():
        ids['child']=cache.current
        imgui.text('Pixel sharp')
        dl=imgui.get_window_draw_list()
        for x in range(62):
            dl.add_rect_filled(x,26,x+1,32,0xFFFFFFFF if x%2 else 0xFF000000)
    @cache.gui(width=90,height=50)
    def parent():
        ids['parent']=cache.current
        imgui.set_cursor_screen_pos(position)
        child()
    try:
        parent()
        source=np.flipud(pixels(cache.gpu.texture(ids['child']),63,33))
        output=np.flipud(pixels(cache.gpu.texture(ids['parent']),90,50))
        x,y=position
        left,top,right,bottom=(math.floor(v+.5) for v in (x,y,x+62.4,y+32.6))
        np.testing.assert_array_equal(output[top:bottom,left:right],source[:bottom-top,:right-left])
    finally:cache.close()


def test_fractional_layout_replay_keeps_native_texture_pixels(gl_context):
    import math
    cache=RetainedGui();ids={};layouts={};calls=[]
    @cache.gui(tint=(0.,0.,0.,1.))
    def child(input_value,draw_state=None):
        ids[input_value]=cache.current
        imgui.text('Sharp')
        for x in range(math.ceil(draw_state.width)):
            imgui.get_window_draw_list().add_rect_filled(x,26,x+1,32,
                                                       0xFFFFFFFF if x%2 else 0xFF000000)
    @cache.gui(width=101,height=42)
    def parent():
        calls.append(1);ids['parent']=cache.current
        with cache.geometry.declare(('a','b'),axis=0,key='cols',padding=1,mins=10) as layout:
            layouts['main']=layout
            for name in ('a','b'):
                with layout.cell(name):child(name,key=name)
    try:
        parent()
        for delta in (.25,.75,0.):
            cache.geometry.drag(layouts['main'],1,delta)
            cache.flush()
            output=np.flipud(pixels(cache.gpu.texture(ids['parent']),101,42))
            for name in ('a','b'):
                node=ids[name]
                x,y,w,h=cache.graph.info(node)['rect']
                source=np.flipud(pixels(cache.gpu.texture(node),math.ceil(w),math.ceil(h)))
                l,t,r,b=(math.floor(v+.5) for v in (x,y,x+w,y+h))
                np.testing.assert_array_equal(output[t:b,l:r],source[:b-t,:r-l])
        assert len(calls)==1
    finally:cache.close()


def test_window_title_and_cached_body_move_on_the_same_pixel_grid(gl_context):
    import ctypes
    import math
    from conftest import begin_frame
    cache=RetainedGui();output=TextureCache();ids={}
    @cache.gui(width=62.4,height=32.6,tint=(0.,0.,0.,1.))
    def child():
        ids['child']=cache.current
        imgui.text('Pixel sharp')
    @cache.gui(width=200,height=150)
    def parent():child(melty_window=True,initial={'window_pos':(10.2,12.2)})
    try:
        parent()
        target=output.target(1,200,150)
        reference=None
        for x,y in ((10.2,12.2),(10.7,12.7),(30.35,45.8),(10.2,12.2)):
            cache.graph.move_window(ids['child'],x,y)
            cache._positions.clear()
            begin_frame()
            cache._begin_window(200,150)
            cache.present((0,0))
            imgui.get_window_draw_list().pop_clip_rect()
            imgui.end();imgui.render()
            output.begin_packet(1)
            for draw_list in imgui.get_draw_data().commands_lists:
                commands=[];offset=0
                for command in draw_list.commands:
                    commands.append((int(command.texture_id),command.elem_count,
                                     tuple(command.clip_rect),offset,0))
                    offset+=command.elem_count
                output.add_packet(1,
                    ctypes.string_at(draw_list.vtx_buffer_data,draw_list.vtx_buffer_size*20),
                    ctypes.string_at(draw_list.idx_buffer_data,draw_list.idx_buffer_size*4),commands)
            output.render(1)
            image=np.flipud(pixels(target,200,150))
            left,top=math.floor(x+.5),math.floor(y+.5)
            crop=image[top:top+60,left:left+55]
            if reference is None:reference=crop.copy()
            else:np.testing.assert_array_equal(crop,reference)
    finally:
        output.close();cache.close()


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

    backend = SimpleNamespace(gui_character_callback=None, drag_mouse_pos=None,
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


def test_nested_capture_owns_python_texture_references(gl_context):
    import weakref
    # Keep enough GL names alive that framework textures cannot rely on Python's
    # interned small integers surviving the binding's shared keepalive clear.
    reserved = gl.glGenTextures(300)
    texture = int(reserved[-1])
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 1, 1, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, bytes([0, 255, 0, 255]))
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    cache = RetainedGui()
    refs, ids = [], {}
    fail_capture = [False]
    host = imgui.get_current_context()

    class TextureReference:
        def __int__(self):
            return texture

    @cache.gui(width=10, height=10)
    def child(fail=False):
        if fail:
            raise RuntimeError('capture failed')
        ids['child'] = cache.current
        imgui.get_window_draw_list().add_rect_filled(0, 0, 10, 10, 0xFF0000FF)

    @cache.gui(width=40, height=40)
    def parent():
        ids['parent'] = cache.current
        reference = TextureReference()
        refs.append(weakref.ref(reference))
        imgui.get_window_draw_list().add_image(reference, (20, 20), (40, 40))
        del reference
        child(key='first')
        if fail_capture[0]:
            child(key='failure', fail=True)
        # Assert before dereferencing a stale ImTextureID could enter native code.
        assert refs[-1]() is not None
        child(key='second')
        assert refs[-1]() is not None

    try:
        parent()
        cache.flush()
        assert imgui.get_current_context() is host
        assert cache.gpu.texture(ids['child']) > 256
        result = pixels(cache.gpu.texture(ids['parent']), 40, 40)
        assert result[5, 25, 1] == 255
        assert result[35, 5, 0] == 255
        assert refs[-1]() is not None  # keep resource-owning IDs alive for packet replay
        cache.invalidate_id(ids['parent'])
        cache.flush()
        assert refs[0]() is None  # replacing the packet releases its old references
        np.testing.assert_array_equal(pixels(cache.gpu.texture(ids['parent']), 40, 40), result)
        fail_capture[0] = True
        cache.invalidate_id(ids['parent'])
        with pytest.raises(RuntimeError, match='capture failed'):
            parent()
        assert imgui.get_current_context() is host
        assert refs[-1]() is None
        np.testing.assert_array_equal(pixels(cache.gpu.texture(ids['parent']), 40, 40), result)
    finally:
        cache.close()
        gl.glDeleteTextures(reserved)
    assert all(reference() is None for reference in refs)


@pytest.mark.parametrize('budget', [6000, 32768])
def test_batched_replay_reuses_bounded_scratch_and_preserves_pixels_and_state(gl_context, budget):
    cache = TextureCache(budget=budget)
    colors = [(1., 0., 0., .5), (0., 1., 0., .75)]
    try:
        for node, size in [(1, 16), (2, 24)]:
            cache.target(node, size, size)
            cache.begin_packet(node)
        gl.glViewport(3, 4, 50, 60)
        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glScissor(5, 6, 30, 40)
        previous = {enum: tuple(np.atleast_1d(gl.glGetIntegerv(enum))) for enum in (
            gl.GL_VIEWPORT, gl.GL_SCISSOR_BOX, gl.GL_CURRENT_PROGRAM, gl.GL_DRAW_FRAMEBUFFER_BINDING,
            gl.GL_VERTEX_ARRAY_BINDING, gl.GL_BLEND_SRC_RGB, gl.GL_BLEND_DST_ALPHA)}
        for _ in range(10):
            cache.render_many([(1, colors[0]), (2, colors[1])])
            assert all(tuple(np.atleast_1d(gl.glGetIntegerv(e))) == v for e, v in previous.items())
            assert cache.stats()[2] + cache.scratch_stats()[1] <= budget
            for node, size, rgba in [(1, 16, (255, 0, 0, 128)), (2, 24, (0, 255, 0, 191))]:
                assert np.max(abs(pixels(cache.texture(node), size, size).astype(int) - rgba)) <= 1
        if budget == 32768:
            assert cache.scratch_stats() == (2, (16**2 + 24**2) * 4, 2)
        else:
            assert cache.scratch_stats()[0] == 1  # memory pressure evicts only scratch
        with pytest.raises(RuntimeError, match='missing target'):
            cache.render_many([(1, colors[0]), (999, colors[0])])
        assert all(tuple(np.atleast_1d(gl.glGetIntegerv(e))) == v for e, v in previous.items())
        with pytest.raises(RuntimeError, match='budget'):
            cache.target(3, 100, 100)
    finally:
        cache.close()
    assert cache.scratch_stats()[:2] == (0, 0)
