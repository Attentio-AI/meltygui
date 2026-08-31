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
import glfw
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

    # LCD subpixel text. The font atlas is 3x oversampled horizontally
    # (fonts.py, FontSpec.oversample), so a glyph edge covers exactly three
    # atlas texels per screen pixel and the texel at -1/0/+1 IS the coverage
    # of the R/G/B sub's centre (stb's 3-wide box prefilter = FreeType's
    # 'light' LCD filter). Dual-source blending applies that per channel:
    #   dst = src.rgb * cov.rgb + dst * (1 - cov.rgb)
    # Everything else imgui draws keeps exact stock behaviour: solid
    # geometry (rects, fills, AA fringes) uses the atlas white pixel with a
    # CONSTANT uv -> fwidth(uv) == 0 exactly; thin AA lines sample the
    # TexUvLines strip, which only varies in u -> the two-axis test keeps
    # them grayscale; images/tiles are actual textures (Atlas == 0) and get
    # uniform coverage == alpha, which with the same blend func is plain
    # alpha blending. Proven byte-identical for non-glyph UI lists.
    FRAGMENT_SHADER_SRC = """
    #version 330

    uniform sampler2D Texture;
    uniform vec2 TexelSize;   // 1 / font atlas size
    uniform int Atlas;        // 1: this command samples the font atlas
    uniform int Lcd;          // Toggles.Fonts.lcd_subpixel
    uniform int Bgr;          // Toggles.Fonts.lcd_bgr
    uniform float Gamma;      // Toggles.Fonts.text_gamma
    in vec2 Frag_UV;
    in vec4 Frag_Color;
    layout(location = 0, index = 0) out vec4 Out_Color;
    layout(location = 0, index = 1) out vec4 Out_Cov;

    void main() {
        vec4 t = texture(Texture, Frag_UV.st);
        vec3 cov = vec3(t.a);
        if (Atlas == 1) {
            vec2 fw = fwidth(Frag_UV);
            if (fw.x > 0.0 && fw.y > 0.0) {
                if (Lcd == 1) {
                    float l = texture(Texture, Frag_UV.st - vec2(TexelSize.x, 0.0)).a;
                    float r = texture(Texture, Frag_UV.st + vec2(TexelSize.x, 0.0)).a;
                    cov = (Bgr == 1) ? vec3(r, t.a, l) : vec3(l, t.a, r);
                }
                cov = pow(cov, vec3(1.0 / Gamma));
            }
        }
        float a = Frag_Color.a;
        Out_Color = vec4(Frag_Color.rgb * t.rgb, a * t.a);
        Out_Cov   = vec4(cov * a, a * t.a);
    }
    """

    _STOCK_FRAGMENT_SHADER_SRC = GlfwRenderer.FRAGMENT_SHADER_SRC

    def __init__(self, window, attach_callbacks: bool = True):
        # Set before super().__init__: it builds the device objects (shader)
        # and the font texture, both of which the overrides below stamp onto
        # these slots.
        self._lcd_ok = False
        self._loc_texel = self._loc_atlas = self._loc_lcd = self._loc_bgr = self._loc_gamma = -1
        self._atlas_texel = (0.0, 0.0)
        super().__init__(window, attach_callbacks=attach_callbacks)
        self._has_overlay = False
        self._scaled_this_frame = False
        self._mask_debug_logged = False

    def process_inputs(self):
        super().process_inputs()
        io = imgui.get_io()
        # The stock backend reports the pointer as (-1, -1) whenever the OS
        # window is not FOCUSED - but mouse hovers an unfocused window just
        # fine, and a pointer at (-1, -1) reads as the top-left corner to
        # titlebar._edge_at (the ↘ resize shape over the whole unfocused
        # studio). While the pointer is over the window (GLFW's HOVERED
        # flag) report its real position, so hover cursors and highlights
        # resolve exactly as they do focused; off the window it stays the
        # stock off-window sentinel.
        if not glfw.get_window_attrib(self.window, glfw.FOCUSED) and \
                glfw.get_window_attrib(self.window, glfw.HOVERED):
            io.mouse_pos = glfw.get_cursor_pos(self.window)
        # Shadow margin (titlebar.window_inset): imgui's display is the
        # CONTENT and the real framebuffer is wider by the inset on every
        # side; the render methods paint into that inset viewport and the
        # pointer shifts back by it. Melty.frame_inset / framebuffer_size
        # carry the two numbers to the masks, tiles, etc.
        from src.lsd.gl_gui.titlebar import window_inset, content_origin
        from src.lsd.gl_gui.melty import Melty
        inset = int(window_inset())
        ox, oy = (int(v) for v in content_origin())
        w, h = io.display_size
        Melty.framebuffer_size = (int(w * io.display_fb_scale[0]), int(h * io.display_fb_scale[1]))
        Melty.frame_inset = inset
        Melty.frame_origin = (ox, oy)
        if inset or ox or oy:
            io.display_size = (max(1.0, w - ox - inset), max(1.0, h - oy - inset))
            mx, my = io.mouse_pos
            if mx > -1e6 and my > -1e6:      # -FLT_MAX / (-1, -1) = off-window stays as is
                io.mouse_pos = (mx - ox, my - oy)
        self._cancel_surface_slide(io)
        # A compositor move/resize grab (gl_gui/wayland_move.py) swallowed a
        # button release, so GLFW's level state - what the stock poll above
        # copies into io.mouse_down - stays PRESS until its next real event.
        from src.lsd.gl_gui import wayland_move
        masked = wayland_move.masked_buttons()
        if masked:
            io = imgui.get_io()
            for button in masked:
                if 0 <= button < 3:
                    io.mouse_down[button] = False
        # Orchestrator replay: while a replay drives, its VCR cursor /
        # buttons / modifiers / chars replace the OS ones for imgui (the
        # handler side is muted at the input remap funnel). No-op otherwise.
        try:
            from src.lsd.gl_gui.view.playground.orchestrator import Orchestrator
            Orchestrator.stamp_io(imgui.get_io())
        except Exception:
            pass

    def _create_device_objects(self):
        """Build the LCD program; if the driver can't link it (no dual-source
        blending), fall back to the stock shader + stock blending so text
        still renders, just grayscale."""
        try:
            super()._create_device_objects()
            if not gl.glGetProgramiv(self._shader_handle, gl.GL_LINK_STATUS):
                raise RuntimeError(gl.glGetProgramInfoLog(self._shader_handle))
            self._loc_texel = gl.glGetUniformLocation(self._shader_handle, "TexelSize")
            self._loc_atlas = gl.glGetUniformLocation(self._shader_handle, "Atlas")
            self._loc_lcd = gl.glGetUniformLocation(self._shader_handle, "Lcd")
            self._loc_bgr = gl.glGetUniformLocation(self._shader_handle, "Bgr")
            self._loc_gamma = gl.glGetUniformLocation(self._shader_handle, "Gamma")
            self._lcd_ok = min(self._loc_texel, self._loc_atlas, self._loc_lcd,
                               self._loc_bgr, self._loc_gamma) >= 0
        except Exception as e:
            print(f"SplitOverlayRenderer: LCD text shader unavailable ({e}); "
                  f"falling back to grayscale text")
            self._lcd_ok = False
        if not self._lcd_ok:
            self.FRAGMENT_SHADER_SRC = self._STOCK_FRAGMENT_SHADER_SRC
            super()._create_device_objects()

    def refresh_font_texture(self):
        """Stock upload, with the atlas routed through FontManager.hint_atlas
        (FreeType light-hinted LCD glyphs) when enabled. Same GL state
        handling as the base: save/restore the bound texture, delete the
        previous atlas texture, clear imgui's CPU copy after upload."""
        last_texture = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
        width, height, pixels = self.io.fonts.get_tex_data_as_rgba32()
        pixels = self._hinted_atlas(width, height, pixels) or pixels

        if self._font_texture is not None:
            gl.glDeleteTextures([self._font_texture])
        self._font_texture = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._font_texture)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, width, height, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, pixels)
        self.io.fonts.texture_id = self._font_texture
        gl.glBindTexture(gl.GL_TEXTURE_2D, last_texture)
        self.io.fonts.clear_tex_data()
        self._atlas_texel = (1.0 / max(1, width), 1.0 / max(1, height))

    def _hinted_atlas(self, width, height, pixels):
        """FreeType-hinted texels for the atlas imgui just built, or None to
        upload stb's. Only a FontManager with live handles (it baked the
        current atlas) may probe it. NB: pyimgui returns a fresh wrapper
        from every get_io(), so io identity can't be compared."""
        from src.lsd.gl_gui.toggles import Toggles
        if not Toggles.Fonts.freetype_hinting:
            return None
        from src.lsd.gl_gui.melty import Melty
        fm = getattr(Melty, "font_mgr", None)
        if fm is None or not fm._handles:
            return None
        try:
            return fm.hint_atlas(width, height, pixels)
        except Exception as e:  # never let the atlas upload fail over hinting
            print(f"SplitOverlayRenderer: FreeType hinting pass failed ({e!r}); "
                  f"uploading stb atlas")
            return None

    def _bind_text_mode(self) -> int:
        """Blend func + per-frame LCD uniforms for the bound program. Returns
        the font texture id the per-command Atlas uniform compares against,
        or -1 when running the stock shader."""
        if not self._lcd_ok:
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            return -1
        from src.lsd.gl_gui.toggles import Toggles
        gl.glBlendFuncSeparate(gl.GL_SRC1_COLOR, gl.GL_ONE_MINUS_SRC1_COLOR,
                               gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glUniform2f(self._loc_texel, *self._atlas_texel)
        gl.glUniform1i(self._loc_lcd, 1 if Toggles.Fonts.lcd_subpixel else 0)
        gl.glUniform1i(self._loc_bgr, 1 if Toggles.Fonts.lcd_bgr else 0)
        gamma = float(Toggles.Fonts.text_gamma)
        gl.glUniform1f(self._loc_gamma, gamma if gamma > 0.0 else 1.0)
        return int(self._font_texture) if self._font_texture is not None else -1

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

    # Pointer slide correction: (surface pointer, relative-motion total) at
    # mouse press, None if held.
    _slide_base = None
    # Last frame's (surface pointer, relative-motion total), and the per-axis
    # relative motion the cursor did NOT follow (clamped at the screen
    # edge) since the press - excluded from the slide.
    _slide_last = None
    _slide_clamped = (0.0, 0.0)
    SLIDE_DEADBAND = 1.5

    def _cancel_surface_slide(self, io):
        """While a mouse button is held, subtract the SURFACE's own motion
        from the pointer. The compositor slides the studio to keep it on
        screen when its surface grows past the workarea (the OS-edge push,
        os_frame); the surface-relative pointer then jumps by the slide
        while the hand did not move — a drag reading that as motion grows
        the window more and is slid again, a feedback snap. The slide is
        the difference between the surface-relative pointer's travel and
        the relative-pointer (screen-space) travel since the press; below
        SLIDE_DEADBAND it is rounding, not a slide. Resets on release, so
        hover after a gesture reads the true pointer again."""
        from src.lsd.gl_gui import wayland_move
        if not wayland_move.relative_motion_available():
            return
        down = any(io.mouse_down[i] for i in range(3))
        if not down:
            self._slide_base = None
            self._slide_last = None
            self._slide_clamped = (0.0, 0.0)
            return
        mx, my = io.mouse_pos
        if mx < -1e6 or my < -1e6:
            return
        rel = wayland_move.relative_motion_total()
        if self._slide_base is None:
            self._slide_base = ((mx, my), rel)
            self._slide_last = ((mx, my), rel)
            self._slide_clamped = (0.0, 0.0)
            return
        # The cursor CLAMPED at the screen edge: the hand (relative pointer)
        # keeps moving on an axis while the surface pointer does not move
        # at all on it. That is not the surface sliding - read as one it
        # re-based every root window by the phantom slide that frame (the
        # melty windows "sliding together" once the studio leaves the
        # display) and pushed the pointer past the edge, so the drag kept
        # going. Excluded from the slide, permanently: the cursor does
        # not owe that motion back.
        if self._slide_last is None:          # hotswapped mid-gesture: start the frame diff here
            self._slide_last = ((mx, my), rel)
        (lx, ly), (lrx, lry) = self._slide_last
        d_rel = (rel[0] - lrx, rel[1] - lry)
        cx, cy = self._slide_clamped
        if d_rel[0] and mx == lx:
            cx += d_rel[0]
        if d_rel[1] and my == ly:
            cy += d_rel[1]
        self._slide_clamped = (cx, cy)
        self._slide_last = ((mx, my), rel)
        (bx, by), (rx0, ry0) = self._slide_base
        slide_x = (mx - bx) - (rel[0] - rx0 - cx)
        slide_y = (my - by) - (rel[1] - ry0 - cy)
        if abs(slide_x) < self.SLIDE_DEADBAND:
            slide_x = 0.0
        if abs(slide_y) < self.SLIDE_DEADBAND:
            slide_y = 0.0
        if slide_x or slide_y:
            io.mouse_pos = (mx - slide_x, my - slide_y)

    @staticmethod
    def _frame_origin():
        """Framebuffer origin of imgui's display: (inset, inset) while the
        frameless window carries its shadow margin, else (0, 0). Viewports
        move by it, scissors add it."""
        from src.lsd.gl_gui.melty import Melty
        ox, oy = getattr(Melty, "frame_origin", None) or (0, 0)
        return int(ox), int(oy)

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
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        ox, oy = self._frame_origin()
        gl.glViewport(ox, oy, fb_width, fb_height)

        ortho_projection = (ctypes.c_float * 16)(
             2.0 / display_width, 0.0,                   0.0, 0.0,
             0.0,                 2.0 / -display_height, 0.0, 0.0,
             0.0,                 0.0,                  -1.0, 0.0,
            -1.0,                 1.0,                   0.0, 1.0,
        )

        gl.glUseProgram(self._shader_handle)
        gl.glUniform1i(self._attrib_location_tex, 0)
        gl.glUniformMatrix4fv(self._attrib_proj_mtx, 1, gl.GL_FALSE, ortho_projection)
        font_tex = self._bind_text_mode()
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

        # (channel, draw_state) for every registered window with a laid-out
        # rect - used to punch holes for windows below each channel. Channels
        # go through Melty.overlay_window_channel - the SAME dense rank map the
        # views routed their overlays with - so the "higher than" comparison
        # below stays exact instead of collapsing at the max_layer clamp.
        top_channel = Melty.max_layer - 1  # global overlay is above all windows
        window_channels = []
        for w in Melty.registered_windows.values():
            ds = getattr(w, "draw_state", None)
            if ds is None or ds.closed:
                continue
            if ds.abs_left is None or ds.abs_top is None or ds.width is None or ds.height is None:
                continue
            window_channels.append((Melty.overlay_channel_for(ds), ds))

        for r in Melty.root_draw_states.values():
            for ds in r:
                if ds is None or ds.closed:
                    continue
                # A window hidden because its spawner scrolled out of view
                # isn't dispatched (no pixels this frame), so its stale rect
                # does not punch stencil holes in lower channels.
                if getattr(ds, "_hidden_offscreen", False):
                    continue
                if ds.abs_left is None or ds.abs_top is None or ds.width is None or ds.height is None:
                    continue
                window_channels.append((Melty.overlay_channel_for(ds), ds))

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

        for channel_idx, idx_lo, idx_hi in ranges:
            if idx_hi <= idx_lo:
                continue
            self._setup_channel_stencil(channel_idx, top_channel, window_channels,
                                        fb_height, fb_scale_x, fb_scale_y, ox, oy)

            for c_lo, c_hi, cmd in cmd_spans:
                seg_lo = max(idx_lo, c_lo)
                seg_hi = min(idx_hi, c_hi)
                if seg_hi <= seg_lo:
                    continue
                gl.glBindTexture(gl.GL_TEXTURE_2D, cmd.texture_id)
                if font_tex >= 0:
                    gl.glUniform1i(self._loc_atlas, 1 if int(cmd.texture_id) == font_tex else 0)
                x, y, z, w = cmd.clip_rect
                gl.glScissor(int(x) + ox, int(fb_height - w) + oy, int(z - x), int(w - y))
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
                               fb_height, fb_scale_x, fb_scale_y, ox=0, oy=0) -> None:
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
            x = int(ds.abs_left * fb_scale_x) + ox
            y = int(fb_height - (ds.abs_top + ds.height) * fb_scale_y) + oy
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
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        ox, oy = self._frame_origin()
        gl.glViewport(ox, oy, fb_width, fb_height)

        ortho_projection = (ctypes.c_float * 16)(
             2.0 / display_width, 0.0,                   0.0, 0.0,
             0.0,                 2.0 / -display_height, 0.0, 0.0,
             0.0,                 0.0,                  -1.0, 0.0,
            -1.0,                 1.0,                   0.0, 1.0,
        )

        gl.glUseProgram(self._shader_handle)
        gl.glUniform1i(self._attrib_location_tex, 0)
        gl.glUniformMatrix4fv(self._attrib_proj_mtx, 1, gl.GL_FALSE, ortho_projection)
        font_tex = self._bind_text_mode()
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
                if font_tex >= 0:
                    gl.glUniform1i(self._loc_atlas, 1 if int(command.texture_id) == font_tex else 0)

                x, y, z, w = command.clip_rect
                gl.glScissor(int(x) + ox, int(fb_height - w) + oy, int(z - x), int(w - y))

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
