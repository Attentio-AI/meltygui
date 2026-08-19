"""CUDA→GL tensor upload: a GPU tensor becomes a GLTexture with no CPU round
trip.

The mechanism (mirrors lsd_studio's update_canvas_voxel, repackaged as one
GLState composite resource):

    GL PBO ──RegisteredBuffer──► CUDA-mapped pointer
                                    ▲ memcpy_dtod(tensor.data_ptr())
    glTexSubImage3D(…, None)  ◄── PBO bound as GL_PIXEL_UNPACK_BUFFER

Context model: all interop driver calls run in whatever CUDA context is
CURRENT on the render thread — that is the studio's deliberate choice of the
DISPLAY GPU (its multi-GPU init leaves that device's primary context pushed),
and GL interop only works from the GL device's context (registering from
another device's context faults the driver and kills the process — learned
the hard way). Tensors on other GPUs are moved to the interop device by
torch first, which stages safely without peer access. This module never
creates contexts in-app; `ensure_context()` exists for tests/standalone use.

Everything degrades to None (callers fall back to the CPU upload path):
no pycuda, pycuda built without GL, no current context, wrong thread, wrong
device, or a runtime interop error (logged once per distinct message — a
broken interop must never take down the render loop / trip hotswap rollback).

Teardown ordering is the one hard rule: the CUDA registration is unregistered
BEFORE the GL buffer is deleted — encoded in the composite's single deleter,
which is why buffer/registration/texture live in ONE GLState resource.
"""

import ctypes

import OpenGL.GL as gl

from src.lsd.gl_gui.gl_state import (GLTexture, _scalar, is_gl_thread, texture3d_fit,
                                     tight_unpack)

# Keeps the test/standalone-pushed primary context referenced; the
# globals().get idiom survives hotswap re-exec (NB: gl_state's _persistent
# helper reads global module globals - can't be imported for this).
_primary_ctx = globals().get("_primary_ctx")
_last_logged = globals().get("_last_logged")


def _log_once(msg):
    global _last_logged
    if msg != _last_logged:
        print(f"[cuda_interop] {msg}")
        _last_logged = msg


def cuda_ready():
    """True when the interop path can run RIGHT HERE: pycuda built with GL
    support and a CUDA context current on this thread."""
    try:
        import pycuda.driver as cuda
        import pycuda.gl  # noqa: F401 - raises if pycuda lacks GL support
    except Exception:
        return False
    try:
        return cuda.Context.get_current() is not None
    except Exception:
        return False


def _detach_primary():
    """Pop + detach the context ensure_context pushed. Registered atexit
    (LIFO — runs before pycuda's own cleanup, which ABORTS the process if a
    context is still current at teardown)."""
    global _primary_ctx
    if _primary_ctx is None:
        return
    try:
        import pycuda.driver as cuda
        cuda.Context.pop()
        _primary_ctx.detach()
    except Exception:
        pass
    _primary_ctx = None


def ensure_context():
    """Make the device-0 primary context current on THIS thread — the same
    retain_primary_context().push() the studio does at init. For tests and
    standalone scripts; the app's render thread already has one (in which
    case this touches nothing). Returns whether a context is current."""
    global _primary_ctx
    try:
        import pycuda.driver as cuda
        import pycuda.gl  # noqa: F401
        import torch
    except Exception:
        return False
    try:
        cuda.init()
        if cuda.Context.get_current() is not None:
            return True
        torch.cuda.init()   # materialize the primary context torch-side first
        _primary_ctx = cuda.Device(0).retain_primary_context()
        _primary_ctx.push()
        import atexit
        atexit.register(_detach_primary)
        return cuda.Context.get_current() is not None
    except Exception as e:
        _log_once(f"ensure_context failed: {e}")
        return False


def current_device_index():
    """The CUDA ordinal of the context current on THIS thread (== the torch
    device index — both use CUDA enumeration order), or None. This is the
    interop device: the studio's render thread deliberately has the DISPLAY
    GPU's context current (its GL context lives there), so registration and
    copies happen in exactly that context — never push a different device's
    context around GL interop calls (registering a GL buffer from the wrong
    device's context faults the driver and takes the whole studio down)."""
    import pycuda.driver as cuda
    ctx = cuda.Context.get_current()
    if ctx is None:
        return None
    bus = cuda.Context.get_device().pci_bus_id()
    for i in range(cuda.Device.count()):
        if cuda.Device(i).pci_bus_id() == bus:
            return i
    return None


class CudaVolume:
    """The composite interop resource: empty-allocated 3-D texture + PBO +
    CUDA registration + the version of the last upload."""

    __slots__ = ("texture", "buffer", "registered", "nbytes", "last_version")

    def __init__(self, texture, buffer, registered, nbytes):
        self.texture = texture
        self.buffer = buffer
        self.registered = registered
        self.nbytes = nbytes
        self.last_version = None


