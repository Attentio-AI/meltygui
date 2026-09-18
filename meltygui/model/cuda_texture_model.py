"""Versioned CUDA tensors exposed as GLTexture values without a CPU upload.

The injected GLState owns each texture/PBO/registration as one resource. CUDA
unregistration must finish before the GL buffer is deleted. The resource holds
no reference to the source tensor; cache hits do no tensor copies or syncs.
"""
import ctypes

import OpenGL.GL as gl

from meltygui.core.graphics.cuda_context_core import current_device_index
from meltygui.core.graphics.cuda_interop_core import (
    interop_context, copy_image_to_buffer, copy_to_buffer, log_once,
    register_buffer, unregister_buffer,
)
from meltygui.core.graphics.gl_state import (
    GLTexture, _scalar, current_context, is_gl_thread, texture3d_fit, tight_unpack,
)


class CudaVolume:
    """Owned allocation behind a texture value; its source tensor stays external."""
    __slots__ = ("texture", "buffer", "registered", "nbytes", "last_version")

    def __init__(self, texture, buffer, registered, nbytes):
        self.texture = texture
        self.buffer = buffer
        self.registered = registered
        self.nbytes = nbytes
        self.last_version = object()  # even version=None must perform its first upload


def _release_volume(volume, context):
    # Each completed step is recorded so a failed deletion can be retried.
    # Keep the implementation at module scope so queued deleters see hot edits.
    if volume.registered is not None:
        unregister_buffer(volume.registered, context)
        volume.registered = None
    if volume.buffer is not None:
        gl.glDeleteBuffers(1, [volume.buffer])
        volume.buffer = None
    if volume.texture is not None:
        gl.glDeleteTextures([volume.texture.texture_id])
        volume.texture = None


def tensor_to_texture(gl_state, key, tensor, version):
    """Return a versioned 3-D float16/32 GLTexture, or None for CPU fallback.

    Noncontiguous tensors are packed only on version misses. Tensors on another
    GPU are staged by Torch onto the GL device, never copied across devices with
    a raw CUDA pointer copy. Calls require the owner's GL context and a compatible
    recorded CUDA context. Core activates it locally and restores the caller's
    native context, or lazily establishes the display context when none exists.
    An incompatible caller-owned PyCUDA context is left untouched.
    """
    if not is_gl_thread() or current_context() is None:
        return None
    if gl_state._context != current_context():
        return None
    import torch
    if not (isinstance(tensor, torch.Tensor) and tensor.is_cuda
            and tensor.dim() == 3 and tensor.dtype in (torch.float16, torch.float32)):
        return None
    with interop_context() as context:
        if context is None:
            return None
        return _upload_tensor(gl_state, key, tensor, version, context)


def _upload_tensor(gl_state, key, tensor, version, context):
    import torch
    device = current_device_index()
    depth, height, width = (int(size) for size in tensor.shape)
    half = tensor.dtype == torch.float16
    internal = gl.GL_R16F if half else gl.GL_R32F
    gl_type = gl.GL_HALF_FLOAT if half else gl.GL_FLOAT
    nbytes = tensor.nelement() * tensor.element_size()

    def delete(volume):
        _release_volume(volume, context)

    def create():
        _, problems = texture3d_fit((depth, height, width), tensor.element_size())
        if problems:
            raise ValueError("tensor_to_texture: " + "; ".join(problems))
        volume = CudaVolume(None, None, None, nbytes)
        previous_buffer = _scalar(gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING))
        previous_unpack = _scalar(gl.glGetIntegerv(gl.GL_PIXEL_UNPACK_BUFFER_BINDING))
        previous_texture = _scalar(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_3D))
        try:
            volume.buffer = _scalar(gl.glGenBuffers(1))
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, volume.buffer)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, nbytes, None, gl.GL_DYNAMIC_DRAW)
            volume.registered = register_buffer(volume.buffer)
            texture_id = _scalar(gl.glGenTextures(1))
            volume.texture = GLTexture(texture_id, gl.GL_TEXTURE_3D,
                                       (depth, height, width), internal)
            gl.glBindTexture(gl.GL_TEXTURE_3D, texture_id)
            # NULL must mean no client data, not offset zero in a caller's PBO.
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, 0)
            gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, internal, width, height, depth, 0,
                            gl.GL_RED, gl_type, ctypes.c_void_p(0))
            for parameter, value in (
                    (gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                    (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                    (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                    (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE),
                    (gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_3D, parameter, value)
            return volume
        except Exception:
            gl_state.defer_delete(key, volume, delete)
            raise
        finally:
            gl.glBindTexture(gl.GL_TEXTURE_3D, previous_texture)
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, previous_unpack)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, previous_buffer)

    try:
        volume = gl_state.get(key, create, delete,
            deps=((depth, height, width), "f16" if half else "f32", context))
        if volume.last_version != version:
            if tensor.device.index != device:
                # Finish the source producer before Torch stages across devices.
                torch.cuda.synchronize(tensor.device)
                tensor = tensor.to(f"cuda:{device}")
            tensor = tensor.contiguous()
            # The producer can be on a nondefault stream. Waiting AFTER the
            # driver copy is too late, and synchronize() without a device can
            # wait on a completely different GPU.
            torch.cuda.synchronize(device)
            copy_to_buffer(volume.registered, context, tensor.data_ptr(), nbytes)
            previous_unpack = _scalar(gl.glGetIntegerv(gl.GL_PIXEL_UNPACK_BUFFER_BINDING))
            previous_texture = _scalar(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_3D))
            try:
                gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, volume.buffer)
                gl.glBindTexture(gl.GL_TEXTURE_3D, volume.texture.texture_id)
                with tight_unpack():
                    gl.glTexSubImage3D(gl.GL_TEXTURE_3D, 0, 0, 0, 0, width, height, depth,
                                       gl.GL_RED, gl_type, None)
            finally:
                gl.glBindTexture(gl.GL_TEXTURE_3D, previous_texture)
                gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, previous_unpack)
            volume.last_version = version
        return volume.texture
    except Exception as error:
        log_once(f"interop upload failed ({error}); falling back to cpu path")
        return None


