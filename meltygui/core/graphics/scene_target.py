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
tagged PQ, "linear" for one tagged extended linear. The EGL window is 8-bit
on this driver (NVIDIA egl-wayland offers no 10-bit / fp16 window configs),
so for "srgb" and "pq" the encode is where HDR is quantized, and PQ ends at
10,000 nits. "linear" does not go through that window: the scene is copied
into a half-float dmabuf the compositor is handed as it is (`_float_target`,
native_wayland `float_buffer`), so nothing is quantized or clamped here.
Levers: `CHAIN`, `SPARE`.

Module-level GL state survives hotswap (`globals().get`), like titlebar.py.
"""
from __future__ import annotations

from meltygui.core.graphics import desktop_gl as gl

from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.gl_state import GLTexture
from meltygui.core.graphics.gl_state import is_gl_thread
from meltygui.hdr_color import GLSL_ENCODE
from meltygui.core.graphics.shader_func import shader_func

_STATE = globals().get("_STATE", {
    "fbo": 0, "tex": None, "rbo": None, "size": (0, 0), "active": False, "gl": None,
})

CHAIN = 3       # float buffers a window cycles through: one on screen, one handed over, one to draw
SPARE = 6       # ... and at most this many when the compositor is slow to give them back


def _float_backend():
    """The window backend when it can present half-float buffers, else None."""
    from meltygui.core.windowing import window_api
    backend = window_api._state['backend']
    if backend is None or not getattr(backend, 'native', None) or not backend.float_ready():
        return None
    return backend


def float_possible() -> bool:
    """Whether "linear" output can be presented in the current window (wayland_color asks before
    it tags the surface): the backend can, and the window was made for it (native_wayland)."""
    backend = _float_backend()
    if backend is None or _STATE.get("float_failed"):
        return False
    return bool(getattr(backend.get_current_context(), "float", False))


def float_fail():
    """Float presentation is not to be tried again for this surface (its tag could not be set)."""
    _STATE["float_failed"] = True


def _float_drop():
    """Delete this surface's float buffers (its context current)."""
    backend = _float_backend()
    for link in _STATE.get("chain") or ():
        if backend is not None:
            backend.float_free(link["record"])
        gl.glDeleteFramebuffers(1, [link["fbo"]])
        gl.glDeleteTextures([link["tex"]])
    _STATE["chain"] = []


def _float_link(backend, window, width, height):
    """One more float buffer for `window`: a texture, the framebuffer that draws into it and the
    wl_buffer on its storage. None when the driver or the compositor will not have it."""
    tex = int(gl.glGenTextures(1))
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, width, height, 0, gl.GL_RGBA, gl.GL_HALF_FLOAT, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    fbo = int(gl.glGenFramebuffers(1))
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, tex, 0)
    complete = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE
    record = backend.float_buffer(window, tex, width, height) if complete else None
    if record is None:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glDeleteFramebuffers(1, [fbo])
        gl.glDeleteTextures([tex])
        return None
    return {"tex": tex, "fbo": fbo, "record": record}


