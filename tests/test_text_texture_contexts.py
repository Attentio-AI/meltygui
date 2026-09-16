"""Voxel label baking owns non-shareable GL objects in each rendering context."""
import glfw
import numpy as np
import OpenGL.GL as gl
import meltygui_imgui as imgui
from meltygui_imgui.integrations.glfw import GlfwRenderer

from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.text_texture import bake_texts


def test_labels_bake_in_two_contexts_reuse_and_release_resources(gl_context):
    first_window, _ = gl_context
    glfw.window_hint(glfw.VISIBLE, False)
    second_window = glfw.create_window(200, 160, 'label context', None, first_window)
    assert second_window
    states = []
    original_imgui = imgui.get_current_context()
    try:
        for window in (first_window, second_window):
            glfw.make_context_current(window)
            # Each real surface owns an ImGui context and its font texture.
            # Do not borrow the suite's mock/recovered headless font atlas.
            context = imgui.create_context()
            renderer = GlfwRenderer(window, attach_callbacks=False)
            imgui.get_io().ini_file_name = None
            state = GLState()
            states.append(state)
            sentinel = gl.glGenVertexArrays(1)
            gl.glBindVertexArray(sentinel)
            texture, rectangles = bake_texts(('batch', 'feature'), gl_state=state)
            assert int(gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING)) == int(sentinel)
            assert set(rectangles) == {'batch', 'feature'}
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture.texture_id)
            pixels = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_FLOAT)
            assert np.asarray(pixels)[..., 3].max() > 0.1
            pipeline = state.peek('text_pipeline')
            layout = state.peek('text_layout')
            second, _ = bake_texts(('layer',), gl_state=state)
            assert state.peek('text_pipeline') is pipeline
            assert state.peek('text_layout') is layout
            assert gl.glIsVertexArray(pipeline['vao'])
            gl.glDeleteTextures([texture.texture_id, second.texture_id])
            state.release()
            GLState.flush_deletes()
            assert not gl.glIsVertexArray(pipeline['vao'])
            assert not gl.glIsProgram(pipeline['prog'])
            assert not gl.glIsBuffer(pipeline['vbo'])
            assert not gl.glIsBuffer(pipeline['ebo'])
            gl.glBindVertexArray(0)
            gl.glDeleteVertexArrays(1, [sentinel])
            renderer.shutdown()
            imgui.destroy_context(context)
        assert not any(state._resources for state in states)
    finally:
        glfw.make_context_current(first_window)
        imgui.set_current_context(original_imgui)
        glfw.destroy_window(second_window)
