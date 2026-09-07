"""The fp16 scene target and the presentation pass.

Every frame renders into ONE offscreen target instead of the window's
default framebuffer: RGBA16F colour (linear scRGB, negatives and values
above 1 kept — see hdr_color.py) plus a D24S8 renderbuffer (the split
renderer's per-window stencil masks need it). Everything that used to say
"framebuffer 0" — the studio's clear, imgui, the tile-cache snapshot, the
filters, the corner pass — asks `Melty.default_framebuffer()` and gets the
scene while a frame is open.

`present()` is the last GL work before the swap: it binds the real default
framebuffer and encodes the scene for the swapchain — `Toggles.HDR.output`
"srgb" for an untagged SDR window, "pq" (BT.2020 + ST 2084) for a surface
tagged PQ. The swapchain is 8-bit on this driver (NVIDIA egl-wayland offers
no 10-bit / fp16 window configs), so the encode is where HDR is quantized;
an fp16 dmabuf swapchain replaces this pass with a copy.

Module-level GL state survives hotswap (`globals().get`), like titlebar.py.
"""
from __future__ import annotations

import OpenGL.GL as gl

from src.lsd.gl_gui.gl_state import GLState, GLTexture, is_gl_thread
from src.lsd.gl_gui.hdr_color import GLSL_ENCODE
from src.lsd.gl_gui.shader_func import shader_func

_STATE = globals().get("_STATE", {
    "fbo": 0, "tex": None, "rbo": None, "size": (0, 0), "active": False, "gl": None,
})


def active() -> bool:
    return bool(_STATE["active"])


def framebuffer() -> int:
    """The framebuffer a frame's draws go to: the scene while one is open, else 0."""
    return int(_STATE["fbo"]) if _STATE["active"] else 0


def texture() -> GLTexture | None:
    return _STATE["tex"]


def _release():
    if _STATE["fbo"]:
        gl.glDeleteFramebuffers(1, [int(_STATE["fbo"])])
    if _STATE["rbo"]:
        gl.glDeleteRenderbuffers(1, [int(_STATE["rbo"])])
    if _STATE["tex"] is not None:
        gl.glDeleteTextures([_STATE["tex"].texture_id])
    _STATE.update(fbo=0, tex=None, rbo=None, size=(0, 0))


def _ensure(width: int, height: int) -> None:
    width, height = max(1, int(width)), max(1, int(height))
    if _STATE["fbo"] and _STATE["size"] == (width, height):
        return
    _release()
    tex = int(gl.glGenTextures(1))
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, width, height, 0,
                    gl.GL_RGBA, gl.GL_HALF_FLOAT, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    rbo = int(gl.glGenRenderbuffers(1))
    gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, rbo)
    gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH24_STENCIL8, width, height)
    gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
    fbo = int(gl.glGenFramebuffers(1))
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, tex, 0)
    gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT, gl.GL_RENDERBUFFER, rbo)
    status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
    if status != gl.GL_FRAMEBUFFER_COMPLETE:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glDeleteFramebuffers(1, [fbo])
        gl.glDeleteRenderbuffers(1, [rbo])
        gl.glDeleteTextures([tex])
        raise RuntimeError(f"scene target incomplete: 0x{status:04X} ({width}x{height})")
    _STATE.update(fbo=fbo, rbo=rbo, size=(width, height),
                  tex=GLTexture(tex, gl.GL_TEXTURE_2D, (height, width), gl.GL_RGBA16F))


def begin(width: int, height: int) -> int:
    """Frame start (LSDStudio.render, before the clear): size the scene to
    the REAL framebuffer and bind it. Returns the framebuffer bound."""
    if not is_gl_thread():
        return 0
    _ensure(width, height)
    _STATE["active"] = True
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, int(_STATE["fbo"]))
    return int(_STATE["fbo"])


_PRESENT_FRAG = """
#version 330 core
uniform sampler2D scene;
in vec2 uv;
out vec4 FragColor;
""" + GLSL_ENCODE + """
void main() {
    vec4 c = texture(scene, uv);
    if (pq_output == 1) {
        // Linear scRGB -> BT.2020 -> PQ. reference_nits is what 1.0 shows
        // as (the desktop's SDR reference), so SDR content matches an
        // untagged window and white(4) lands at 4x that.
        // Convert primaries FIRST, clamp after: a P3 / wide colour is
        // scRGB with NEGATIVE components (p3(1,0,0) = (1.22, -0.04, -0.02)
        // linear) and clamping them before the matrix collapses it back to
        // the sRGB gamut. BT.2020 contains P3, so the result is non-negative.
        vec3 nits = max(MELTY_SRGB_TO_BT2020 * c.rgb, 0.0) * reference_nits;
        FragColor = vec4(melty_pq_encode(nits), c.a);
    } else {
        FragColor = vec4(melty_linear_to_srgb(c.rgb), c.a);
    }
}
"""


@shader_func(fragment=_PRESENT_FRAG)
def _present_pass(gl_state: GLState = None, scene=None, pq_output=0, reference_nits=250.0, **kwargs):
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


def present(width: int, height: int) -> bool:
    """Frame end (Melty.post_frame, after the corner pass): encode the scene
    into the default framebuffer. The scene stays intact for readbacks."""
    if not _STATE["active"] or not is_gl_thread():
        return False
    from src.lsd.gl_gui.toggles import Toggles
    _STATE["active"] = False
    if _STATE["gl"] is None:
        _STATE["gl"] = GLState()
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
    gl.glViewport(0, 0, int(width), int(height))
    gl.glDisable(gl.GL_SCISSOR_TEST)
    gl.glDisable(gl.GL_DEPTH_TEST)
    gl.glDisable(gl.GL_STENCIL_TEST)
    gl.glDisable(gl.GL_BLEND)
    gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
    # PQ only once the surface actually carries the tag (wayland_color.sync):
    # an untagged app is sRGB to the compositor, whatever the toggle says.
    from src.lsd.gl_gui import wayland_color
    pq = wayland_color.resolved_output() == "pq" and (wayland_color.applied() == "pq" or not wayland_color.available())
    # 1.0 = the desktop's SDR white: the reference the applied PQ tag carries
    # (wayland_color.desired_reference - the compositor's preferred
    # description, else Toggles.HDR.pq_reference_nits).
    reference = wayland_color.reference_nits() or wayland_color.desired_reference()
    _present_pass(_STATE["gl"], scene=_STATE["tex"], pq_output=1 if pq else 0,
                  reference_nits=float(reference))
    gl.glBindVertexArray(0)
    return True


def shutdown() -> None:
    if is_gl_thread():
        _release()
    _STATE["active"] = False