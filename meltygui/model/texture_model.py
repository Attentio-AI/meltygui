"""Integer-like texture values with lazy, context-owned GPU storage."""
from functools import total_ordering
import operator

import OpenGL.GL as gl
from meltygui.core.graphics.gl_state import GLTexture, tight_unpack
from meltygui.core.runtime.toggles import Toggles

from meltygui.core.graphics.gl_state import GLState, current_context, is_gl_thread


@total_ordering
class TextureId:
    """A texture ID proxy. Resolve it only where an OpenGL ID is needed.

    Subclasses provide ``target`` and ``_upload(state)``. Constructing a value
    does no GL work; int/index conversion uploads it in the current context.
    The numeric ID may change after an edit, so this mutable proxy is unhashable.
    """
    __hash__ = None

    def __init__(self, target):
        self.target = int(target)
        self._states = {}

    def _state_for_context(self, context):
        if context not in self._states:
            state = GLState()
            state._context = context
            self._states[context] = state
        return self._states[context]

    def _state(self):
        context = current_context()
        if context is None or not is_gl_thread():
            raise RuntimeError('A texture ID needs the current rendering context')
        return self._state_for_context(context)

    def _upload(self, state):
        raise NotImplementedError

    def __int__(self):
        return self._upload(self._state()).texture_id

    def __index__(self):
        return int(self)

    def __bool__(self):
        return bool(int(self))

    def __eq__(self, other):
        try:
            other_id = operator.index(other)
        except TypeError:
            return NotImplemented
        return int(self) == other_id

    def __lt__(self, other):
        try:
            other_id = operator.index(other)
        except TypeError:
            return NotImplemented
        return int(self) < other_id

    @property
    def texture_id(self):
        return int(self)

    @property
    def _as_parameter_(self):
        """ctypes/PyOpenGL accept the proxy directly as an integer argument."""
        return int(self)

    def adopt(self, state, old_key, key='texture'):
        """Move an existing allocation into this value without re-uploading it."""
        record = state._resources.pop(old_key, None)
        if record is not None:
            target = self._state_for_context(state._context)
            target.drop(key)
            target._resources[key] = record

    def release(self):
        for state in self._states.values():
            state.release()
        self._states.clear()

    def __repr__(self):
        return f'{type(self).__name__}(target={self.target:#x})'


class ImageTexture(TextureId):
    """Decoded pixels with lazy, context-owned GPU storage.

    Construction is safe on a decode worker. Integer conversion uploads once
    per GL context; GLState owns deletion when the value or context is released.
    """
    def __init__(self, name, tex_width, tex_height, gl_format, data,
                 tint=(1.0, 1.0, 1.0)):
        super().__init__(gl.GL_TEXTURE_2D)
        self.name = name
        self.tex_width = tex_width
        self.tex_height = tex_height
        self.gl_format = gl_format
        self.data = data
        self.tint = tint

    def _upload(self, state):
        def create():
            previous = int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
            texture = int(gl.glGenTextures(1))
            internal = {gl.GL_RGBA: gl.GL_SRGB8_ALPHA8,
                        gl.GL_RGB: gl.GL_SRGB8}.get(self.gl_format, self.gl_format)
            try:
                gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
                with tight_unpack():
                    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal,
                                    self.tex_width, self.tex_height, 0,
                                    self.gl_format, gl.GL_UNSIGNED_BYTE, self.data)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
                return GLTexture(texture, gl.GL_TEXTURE_2D,
                                 (self.tex_height, self.tex_width), internal)
            except Exception:
                gl.glDeleteTextures([texture])
                raise
            finally:
                gl.glBindTexture(gl.GL_TEXTURE_2D, previous)
        return state.get('image', create, lambda texture: gl.glDeleteTextures([texture.texture_id]))

    def __getstate__(self):
        return {key: value for key, value in self.__dict__.items() if key != '_states'}

    def __setstate__(self, state):
        # Saved image values contain pixels, never reusable GL names.
        state = dict(state)
        state.pop('texture_id', None)
        state.pop('_states', None)
        self.__dict__.update(state)
        self.target = int(gl.GL_TEXTURE_2D)
        self._states = {}


