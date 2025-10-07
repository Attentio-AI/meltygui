# offscreen_tiles_masked.py
from __future__ import annotations

import random
from dataclasses import dataclass
from math import ceil, floor
from typing import Dict, List, Optional, Tuple
from OpenGL import GL as gl
import imgui

from src.lsd.gl_gui.utils.glfw_utils import request_render

"""
Per-view tile caching with a post-frame mask (no ImGui draw-list replay).

API:
  cache = TileCacheMasked()
  cache.set_enabled(True)                # toggle on/off
  cache.set_top_is_low(True)             # set layering rule: True => smaller layer index is on top
  cache.mask_begin_frame((fb_w, fb_h))   # once per frame (fb size in *framebuffer* pixels)

  if cache.mark_start_offscreen("key", w, h, layer):
      draw_view_live()                   # your existing code
  cache.mark_end_offscreen()             # pairs with mark_start_offscreen

  impl.render(imgui.get_draw_data())     # your normal ImGui render
  cache.finalize_captures((fb_w, fb_h))  # once per frame (after render)
"""


# ==============================
# Small structs
# ==============================
@dataclass
class _Tile:
    fbo: int
    tex: int
    rbo: int
    size: Tuple[int, int]
    dirty: bool = True


@dataclass
class _Ctx:
    draw_state: any
    key: str
    pos: Tuple[float, float]  # ImGui screen-space (logical, top-left) at begin
    size: Tuple[int, int]
    layer: int
    drew_cached: bool


@dataclass
class _Pending:
    tile: _Tile
    pos: Tuple[float, float]
    size: Tuple[int, int]
    layer: int


@dataclass
class _Rect:
    # stored in ImGui logical coords; converted at render time
    layer: int
    x: float
    y: float
    w: float
    h: float


# ==============================
# GL helpers
# ==============================
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


def _create_mask_tex(w: int, h: int) -> int:
    # single-channel 8-bit (0..255) for layer; NEAREST filtering
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R8, w, h, 0, gl.GL_RED, gl.GL_UNSIGNED_BYTE, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return tex


def snap_int(v: float) -> int:
    return int(round(v))

def _create_fbo_with_tex(tex: int, depth_stencil: bool, w, h) -> Tuple[int, Optional[int]]:
    fbo = gl.glGenFramebuffers(1)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, tex, 0)
    gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])  # explicit draw buffer

    rbo = None
    if depth_stencil:
        rbo = gl.glGenRenderbuffers(1)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, rbo)
        gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH24_STENCIL8, snap_int(w), snap_int(h))
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT, gl.GL_RENDERBUFFER, rbo)

    status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
    if status != gl.GL_FRAMEBUFFER_COMPLETE:
        raise RuntimeError(f"FBO incomplete: 0x{status:04X}")
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
    return fbo, rbo


