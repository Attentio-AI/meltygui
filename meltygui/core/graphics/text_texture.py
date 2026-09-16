"""Text → GLTexture via imgui's own font atlas — no freetype dependency.

A private imgui context SHARING the main context's font atlas does the text
layout: `add_text` on its foreground draw list, `imgui.render()`, and the
resulting draw data renders into a small FBO with the same mechanics the
screen pass uses (SplitOverlayRenderer._render_command_lists): identical
vertex layout (pos/uv/col at imgui.VERTEX_SIZE strides), an imgui-style
ortho shader, glDrawElements straight off vtx_buffer_data/idx_buffer_data.
Glyphs come from the already-rasterized shared atlas texture, so any font
the FontManager loaded works via `font=`.

GL-thread only — callers sit inside a @render_func body on the render
thread, with the MAIN imgui context current (it is restored even on error).
Module globals survive hotswap re-exec, so the private context and program
are created once per process.
"""

import ctypes

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
import OpenGL.GL as gl

from meltygui.core.graphics.gl_state import GLTexture
from meltygui.hdr_color import GLSL_DECODE as _GLSL_DECODE
from meltygui.hdr_color import GLSL_UNPREMULTIPLY as _GLSL_UNPREMULTIPLY
from meltygui.hdr_color import GLSL_TEXT_CLAMP as _GLSL_TEXT_CLAMP
from meltygui.hdr_color import set_decode_uniforms

_VS = """
#version 330 core
layout(location = 0) in vec2 Position;
layout(location = 1) in vec2 UV;
layout(location = 2) in vec4 Color;
uniform vec2 uSize;
out vec2 fUV;
out vec4 fColor;
""" + _GLSL_DECODE + """
void main() {
    fUV = UV;
    fColor = melty_decode_premultiplied(Color);   // linear scRGB, premultiplied, like the screen pass
    // imgui-style ortho: display y=0 (text top) -> ndc +1, so the baked
    // texture's v=1 row is the TOP of the text.
    gl_Position = vec4(Position.x * 2.0 / uSize.x - 1.0,
                       1.0 - Position.y * 2.0 / uSize.y, 0.0, 1.0);
}
"""

_FS = """
#version 330 core
uniform sampler2D Texture;
in vec2 fUV;
in vec4 fColor;   // premultiplied (see _VS)
out vec4 OutColor;
""" + _GLSL_UNPREMULTIPLY + _GLSL_TEXT_CLAMP + """
void main() {
    vec4 color = melty_unpremultiply(fColor);
    color.rgb = melty_clamp_text(color.rgb);   // the bake is all text: Toggles.HDR.text_max_stops
    OutColor = color * texture(Texture, fUV);
}
"""

# Survives hotswap re-exec (module dict is reused): one private context and
# one GL program per process.
_state = globals().get("_state", {"ctx": None, "prog": None, "u_size": None,
                                  "vao": None, "vbo": None, "ebo": None})


def _compile(shader_type, src):
    s = gl.glCreateShader(shader_type)
    gl.glShaderSource(s, src)
    gl.glCompileShader(s)
    if gl.glGetShaderiv(s, gl.GL_COMPILE_STATUS) != gl.GL_TRUE:
        raise RuntimeError(gl.glGetShaderInfoLog(s).decode(errors="replace"))
    return s


def _ensure_gl():
    if _state["prog"] is not None:
        return
    vs, fs = _compile(gl.GL_VERTEX_SHADER, _VS), _compile(gl.GL_FRAGMENT_SHADER, _FS)
    prog = gl.glCreateProgram()
    gl.glAttachShader(prog, vs)
    gl.glAttachShader(prog, fs)
    gl.glLinkProgram(prog)
    if gl.glGetProgramiv(prog, gl.GL_LINK_STATUS) != gl.GL_TRUE:
        raise RuntimeError(gl.glGetProgramInfoLog(prog).decode(errors="replace"))
    gl.glDeleteShader(vs)
    gl.glDeleteShader(fs)

    vao = gl.glGenVertexArrays(1)
    vbo = gl.glGenBuffers(1)
    ebo = gl.glGenBuffers(1)
    gl.glBindVertexArray(vao)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ebo)
    gl.glEnableVertexAttribArray(0)
    gl.glEnableVertexAttribArray(1)
    gl.glEnableVertexAttribArray(2)
    gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, imgui.VERTEX_SIZE,
                             ctypes.c_void_p(imgui.VERTEX_BUFFER_POS_OFFSET))
    gl.glVertexAttribPointer(1, 2, gl.GL_FLOAT, gl.GL_FALSE, imgui.VERTEX_SIZE,
                             ctypes.c_void_p(imgui.VERTEX_BUFFER_UV_OFFSET))
    gl.glVertexAttribPointer(2, 4, gl.GL_UNSIGNED_BYTE, gl.GL_TRUE, imgui.VERTEX_SIZE,
                             ctypes.c_void_p(imgui.VERTEX_BUFFER_COL_OFFSET))
    gl.glBindVertexArray(0)

    _state.update(prog=prog, u_size=gl.glGetUniformLocation(prog, "uSize"),
                  vao=vao, vbo=vbo, ebo=ebo)


