"""Ordered instanced full-mask stamps, owned by one render-thread tile cache."""
import ctypes
from array import array

from OpenGL import GL as gl

# Use only the three texture units already owned by finalize_captures.
# This avoids querying/restoring unrelated units in the middle of a GPU pass.
TEXTURES_PER_BATCH = 3
_VERTEX = """
#version 330 core
layout(location=0) in vec4 aRect;
layout(location=1) in vec4 aClip;
layout(location=2) in vec4 aUV;
layout(location=3) in vec4 aParams;
uniform vec2 uFBSize;
out vec2 vUV;
flat out vec4 vUVRect;
flat out vec4 vParams;
flat out vec2 vSize;
void main() {
    vec2 c = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1));
    vec2 p = mix(aClip.xy, aClip.zw, c);
    vUV = (p - aRect.xy) / aRect.zw;
    vUVRect = aUV;
    vParams = aParams;
    vSize = aRect.zw;
    gl_Position = vec4(p / uFBSize * 2.0 - 1.0, 0.0, 1.0);
}
"""
_FRAGMENT = """
#version 330 core
uniform sampler2D uTextures[3];
in vec2 vUV;
flat in vec4 vUVRect;
flat in vec4 vParams; // rank/offset, rounding, margin, texture slot (-1 = flat)
flat in vec2 vSize;
out vec4 oColor;
float sampleMask(int slot, vec2 uv) {
    // GLSL 330 requires constant sampler-array indices.
    switch(slot) {
        case 0: return texture(uTextures[0], uv).r;
        case 1: return texture(uTextures[1], uv).r;
        default: return texture(uTextures[2], uv).r;
    }
}
void main() {
    int slot = int(vParams.w);
    if (slot >= 0 || vParams.y > 0.0) {
        vec2 halfSize = vSize * 0.5;
        float r = min(vParams.y, min(halfSize.x, halfSize.y));
        vec2 q = abs((vUV - 0.5) * vSize) - halfSize + r;
        float d = min(max(q.x, q.y), vParams.z) + length(max(q, vParams.z)) - r;
        if (d > 0.0) discard;
    }
    float value = vParams.x;
    if (slot >= 0) {
        float sampled = sampleMask(slot, vUV * vUVRect.xy + vUVRect.zw);
        if (sampled <= 0.0) discard;
        value += sampled;
    }
    oColor = vec4(value, 0.0, 0.0, 1.0);
}
"""


def batches(stamps):
    """Pack consecutive stamps, splitting only when the sampler bank fills.

    Each stamp is (texture or None, rect xywh, clip xywh, UV transform,
    rank/offset, corner radius, shadow margin). Clip geometry, not UVs.
    """
    textures, slots, records = [], {}, []
    for texture, rect, clip, uv, rank, radius, margin in stamps:
        x, y, w, h = rect
        cx, cy, cw, ch = clip
        left, bottom, right, top = max(x, cx), max(y, cy), min(x + w, cx + cw), min(y + h, cy + ch)
        if right <= left or top <= bottom:
            continue
        if texture is not None and texture not in slots:
            if len(textures) == TEXTURES_PER_BATCH:
                yield textures, array('f', records).tobytes(), len(records) // 16
                textures, slots, records = [], {}, []
            slots[texture] = len(textures)
            textures.append(texture)
        records.extend((*rect, left, bottom, right, top, *uv,
                        rank, radius, margin, slots[texture] if texture is not None else -1))
    if records:
        yield textures, array('f', records).tobytes(), len(records) // 16


class MaskBatch:
    def __init__(self):
        self.program = self.vao = self.vbo = None
        self.source = None
        self.multi_bind = False
        self.fb_location = self.textures_location = None

    def close(self):
        if self.program is not None:
            gl.glDeleteProgram(self.program)
        if self.vbo is not None:
            gl.glDeleteBuffers(1, [self.vbo])
        if self.vao is not None:
            gl.glDeleteVertexArrays(1, [self.vao])
        self.program = self.vao = self.vbo = None
        self.source = None

    def ensure(self):
        if (self.program is not None and self.source == (id(_VERTEX), id(_FRAGMENT), id(type(self).ensure.__code__))
                and getattr(self, "multi_bind", None) is not None):
            return
        self.close()
        from meltygui.core.cache.tile_cache import _compile, _link
        self.program = _link(_compile(gl.GL_VERTEX_SHADER, _VERTEX),
                             _compile(gl.GL_FRAGMENT_SHADER, _FRAGMENT))
        self.source = (id(_VERTEX), id(_FRAGMENT), id(type(self).ensure.__code__))
        self.fb_location = gl.glGetUniformLocation(self.program, 'uFBSize')
        self.textures_location = gl.glGetUniformLocation(self.program, 'uTextures[0]')
        version = (int(gl.glGetIntegerv(gl.GL_MAJOR_VERSION)), int(gl.glGetIntegerv(gl.GL_MINOR_VERSION)))
        from OpenGL.GL.ARB.multi_bind import glInitMultiBindARB
        self.multi_bind = (version >= (4, 2) and (version >= (4, 4) or glInitMultiBindARB())
                           and bool(gl.glBindTextures) and bool(gl.glDrawArraysInstancedBaseInstance))
        gl.glUseProgram(self.program)
        gl.glUniform1iv(self.textures_location, TEXTURES_PER_BATCH, list(range(TEXTURES_PER_BATCH)))
        self.vao, self.vbo = int(gl.glGenVertexArrays(1)), int(gl.glGenBuffers(1))
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        for location in range(4):
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribPointer(location, 4, gl.GL_FLOAT, gl.GL_FALSE,
                                     64, ctypes.c_void_p(location * 16))
            gl.glVertexAttribDivisor(location, 1)

    def draw(self, stamps, width, height, *, restore_vao):
        """Draw within the cache's mask pass, which owns texture units 0–2.

        Like the other mask painters, this owns viewport/program/scissor state.
        Return to the caller's fullscreen VAO and texture unit zero; no GL
        queries or unrelated sampler units enter the submission loop.
        """
        packed = list(batches(stamps))
        if not packed:
            return
        try:
            self.ensure()
            gl.glUseProgram(self.program)
            gl.glUniform2f(self.fb_location, width, height)
            gl.glBindVertexArray(self.vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glViewport(0, 0, width, height)
            if self.multi_bind:
                data = b''.join(data for _, data, _ in packed)
                gl.glBufferData(gl.GL_ARRAY_BUFFER, len(data), data, gl.GL_STREAM_DRAW)
            first_instance = 0
            for textures, data, count in packed:
                # Populate unused slots with a valid source, avoiding feedback
                # from a texture attached to the current framebuffer. Zero is
                # bound to GL_TEXTURE_2D explicitly (bulk zero unbinds ALL targets).
                bank = textures + [textures[0]] * (TEXTURES_PER_BATCH - len(textures)) if textures else [0] * TEXTURES_PER_BATCH
                if self.multi_bind and textures:
                    gl.glBindTextures(0, TEXTURES_PER_BATCH, bank)
                else:
                    for unit, texture in enumerate(bank):
                        gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
                        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
                if self.multi_bind:
                    gl.glDrawArraysInstancedBaseInstance(gl.GL_TRIANGLE_STRIP, 0, 4, count, first_instance)
                    first_instance += count
                else:
                    gl.glBufferData(gl.GL_ARRAY_BUFFER, len(data), data, gl.GL_STREAM_DRAW)
                    gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, count)
        finally:
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindVertexArray(restore_vao)