def _ensure_tile(existing: Optional[_Tile], w: int, h: int) -> _Tile:
    if existing and existing.size == (w, h):
        return existing

    if w == 0 or h == 0:
        return None

    # make the new tile
    new_tex = _create_color_tex(w, h)
    new_fbo, new_rbo = _create_fbo_with_tex(new_tex, True, w, h)

    # if we had an old tile, blit its contents into the new one
    if existing:
        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, existing.fbo)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, new_fbo)
            # print(f"TileCacheMasked: resizing tile {existing.size} -> {(w,h)}, blit {mw}x{mh}")
            gl.glBlitFramebuffer(0, 0, snap_int(existing.size[0]), snap_int(existing.size[1]), 0, 0,
                                 snap_int(w), snap_int(h), gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
            # mw = min(existing.size[0], w)
            # mh = min(existing.size[1], h)
            # gl.glBlitFramebuffer(0, 0, mw, int(mh), 0, 0, mw, h, gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)

        finally:
            st.restore()
        # cleanup old
        gl.glDeleteFramebuffers(1, [existing.fbo])
        gl.glDeleteTextures(1, [existing.tex])
        if existing.rbo is not None:
            gl.glDeleteRenderbuffers(1, [existing.rbo])
    else:
        # first tile allocation: clear to transparent once
        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, new_fbo)
            gl.glViewport(0, 0, snap_int(w), snap_int(h))
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glClearColor(0, 0, 0, 0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        finally:
            st.restore()

    return _Tile(fbo=new_fbo, tex=new_tex, rbo=new_rbo, size=(w, h), dirty=True)

class _GLState:
    def __init__(self):
        self.draw_fbo = gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING)
        self.read_fbo = gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING)
        self.viewport = tuple(gl.glGetIntegerv(gl.GL_VIEWPORT))
        self.scissor_enabled = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        self.scissor_box = tuple(gl.glGetIntegerv(gl.GL_SCISSOR_BOX))
        self.blend_enabled = bool(gl.glIsEnabled(gl.GL_BLEND))
        self.blend_eq_rgb = gl.glGetIntegerv(gl.GL_BLEND_EQUATION_RGB)
        self.blend_eq_a = gl.glGetIntegerv(gl.GL_BLEND_EQUATION_ALPHA)
        self.blend_src_rgb = gl.glGetIntegerv(gl.GL_BLEND_SRC_RGB)
        self.blend_dst_rgb = gl.glGetIntegerv(gl.GL_BLEND_DST_RGB)
        self.blend_src_a = gl.glGetIntegerv(gl.GL_BLEND_SRC_ALPHA)
        self.blend_dst_a = gl.glGetIntegerv(gl.GL_BLEND_DST_ALPHA)
        self.color_mask = tuple(gl.glGetBooleanv(gl.GL_COLOR_WRITEMASK))

    def restore(self):
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.draw_fbo)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.read_fbo)
        gl.glViewport(*self.viewport)
        (gl.glEnable if self.scissor_enabled else gl.glDisable)(gl.GL_SCISSOR_TEST)
        gl.glScissor(*self.scissor_box)
        if self.blend_enabled:
            gl.glEnable(gl.GL_BLEND)
        else:
            gl.glDisable(gl.GL_BLEND)
        gl.glBlendEquationSeparate(self.blend_eq_rgb, self.blend_eq_a)
        gl.glBlendFuncSeparate(self.blend_src_rgb, self.blend_dst_rgb, self.blend_src_a, self.blend_dst_a)
        gl.glColorMask(*self.color_mask)


# ==============================
# Shader sources + programs
# ==============================
def _compile(shader_type, src: str) -> int:
    s = gl.glCreateShader(shader_type)
    gl.glShaderSource(s, src)
    gl.glCompileShader(s)
    if gl.glGetShaderiv(s, gl.GL_COMPILE_STATUS) != gl.GL_TRUE:
        raise RuntimeError(gl.glGetShaderInfoLog(s).decode("utf-8"))
    return s


def _link(vs: int, fs: int) -> int:
    p = gl.glCreateProgram()
    gl.glAttachShader(p, vs)
    gl.glAttachShader(p, fs)
    gl.glLinkProgram(p)
    if gl.glGetProgramiv(p, gl.GL_LINK_STATUS) != gl.GL_TRUE:
        raise RuntimeError(gl.glGetProgramInfoLog(p).decode("utf-8"))
    gl.glDetachShader(p, vs)
    gl.glDetachShader(p, fs)
    gl.glDeleteShader(vs)
    gl.glDeleteShader(fs)
    return p


_FULLSCREEN_VS = """
#version 330 core
const vec2 V[3] = vec2[3](vec2(-1,-1), vec2(3,-1), vec2(-1,3));
out vec2 vUV; // 0..1
void main() {
  gl_Position = vec4(V[gl_VertexID], 0, 1);
  vUV = 0.5 * (gl_Position.xy + 1.0);
}
"""