def _ensure_context():
    """The private layout context, created once, sharing the main atlas."""
    if _state["ctx"] is not None:
        return _state["ctx"]
    main = imgui.get_current_context()
    if main is None:
        raise RuntimeError("text_texture: no main imgui context")
    ctx = imgui.create_context(shared_font_atlas=imgui.get_io().fonts)
    imgui.set_current_context(ctx)
    try:
        io = imgui.get_io()
        io.display_size = (4096.0, 512.0)   # layout surface, never rendered
        io.delta_time = 1.0 / 60.0
    finally:
        imgui.set_current_context(main)
    _state["ctx"] = ctx
    return ctx


class _SavedGL:
    """Targeted save/restore of everything the bake pass touches."""

    def __init__(self):
        self.fbo = gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING)
        self.read_fbo = gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING)
        self.viewport = tuple(gl.glGetIntegerv(gl.GL_VIEWPORT))
        self.program = gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM)
        self.vao = gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING)
        self.array_buffer = gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING)
        self.active_texture = gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)
        self.texture_2d = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
        self.blend = bool(gl.glIsEnabled(gl.GL_BLEND))
        self.scissor = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        self.depth = bool(gl.glIsEnabled(gl.GL_DEPTH_TEST))
        self.cull = bool(gl.glIsEnabled(gl.GL_CULL_FACE))

    def restore(self):
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.fbo)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.read_fbo)
        gl.glViewport(*self.viewport)
        gl.glUseProgram(self.program)
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.array_buffer)
        gl.glActiveTexture(self.active_texture)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture_2d)
        (gl.glEnable if self.blend else gl.glDisable)(gl.GL_BLEND)
        (gl.glEnable if self.scissor else gl.glDisable)(gl.GL_SCISSOR_TEST)
        (gl.glEnable if self.depth else gl.glDisable)(gl.GL_DEPTH_TEST)
        (gl.glEnable if self.cull else gl.glDisable)(gl.GL_CULL_FACE)


def _render_draw_data(dd, w, h):
    """Render finalized draw data into a fresh RGBA8 (w, h) texture, exactly
    like the screen pass: stream the vtx/idx pointers, draw per command. No
    scissoring — the bake surface IS the clip."""
    _ensure_gl()
    saved = _SavedGL()
    fbo = gl.glGenFramebuffers(1)
    tex = gl.glGenTextures(1)
    try:
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, w, h, 0,
                        gl.GL_RGBA, gl.GL_HALF_FLOAT, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                           gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, tex, 0)
        if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError("text_texture: bake FBO incomplete")

        gl.glViewport(0, 0, w, h)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        # WHITE-transparent, not black-transparent: mip levels average the
        # RGB of empty texels into the glyph shape, and black gives scaled
        # down/angled text a dark halo. White RGB everywhere keeps every mip
        # level pure white with only coverage in alpha.
        gl.glClearColor(1.0, 1.0, 1.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_FUNC_ADD)
        # ONE/ONE_MINUS_SRC_ALPHA on the alpha channel so coverage accumulates
        # into a transparent target instead of multiplying away.
        gl.glBlendFuncSeparate(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA,
                               gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)

        gl.glUseProgram(_state["prog"])
        gl.glUniform2f(_state["u_size"], float(w), float(h))
        set_decode_uniforms(_state["prog"])
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindVertexArray(_state["vao"])
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, _state["vbo"])
        gltype = gl.GL_UNSIGNED_SHORT if imgui.INDEX_SIZE == 2 else gl.GL_UNSIGNED_INT
        for commands in dd.commands_lists:
            gl.glBufferData(gl.GL_ARRAY_BUFFER,
                            commands.vtx_buffer_size * imgui.VERTEX_SIZE,
                            ctypes.c_void_p(commands.vtx_buffer_data), gl.GL_STREAM_DRAW)
            gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, _state["ebo"])
            gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER,
                            commands.idx_buffer_size * imgui.INDEX_SIZE,
                            ctypes.c_void_p(commands.idx_buffer_data), gl.GL_STREAM_DRAW)
            offset = 0
            for cmd in commands.commands:
                gl.glBindTexture(gl.GL_TEXTURE_2D, cmd.texture_id)
                gl.glDrawElements(gl.GL_TRIANGLES, cmd.elem_count, gltype,
                                  ctypes.c_void_p(offset))
                offset += cmd.elem_count * imgui.INDEX_SIZE
        gl.glBindVertexArray(0)

        # Trilinear mips + anisotropy keep scaled/angled billboard sampling
        # sharp instead of shimmering. Anisotropic filtering is technically an
        # extension (universally supported) - soft-fail if the driver lacks it.
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        try:
            GL_TEXTURE_MAX_ANISOTROPY = 0x84FE
            GL_MAX_TEXTURE_MAX_ANISOTROPY = 0x84FF
            max_aniso = gl.glGetFloatv(GL_MAX_TEXTURE_MAX_ANISOTROPY)
            gl.glTexParameterf(gl.GL_TEXTURE_2D, GL_TEXTURE_MAX_ANISOTROPY,
                               min(8.0, float(max_aniso)))
        except Exception:
            pass
    except Exception:
        gl.glDeleteTextures([tex])
        raise
    finally:
        gl.glDeleteFramebuffers(1, [fbo])
        saved.restore()
    return GLTexture(tex, gl.GL_TEXTURE_2D, (h, w), gl.GL_RGBA16F)


