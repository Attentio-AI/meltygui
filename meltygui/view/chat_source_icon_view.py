"""Bundled monochrome provider marks, uploaded once per OpenGL context."""
from pathlib import Path

from OpenGL import GL
from PIL import Image

import meltygui_imgui as imgui
import meltygui.core.windowing.window_api as window_api
from meltygui.hdr_color import pack_color

# The marks ship with the chat package (their license is beside them).
ICONS = Path(__file__).parent.parent / 'chat' / 'assets' / 'source-icons'

_textures = {}


def context_key(context):
    """A stable key for the current GL context on either window backend."""
    if window_api.is_native_window(context):
        return id(context)
    # GLFW's pointer wrapper is recreated on each call; use its address as key.
    import ctypes
    return ctypes.cast(context, ctypes.c_void_p).value


def draw_source_icon(provider, x, y, size, tint):
    name = {'anthropic': 'claude', 'codex': 'codex'}.get(provider)
    if name is None:
        return False
    key = (context_key(window_api.get_current_context()), name)
    texture = _textures.get(key)
    if texture is None:
        with Image.open(ICONS / f'{name}.png') as source:
            pixels = source.convert('RGBA')
            previous = int(GL.glGetIntegerv(GL.GL_TEXTURE_BINDING_2D))
            texture = int(GL.glGenTextures(1))
            try:
                GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, pixels.width, pixels.height,
                                0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, pixels.tobytes())
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            finally:
                GL.glBindTexture(GL.GL_TEXTURE_2D, previous)
        _textures[key] = texture
    imgui.get_window_draw_list().add_image(
        texture, (x, y), (x + size, y + size), col=pack_color(*tint[:3], 1.0))
    return True