_MASK_FS = """
#version 330 core
uniform float uLayerNorm; // layer/255 in [0,1]
out vec4 oColor;
void main(){
  oColor = vec4(uLayerNorm, 0.0, 0.0, 1.0);
}
"""

# NO 0.5 offset; NEAREST mask sampling; integer compare
_COPY_FS = """
#version 330 core
in vec2 vUV;
uniform vec4 uTint;
uniform sampler2D uSrc;      // snapshot
uniform sampler2D uMask;     // GL_R8, NEAREST
uniform vec2  uFBSize;       // framebuffer size in px
uniform vec4  uSrcRectPx;    // x0,y0,x1,y1 (framebuffer coords; y bottom-left)
uniform int   uLayer;        // 0..255
out vec4 oColor;

void main(){
  float x0 = uSrcRectPx.x, y0 = uSrcRectPx.y, x1 = uSrcRectPx.z, y1 = uSrcRectPx.w;

  // Map tile UV -> framebuffer pixel coords (no sub-pixel offset)
  vec2 srcPx = vec2(mix(x0, x1, vUV.x), mix(y0, y1, vUV.y));
  vec2 uv    = srcPx / uFBSize;

  // mask is GL_R8 -> k/255 values with NEAREST
  int maskLayer = int(floor(texture(uMask, uv).r * 255.0 + 0.5));

  if (maskLayer <= uLayer) {
    oColor = texture(uSrc, uv) * uTint;
  } else {
    discard; // preserve pre-existing (stale) pixels in tile
  }
}
"""