# Bytes per pixel of the raymarchers' image: linear premultiplied RGBA16F (hdr_color.py).
IMAGE_PIXEL_BYTES = 8


def image_to_texture(gl_state, key, pointer, source_device, width, height):
    """A finished width x height RGBA16F image in CUDA memory (any GPU, this
    process's or one opened from another process) as a display-GPU GLTexture,
    without touching client memory: device/peer copy into a registered pixel
    buffer, then a texture upload that never leaves the display GPU's VRAM.

    Returns None when GL interop is unavailable (no CUDA device behind this GL
    context, a non-NVIDIA display GPU): the caller then carries the image over a
    pinned host buffer. Uploads every call; the image is a new frame each time."""
    if not is_gl_thread() or current_context() is None or gl_state._context != current_context():
        return None
    with interop_context() as context:
        if context is None:
            return None
        return _upload_image(gl_state, key, int(pointer), int(source_device),
                             int(width), int(height), context)


def _upload_image(gl_state, key, pointer, source_device, width, height, context):
    nbytes = width * height * IMAGE_PIXEL_BYTES

    def delete(volume):
        _release_volume(volume, context)

    def create():
        volume = CudaVolume(None, None, None, nbytes)
        previous_buffer = _scalar(gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING))
        previous_unpack = _scalar(gl.glGetIntegerv(gl.GL_PIXEL_UNPACK_BUFFER_BINDING))
        previous_texture = _scalar(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
        try:
            volume.buffer = _scalar(gl.glGenBuffers(1))
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, volume.buffer)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, nbytes, None, gl.GL_STREAM_DRAW)
            volume.registered = register_buffer(volume.buffer)
            texture_id = _scalar(gl.glGenTextures(1))
            volume.texture = GLTexture(texture_id, gl.GL_TEXTURE_2D, (height, width), gl.GL_RGBA16F)
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)
            # NULL must mean no client data, not offset zero in a caller's PBO.
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, 0)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, width, height, 0,
                            gl.GL_RGBA, gl.GL_HALF_FLOAT, ctypes.c_void_p(0))
            for parameter, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                                     (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                                     (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                     (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, parameter, value)
            return volume
        except Exception:
            gl_state.defer_delete(key, volume, delete)
            raise
        finally:
            gl.glBindTexture(gl.GL_TEXTURE_2D, previous_texture)
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, previous_unpack)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, previous_buffer)

    try:
        volume = gl_state.get(key, create, delete, deps=(width, height, context))
        copy_image_to_buffer(volume.registered, context, pointer, nbytes, source_device)
        previous_unpack = _scalar(gl.glGetIntegerv(gl.GL_PIXEL_UNPACK_BUFFER_BINDING))
        previous_texture = _scalar(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
        try:
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, volume.buffer)
            gl.glBindTexture(gl.GL_TEXTURE_2D, volume.texture.texture_id)
            with tight_unpack():
                gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                                   gl.GL_RGBA, gl.GL_HALF_FLOAT, None)
        finally:
            gl.glBindTexture(gl.GL_TEXTURE_2D, previous_texture)
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, previous_unpack)
        return volume.texture
    except Exception as error:
        log_once(f"image interop failed ({error}); carrying the image over pinned host memory")
        return None
