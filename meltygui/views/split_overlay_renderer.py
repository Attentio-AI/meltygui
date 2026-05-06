"""GlfwRenderer variant that lets the foreground/overlay draw list be deferred.

Frame lifecycle:

    imgui.new_frame()
    # ... build UI; emit overlay primitives via get_foreground_draw_list() ...
    end_frame()
    renderer.begin_frame_split()
    imgui.render()
    draw_data = imgui.get_draw_data()
    renderer.render_except_overlay(draw_data)
    # ... custom GL work happens here, on top of windows but below overlay ...
    renderer.render_overlay_only(draw_data)
"""
from __future__ import absolute_import

import ctypes

import OpenGL.GL as gl
import imgui
from imgui.integrations.glfw import GlfwRenderer
from imgui.integrations.opengl import (
    get_common_gl_state,
    restore_common_gl_state,
)


class SplitOverlayRenderer(GlfwRenderer):
    def __init__(self, window, attach_callbacks: bool = True):
        super().__init__(window, attach_callbacks=attach_callbacks)
        self._has_overlay = False
        self._scaled_this_frame = False

    def begin_frame_split(self) -> None:
        """Snapshot whether the foreground list has content. Call after the UI
        is fully built and before ``imgui.render()``."""
        self._has_overlay = imgui.get_foreground_draw_list().vtx_buffer_size > 0
        self._scaled_this_frame = False

    def render_except_overlay(self, draw_data) -> None:
        self._scale_clip_rects_once(draw_data)
        lists = draw_data.commands_lists
        if self._has_overlay and lists:
            lists = lists[:-1]
        self._render_command_lists(draw_data, lists)

    def render_overlay_only(self, draw_data) -> None:
        if not self._has_overlay:
            return
        self._scale_clip_rects_once(draw_data)
        lists = draw_data.commands_lists
        if lists:
            self._render_command_lists(draw_data, lists[-1:])
        self._has_overlay = False

    def render(self, draw_data) -> None:
        """Drop-in replacement matching the upstream signature; renders all
        lists in a single pass (no split)."""
        self._scale_clip_rects_once(draw_data)
        self._render_command_lists(draw_data, draw_data.commands_lists)
        self._has_overlay = False

    def _scale_clip_rects_once(self, draw_data) -> None:
        if self._scaled_this_frame:
            return
        io = self.io
        draw_data.scale_clip_rects(*io.display_fb_scale)
        self._scaled_this_frame = True

    def _render_command_lists(self, draw_data, command_lists) -> None:
        """Body of ProgrammablePipelineRenderer.render, parameterized over
        which command lists to iterate. GL state save/restore is self-contained
        so this is safe to call multiple times per frame."""
        io = self.io

        display_width, display_height = io.display_size
        fb_width = int(display_width * io.display_fb_scale[0])
        fb_height = int(display_height * io.display_fb_scale[1])

        if fb_width == 0 or fb_height == 0:
            return
        if not command_lists:
            return

        common_gl_state_tuple = get_common_gl_state()
        last_program = gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM)
        last_active_texture = gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)
        last_array_buffer = gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING)
        last_element_array_buffer = gl.glGetIntegerv(gl.GL_ELEMENT_ARRAY_BUFFER_BINDING)
        last_vertex_array = gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING)

        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_FUNC_ADD)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        gl.glViewport(0, 0, fb_width, fb_height)

        ortho_projection = (ctypes.c_float * 16)(
             2.0 / display_width, 0.0,                   0.0, 0.0,
             0.0,                 2.0 / -display_height, 0.0, 0.0,
             0.0,                 0.0,                  -1.0, 0.0,
            -1.0,                 1.0,                   0.0, 1.0,
        )

        gl.glUseProgram(self._shader_handle)
        gl.glUniform1i(self._attrib_location_tex, 0)
        gl.glUniformMatrix4fv(self._attrib_proj_mtx, 1, gl.GL_FALSE, ortho_projection)
        gl.glBindVertexArray(self._vao_handle)

        for commands in command_lists:
            idx_buffer_offset = 0

            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._vbo_handle)
            gl.glBufferData(
                gl.GL_ARRAY_BUFFER,
                commands.vtx_buffer_size * imgui.VERTEX_SIZE,
                ctypes.c_void_p(commands.vtx_buffer_data),
                gl.GL_STREAM_DRAW,
            )

            gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self._elements_handle)
            gl.glBufferData(
                gl.GL_ELEMENT_ARRAY_BUFFER,
                commands.idx_buffer_size * imgui.INDEX_SIZE,
                ctypes.c_void_p(commands.idx_buffer_data),
                gl.GL_STREAM_DRAW,
            )

            for command in commands.commands:
                gl.glBindTexture(gl.GL_TEXTURE_2D, command.texture_id)

                x, y, z, w = command.clip_rect
                gl.glScissor(int(x), int(fb_height - w), int(z - x), int(w - y))

                if imgui.INDEX_SIZE == 2:
                    gltype = gl.GL_UNSIGNED_SHORT
                else:
                    gltype = gl.GL_UNSIGNED_INT

                gl.glDrawElements(
                    gl.GL_TRIANGLES,
                    command.elem_count,
                    gltype,
                    ctypes.c_void_p(idx_buffer_offset),
                )

                idx_buffer_offset += command.elem_count * imgui.INDEX_SIZE

        restore_common_gl_state(common_gl_state_tuple)

        gl.glUseProgram(last_program)
        gl.glActiveTexture(last_active_texture)
        gl.glBindVertexArray(last_vertex_array)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, last_array_buffer)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, last_element_array_buffer)
