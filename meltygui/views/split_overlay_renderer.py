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


class _FakeWindowRect:
    """Synthetic stand-in for a window draw_state, used to feed a fixed test
    rect through the real masking path (_setup_channel_stencil)."""
    closed = False
    def __init__(self, layer, x, y, w, h):
        self.layer = layer
        self.abs_left = x
        self.abs_top = y
        self.width = w
        self.height = h


class SplitOverlayRenderer(GlfwRenderer):
    debug_overlay_mask = False  # one-shot diagnostics of window-mask overlay
    # ISOLATION TEST: when set, inject a synthetic high-layer "window" with this
    # fixed-screen rect into window_channels, so it flows through the SAME
    # _setup_channel_stencil path as real windows. If sub-top overlays get cut
    # inside this rect, the per-window masking path itself is correct and the
    # bug is in the real windows' channel/coord data.
    debug_static_mask = False
    debug_static_rect = (400, 400, 400, 400)  # screen-space (x, y, w, h)

    def __init__(self, window, attach_callbacks: bool = True):
        super().__init__(window, attach_callbacks=attach_callbacks)
        self._has_overlay = False
        self._scaled_this_frame = False
        self._mask_debug_logged = False

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
        if not lists:
            self._has_overlay = False
            return

        # If Melty channel-split the foreground list, render each channel
        # with a per-layer stencil mask. Otherwise fall back to a regular draw.
        from src.lsd.gl_gui.melty import Melty
        ranges = getattr(Melty, "_overlay_channel_ranges", None)
        if ranges:
            self._render_overlay_channels(draw_data, lists[-1], ranges, Melty)
        else:
            self._render_command_lists(draw_data, lists[-1:])
        self._has_overlay = False

    def _render_overlay_channels(self, draw_data, overlay_list, ranges, Melty) -> None:
        """Render the foreground draw list one channel at a time. Channel index
        equals layer_channel(draw_state.layer); the top channel is the unmasked
        global overlay. Each channel renders with stencil set to exclude any
        window whose layer_channel is greater than this channel — so the overlay
        appears 'underneath' higher windows."""
        io = self.io
        display_width, display_height = io.display_size
        fb_scale_x, fb_scale_y = io.display_fb_scale
        fb_width = int(display_width * fb_scale_x)
        fb_height = int(display_height * fb_scale_y)

        if fb_width == 0 or fb_height == 0:
            return

        common_gl_state_tuple = get_common_gl_state()
        last_program = gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM)
        last_active_texture = gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)
        last_array_buffer = gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING)
        last_element_array_buffer = gl.glGetIntegerv(gl.GL_ELEMENT_ARRAY_BUFFER_BINDING)
        last_vertex_array = gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING)
        last_stencil_test = gl.glIsEnabled(gl.GL_STENCIL_TEST)

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

        # Upload the merged foreground vtx/idx buffers once for all channels.
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._vbo_handle)
        gl.glBufferData(
            gl.GL_ARRAY_BUFFER,
            overlay_list.vtx_buffer_size * imgui.VERTEX_SIZE,
            ctypes.c_void_p(overlay_list.vtx_buffer_data),
            gl.GL_STREAM_DRAW,
        )
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self._elements_handle)
        gl.glBufferData(
            gl.GL_ELEMENT_ARRAY_BUFFER,
            overlay_list.idx_buffer_size * imgui.INDEX_SIZE,
            ctypes.c_void_p(overlay_list.idx_buffer_data),
            gl.GL_STREAM_DRAW,
        )

        gltype = gl.GL_UNSIGNED_SHORT if imgui.INDEX_SIZE == 2 else gl.GL_UNSIGNED_INT

        # Each command's (idx_lo, idx_hi) span in the merged index buffer. The
        # channel ranges are also in index space, so we render the intersection
        # of each command with each channel - splitting fused commands at
        # channel boundaries (a command's texture/clip are uniform over its span,
        # so any sub-range is valid to draw).
        commands = list(overlay_list.commands)
        cmd_spans = []
        running = 0
        for cmd in commands:
            cmd_spans.append((running, running + cmd.elem_count, cmd))
            running += cmd.elem_count

        # (layer_channel, draw_state) for every registered window with a laid
        # out rect. used to punch holes for windows above each channel.
        top_channel = Melty.max_layer - 1  # global overlay is above all layers
        window_channels = []
        for w in Melty.registered_windows.values():
            ds = getattr(w, "draw_state", None)
            if ds is None or ds.closed:
                continue
            if ds.abs_left is None or ds.abs_top is None or ds.width is None or ds.height is None:
                continue
            window_channels.append((ds.window_index, ds))

        for r in Melty.root_draw_states.values():
            for ds in r:
                if ds is None or ds.closed:
                    continue
                if ds.abs_left is None or ds.abs_top is None or ds.width is None or ds.height is None:
                    continue
                window_channels.append((ds.window_index, ds))

        if self.debug_static_mask:
            sx, sy, sw, sh = self.debug_static_rect
            # Inject at a non-TOP channel so it can mask the controlled debug
            # content drawn on channel 10 (see Melty.end_frame() rect).
            fake_channel = 20
            fake = _FakeWindowRect(fake_channel, sx, sy, sw, sh)
            window_channels.append((fake_channel, fake))

        if self.debug_overlay_mask and not self._mask_debug_logged:
            self._mask_debug_logged = False
            fbo = gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING)
            # GL_STENCIL_BITS is removed in coreGL; query the bound
            # framebuffer's stencil attachment size instead.
            attachment = gl.GL_STENCIL if fbo == 0 else gl.GL_STENCIL_ATTACHMENT
            try:
                stencil_bits = gl.glGetFramebufferAttachmentParameteriv(
                    gl.GL_DRAW_FRAMEBUFFER, attachment,
                    gl.GL_FRAMEBUFFER_ATTACHMENT_STENCIL_SIZE)
            except Exception as e:
                stencil_bits = f"query-failed({e})"
            non_empty = [(c, s, e) for (c, s, e) in ranges if e > s]
            print(f"[overlay-mask] stencil_size={stencil_bits} draw_fbo={fbo} "
                  f"top_channel={top_channel} total_cmds={len(commands)}")
            print(f"[overlay-mask] window_channels={[(c, round(ds.abs_left or 0), round(ds.abs_top or 0)) for c, ds in window_channels]}")
            print(f"[overlay-mask] non_empty_channel_ranges={non_empty}")

        for channel_idx, idx_lo, idx_hi in ranges:
            if idx_hi <= idx_lo:
                continue
            self._setup_channel_stencil(channel_idx, top_channel, window_channels,
                                        fb_height, fb_scale_x, fb_scale_y)

            for c_lo, c_hi, cmd in cmd_spans:
                seg_lo = max(idx_lo, c_lo)
                seg_hi = min(idx_hi, c_hi)
                if seg_hi <= seg_lo:
                    continue
                gl.glBindTexture(gl.GL_TEXTURE_2D, cmd.texture_id)
                x, y, z, w = cmd.clip_rect
                gl.glScissor(int(x), int(fb_height - w), int(z - x), int(w - y))
                gl.glDrawElements(gl.GL_TRIANGLES, seg_hi - seg_lo, gltype,
                                  ctypes.c_void_p(seg_lo * imgui.INDEX_SIZE))

        if self.debug_overlay_mask:
            self._debug_draw_window_rects(window_channels, fb_height,
                                          fb_scale_x, fb_scale_y)

        if last_stencil_test:
            gl.glEnable(gl.GL_STENCIL_TEST)
        else:
            gl.glDisable(gl.GL_STENCIL_TEST)

        restore_common_gl_state(common_gl_state_tuple)
        gl.glUseProgram(last_program)
        gl.glActiveTexture(last_active_texture)
        gl.glBindVertexArray(last_vertex_array)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, last_array_buffer)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, last_element_array_buffer)

    def _debug_draw_window_rects(self, window_channels, fb_height,
                                 fb_scale_x, fb_scale_y) -> None:
        """Visual debug: draw a bright border around each window rect using the
        EXACT transform _setup_channel_stencil uses to punch stencil holes. If
        these borders line up with the on-screen windows, the stencil holes are
        positioned correctly. Magenta border; thickness scales with channel."""
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glEnable(gl.GL_SCISSOR_TEST)
        t = 2
        for win_channel, ds in window_channels:
            x = int(ds.abs_left * fb_scale_x)
            y = int(fb_height - (ds.abs_top + ds.height) * fb_scale_y)
            sw = int(ds.width * fb_scale_x)
            sh = int(ds.height * fb_scale_y)
            if sw <= 0 or sh <= 0:
                continue
            # Color-code by channel so overlapping windows are distinguishable.
            r = 0.2 + 0.8 * ((win_channel * 37) % 100) / 100.0
            g = 0.2 + 0.8 * ((win_channel * 71) % 100) / 100.0
            b = 0.2 + 0.8 * ((win_channel * 13) % 100) / 100.0
            gl.glClearColor(r, g, b, 1.0)
            for bx, by, bw, bh in (
                (x, y + sh - t, sw, t),  # top
                (x, y, sw, t),           # bottom
                (x, y, t, sh),           # left
                (x + sw - t, y, t, sh),  # right
            ):
                gl.glScissor(bx, by, bw, bh)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)

    def _setup_channel_stencil(self, channel_idx, top_channel, window_channels,
                               fb_height, fb_scale_x, fb_scale_y) -> None:
        """Top channel: no stencil (global overlay, never masked). Otherwise
        stencil=1 everywhere, then punched to 0 inside the rect of every window
        whose layer_channel is greater than this channel. The stencil test then
        accepts only non-zero pixels, hiding the overlay where a higher window
        covers it."""
        higher = [(c, ds) for (c, ds) in window_channels if c > channel_idx]
        if channel_idx >= top_channel or not higher:
            gl.glDisable(gl.GL_STENCIL_TEST)
            return

        gl.glEnable(gl.GL_STENCIL_TEST)
        gl.glStencilMask(0xFF)

        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glClearStencil(1)
        gl.glClear(gl.GL_STENCIL_BUFFER_BIT)

        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glClearStencil(0)
        for win_channel, ds in higher:
            x = int(ds.abs_left * fb_scale_x)
            y = int(fb_height - (ds.abs_top + ds.height) * fb_scale_y)
            sw = int(ds.width * fb_scale_x)
            sh = int(ds.height * fb_scale_y)
            if sw <= 0 or sh <= 0:
                continue
            gl.glScissor(x, y, sw, sh)
            gl.glClear(gl.GL_STENCIL_BUFFER_BIT)

        gl.glStencilFunc(gl.GL_NOTEQUAL, 0, 0xFF)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)

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