# ==============================
# Main class
# ==============================
class TileCacheMasked:
    def __init__(self):
        self.enabled: bool = True
        self.top_is_low: bool = True  # True => small layer index is on top; False => larger is on top


        random_float = random.Random().random
        self.frame_tint = (0.5 + 0.5 * random_float(),
                0.5 + 0.5 * random_float(),
                0.5 + 0.5 * random_float(), 1.0)
        # Lookup dicts for bubbling
        self.py_id_to_keys: Dict[str, set] = {}
        self.key_to_parent_key: Dict[str, str] = {}

        self._tiles: Dict[str, _Tile] = {}
        self._sizes = {}  # key -> (w,h)
        self._stack: List[_Ctx] = []
        self._pending: List[_Pending] = []

        # mask/snapshot
        self._fb_size: Tuple[int, int] = (0, 0)
        self._mask_tex: Optional[int] = None
        self._mask_fbo: Optional[int] = None
        self._snapshot_tex: Optional[int] = None
        self._snapshot_fbo: Optional[int] = None
        self._mask_rects: List[_Rect] = []

        # programs and uniform locations
        self._prog_mask: Optional[int] = None
        self._prog_copy: Optional[int] = None
        self._loc_uSrc = None
        self._loc_uMask = None
        self._loc_uFBSize = None
        self._loc_uSrcRectPx = None
        self._loc_uLayer = None
        self.pending_invalid = []


    # ----- Public toggles / lifecycle -----
    def set_enabled(self, on: bool) -> None:
        if on and not self.enabled:
            for t in self._tiles.values():
                if t is not None:
                    t.dirty = True
            request_render()
        self.enabled = on

    def set_top_is_low(self, v: bool) -> None:
        self.top_is_low = bool(v)

    def invalidate_by_obj(self, obj, name=None):

        if name is not None:
            keys = self.py_id_to_keys.get(f"{id(obj)}.{name}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate(k)

        keys = self.py_id_to_keys.get(f"{id(obj)}", None)
        if keys is not None:
            for k in keys:
                self.invalidate(k)

    def apply_invalid(self):
        for t in self.pending_invalid:
            if t is not None:
                t.dirty = True
        # if self.pending_invalid:
        #     request_render()
        self.pending_invalid.clear()

    def invalidate(self, key: str, immediate=False) -> None:
        t = self._tiles.get(key)
        if t is not None:
            if t: t.dirty = True

        # Invalidate parent
        self.pending_invalid.append(t)
        parent = self.key_to_parent_key.get(key, None)

        # self.invalidate_all()
        if parent is not None:
            self.invalidate(parent)

    def invalidate_all(self) -> None:
        for t in self._tiles.values():
            if t is not None:
                t.dirty = True
        request_render()

    def get_texture_id(self, key: str) -> Optional[int]:
        t = self._tiles.get(key)
        if t is None:
            return None
        return t.tex if t else None

    def cleanup(self) -> None:
        for t in self._tiles.values():
            gl.glDeleteFramebuffers(1, [t.fbo])
            gl.glDeleteTextures(1, [t.tex])
            if t.rbo is not None:
                gl.glDeleteRenderbuffers(1, [t.rbo])
        self._tiles.clear()

        if self._mask_fbo:
            gl.glDeleteFramebuffers(1, [self._mask_fbo]);
            self._mask_fbo = None
        if self._mask_tex:
            gl.glDeleteTextures(1, [self._mask_tex]);
            self._mask_tex = None
        if self._snapshot_fbo:
            gl.glDeleteFramebuffers(1, [self._snapshot_fbo]);
            self._snapshot_fbo = None
        if self._snapshot_tex:
            gl.glDeleteTextures(1, [self._snapshot_tex]);
            self._snapshot_tex = None
        if self._prog_mask:
            gl.glDeleteProgram(self._prog_mask);
            self._prog_mask = None
        if self._prog_copy:
            gl.glDeleteProgram(self._prog_copy);
            self._prog_copy = None

    # ----- Mask API (per-view rectangles) -----
    def mask_begin_frame(self, framebuffer_size: Tuple[int, int]) -> None:
        fb_w, fb_h = map(int, framebuffer_size)

        random_float = random.Random().random
        self.frame_tint = (0.5 + 0.5 * random_float(),
                           0.5 + 0.5 * random_float(),
                           0.5 + 0.5 * random_float(), 1.0)

        if (fb_w, fb_h) != self._fb_size or self._snapshot_fbo is None:
            self._fb_size = (fb_w, fb_h)
            # reallocate mask/snapshot
            if self._mask_tex:
                gl.glDeleteTextures(1, [self._mask_tex]);
                self._mask_tex = None
            if self._mask_fbo:
                gl.glDeleteFramebuffers(1, [self._mask_fbo]);
                self._mask_fbo = None
            if self._snapshot_tex:
                gl.glDeleteTextures(1, [self._snapshot_tex]);
                self._snapshot_tex = None
            if self._snapshot_fbo:
                gl.glDeleteFramebuffers(1, [self._snapshot_fbo]);
                self._snapshot_fbo = None

            self._mask_tex = _create_mask_tex(fb_w, fb_h)
            self._mask_fbo, _ = _create_fbo_with_tex(self._mask_tex, False, fb_w, fb_h)

            self._snapshot_tex = _create_color_tex(fb_w, fb_h)
            self._snapshot_fbo, _ = _create_fbo_with_tex(self._snapshot_tex, False, fb_w, fb_h)

        self._mask_rects.clear()

    def mask_mark_rect(self, layer: int, x: float, y: float, w: float, h: float) -> None:
        self._mask_rects.append(_Rect(layer, x, y, w, h))

    def mask_mark_view(self, layer: int, x: float, y: float, w: float, h: float) -> None:
        self.mask_mark_rect(layer, x, y, w, h)

    # ----- Helpers for transforms & clipping -----
    def _get_current_clip_rect_screen(self) -> Tuple[float, float, float, float]:
        """Screen-space clip of what's actually drawable right now."""
        padding = 5
        dl = imgui.get_window_draw_list()
        if hasattr(dl, "get_clip_rect_min") and hasattr(dl, "get_clip_rect_max"):
            minx, miny = dl.get_clip_rect_min()
            maxx, maxy = dl.get_clip_rect_max()
            return (minx, miny, maxx, maxy)

        wx, wy = imgui.get_window_position()
        # scr
        crx0, cry0 = imgui.get_window_content_region_min()
        crx1, cry1 = imgui.get_window_content_region_max()
        sx = imgui.get_scroll_x()
        sy = imgui.get_scroll_y()
        x0 = wx + crx0 - sx
        y0 = wy + cry0 - sy
        x1 = wx + crx1 - sx
        y1 = wy + cry1 - sy
        return (x0, y0, x1, y1)

    @staticmethod
    def _clip_rect(x: float, y: float, w: float, h: float, clip_xyxy: Tuple[float, float, float, float]
                   ) -> Optional[Tuple[float, float, float, float]]:
        cx0, cy0, cx1, cy1 = clip_xyxy
        x0 = max(x, cx0);
        y0 = max(y, cy0)
        x1 = min(x + w, cx1);
        y1 = min(y + h, cy1)
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    @staticmethod
    def _get_draw_xform():
        dd = imgui.get_draw_data()
        dp_x, dp_y = dd.display_pos  # top-left of draw space (screen pixels)
        s_x, s_y = dd.frame_buffer_scale  # DPI scale to framebuffer pixels
        fb_w = snap_int(dd.display_size[0] * s_x)
        fb_h = snap_int(dd.display_size[1] * s_y)
        return dp_x, dp_y, s_x, s_y, fb_w, fb_h

    @staticmethod
    def _screen_rect_to_fb_xyxy(x, y, w, h, dp_x, dp_y, s_x, s_y, fb_h):
        # ImGui top-left rect to framebuffer (bottom-left origin), in px
        x0 = (x - dp_x) * s_x
        x1 = (x + w - dp_x) * s_x
        y_top0 = (y - dp_y) * s_y
        y_top1 = (y + h - dp_y) * s_y
        y0 = fb_h - y_top1
        y1 = fb_h - y_top0
        return ((x0), (y0), (x1), y1)

    # ----- Begin/End pair with per-view layer -----
    def mark_start_offscreen(self, input_value, collection, draw_state, name, key: str, layer: int, global_toggles=None,
                             indent_size=0, width=0, height=0) -> bool:
        x, y = imgui.get_cursor_screen_pos()
        # Snap cursor to nearest pixel to avoid sub-pixel jitter
        imgui.set_cursor_screen_pos((snap_int(x), snap_int(y)))

        layer = int(max(0, min(255, layer)))

        # Avoid collision with background clear value used in the mask
        if self.top_is_low:
            if layer == 255: layer = 254  # reserve 255 for background when using MIN
        else:
            if layer == 0: layer = 1  # reserve 0 for background when using MAX
        size = self._sizes.get(key, None)

        parent_ctx = self._stack[-1] if self._stack else None
        self.key_to_parent_key[key] = parent_ctx.key if parent_ctx else None
        if name is not None:
            name_key = f"{id(collection)}.{name}"
            self.py_id_to_keys[name_key] = self.py_id_to_keys.get(name_key, set())
            self.py_id_to_keys[name_key].add(key)

        if isinstance(input_value, (list, dict, set)) or hasattr(input_value, '__dict__'):
            self.py_id_to_keys.setdefault(f"{id(input_value)}", set()).add(key)

        self.py_id_to_keys.setdefault(f"{id(draw_state)}", set()).add(key)

        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0,0))
        imgui.begin_group()
        imgui.pop_style_var(2)
        imgui.push_id(f"tilecache_{key}")
        if size is not None:
            w = max(0, size[0])
            h = max(0, size[1])

            # Mark THIS VIEW's rect in the mask for this frame (clipped to visible area)
            if w > 0 and h > 0:
                clip = self._get_current_clip_rect_screen()
                clipped = self._clip_rect(x, y, w, h, clip)
                if clipped:
                    cx, cy, cw, ch = clipped
                    self.mask_mark_view(layer, cx, cy, cw, ch)

            if not self.enabled:
                self._stack.append(_Ctx(key, (x, y), size, layer, False))
                return True

            tile = _ensure_tile(self._tiles.get(key), size[0], size[1])
            self._tiles[key] = tile

            if tile is not None and not tile.dirty and size[0] > 0 and size[1] > 0:

                if global_toggles.offscreen_debug:
                    tint = (1, 1, 1, 1)
                else:
                    tint = (1,1,1,1)

                imgui.image(tile.tex, snap_int(size[0]), snap_int(size[1]), uv0=(0.0, 1.0), uv1=(1.0, 0.0), tint_color=tint)
                draw_state.imgui_is_active = imgui.is_item_active()
                draw_state.imgui_is_focused = imgui.is_item_focused()
                draw_state.imgui_is_hovered = imgui.is_item_hovered()
                draw_state.imgui_is_edited = imgui.is_item_edited()
                self._stack.append(_Ctx(draw_state, key, (x, y), size, layer, True))
                return False


        self._stack.append(_Ctx(draw_state, key, (x, y), size, layer, False))
        draw_state.imgui_is_active = imgui.is_item_active()
        draw_state.imgui_is_focused = imgui.is_item_focused()
        draw_state.imgui_is_hovered = imgui.is_item_hovered()
        draw_state.imgui_is_edited = imgui.is_item_edited()

        return True

    def mark_end_offscreen(self) -> None:
        ctx = self._stack.pop()

        imgui.pop_id()
        imgui.end_group()

        if not self._stack:
            return
        no_size_yet = ctx.size is None



        if not ctx.drew_cached:
            rect_size = imgui.get_item_rect_size()

            if ctx.size is None or abs(ctx.size[0] - rect_size.x) > 2 or abs(ctx.size[1] - rect_size.y) > 2:
                ctx.size = (max(0, rect_size.x), max(0, rect_size.y))

        if not self.enabled or ctx.drew_cached:
            return

        if no_size_yet:
            pass

        self._sizes[ctx.key] = ctx.size
        if not no_size_yet:
            tile = _ensure_tile(self._tiles.get(ctx.key), ctx.size[0], ctx.size[1])
            self._tiles[ctx.key] = tile
            self._pending.append(_Pending(tile=tile, pos=ctx.pos, size=ctx.size, layer=ctx.layer))

    # ----- Finalize (post-frame) -----
    def finalize_captures(self, framebuffer_size: Tuple[int, int], global_toggles=None) -> None:

        if self._snapshot_fbo is None:
            return

        if not self._pending:
            return

        # Use DrawData mapping (fixes drift with scroll/clip offsets)
        dp_x, dp_y, s_x, s_y, dd_fb_w, dd_fb_h = self._get_draw_xform()
        fb_w, fb_h = self._fb_size  # textures were allocated to this during mask_begin_frame()

        # If sizes mismatch (e.g., you captured the wrong FBO), fall back to allocated size.
        if (fb_w != dd_fb_w) or (fb_h != dd_fb_h):
            dd_fb_w, dd_fb_h = fb_w, fb_h

        st = _GLState()
        try:
            # 1) Snapshot default framebuffer to texture (resolves MSAA via blit)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, 0)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self._snapshot_fbo)
            gl.glBlitFramebuffer(0, 0, dd_fb_w, dd_fb_h, 0, 0, dd_fb_w, dd_fb_h,
                                 gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)

            # 2) Build mask from queued VIEW rectangles using MIN or MAX on RED
            self._ensure_programs()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._mask_fbo)
            gl.glViewport(0, 0, fb_w, fb_h)
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)

            # Clear depends on blend rule:
            if self.top_is_low:
                gl.glClearColor(1.0, 0.0, 0.0, 1.0)  # start at max, MIN keeps smaller (bottom)
            else:
                gl.glClearColor(0.0, 0.0, 0.0, 1.0)  # start at min, MAX keeps larger (top)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)

            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendEquation(gl.GL_MIN if self.top_is_low else gl.GL_MAX)
            gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)  # factors ignored by MIN/MAX

            gl.glUseProgram(self._prog_mask)
            loc_layer_norm = gl.glGetUniformLocation(self._prog_mask, "uLayerNorm")
            for r in self._mask_rects:
                # ImGui top-left -> FB bottom-left via transform
                x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(
                    r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h
                )
                w = max(0.0, x1 - x0);
                h = max(0.0, y1 - y0)
                if w <= 0 or h <= 0:
                    continue
                gl.glScissor(snap_int(x0), snap_int(y0), snap_int(w), snap_int(h))
                gl.glUniform1f(loc_layer_norm, r.layer / 255.0)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

            # restore default write masks
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glUseProgram(0)

            # 3) For each pending tile, copy only where mask/layer; leave stale elsewhere
            gl.glUseProgram(self._prog_copy)
            gl.glActiveTexture(gl.GL_TEXTURE0);
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._snapshot_tex)
            gl.glUniform1i(self._loc_uSrc, 0)
            gl.glActiveTexture(gl.GL_TEXTURE1);
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._mask_tex)
            gl.glUniform1i(self._loc_uMask, 1)
            gl.glUniform2f(self._loc_uFBSize, float(fb_w), float(fb_h))

            for p in self._pending:
                x, y = p.pos
                w, h = p.size

                # source rect in framebuffer coords (xyxy, y bottom-left)
                x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(
                    x, y, w, h, dp_x, dp_y, s_x, s_y, fb_h
                )

                if p.tile is not None:

                    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, p.tile.fbo)
                    gl.glViewport(0, 0, snap_int(p.tile.size[0]), snap_int(p.tile.size[1]))

                    if global_toggles is not None and global_toggles.offscreen_debug:
                        gl.glUniform4f(gl.glGetUniformLocation(self._prog_copy, "uTint"), *self.frame_tint)
                    else:
                        gl.glUniform4f(gl.glGetUniformLocation(self._prog_copy, "uTint"), 1.0, 1.0, 1.0, 1.0)

                    # do NOT clear; preserve stale pixels under overlaps
                    gl.glUniform4f(self._loc_uSrcRectPx, float(x0), float(y0), float(x1), float(y1))

                    gl.glUniform1i(self._loc_uLayer, snap_int(p.layer))
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
                    p.tile.dirty = False

            gl.glUseProgram(0)
        finally:
            st.restore()
            self._pending.clear()
            self._mask_rects.clear()

            self.apply_invalid()

    # ----- internal -----
    def _ensure_programs(self):
        if self._prog_mask is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _MASK_FS)
            self._prog_mask = _link(vs, fs)

        if self._prog_copy is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _COPY_FS)
            self._prog_copy = _link(vs, fs)

            # cache & validate uniform locations
            self._loc_uSrc = gl.glGetUniformLocation(self._prog_copy, "uSrc")
            self._loc_uMask = gl.glGetUniformLocation(self._prog_copy, "uMask")
            self._loc_uFBSize = gl.glGetUniformLocation(self._prog_copy, "uFBSize")
            self._loc_uSrcRectPx = gl.glGetUniformLocation(self._prog_copy, "uSrcRectPx")
            self._loc_uLayer = gl.glGetUniformLocation(self._prog_copy, "uLayer")
            for name, loc in [("uSrc", self._loc_uSrc),
                              ("uMask", self._loc_uMask),
                              ("uFBSize", self._loc_uFBSize),
                              ("uSrcRectPx", self._loc_uSrcRectPx),
                              ("uLayer", self._loc_uLayer)]:
                assert loc != -1, f"[copy] uniform {name} missing/optimized out (loc=-1)"