def texture_filter_target(state, key, width, height, nearest=False):
    """Own a view's filter output instead of borrowing Filter's shared cache.

    Change precision here for all texture-view color passes. HDR values require
    floating-point storage; sampler choice belongs to this output, not the input.
    """
    def create():
        previous = int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
        texture = int(gl.glGenTextures(1))
        try:
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, width, height, 0,
                            gl.GL_RGBA, gl.GL_HALF_FLOAT, None)
            mode = gl.GL_NEAREST if nearest else gl.GL_LINEAR
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, mode)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, mode)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            return GLTexture(texture, gl.GL_TEXTURE_2D, (height, width), gl.GL_RGBA16F)
        except Exception:
            gl.glDeleteTextures([texture])
            raise
        finally:
            gl.glBindTexture(gl.GL_TEXTURE_2D, previous)
    return state.get(key, create, lambda texture: gl.glDeleteTextures([texture.texture_id]),
                     deps=(width, height, nearest)).texture_id


def _upload_cuda_image(gl_state, out):
    """Transfer only the finished 2-D pixels; never the source tensor.

    With CUDA-GL interop the image goes GPU to GPU into a registered pixel
    buffer and becomes a texture inside the display GPU's VRAM
    (cuda_texture_model.image_to_texture). Without it (a display GPU CUDA
    cannot reach) a pinned host buffer carries it, as before."""
    import torch
    H, W = int(out.shape[0]), int(out.shape[1])
    if Toggles.Voxels.cuda_image_interop:
        from meltygui.model.cuda_texture_model import image_to_texture
        img = image_to_texture(gl_state, "cuda_image_interop", out.data_ptr(), out.device.index, W, H)
        if img is not None:
            gl_state.drop("cuda_image"); gl_state.drop("cuda_host")
            return img
    gl_state.drop("cuda_image_interop")
    host = gl_state.get("cuda_host",
                        lambda: torch.empty(H, W, 4, dtype=torch.float16).pin_memory(),
                        deps=(W, H))
    # D2H on torch's (legacy default) stream orders after the kernel on
    # the same device's null stream — no explicit synchronize.
    host.copy_(out)

    def create():
        tex_id = _scalar_int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        # cuda_march writes linear premultiplied fp16 — the same light
        # the GL pass writes — so the image rides into the fp16 scene
        # (hdr_color.py) unclamped: no encode, no 8-bit ceiling.
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, W, H, 0,
                        gl.GL_RGBA, gl.GL_HALF_FLOAT, None)
        for pn, pv in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                       (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                       (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                       (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, pn, pv)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        return GLTexture(tex_id, gl.GL_TEXTURE_2D, (H, W), gl.GL_RGBA16F)

    img = gl_state.get("cuda_image", create,
                       lambda tx: gl.glDeleteTextures([tx.texture_id]),
                       deps=(W, H))
    gl.glBindTexture(gl.GL_TEXTURE_2D, img.texture_id)
    with tight_unpack():
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, W, H, gl.GL_RGBA,
                           gl.GL_HALF_FLOAT, host.numpy())
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return img


def _scalar_int(v):
    try:
        return int(v)
    except TypeError:
        return int(v[0])


def _cached_volume_texture(gl_state, vol_key, keys=("volume_cuda", "volume", "cuda_view")):
    """The already-uploaded volume texture for `vol_key`, or None. Checks
    both upload paths (interop CudaVolume wraps its GLTexture as .texture);
    a hit means draw_voxels skips slice_volume AND the upload outright."""
    for key in keys:
        rec = gl_state.peek(key)
        if rec is None:
            continue
        tex = getattr(rec, "texture", rec)
        if getattr(tex, "_vol_key", None) == vol_key:
            return tex
    return None
