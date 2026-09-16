import threading
from dataclasses import dataclass

import numpy
from OpenGL.GL import (
    glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
    glDeleteTextures, glGetTexImage, glActiveTexture,
    GL_TEXTURE_2D, GL_RGBA, GL_RGB, GL_RED, GL_RG, GL_UNSIGNED_BYTE,
    GL_SRGB8_ALPHA8, GL_SRGB8,
)
from PIL import Image
import io
from typing import Any

from meltygui.core.windowing.glfw_utils import print_stack_trace
from meltygui.core.rendering.core_decoration import defaults

# Map PIL modes to GL formats
PIL_TO_GL_FORMAT = {
    "L": GL_RED,  # Grayscale
    "LA": GL_RG,  # Grayscale + Alpha
    "RGB": GL_RGB,
    "RGBA": GL_RGBA,
}

# Reverse mapping for dumps
GL_TO_PIL_MODE = {v: k for k, v in PIL_TO_GL_FORMAT.items()}

from OpenGL.GL import (
    glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
    glDeleteTextures, glGetTexImage, glActiveTexture,
    GL_TEXTURE_2D, GL_RGBA, GL_UNSIGNED_BYTE,
    GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER, GL_LINEAR,
    GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE,
    GL_LINEAR_MIPMAP_LINEAR, glGenerateMipmap, GL_TEXTURE0, glGetIntegerv
)

@dataclass
class PendingTexture:
    """Decoded image data awaiting GL upload on main thread.

    ``name`` / ``tex_width`` / ``tex_height`` / ``gl_format`` / ``data`` are
    populated at decode time off the GL thread (e.g. by ``PngCodec.loads``).
    ``tint`` is overwritten with the avg color after construction. ``texture_id``
    is filled in once ``upload_to_gl`` runs on the GL thread.
    """
    name: str
    tex_width: int
    tex_height: int
    gl_format: int
    data: bytes
    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    texture_id: numpy.uint32 | None = None  # Filled after upload

    def pending_upload(self) -> int:
        if self.texture_id is not None:
            return False

        """Upload using Melty library. MUST be called from GL thread."""
        # Texture manager singleton
        texture_manager = TextureManager()
        texture_id = texture_manager.upload_to_gl(self)
        self.texture_id = texture_id
        return True

class TextureManager:
    """Manages GL texture lifecycle with path-based caching and deferred uploads."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._cache = {}  # path -> texture_id
            cls._instance._pending = {}  # path -> PendingTexture
            cls._instance._ref_counts = {}  # path -> count
            cls._instance._metadata = {}  # path -> (width, height, gl_format)
            cls._instance._lock = threading.Lock()
        return cls._instance

    def upload_to_gl(self, pending: PendingTexture) -> int:
        """Create the GL texture. MUST be called from GL thread."""
        texture_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, texture_id)
        # 8-bit images are sRGB-encoded; an sRGB internal format decodes
        # to linear at sample time, which is what the linear scRGB scene
        # (scene_color.py) composites.
        internal = {GL_RGBA: GL_SRGB8_ALPHA8, GL_RGB: GL_SRGB8}.get(pending.gl_format, pending.gl_format)
        glTexImage2D(
            GL_TEXTURE_2D, 0, internal,
            pending.tex_width, pending.tex_height, 0,
            pending.gl_format, GL_UNSIGNED_BYTE, pending.data
        )
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)

        glBindTexture(GL_TEXTURE_2D, 0)
        pending.texture_id = texture_id

        return texture_id

    def get(self, path: str) -> int | None:
        """Get a cached texture ID by path, or None if not yet uploaded."""
        with self._lock:
            return self._cache.get(path)

    def put_pending(self, path: str, pending: PendingTexture):
        """Queue decoded image data for later GL upload. Thread-safe."""
        with self._lock:
            if path in self._cache:
                pending.texture_id = self._cache[path]
                return
            self._pending[path] = pending

    def is_pending(self, path: str) -> bool:
        """Check if a texture is queued but not yet uploaded."""
        with self._lock:
            return path in self._pending

    def get_metadata(self, path: str) -> tuple[int, int, int] | None:
        """Get (width, height, gl_format) for an uploaded texture."""
        with self._lock:
            return self._metadata.get(path)

    def acquire(self, path: str) -> int | None:
        """Increment ref count and return texture ID."""
        with self._lock:
            if path in self._cache:
                self._ref_counts[path] = self._ref_counts.get(path, 0) + 1
                return self._cache[path]
            return None

    def release(self, path: str):
        """Decrement ref count, delete texture when it hits 0."""
        with self._lock:
            if path not in self._cache:
                return
            self._ref_counts[path] -= 1
            if self._ref_counts[path] <= 0:
                texture_id = self._cache.pop(path, None)
                self._ref_counts.pop(path, None)
                self._metadata.pop(path, None)
                # Must delete outside lock if GL calls can block

        if texture_id:
            try:
                glDeleteTextures([texture_id])
            except Exception:
                pass

    def clear(self):
        """Release all textures and pending uploads."""
        with self._lock:
            texture_ids = list(self._cache.values())
            self._cache.clear()
            self._pending.clear()
            self._ref_counts.clear()
            self._metadata.clear()

        for texture_id in texture_ids:
            try:
                glDeleteTextures([texture_id])
            except Exception:
                pass

    def __contains__(self, path: str) -> bool:
        with self._lock:
            return path in self._cache or path in self._pending