def _float_target(width: int, height: int):
    """(backend, window, link) of a float buffer the compositor is not reading, sized for this
    frame, or None: then the frame goes through the EGL window as before."""
    backend = _float_backend()
    if backend is None:
        return None
    window = backend.get_current_context()
    if window is None or not getattr(window, "surface", None):
        return None
    if _STATE.get("chain_size") != (width, height):
        _float_drop()
        _STATE["chain_size"] = (width, height)
    chain = _STATE.setdefault("chain", [])
    for link in chain:
        if not link["record"].busy:
            return backend, window, link
    if len(chain) >= SPARE:
        return None
    link = _float_link(backend, window, width, height)
    if link is None:
        # Not here after all (the compositor does not take this driver's buffers): stop asking, and
        # wayland_color goes back to PQ at its next sync.
        _STATE["float_failed"] = True
        return None
    chain.append(link)
    return backend, window, link


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
    // A dmabuf's first row is the top one, a GL texture's the bottom one: the float copy is flipped.
    vec4 c = texture(scene, pq_output == 2 ? vec2(uv.x, 1.0 - uv.y) : uv);
    // The scene is premultiplied in LINEAR light; the swapchain is
    // premultiplied in ENCODED (electrical) values — what Wayland assumes
    // without a wp_color_representation alpha mode, so the compositor
    // divides the stored value by alpha before decoding. Encoding a*c as
    // it stood read back as pq(a*c)/a: far above pq(c) at the dark end
    // (PQ and sRGB are steepest there), so every translucent pixel — the
    // shadow margin, the rounded corners' anti-aliased edge — came out
    // much brighter than the scene held, the shadow beside the content a
    // bright blue rim over a black desktop (09-09). Un-premultiply, encode
    // the straight colour, multiply back. Opaque pixels are unchanged.
    float a = clamp(c.a, 0.0, 1.0);
    vec3 rgb = a > 0.0 ? c.rgb / a : vec3(0.0);
    vec3 encoded;
    if (pq_output == 2) {
        // Extended linear, half float: the scene as it is. Premultiplied in linear light, which
        // is what the values are, so nothing is divided out and nothing is clamped.
        FragColor = c;
        return;
    }
    if (pq_output == 1) {
        // Linear scRGB -> BT.2020 -> PQ. reference_nits is what 1.0 shows
        // as (the desktop's SDR reference), so SDR content matches an
        // untagged window and white(4) lands at 4x that.
        // Convert primaries FIRST, clamp after: a P3 / wide colour is
        // scRGB with NEGATIVE components (p3(1,0,0) = (1.22, -0.04, -0.02)
        // linear) and clamping them before the matrix collapses it back to
        // the sRGB gamut. BT.2020 contains P3, so the result is non-negative.
        vec3 nits = max(MELTY_SRGB_TO_BT2020 * rgb, 0.0) * reference_nits;
        encoded = melty_pq_encode(nits);
    } else {
        encoded = melty_linear_to_srgb(rgb);
    }
    FragColor = vec4(encoded * a, a);
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
    from meltygui.core.runtime.toggles import Toggles
    _STATE["active"] = False
    if _STATE["gl"] is None:
        _STATE["gl"] = GLState()
    import meltygui.core.graphics.wayland_color as wayland_color
    # Extended linear once the surface carries that tag: the scene goes into a float buffer of
    # its own instead of the EGL window's 8 bit one, and swap_buffers hands that over.
    floating = None
    if wayland_color.applied() == "linear":
        floating = _float_target(int(width), int(height))
    if floating is None:
        # This frame goes through an EGL window. One made for float buffers has none: it gets
        # one now, and presents that way from here on (another output mode, or float failed).
        backend = _float_backend()
        window = backend.get_current_context() if backend is not None else None
        if getattr(window, "float", False) and (wayland_color.resolved_output() != "linear" or _STATE.get("float_failed")):
            _float_drop()
            backend.float_abandon(window)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, floating[2]["fbo"] if floating else 0)
    gl.glViewport(0, 0, int(width), int(height))
    gl.glDisable(gl.GL_SCISSOR_TEST)
    gl.glDisable(gl.GL_DEPTH_TEST)
    gl.glDisable(gl.GL_STENCIL_TEST)
    gl.glDisable(gl.GL_BLEND)
    gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
    if floating:
        backend, window, link = floating
        _present_pass(_STATE["gl"], scene=_STATE["tex"], pq_output=2, reference_nits=1.0)
        gl.glBindVertexArray(0)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        backend.float_commit(window, link["record"])
        return True
    # PQ only once the surface actually carries the tag (wayland_color.sync):
    # an untagged app is sRGB to the compositor, whatever the toggle says.
    pq = wayland_color.resolved_output() == "pq" and (wayland_color.applied() == "pq" or not wayland_color.available())
    # 1.0 = the desktop's SDR white: the reference the applied PQ tag carries
    # (wayland_color.desired_reference - the compositor's preferred
    # description, else Toggles.HDR.pq_reference_nits).
    reference = wayland_color.reference_nits() or wayland_color.desired_reference()
    _present_pass(_STATE["gl"], scene=_STATE["tex"], pq_output=1 if pq else 0,
                  reference_nits=float(reference))
    gl.glBindVertexArray(0)
    return True


def read_presented(x: int, y: int, width: int, height: int):
    """What a screenshot reads from the back buffer, for a window that has none (it presents float
    buffers): the rect of the scene as 8 bit sRGB, premultiplied, rows bottom up, as a
    (height, width, 4) uint8 array. None for every other window: read GL_BACK."""
    backend = _float_backend()
    window = backend.get_current_context() if backend is not None else None
    if not getattr(window, "float", False) or not _STATE["fbo"]:
        return None
    import numpy as np
    gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, int(_STATE["fbo"]))
    gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
    data = gl.glReadPixels(int(x), int(y), int(width), int(height), gl.GL_RGBA, gl.GL_FLOAT)
    gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, 0)
    pixels = np.frombuffer(data, dtype=np.float32) if isinstance(data, bytes) else np.asarray(data, dtype=np.float32)
    pixels = pixels.reshape(int(height), int(width), 4)
    alpha = np.clip(pixels[..., 3:], 0.0, 1.0)
    straight = np.clip(np.where(alpha > 0.0, pixels[..., :3] / np.maximum(alpha, 1e-6), 0.0), 0.0, 1.0)
    encoded = np.where(straight <= 0.0031308, straight * 12.92, 1.055 * straight ** (1.0 / 2.4) - 0.055)
    return (np.concatenate([encoded * alpha, alpha], -1) * 255.0 + 0.5).astype(np.uint8)


def shutdown() -> None:
    if is_gl_thread():
        _release()
        _float_drop()
    _STATE["active"] = False