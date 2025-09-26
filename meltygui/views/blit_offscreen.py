# offscreen_tiles.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from OpenGL import GL as gl
import imgui


@dataclass
class _Tile:
    fbo: int
    tex: int
    rbo: int
    size: Tuple[int, int]
    dirty: bool = True


@dataclass
class _Ctx:
    key: str
    pos: Tuple[float, float]  # ImGui screen pos (logical) at begin
    size: Tuple[int, int]
    drew_cached: bool


@dataclass
class _Pending:
    tile: _Tile
    pos: Tuple[float, float]  # logical coords (x,y)
    size: Tuple[int, int]  # logical size (w,h)


def _create_color_tex(w: int, h: int, internal_format=gl.GL_RGBA8) -> int:
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal_format, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return tex


def _create_fbo(w: int, h: int) -> Tuple[int, int, int]:
    fbo = gl.glGenFramebuffers(1)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    color_tex = _create_color_tex(w, h)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, color_tex, 0)
    rbo = gl.glGenRenderbuffers(1)
    gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, rbo)
    gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH24_STENCIL8, w, h)
    gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT, gl.GL_RENDERBUFFER, rbo)
    status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
    if status != gl.GL_FRAMEBUFFER_COMPLETE:
        raise RuntimeError(f"FBO incomplete: 0x{status:04X}")
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
    return fbo, color_tex, rbo


def _ensure_tile(existing: Optional[_Tile], w: int, h: int) -> _Tile:
    if existing and existing.size == (w, h):
        return existing
    if existing:
        gl.glDeleteFramebuffers(1, [existing.fbo])
        gl.glDeleteTextures(1, [existing.tex])
        gl.glDeleteRenderbuffers(1, [existing.rbo])
    fbo, tex, rbo = _create_fbo(w, h)
    return _Tile(fbo=fbo, tex=tex, rbo=rbo, size=(w, h), dirty=True)


class _GLState:
    def __init__(self):
        self.draw_fbo = gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING)
        self.read_fbo = gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING)
        self.viewport = tuple(gl.glGetIntegerv(gl.GL_VIEWPORT))
        self.scissor_enabled = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        self.scissor_box = tuple(gl.glGetIntegerv(gl.GL_SCISSOR_BOX))

    def restore(self):
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.draw_fbo)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.read_fbo)
        gl.glViewport(*self.viewport)
        (gl.glEnable if self.scissor_enabled else gl.glDisable)(gl.GL_SCISSOR_TEST)
        gl.glScissor(*self.scissor_box)


class TileCacheMinimal:
    """
    Minimal begin/end API for caching ImGui regions as offscreen tiles.

      should_draw = cache.mark_start_offscreen(key, w, h)
      if should_draw:
          ... your existing widget code ...
      cache.mark_end_offscreen()

    Then once per frame *after* your normal impl.render(...):
      cache.finalize_captures((fb_w, fb_h))

    - No x/y args: they’re inferred from imgui.get_cursor_screen_pos() at begin.
    - When enabled and tile is valid: begin draws the cached image and returns False.
    """

    def __init__(self):
        self.enabled: bool = False
        self._tiles: Dict[str, _Tile] = {}
        self._stack: List[_Ctx] = []
        self._pending: List[_Pending] = []

    # ---- Public toggles/ops ----
    def set_enabled(self, on: bool) -> None:
        if on and not self.enabled:
            # first enable -> mark all dirty so we capture fresh
            for t in self._tiles.values():
                t.dirty = True
        self.enabled = on

    def invalidate(self, key: str) -> None:
        t = self._tiles.get(key)
        if t: t.dirty = True

    def invalidate_all(self) -> None:
        for t in self._tiles.values():
            t.dirty = True

    def get_texture_id(self, key: str) -> Optional[int]:
        t = self._tiles.get(key)
        return t.tex if t else None

    def cleanup(self) -> None:
        for t in self._tiles.values():
            gl.glDeleteFramebuffers(1, [t.fbo])
            gl.glDeleteTextures(1, [t.tex])
            gl.glDeleteRenderbuffers(1, [t.rbo])
        self._tiles.clear()

    # ---- Begin/End pair ----
    def mark_start_offscreen(self, key: str, w: int, h: int) -> bool:
        x, y = imgui.get_cursor_screen_pos()
        size = (int(w), int(h))

        if not self.enabled:
            self._stack.append(_Ctx(key, (x, y), size, False))
            return True

        tile = _ensure_tile(self._tiles.get(key), size[0], size[1])
        self._tiles[key] = tile

        if not tile.dirty:
            # Draw cached image now and skip live draw
            imgui.image(tile.tex, size[0], size[1], uv0=(0.0, 1.0), uv1=(1.0, 0.0))
            self._stack.append(_Ctx(key, (x, y), size, True))
            return False

        # Need live draw to update pixels this frame
        self._stack.append(_Ctx(key, (x, y), size, False))
        return True

    def mark_end_offscreen(self) -> None:
        if not self._stack:
            return
        ctx = self._stack.pop()
        if not self.enabled or ctx.drew_cached:
            return
        # Queue capture of new for after the frame is rendered
        tile = _ensure_tile(self._tiles.get(ctx.key), ctx.size[0], ctx.size[1])
        self._tiles[ctx.key] = tile
        self._pending.append(_Pending(tile=tile, pos=ctx.pos, size=ctx.size))

    # ---- Call once per frame AFTER impl.render(draw_data) ----
    def finalize_captures(self, framebuffer_size: Tuple[int, int]) -> None:
        if not self._pending:
            return
        fb_w, fb_h = map(int, framebuffer_size)
        io = imgui.get_io()
        disp_w, disp_h = io.display_size
        # Protect against divide-by-zero if minimized
        sx = fb_w / max(1.0, disp_w)
        sy = fb_h / max(1.0, disp_h)

        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, 0)
            for pending in self._pending:
                x, y = pending.pos
                w, h = pending.size
                # Convert logical (top-left) -> GL (bottom-left)
                src_x0 = int(round(x * sx))
                src_x1 = int(round((x + w) * sx))
                src_y0 = int(round((disp_h - (y + h)) * sy))
                src_y1 = int(round((disp_h - y) * sy))

                gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, pending.tile.fbo)
                gl.glViewport(0, 0, pending.tile.size[0], pending.tile.size[1])
                gl.glDisable(gl.GL_SCISSOR_TEST)
                gl.glBlitFramebuffer(
                    src_x0, src_y0, src_x1, src_y1,
                    0, 0, pending.tile.size[0], pending.tile.size[1],
                    gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST
                )
                pending.tile.dirty = False
        finally:
            st.restore()
            self._pending.clear()