def tensor_to_texture(gl_state, key, tensor, version):
    """Upload a CUDA tensor into a GLState-owned interop texture, returning
    its GLTexture — or None when the interop path isn't available here, so
    the caller falls back to the CPU upload.

    `tensor` must be 3-D (depth, height, width), contiguous, float16/32, on
    the GL context's device (0). The copy only happens when `version`
    differs from the last upload."""
    if not is_gl_thread() or not cuda_ready():
        return None
    import torch
    if not (isinstance(tensor, torch.Tensor) and tensor.is_cuda):
        return None
    if tensor.dim() != 3:
        return None
    if tensor.dtype not in (torch.float16, torch.float32):
        return None

    import pycuda.driver as cuda
    from pycuda import gl as cuda_gl

    # The interop device is the current context's device (where GL lives).
    # A tensor on another GPU is moved there by torch first - torch stages
    # through host when there's no peer access (4090 pairs have none), and
    # a raw cross-device memcpy_dtod is exactly the kind of thing that
    # faults. Same-device tensors keep the pure zero-CPU path.
    gl_dev = current_device_index()
    if gl_dev is None:
        return None
    if (tensor.device.index or 0) != gl_dev:
        tensor = tensor.to(f"cuda:{gl_dev}")
    tensor = tensor.contiguous()

    depth, height, width = (int(s) for s in tensor.shape)
    half = tensor.dtype == torch.float16
    internal = gl.GL_R16F if half else gl.GL_R32F
    gl_type = gl.GL_HALF_FLOAT if half else gl.GL_FLOAT
    nbytes = tensor.nelement() * (2 if half else 4)

    def create():
        # Same pre-flight as GLState.texture3d: an over-limit shape must not
        # reach glBufferData/glTexImage3D (it raised GL_INVALID_VALUE, leaked
        # the buffer + registration, and the "cpu path path then failed
        # identically). Callers clamp extents via texture3d_fit first; this
        # refuses allocation on a MISS only (the VRAM budget is measured against
        # free memory, which a cache hit's own allocation already consumed).
        _, problems = texture3d_fit((depth, height, width), 2 if half else 4)
        if problems:
            raise ValueError("tensor_to_texture: " + "; ".join(problems))
        # The proven sequence (lsd_studio's update_canvas_voxel): buffer +
        # registration first, then the texture allocated with a NULL pointer
        # and the single-pixel internal format - all data flows through the
        # PBO, never from client memory.
        prev = _scalar(gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING))
        buf = _scalar(gl.glGenBuffers(1))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, buf)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, nbytes, None, gl.GL_DYNAMIC_DRAW)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, prev)
        try:
            registered = cuda_gl.RegisteredBuffer(
                buf, cuda_gl.graphics_map_flags.WRITE_DISCARD)
        except Exception:
            gl.glDeleteBuffers(1, [buf])
            raise

        tex_id = _scalar(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_3D, tex_id)
        gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, internal, width, height, depth, 0,
                        gl.GL_RED, gl_type, ctypes.c_void_p(0))
        gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
        gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
        texture = GLTexture(tex_id, gl.GL_TEXTURE_3D, (depth, height, width), internal)
        return CudaVolume(texture, buf, registered, nbytes)

    def delete(cv):
        # Unregister strictly BEFORE the buffer dies - the reason this is one
        # composite resource instead of three: flush_deletes runs in the same
        # GL thread, so the registration's context is current here too.
        try:
            cv.registered.unregister()
        except Exception as e:
            print(f"[cuda_interop] unregister failed: {e}")
        gl.glDeleteBuffers(1, [cv.buffer])
        gl.glDeleteTextures([cv.texture.texture_id])

    try:
        cv = gl_state.get(key, create, delete,
                          deps=((depth, height, width), "f16" if half else "f32"))
        if cv.last_version != version:
            mapping = cv.registered.map()
            try:
                ptr, _size = mapping.device_ptr_and_size()
                cuda.memcpy_dtod(ptr, tensor.data_ptr(), nbytes)
                # Torch queues its work on its own stream; make sure the
                # producer AND the copy are done before GL reads the PBO.
                torch.cuda.synchronize()
                cuda.Context.synchronize()
            finally:
                mapping.unmap()
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, cv.buffer)
            gl.glBindTexture(gl.GL_TEXTURE_3D, cv.texture.texture_id)
            with tight_unpack():
                gl.glTexSubImage3D(gl.GL_TEXTURE_3D, 0, 0, 0, 0, width, height, depth,
                                   gl.GL_RED, gl_type, None)
            gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, 0)
            cv.last_version = version
        return cv.texture
    except Exception as e:
        _log_once(f"interop upload failed ({e}); falling back to cpu path")
        return None