def bake_text(text, font=None, pad=2):
    """Rasterize one line of `text` into a fresh RGBA8 GLTexture — white
    glyphs on transparent, `pad` transparent pixels on every side, texture
    v=1 at the TOP of the text. `font` is any shared-atlas handle (e.g.
    Melty.font_mgr.get(Font.JETBRAINS_MONO_30)); None = the atlas default.
    The caller owns deletion (glDeleteTextures)."""
    main = imgui.get_current_context()
    ctx = _ensure_context()
    imgui.set_current_context(ctx)
    try:
        io = imgui.get_io()
        io.delta_time = 1.0 / 60.0
        imgui.new_frame()
        if font is not None:
            imgui.push_font(font)
        try:
            ts = imgui.calc_text_size(text)
            w = max(1, int(ts.x + 0.5)) + pad * 2
            h = max(1, int(ts.y + 0.5)) + pad * 2
            imgui.get_overlay_draw_list().add_text(
                float(pad), float(pad), pack_color(1, 1, 1, 1), text)
        finally:
            if font is not None:
                imgui.pop_font()
        imgui.render()
        return _render_draw_data(imgui.get_draw_data(), w, h)
    finally:
        imgui.set_current_context(main)


def bake_texts(texts, font=None, pad=2, gap=8):
    """Rasterize MANY strings into ONE texture (a vertical-strip atlas), so a
    whole label set renders as a single instanced draw. Returns
    (GLTexture, {text: (u0, v0, u1, v1, px_w, px_h)}) with v1 at the TOP of
    each string. `gap` transparent rows between entries keep mip levels from
    bleeding neighbours into each other (labels draw near 1:1, so only the
    first mips matter)."""
    texts = list(dict.fromkeys(texts))   # de-dupe, preserving order
    main = imgui.get_current_context()
    ctx = _ensure_context()
    imgui.set_current_context(ctx)
    try:
        io = imgui.get_io()
        io.display_size = (4096.0, 16384.0)   # layout surface; never rendered
        io.delta_time = 1.0 / 60.0
        imgui.new_frame()
        if font is not None:
            imgui.push_font(font)
        try:
            dl = imgui.get_overlay_draw_list()
            col = pack_color(1, 1, 1, 1)
            rows, y, w_max = {}, 0, 1
            for t in texts:
                ts = imgui.calc_text_size(t)
                w = max(1, int(ts.x + 0.5)) + pad * 2
                h = max(1, int(ts.y + 0.5)) + pad * 2
                dl.add_text(float(pad), float(y + pad), col, t)
                rows[t] = (y, w, h)
                w_max = max(w_max, w)
                y += h + gap
            height = max(1, y - gap)
        finally:
            if font is not None:
                imgui.pop_font()
        imgui.render()
        tex = _render_draw_data(imgui.get_draw_data(), w_max, height)
    finally:
        imgui.set_current_context(main)
    rects = {}
    for t, (y0, w, h) in rows.items():
        rects[t] = (0.0, 1.0 - (y0 + h) / height,   # u0, v0 (bottom)
                    w / w_max, 1.0 - y0 / height,   # u1, v1 (top)
                    w, h)
        # consistency with bake_text: display y=0 maps to v=1
    return tex, rects
