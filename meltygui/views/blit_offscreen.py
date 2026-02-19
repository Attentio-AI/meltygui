from __future__ import annotations

import random
from collections import deque, defaultdict
from copy import copy
from dataclasses import dataclass
from math import ceil, floor
from typing import Dict, List, Optional, Tuple, MutableMapping

from OpenGL import GL as gl
import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.core_model.core_enums import OffscreenDebugMode
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace

"""
Per-view tile caching with a post-frame mask.

PASS STRUCTURE:
1. Snapshot framebuffer
2. Build _mask_tex (flat, fresh geometry only) - for pixel copying
3. For each dirty tile: build _sub_mask_tex and copy pixels
4. For each dirty tile: build _full_sub_mask_tex (with children's cached masks) and save to tile.mask_tex
5. Build _full_mask_tex using all tiles' cached mask_tex - complete depth map

This ensures:
- Pixel copying ignores children (uses flat mask)
- Final depth map includes all nested children (uses full mask with cached subtrees)
"""

INV_65535 = 1.0 / 65535.0


# ==============================
# Small structs
# ==============================
@dataclass
class Tile:
    draw_state: any
    fbo: int
    tex: int
    mask_tex: int  # Cached subtree mask for this tile
    rbo: Optional[int]
    size: Tuple[int, int]
    dirty: bool = True
    last_clean_frame: int = -1
    last_invalidated_frame: int = 3
    force_invalidate: bool = False
    mask_layer: int = 0  # Layer at which mask_tex was built (for relative depth offset)


@dataclass
class _Ctx:
    draw_state: any
    key: str
    pos: Tuple[float, float]
    size: Optional[Tuple[int, int]]
    layer: int
    depth_and_layer: any
    drew_cached: bool
    auto_resize: bool


@dataclass
class _Pending:
    draw_state: any
    tile: Tile
    pos: Tuple[float, float]
    size: Tuple[int, int]
    layer: int
    depth_and_layer: any
    key: str


@dataclass
class _Rect:
    draw_state: any
    layer: int
    depth_and_layer: any
    x: float
    y: float
    w: float
    h: float
    key: str
    order: int
    corner_radius: float = 6.0  # Corner radius for rounded rectangles
    blend_max: bool = False


# ==============================
# GL helpers
# ==============================
def _create_color_tex(w: int, h: int, internal_format=gl.GL_RGBA8) -> int:
    Melty.cache.tex_init_count += 1

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
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R16, w, h, 0, gl.GL_RED, gl.GL_UNSIGNED_SHORT, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return tex


def snap_int(v: float) -> int:
    return int(v)


def _create_fbo_with_tex(tex: int, depth_stencil: bool, w, h) -> Tuple[int, Optional[int]]:
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
    fbo = gl.glGenFramebuffers(1)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, tex, 0)
    gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])

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


def _ensure_tile(existing: Optional[Tile], w: int, h: int, frame_id: int = 0, draw_state=None, tile_id=None) -> \
Optional[Tile]:
    if existing and existing.size == (w, h):
        return existing

    if w == 0 or h == 0:
        return None

    try:
        new_tex = _create_color_tex(w, h)
    except Exception as e:
        existing_size = existing.size if existing else None
        reset = "\033[0m"
        pink = "\033[95m"
        print(f"{pink}{draw_state.to_dict()}\n{'=' * 10} "
              f"Failed to create color texture for tile (size {w}x{h}): {e}"
              f"\nCurrent size {existing_size}\n{'=' * 10}{reset}")
        return None

    new_mask_tex = _create_mask_tex(w, h)
    new_fbo, new_rbo = _create_fbo_with_tex(new_tex, True, w, h)

    if existing:
        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, existing.fbo)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, new_fbo)
            gl.glClearColor(0, 0, 0, 0.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
            gl.glBlitFramebuffer(
                0,
                0,
                snap_int(existing.size[0]),
                snap_int(existing.size[1]),
                0,
                0,
                snap_int(w),
                snap_int(h),
                gl.GL_COLOR_BUFFER_BIT,
                gl.GL_NEAREST,
            )
        finally:
            st.restore()

        gl.glDeleteFramebuffers(1, [existing.fbo])
        gl.glDeleteTextures(1, [existing.tex])
        gl.glDeleteTextures(1, [existing.mask_tex])
        if existing.rbo is not None:
            gl.glDeleteRenderbuffers(1, [existing.rbo])

    else:
        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, new_fbo)
            gl.glViewport(0, 0, snap_int(w), snap_int(h))
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glClearColor(0, 0, 0, 0.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        finally:
            st.restore()

    t = Tile(draw_state=draw_state, fbo=new_fbo, tex=new_tex, mask_tex=new_mask_tex, rbo=new_rbo, size=(w, h),
             dirty=True)
    t.last_invalidated_frame = frame_id + 1
    request_render()
    return t


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
        gl.glBlendFuncSeparate(
            self.blend_src_rgb, self.blend_dst_rgb, self.blend_src_a, self.blend_dst_a
        )
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
out vec2 vUV;
void main() {
  gl_Position = vec4(V[gl_VertexID], 0, 1);
  vUV = 0.5 * (gl_Position.xy + 1.0);
}
"""

_MASK_FS = """
#version 330 core
uniform float uRankNorm;
out vec4 oColor;
void main(){
  oColor = vec4(uRankNorm, 0.0, 0.0, 1.0);
}
"""

# Rounded rectangle mask shader using SDF
_MASK_ROUNDED_FS = """
#version 330 core
uniform float uRankNorm;
uniform vec2 uRectSize;      // Width and height in pixels
uniform float uCornerRadius; // Corner radius in pixels
uniform float uMargin;
in vec2 vUV;
out vec4 oColor;

float sdRoundedBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), uMargin) + length(max(q, uMargin)) - r;
}

void main() {
    // Convert UV (0-1) to pixel coordinates centered at rect center
    vec2 pixelPos = (vUV - 0.5) * uRectSize;
    vec2 halfSize = uRectSize * 0.5;

    // Clamp corner radius to half of smallest dimension
    float r = min(uCornerRadius, min(halfSize.x, halfSize.y));

    float d = sdRoundedBox(pixelPos, halfSize, r);

    if (d > 0.0) {
        if (uMargin == 0.0) {
            oColor = vec4(0.0, 0.0, 0.0, 0.0);

        } else { 
            discard;
        }
    }

    oColor = vec4(uRankNorm, 0.0, 0.0, 1.0);
}
"""

_MASK_TEXTURED_FS = """
#version 330 core
uniform sampler2D uTex;
in vec2 vUV;
out vec4 oColor;
void main() {
    oColor = texture(uTex, vUV);
}
"""

# Rounded rectangle textured mask - clips texture to rounded rect
_MASK_TEXTURED_ROUNDED_FS = """
#version 330 core
uniform sampler2D uTex;
uniform vec2 uRectSize;      // Width and height in pixels
uniform float uCornerRadius; // Corner radius in pixels
in vec2 vUV;
out vec4 oColor;
uniform float uMargin;

float sdRoundedBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), uMargin) + length(max(q, uMargin)) - r;
}

void main() {
    vec2 pixelPos = (vUV - 0.5) * uRectSize;
    vec2 halfSize = uRectSize * 0.5;
    float r = min(uCornerRadius, min(halfSize.x, halfSize.y));

    float d = sdRoundedBox(pixelPos, halfSize, r);

    if (d > 0.0) {
        discard;
    }

    oColor = texture(uTex, vUV);
}
"""

_MASK_TEXTURED_OFFSET_FS = """
#version 330 core
uniform sampler2D uTex;
uniform float uOffset;
in vec2 vUV;
out vec4 oColor;
void main() {
    float val = texture(uTex, vUV).r;
    if (val > -10.0) {
        oColor = vec4(val + uOffset, 0.0, 0.0, 1.0);
    } else {
        discard;
    }
}
"""

# Rounded rectangle textured mask with offset - clips texture to rounded rect
_MASK_TEXTURED_OFFSET_ROUNDED_FS = """
#version 330 core
uniform sampler2D uTex;
uniform float uOffset;
uniform vec2 uRectSize;      // Width and height in pixels
uniform float uCornerRadius; // Corner radius in pixels
in vec2 vUV;
out vec4 oColor;
uniform float uMargin;

float sdRoundedBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), uMargin) + length(max(q, uMargin)) - r;
}

void main() {
    vec2 pixelPos = (vUV - 0.5) * uRectSize;
    vec2 halfSize = uRectSize * 0.5;
    float r = min(uCornerRadius, min(halfSize.x, halfSize.y));

    float d = sdRoundedBox(pixelPos, halfSize, r);

    if (d > 0.0) {
        discard;
    }

    float val = texture(uTex, vUV).r;
    if (val > -10.0) {
        oColor = vec4(val + uOffset, 0.0, 0.0, 1.0);
    } else {
        discard;
    }
}
"""

_COPY_FS = """
#version 330 core
in vec2 vUV;

uniform int   uCopyDebugMode; 
uniform float uDebugScale;
uniform vec4  uTint;

uniform sampler2D uSrc;       
uniform sampler2D uTopMask;   
uniform sampler2D uSubMask;   
uniform vec2  uFBSize;
uniform vec4  uSrcRectPx;     

out vec4 oColor;

float checker(vec2 uv) {
  vec2 px = uv * uFBSize;
  int cx = int(floor(px.x / 16.0));
  int cy = int(floor(px.y / 16.0));
  return float((cx + cy) % 2);
}

void main(){
  float x0 = uSrcRectPx.x, y0 = uSrcRectPx.y, x1 = uSrcRectPx.z, y1 = uSrcRectPx.w;

  vec2 srcPx = vec2(mix(x0, x1, vUV.x), mix(y0, y1, vUV.y));
  vec2 uv    = srcPx / uFBSize;

  int topRank = int(floor(texture(uTopMask, uv).r * 65535.0 + 0.5));
  int subRank = int(floor(texture(uSubMask, uv).r * 65535.0 + 0.5));

  if (uCopyDebugMode == 1) {                
    oColor = vec4(uv, 0.0, 1.0) * uTint; return;
  } else if (uCopyDebugMode == 2) {         
    vec2 sp = clamp(srcPx / max(uFBSize, vec2(1.0)), 0.0, 1.0);
    oColor = vec4(sp, 0.0, 1.0) * uTint; return;
  } else if (uCopyDebugMode == 3) {         
    float g = float(topRank) / uDebugScale;
    oColor = vec4(g, g, g, 1.0) * uTint; return;
  } else if (uCopyDebugMode == 4) {         
    float g = float(subRank) / uDebugScale;
    oColor = vec4(g, g, g, 1.0) * uTint; return;
  } else if (uCopyDebugMode == 5) {         
    float c = checker(uv);
    oColor = vec4(vec3(c), 1.0) * uTint; return;
  } else if (uCopyDebugMode == 6) {         
    oColor = uTint; return;
  }

  if (subRank > 0 && topRank == subRank) {
      oColor = texture(uSrc, uv) * uTint;
      oColor.a = 1.0;
  } else {
      discard;
  }
}
"""

_BLIT_FS = """
#version 330 core
in vec2 vUV;
uniform sampler2D uTex;
out vec4 oColor;
void main() {
  oColor = texture(uTex, vUV);
}
"""


# ==============================
# Main class
# ==============================
class TileCacheMasked:
    def __init__(self):
        self.initial_value = {}
        self.did_deviate = {}
        self.enabled: bool = False

        self._LAYER_BG = 0
        self._LAYER_MIN = -2048
        self._LAYER_MAX = 2048

        self.seen_ids = set()
        self.tex_init_count = 0

        self.offscreen_debug_mode: OffscreenDebugMode = OffscreenDebugMode.OFF
        self.offscreen_scale = 200.0

        self.copy_debug_mode = OffscreenDebugMode.OFF
        self.debug_overlay_mask_to_screen: bool = False
        self.debug_overlay_src_to_screen: bool = False

        # Reuse RNG (avoid Random() per frame)
        self._rng = random.Random()
        self._rand = self._rng.random
        rf = self._rand
        self.frame_tint = (0.5 + 0.5 * rf(), 0.5 + 0.5 * rf(), 0.5 + 0.5 * rf(), 1.0)

        self.py_id_to_keys: Dict[str, set] = {}
        self.key_to_parent_key: Dict[str, str] = {}
        self.parent_key_to_child_keys: Dict[str, dict] = {}
        self.parent_key_to_child_keys_last: Dict[str, dict] = {}
        self.key_to_draw_state: Dict[str, any] = {}

        self._tiles: Dict[str, Tile] = {}
        self._sizes = {}
        self._stack: List[_Ctx] = []
        self._key_to_ctx: Dict[str, _Ctx] = {}
        self._pending: List[_Pending] = []
        self.all_keys = set()

        self._fb_size: Tuple[int, int] = (0, 0)

        # Top mask - fresh geometry only, for pixel copying
        self._mask_tex: Optional[int] = None
        self._mask_fbo: Optional[int] = None

        # Full mask - uses cached subtree masks, for complete depth map
        self._full_mask_tex: Optional[int] = None
        self._full_mask_fbo: Optional[int] = None

        # Subtree mask for pixel copying - fresh geometry only
        self._sub_mask_tex: Optional[int] = None
        self._sub_mask_fbo: Optional[int] = None

        # Subtree mask for tile.mask() - uses children's cached masks
        self._full_sub_mask_tex: Optional[int] = None
        self._full_sub_mask_fbo: Optional[int] = None

        self.snapshot_tex: Optional[int] = None
        self._snapshot_fbo: Optional[int] = None

        self._scratch_fbo: Optional[int] = None

        self._mask_rects: List[_Rect] = []
        self._shadow_mask_keys = set()
        self._shadow_rects: List[_Rect] = []

        self._rect_seq: int = 0

        self._prog_mask: Optional[int] = None
        self._prog_mask_rounded: Optional[int] = None
        self._prog_mask_textured: Optional[int] = None
        self._prog_mask_textured_rounded: Optional[int] = None
        self._prog_mask_textured_offset: Optional[int] = None
        self._prog_mask_textured_offset_rounded: Optional[int] = None
        self._prog_copy: Optional[int] = None
        self._prog_blit: Optional[int] = None

        # Cached uniform locations
        self._loc_mask_uRankNorm = None

        self._loc_maskr_uRankNorm = None
        self._loc_maskr_uRectSize = None
        self._loc_maskr_uCornerRadius = None
        self._loc_maskr_uMargin = None
        self._loc_tex_uTex = None

        self._loc_texr_uTex = None
        self._loc_texr_uRectSize = None
        self._loc_texr_uCornerRadius = None
        self._loc_texr_uMargin = None

        self._loc_texoff_uTex = None
        self._loc_texoff_uOffset = None

        self._loc_texoffr_uTex = None
        self._loc_texoffr_uOffset = None
        self._loc_texoffr_uRectSize = None
        self._loc_texoffr_uCornerRadius = None
        self._loc_texoffr_uMargin = None

        self._loc_uSrc = None
        self._loc_uTopMask = None
        self._loc_uSubMask = None
        self._loc_uFBSize = None
        self._loc_uSrcRectPx = None
        self._loc_copy_uCopyDebugMode = None
        self._loc_copy_uDebugScale = None
        self._loc_copy_uTint = None

        self._dummy_vao: Optional[int] = None
        self._recording: bool = False
        self._cancelled_keys: set[str] = set()
        self._enq_mask_keys: set[str] = set()
        self._enq_copy_keys: set[str] = set()
        self._frame_id: int = 0

        self.pending_invalid = []

    @property
    def full_mask_tex(self) -> Optional[int]:
        return self._full_mask_tex

    def _is_dirty(self, t: Optional[Tile]) -> bool:
        if t is None:
            return True
        return t.last_clean_frame < t.last_invalidated_frame

    def set_enabled(self, on: bool) -> None:
        if on and not self.enabled:
            for t in self._tiles.values():
                if t is not None:
                    t.dirty = True
                    t.last_invalidated_frame = max(t.last_invalidated_frame, self._frame_id)
            request_render()
        self.enabled = on

    def set_top_is_low(self, v: bool) -> None:
        pass

    def _resolve_key(self, key: str) -> str:
        return key

    def invalidate_current(self, force=False):
        if len(self._stack) == 0:
            return
        self.invalidate(self._stack[-1].key, force=force)

    def invalidate_up_current(self, max_depth=4, force=False):
        if len(self._stack) == 0:
            return
        self.invalidate_up(self._stack[-1].key, max_depth=max_depth, force=force)

    def invalidate_up_by_obj(self, obj, name=None, max_depth=4, force=False):
        if name is not None:
            keys = self.py_id_to_keys.get(f"{id(obj)}.{name}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate_up(k, max_depth=max_depth, force=force)
        else:
            keys = self.py_id_to_keys.get(f"{id(obj)}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate_up(k, max_depth=max_depth, force=force)

    def invalidate_by_obj(self, obj, name=None):
        if name is not None:
            keys = self.py_id_to_keys.get(f"{id(obj)}.{name}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate(k)
        else:
            keys = self.py_id_to_keys.get(f"{id(obj)}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate(k)

    def apply_invalid(self):
        for t in self.pending_invalid:
            if t is not None:
                t.dirty = self._is_dirty(t)
        self.pending_invalid.clear()

    def get_parent_keys(self, key):
        all_keys = [key]
        parent_key = self.key_to_parent_key.get(key, None)
        if parent_key and parent_key != key:
            all_keys.extend(self.get_parent_keys(parent_key))
        return all_keys

    def get_child_keys(self, key, depth=0, max_depth=4):
        if depth >= max_depth:
            return set()
        child_keys = self.parent_key_to_child_keys.get(key, {})
        all_keys = child_keys.copy()
        for ck in child_keys.values():
            all_keys.update(self.get_child_keys(ck[1], depth + 1, max_depth=max_depth))
        return all_keys

    def invalidate_up(self, k: str, max_depth=4, force=False) -> None:
        if k not in self._tiles:
            k = self.key_to_parent_key.get(k, None)

        self.invalidate(k, force=force)
        child_keys = self.get_child_keys(k, max_depth=max_depth).values()
        child_keys_list = list(child_keys)
        child_keys_list.sort(key=lambda x: x[0] if x[0] is not None else 0)

        parent_draw_state = self.key_to_draw_state.get(k, None)

        for top, child, child_draw_state in child_keys_list:
            inside_clip, below, above = parent_draw_state.inside_clip(child_draw_state)
            if child_draw_state.clipped and inside_clip:
                if child != k:
                    pt = self._tiles.get(child)
                    if pt is not None:
                        pt.last_invalidated_frame = max(pt.last_invalidated_frame, self._frame_id + 1)
                        pt.dirty = self._is_dirty(pt)
                        pt.force_invalidate = True
                        self.pending_invalid.append(pt)
            if above:
                continue
            if below:
                return

    def get_hash(self, draw_state):
        from src.lsd.gl_gui.model.dict_conversion import DictConversion

        if hasattr(draw_state._input_value, "hash") or isinstance(
                draw_state._input_value,
                (dict, list, set, DictConversion, tuple, int, float, str, bool, type(None)),
        ):
            input_val_hash = DictConversion.compute_hash(
                draw_state._input_value,
                exclude=draw_state.__excluded_attrs__,
                include_hidden=False,
            )
        else:
            input_val_hash = 0
        return (
            DictConversion.compute_hash(draw_state, exclude=draw_state.__excluded_attrs__, include_hidden=False),
            input_val_hash,
        )

    def invalidate(self, k: str, force=False) -> None:
        t = self._tiles.get(k)
        if t is not None:
            target_frame = self._frame_id + 1
            t.last_invalidated_frame = max(t.last_invalidated_frame, target_frame)
            t.dirty = self._is_dirty(t)
            self.pending_invalid.append(t)
            if force:
                t.force_invalidate = True

        parent_keys = self.get_parent_keys(k)
        for parent in parent_keys:
            if parent and parent != k:
                pt = self._tiles.get(parent)
                if pt is not None:
                    pt.force_invalidate = True
                    pt.last_invalidated_frame = max(pt.last_invalidated_frame, self._frame_id + 1)
                    pt.dirty = self._is_dirty(pt)
                    self.pending_invalid.append(pt)

    def invalidate_all(self) -> None:
        for t in self._tiles.values():
            if t is not None:
                t.last_invalidated_frame = max(t.last_invalidated_frame, self._frame_id + 1)
                t.force_invalidate = True
                self.pending_invalid.append(t)
        request_render()

    def get_texture_id(self, key: str) -> Optional[int]:
        rk = self._resolve_key(key)
        t = self._tiles.get(rk) or self._tiles.get(key)
        return t.tex if t else None

    def cleanup(self) -> None:
        for t in self._tiles.values():
            gl.glDeleteFramebuffers(1, [t.fbo])
            gl.glDeleteTextures(1, [t.tex])
            gl.glDeleteTextures(1, [t.mask_tex])
            if t.rbo is not None:
                gl.glDeleteRenderbuffers(1, [t.rbo])
        self._tiles.clear()

        if self._mask_fbo:
            gl.glDeleteFramebuffers(1, [self._mask_fbo])
            self._mask_fbo = None
        if self._mask_tex:
            gl.glDeleteTextures(1, [self._mask_tex])
            self._mask_tex = None
        if self._full_mask_fbo:
            gl.glDeleteFramebuffers(1, [self._full_mask_fbo])
            self._full_mask_fbo = None
        if self._full_mask_tex:
            gl.glDeleteTextures(1, [self._full_mask_tex])
            self._full_mask_tex = None
        if self._sub_mask_fbo:
            gl.glDeleteFramebuffers(1, [self._sub_mask_fbo])
            self._sub_mask_fbo = None
        if self._sub_mask_tex:
            gl.glDeleteTextures(1, [self._sub_mask_tex])
            self._sub_mask_tex = None
        if self._full_sub_mask_fbo:
            gl.glDeleteFramebuffers(1, [self._full_sub_mask_fbo])
            self._full_sub_mask_fbo = None
        if self._full_sub_mask_tex:
            gl.glDeleteTextures(1, [self._full_sub_mask_tex])
            self._full_sub_mask_tex = None
        if self._snapshot_fbo:
            gl.glDeleteFramebuffers(1, [self._snapshot_fbo])
            self._snapshot_fbo = None
        if self.snapshot_tex:
            gl.glDeleteTextures(1, [self.snapshot_tex])
            self.snapshot_tex = None
        if self._scratch_fbo:
            gl.glDeleteFramebuffers(1, [self._scratch_fbo])
            self._scratch_fbo = None
        if self._prog_mask:
            gl.glDeleteProgram(self._prog_mask)
            self._prog_mask = None
        if self._prog_mask_rounded:
            gl.glDeleteProgram(self._prog_mask_rounded)
            self._prog_mask_rounded = None
        if self._prog_mask_textured:
            gl.glDeleteProgram(self._prog_mask_textured)
            self._prog_mask_textured = None
        if self._prog_mask_textured_rounded:
            gl.glDeleteProgram(self._prog_mask_textured_rounded)
            self._prog_mask_textured_rounded = None
        if self._prog_mask_textured_offset:
            gl.glDeleteProgram(self._prog_mask_textured_offset)
            self._prog_mask_textured_offset = None
        if self._prog_mask_textured_offset_rounded:
            gl.glDeleteProgram(self._prog_mask_textured_offset_rounded)
            self._prog_mask_textured_offset_rounded = None
        if self._prog_copy:
            gl.glDeleteProgram(self._prog_copy)
            self._prog_copy = None
        if self._prog_blit:
            gl.glDeleteProgram(self._prog_blit)
            self._prog_blit = None

    def mask_begin_frame(self, framebuffer_size: Tuple[int, int]) -> None:
        fb_w, fb_h = map(int, framebuffer_size)
        self._frame_id += 1
        self._recording = True
        self._cancelled_keys.clear()
        self._enq_mask_keys.clear()
        self._enq_copy_keys.clear()

        rf = self._rand
        self.frame_tint = (0.5 + 0.5 * rf(), 0.5 + 0.5 * rf(), 0.5 + 0.5 * rf(), 1.0)

        if (fb_w, fb_h) != self._fb_size or self._snapshot_fbo is None:
            self._fb_size = (fb_w, fb_h)

            def safe_del_tex(t):
                if t:
                    gl.glDeleteTextures(1, [t])

            def safe_del_fbo(f):
                if f:
                    gl.glDeleteFramebuffers(1, [f])

            safe_del_tex(self._mask_tex)
            safe_del_fbo(self._mask_fbo)
            safe_del_tex(self._full_mask_tex)
            safe_del_fbo(self._full_mask_fbo)
            safe_del_tex(self._sub_mask_tex)
            safe_del_fbo(self._sub_mask_fbo)
            safe_del_tex(self._full_sub_mask_tex)
            safe_del_fbo(self._full_sub_mask_fbo)
            safe_del_tex(self.snapshot_tex)
            safe_del_fbo(self._snapshot_fbo)
            safe_del_fbo(self._scratch_fbo)

            self._mask_tex = _create_mask_tex(fb_w, fb_h)
            self._mask_fbo, _ = _create_fbo_with_tex(self._mask_tex, False, fb_w, fb_h)

            self._full_mask_tex = _create_mask_tex(fb_w, fb_h)
            self._full_mask_fbo, _ = _create_fbo_with_tex(self._full_mask_tex, False, fb_w, fb_h)

            self._sub_mask_tex = _create_mask_tex(fb_w, fb_h)
            self._sub_mask_fbo, _ = _create_fbo_with_tex(self._sub_mask_tex, False, fb_w, fb_h)

            self._full_sub_mask_tex = _create_mask_tex(fb_w, fb_h)
            self._full_sub_mask_fbo, _ = _create_fbo_with_tex(self._full_sub_mask_tex, False, fb_w, fb_h)

            self.snapshot_tex = _create_color_tex(fb_w, fb_h)
            self._snapshot_fbo, _ = _create_fbo_with_tex(self.snapshot_tex, False, fb_w, fb_h)

            self._scratch_fbo = gl.glGenFramebuffers(1)

        self._mask_rects.clear()
        self._rect_seq = 0

    def mask_mark_rect(
            self, draw_state: any, layer: int, depth_and_layer: any, x: float, y: float, w: float, h: float,
            key: str, corner_radius: float = 6.0
    ) -> None:
        # if key in self._enq_mask_keys:
        #     return
        self._enq_mask_keys.add(key)
        self._rect_seq = (self._rect_seq + 1) & 0xFF
        # if draw_state.total_z_offset < 0:
        #     blend_max = False
        # else:
        #     blend_max = True

        self._mask_rects.append(
            _Rect(draw_state, layer, depth_and_layer, x, y, w, h, key, self._rect_seq, corner_radius, blend_max=False))

    def mark_shadow(
            self, layer: int, depth_and_layer: any, x: float, y: float, w: float, h: float,
            corner_radius: float = 6.0, draw_state=None, parent_key=None,
    ) -> None:
        self._rect_seq = (self._rect_seq + 1) & 0xFF
        key = f"shadow_{self._rect_seq}"
        parent_ctx = self._stack[-1] if self._stack else None

        self._mask_rects.append(
            _Rect(draw_state, layer, depth_and_layer, x, y, w, h, key, self._rect_seq, corner_radius, blend_max=False))
        parent_key = parent_ctx.key if parent_ctx else None
        self._shadow_mask_keys.add(key)
        self.key_to_parent_key[key] = parent_key

    def mask_mark_view(
            self, draw_state: any, layer: int, depth_and_layer: any, x: float, y: float, w: float,
            h: float, key: str, corner_radius: float = 6.0
    ) -> None:
        self.mask_mark_rect(draw_state, layer, copy(depth_and_layer), x, y, w, h, key, corner_radius)

    def _get_current_clip_rect_screen(self) -> Tuple[float, float, float, float]:
        clip = Melty.get_clip_rect()
        return clip

    @staticmethod
    def _clip_rect(
            x: float,
            y: float,
            w: float,
            h: float,
            clip_xyxy: Tuple[float, float, float, float],
    ) -> Optional[Tuple[float, float, float, float]]:
        if clip_xyxy is None:
            return None
        cx0, cy0, cx1, cy1 = clip_xyxy
        x0 = max(x, cx0)
        y0 = max(y, cy0)
        x1 = min(x + w, cx1)
        y1 = min(y + h, cy1)
        if x1 <= x0 and y1 <= y0:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    @staticmethod
    def _fully_clipped(
            x: float,
            y: float,
            w: float,
            h: float,
            clip_xyxy: Tuple[float, float, float, float],
    ) -> bool:
        if clip_xyxy is None:
            return False
        cx0, cy0, cx1, cy1 = clip_xyxy
        x0 = max(x, cx0)
        y0 = max(y, cy0)
        x1 = min(x + w, cx1)
        y1 = min(y + h, cy1)
        return x1 <= x0 or y1 <= y0

    @staticmethod
    def _get_draw_xform():
        dd = imgui.get_draw_data()
        dp_x, dp_y = dd.display_pos
        s_x, s_y = 1, 1
        fb_w = snap_int(dd.display_size[0] * s_x)
        fb_h = snap_int(dd.display_size[1] * s_y)
        return dp_x, dp_y, s_x, s_y, fb_w, fb_h

    @staticmethod
    def _screen_rect_to_fb_xyxy(x, y, w, h, dp_x, dp_y, s_x, s_y, fb_h):
        x0 = (x - dp_x) * s_x
        x1 = (x + w - dp_x) * s_x
        y_top0 = (y - dp_y) * s_y
        y_top1 = (y + h - dp_y) * s_y
        y0 = fb_h - y_top1
        y1 = fb_h - y_top0
        return (x0, y0, x1, y1)

    def _collect_subtree_keys(self, root_key: str, mask_rects: List[_Rect]) -> set[str]:
        subtree = set()
        parent_of = self.key_to_parent_key
        for r in mask_rects:
            k = r.key
            while k is not None:
                if k == root_key:
                    subtree.add(r.key)
                    break
                k = parent_of.get(k)
        return subtree

    def get_current_parent(self):
        return self._stack[-1] if self._stack else None

    def insert_parent(self, parent):
        self._stack.append(parent)

    def remove_parent(self):
        if self._stack:
            self._stack.pop()

    def mark_uncached(self, name, input_value, collection, key: str, draw_state) -> None:
        rkey = key
        parent_ctx = self._stack[-1] if self._stack else None
        self.key_to_draw_state[rkey] = draw_state
        parent_key = parent_ctx.key if parent_ctx else None
        if parent_key is not None:
            if parent_key not in self.parent_key_to_child_keys:
                self.parent_key_to_child_keys[parent_key] = {}
            self.parent_key_to_child_keys[parent_key][rkey] = (draw_state.top, rkey, draw_state)

        if name is not None:
            name_key = f"{id(collection)}.{name}"
            self.py_id_to_keys.setdefault(name_key, set()).add(rkey)

        if isinstance(input_value, (list, dict, set, deque, MutableMapping)) or hasattr(input_value, "__dict__"):
            self.py_id_to_keys.setdefault(f"{id(input_value)}", set()).add(rkey)

        if f"{id(draw_state)}" not in self.py_id_to_keys:
            self.py_id_to_keys[f"{id(draw_state)}"] = set()

        self.py_id_to_keys[f"{id(draw_state)}"].add(rkey)
        rkey = self._resolve_key(key)
        self.py_id_to_keys.setdefault(f"{id(input_value)}", set()).add(rkey)
        self.py_id_to_keys.setdefault(f"{id(collection)}", set()).add(rkey)
        self.key_to_parent_key[rkey] = parent_ctx.key if parent_ctx else None

    def draw_tile(self, draw_state):
        imgui.push_id(f"{draw_state._tile_id}")
        rkey = draw_state._tile_id

        if draw_state.parent_window is not None:
            draw_state.left = draw_state.abs_left
            draw_state.top = draw_state.abs_top

        t = self._tiles.get(rkey)
        size = (draw_state.width, draw_state.height)
        layer = draw_state.z_pos

        has_area = size is not None and size[0] != 0 and size[1] != 0
        use_image = t and has_area and (t.size == (size[0], size[1])) and (not self._is_dirty(t))
        if has_area and not draw_state.closed and use_image:
            corner_radius = getattr(draw_state, "corner_radius", 6.0) or 6.0
            self.mask_mark_view(
                draw_state,
                layer,
                draw_state.shadow_depth,
                draw_state.left,
                draw_state.top,
                draw_state.width,
                draw_state.height,
                draw_state._tile_id,
                corner_radius,
            )

        if use_image:
            imgui.set_cursor_screen_pos((draw_state.left, snap_int(draw_state.top)))
            imgui.image(
                t.tex,
                snap_int(size[0]),
                snap_int(size[1]),
                uv0=(0.0, 1.0),
                uv1=(1.0, 0.0),
            )
            imgui.set_item_allow_overlap()

        imgui.pop_id()
        draw_state.last_seen = Melty.frame_count

    def mark_start_offscreen(self, draw_state) -> bool:
        if not self.enabled:
            return True

        if not draw_state.use_cache:
            return True

        input_value = draw_state._input_value
        collection = draw_state._collection
        key = draw_state._tile_id

        layer = draw_state.z_pos
        name = draw_state.name

        gl.glDisable(gl.GL_DEPTH_TEST)

        Melty.tile_id_stack.append(key)
        x, y = imgui.get_cursor_screen_pos()
        imgui.set_cursor_screen_pos((snap_int(x), snap_int(y)))
        layer = max(self._LAYER_MIN, min(self._LAYER_MAX, layer))

        parent_ctx = self._stack[-1] if self._stack else None
        rkey = self._resolve_key(key)
        size = (draw_state.width, draw_state.height)

        if not draw_state.auto_resize and draw_state.width is not None and draw_state.height is not None:
            size = snap_int(draw_state.width), snap_int(draw_state.height)
            self._sizes[rkey] = size

        if size is not None:
            min_width = draw_state.min_width
            min_height = draw_state.min_height
            if draw_state.expanded:
                if min_width is not None and size[0] < min_width:
                    size = (snap_int(min_width), size[1])
                if min_height is not None and size[1] < min_height:
                    size = (size[0], snap_int(min_height))

            self._sizes[rkey] = size

        self.key_to_parent_key[rkey] = parent_ctx.key if parent_ctx else None
        self.key_to_draw_state[rkey] = draw_state
        parent_key = parent_ctx.key if parent_ctx else None
        if parent_key is not None:
            if parent_key not in self.parent_key_to_child_keys:
                self.parent_key_to_child_keys[parent_key] = {}
            top = draw_state.top if draw_state.top is not None else 0
            self.parent_key_to_child_keys[parent_key][rkey] = (top, rkey, draw_state)

        if name is not None:
            name_key = f"{id(collection)}.{name}"
            self.py_id_to_keys.setdefault(name_key, set()).add(rkey)

        if isinstance(input_value, (list, dict, set, deque, MutableMapping)) or hasattr(input_value, "__dict__"):
            self.py_id_to_keys.setdefault(f"{id(input_value)}", set()).add(rkey)

        if f"{id(draw_state)}" not in self.py_id_to_keys:
            self.py_id_to_keys[f"{id(draw_state)}"] = set()

        self.py_id_to_keys[f"{id(draw_state)}"].add(rkey)

        imgui.push_id(f"{rkey}{layer}_offscreen")
        imgui.begin_group()
        has_area = size is not None and size[0] != 0 and size[1] != 0

        if size is not None and self.enabled and draw_state.frame_count >= 2:
            t = self._tiles.get(rkey)
            use_image = t and has_area and (t.size == (size[0], size[1])) and (not self._is_dirty(t))

            if use_image:
                imgui.set_cursor_screen_pos((draw_state.left, draw_state.top))
                imgui.image(
                    t.tex,
                    snap_int(size[0]),
                    snap_int(size[1]),
                    uv0=(0.0, 1.0),
                    uv1=(1.0, 0.0),
                )
                imgui.set_item_allow_overlap()
                self._stack.append(
                    _Ctx(
                        draw_state=draw_state,
                        key=rkey,
                        pos=(x, y),
                        size=size,
                        layer=layer,
                        depth_and_layer=draw_state.shadow_depth,
                        drew_cached=True,
                        auto_resize=draw_state.auto_resize,
                    )
                )
                return False

        self._stack.append(
            _Ctx(
                draw_state=draw_state,
                key=rkey,
                pos=(x, y),
                size=size,
                layer=layer,
                depth_and_layer=draw_state.shadow_depth,
                drew_cached=False,
                auto_resize=draw_state.auto_resize,
            )
        )

        return True

    def mark_end_offscreen(self, draw_state=None) -> None:
        if not self.enabled:
            return

        if draw_state is not None and not draw_state.use_cache:
            return

        ctx = self._stack.pop()
        imgui.pop_id()

        imgui.end_group()
        Melty.tile_id_stack.pop()

        minx, miny = ctx.draw_state.left, ctx.draw_state.top

        ctx.pos = (float(minx), float(miny))
        ctx.size = (ctx.draw_state.width, ctx.draw_state.height)

        min_width = ctx.draw_state.min_width
        min_height = ctx.draw_state.min_height
        if min_width is not None and ctx.size[0] < min_width:
            ctx.size = (snap_int(min_width), ctx.size[1])
        if min_height is not None and ctx.size[1] < min_height:
            ctx.size = (ctx.size[0], snap_int(min_height))

        x, y = ctx.pos
        w, h = ctx.size
        clip = self._get_current_clip_rect_screen()
        clipped = self._clip_rect(x, y, w, h, clip)
        self._key_to_ctx[ctx.key] = ctx

        corner_radius = getattr(ctx.draw_state, "corner_radius", 6.0) or 6.0

        if ctx.size:
            if clipped:
                cx, cy, cw, ch = clipped
                if cw > 0 and ch > 0:
                    self.mask_mark_view(ctx.draw_state, ctx.layer, ctx.depth_and_layer, cx, cy, cw, ch, ctx.key,
                                        corner_radius)
            else:
                if w > 0 and h > 0:
                    self.mask_mark_view(ctx.draw_state, ctx.layer, ctx.depth_and_layer, x, y, w, h, ctx.key,
                                        corner_radius)

        if not self.enabled or ctx.drew_cached or ctx.draw_state.frame_count < 2:
            self._sizes[ctx.key] = ctx.size
            return

        self._sizes[ctx.key] = ctx.size
        if ctx.size and ctx.size[0] > 0 and ctx.size[1] > 0:
            t = self._tiles.get(ctx.key)
            if self._dummy_vao is None:
                vao = gl.glGenVertexArrays(1)
                if isinstance(vao, (list, tuple)):
                    vao = vao[0]
                self._dummy_vao = int(vao)

            gl.glBindVertexArray(self._dummy_vao)

            if (((t is None) or (t.size != (ctx.size[0], ctx.size[1]))) and not imgui.is_mouse_down(0)
                    and not imgui.is_mouse_down(1) and not imgui.is_mouse_down(2)):
                t = _ensure_tile(t, ctx.size[0], ctx.size[1], frame_id=self._frame_id, draw_state=ctx.draw_state)
                self.invalidate(ctx.key)
                self._tiles[ctx.key] = t

            if self._is_dirty(t) and (ctx.key not in self._enq_copy_keys):
                self._pending.append(
                    _Pending(draw_state=ctx.draw_state, tile=t, pos=ctx.pos, size=ctx.size, layer=ctx.layer,
                             depth_and_layer=ctx.depth_and_layer, key=ctx.key))
                self._enq_copy_keys.add(ctx.key)

    def \
            _ensure_programs(self):
        if self._dummy_vao is None:
            vao = gl.glGenVertexArrays(1)
            if isinstance(vao, (list, tuple)):
                vao = vao[0]
            self._dummy_vao = int(vao)

        gl.glBindVertexArray(self._dummy_vao)

        if self._prog_mask is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _MASK_FS)
            self._prog_mask = _link(vs, fs)
            self._loc_mask_uRankNorm = gl.glGetUniformLocation(self._prog_mask, "uRankNorm")

        if self._prog_mask_rounded is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _MASK_ROUNDED_FS)
            self._prog_mask_rounded = _link(vs, fs)
            self._loc_maskr_uRankNorm = gl.glGetUniformLocation(self._prog_mask_rounded, "uRankNorm")
            self._loc_maskr_uRectSize = gl.glGetUniformLocation(self._prog_mask_rounded, "uRectSize")
            self._loc_maskr_uCornerRadius = gl.glGetUniformLocation(self._prog_mask_rounded, "uCornerRadius")
            self._loc_maskr_uMargin = gl.glGetUniformLocation(self._prog_mask_rounded, "uMargin")

        if self._prog_mask_textured is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _MASK_TEXTURED_FS)
            self._prog_mask_textured = _link(vs, fs)
            self._loc_tex_uTex = gl.glGetUniformLocation(self._prog_mask_textured, "uTex")

        if self._prog_mask_textured_rounded is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _MASK_TEXTURED_ROUNDED_FS)
            self._prog_mask_textured_rounded = _link(vs, fs)
            self._loc_texr_uTex = gl.glGetUniformLocation(self._prog_mask_textured_rounded, "uTex")
            self._loc_texr_uRectSize = gl.glGetUniformLocation(self._prog_mask_textured_rounded, "uRectSize")
            self._loc_texr_uCornerRadius = gl.glGetUniformLocation(
                self._prog_mask_textured_rounded, "uCornerRadius"
            )
            self._loc_texr_uMargin = gl.glGetUniformLocation(
                self._prog_mask_textured_rounded, "uMargin"
            )

        if self._prog_mask_textured_offset is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _MASK_TEXTURED_OFFSET_FS)
            self._prog_mask_textured_offset = _link(vs, fs)
            self._loc_texoff_uTex = gl.glGetUniformLocation(self._prog_mask_textured_offset, "uTex")
            self._loc_texoff_uOffset = gl.glGetUniformLocation(self._prog_mask_textured_offset, "uOffset")

        if self._prog_mask_textured_offset_rounded is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _MASK_TEXTURED_OFFSET_ROUNDED_FS)
            self._prog_mask_textured_offset_rounded = _link(vs, fs)
            self._loc_texoffr_uTex = gl.glGetUniformLocation(self._prog_mask_textured_offset_rounded, "uTex")
            self._loc_texoffr_uOffset = gl.glGetUniformLocation(self._prog_mask_textured_offset_rounded, "uOffset")
            self._loc_texoffr_uRectSize = gl.glGetUniformLocation(
                self._prog_mask_textured_offset_rounded, "uRectSize"
            )
            self._loc_texoffr_uCornerRadius = gl.glGetUniformLocation(
                self._prog_mask_textured_offset_rounded, "uCornerRadius"
            )
            self._loc_texoffr_uMargin = gl.glGetUniformLocation(
                self._prog_mask_textured_offset_rounded, "uMargin"
            )

        if self._prog_copy is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _COPY_FS)
            self._prog_copy = _link(vs, fs)

            self._loc_uSrc = gl.glGetUniformLocation(self._prog_copy, "uSrc")
            self._loc_uTopMask = gl.glGetUniformLocation(self._prog_copy, "uTopMask")
            self._loc_uSubMask = gl.glGetUniformLocation(self._prog_copy, "uSubMask")
            self._loc_uFBSize = gl.glGetUniformLocation(self._prog_copy, "uFBSize")
            self._loc_uSrcRectPx = gl.glGetUniformLocation(self._prog_copy, "uSrcRectPx")

            self._loc_copy_uDebugScale = gl.glGetUniformLocation(self._prog_copy, "uDebugScale")
            self._loc_copy_uCopyDebugMode = gl.glGetUniformLocation(self._prog_copy, "uCopyDebugMode")
            self._loc_copy_uTint = gl.glGetUniformLocation(self._prog_copy, "uTint")

        if self._prog_blit is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _BLIT_FS)
            self._prog_blit = _link(vs, fs)

    def _copy_debug_mode_to_int(self) -> int:
        table = {
            "off": 0,
            "uv": 1,
            "srcpx": 2,
            "mask": 3,
            "layer": 4,
            "checker": 5,
            "solid": 6,
        }
        return table.get(self.copy_debug_mode.value, 0)

    def _draw_mask_rect_fresh(self, r: _Rect, dp_x, dp_y, s_x, s_y, fb_h, rank_norm: float, shadow_margin=0.0):
        """Draw a fresh mask rect (no cached texture) with optional rounded corners."""
        x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h)
        ix0, iy0 = int(floor(x0)), int(floor(y0))
        ix1, iy1 = int(ceil(x1)), int(ceil(y1))
        iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)

        if iw <= 0 or ih <= 0:
            return

        gl.glViewport(ix0, iy0, iw, ih)

        if r.corner_radius > 0:
            gl.glUseProgram(self._prog_mask_rounded)
            gl.glUniform1f(self._loc_maskr_uRankNorm, rank_norm)
            gl.glUniform2f(self._loc_maskr_uRectSize, float(iw), float(ih))
            gl.glUniform1f(self._loc_maskr_uCornerRadius, r.corner_radius)
            gl.glUniform1f(self._loc_maskr_uMargin, shadow_margin)

        else:
            gl.glUseProgram(self._prog_mask)
            gl.glUniform1f(self._loc_mask_uRankNorm, rank_norm)

        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

    def _draw_mask_rect_cached(self, tex: int, ix0: int, iy0: int, iw: int, ih: int, offset: float,
                               corner_radius: float, shadow_margin: float = 0.0):
        """Draw a cached mask texture with offset and optional rounded corners.
        Note: preserves existing behavior (rounded path effectively always used by callers).
        """
        gl.glViewport(ix0, iy0, iw, ih)

        # Preserve existing behavior: callers often pass max(5.0, corner_radius) anyway.
        gl.glUseProgram(self._prog_mask_textured_offset_rounded)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glUniform1i(self._loc_texoffr_uTex, 0)
        gl.glUniform1f(self._loc_texoffr_uOffset, offset)
        gl.glUniform2f(self._loc_texoffr_uRectSize, float(iw), float(ih))
        gl.glUniform1f(self._loc_texoffr_uCornerRadius, corner_radius)
        gl.glUniform1f(self._loc_maskr_uMargin, shadow_margin)

        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

    def _draw_mask_rect(self, r: _Rect, dp_x, dp_y, s_x, s_y, fb_h, use_cached: bool, shadow_margin=0.0):
        """Helper to draw a single mask rect, optionally using cached subtree mask."""
        x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h)
        ix0, iy0 = int(snap_int(x0)), int(snap_int(y0))
        ix1, iy1 = int(snap_int(x1)), int(snap_int(y1))
        iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)

        if iw <= 0 or ih <= 0:
            return

        gl.glViewport(ix0, iy0, iw, ih)

        t = self._tiles.get(r.key)
        can_use_cached = use_cached and (t is not None) and (not self._is_dirty(t)) and (t.mask_tex is not None)

        if can_use_cached:
            gl.glUseProgram(self._prog_mask_textured_rounded)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, t.mask_tex)
            gl.glUniform1i(self._loc_texr_uTex, 0)
            gl.glUniform2f(self._loc_texr_uRectSize, float(iw), float(ih))
            gl.glUniform1f(self._loc_texr_uCornerRadius, r.corner_radius)
            gl.glUniform1f(self._loc_texr_uMargin, shadow_margin)

        else:
            gl.glUseProgram(self._prog_mask_rounded)
            gl.glUniform1f(self._loc_maskr_uRankNorm, float(r.layer) * INV_65535)
            gl.glUniform2f(self._loc_maskr_uRectSize, float(iw), float(ih))
            gl.glUniform1f(self._loc_maskr_uCornerRadius, r.corner_radius)
            gl.glUniform1f(self._loc_maskr_uMargin, shadow_margin)

        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

    def finalize_captures(self, framebuffer_size: Tuple[int, int], global_toggles=None) -> None:
        self.all_keys = set()
        if self._snapshot_fbo is None:
            return

        if not self._pending and not self._mask_rects:
            return

        # Grab direct references (we clear at end anyway)
        local_mask_rects = self._mask_rects
        # Reversed
        local_mask_rects_rev = list(reversed(local_mask_rects))
        local_pending = self._pending
        local_pending_rev = list(reversed(local_pending))

        if not local_pending and not local_mask_rects:
            self._pending.clear()
            self._mask_rects.clear()
            self._enq_mask_keys.clear()
            self._enq_copy_keys.clear()
            return

        dp_x, dp_y, s_x, s_y, dd_fb_w, dd_fb_h = self._get_draw_xform()
        fb_w, fb_h = self._fb_size

        if (fb_w != dd_fb_w) or (fb_h != dd_fb_h):
            self.mask_begin_frame((dd_fb_w, dd_fb_h))
            fb_w, fb_h = self._fb_size

        # Precompute subtree rect lists once (key -> rects in its subtree, draw order)
        parent_of = self.key_to_parent_key
        subtree_rects_by_root = defaultdict(list)
        for r in local_mask_rects:
            k = r.key

            while k is not None:
                subtree_rects_by_root[k].append(r)
                k = parent_of.get(k)

        # Reverse
        subtree_rects_by_root = {k: list(reversed(v)) for k, v in subtree_rects_by_root.items()}

        st = _GLState()
        try:
            self._ensure_programs()
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            gl.glBindVertexArray(self._dummy_vao)

            # ================================================================
            # PASS 1: Snapshot the current framebuffer
            # ================================================================
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, 0)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self._snapshot_fbo)
            gl.glBlitFramebuffer(
                0, 0, dd_fb_w, dd_fb_h, 0, 0, dd_fb_w, dd_fb_h, gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST
            )

            # ================================================================
            # PASS 2: Build _mask_tex (flat, fresh geometry only)
            # ================================================================
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._mask_fbo)
            gl.glViewport(0, 0, fb_w, fb_h)
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
            gl.glClearColor(0, 0, 0, 0.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)

            gl.glEnable(gl.GL_BLEND)
            gl.glBlendEquation(gl.GL_MAX)
            gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)

            for r in local_mask_rects_rev:
                if r.blend_max:
                    continue
                self._draw_mask_rect(r, dp_x, dp_y, s_x, s_y, fb_h, use_cached=False)

            gl.glViewport(0, 0, fb_w, fb_h)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)

            # ================================================================
            # PASS 3: Process dirty tiles - copy pixels using the mask
            # ================================================================
            gl.glUseProgram(self._prog_copy)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.snapshot_tex)
            gl.glUniform1i(self._loc_uSrc, 0)

            gl.glActiveTexture(gl.GL_TEXTURE1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._mask_tex)
            gl.glUniform1i(self._loc_uTopMask, 1)

            gl.glUniform2f(self._loc_uFBSize, float(fb_w), float(fb_h))
            gl.glUniform1f(self._loc_copy_uDebugScale, float(self.offscreen_scale))
            gl.glUniform1i(self._loc_copy_uCopyDebugMode, self._copy_debug_mode_to_int())

            for p in local_pending:
                x, y = p.pos
                w, h = p.size
                x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(x, y, w, h, dp_x, dp_y, s_x, s_y, fb_h)

                # Build _sub_mask_tex with fresh geometry only (scissor to tile rect)
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._sub_mask_fbo)
                gl.glViewport(0, 0, fb_w, fb_h)

                sc_x0, sc_y0 = int(floor(x0)), int(floor(y0))
                sc_x1, sc_y1 = int(ceil(x1)), int(ceil(y1))
                sc_w, sc_h = max(0, sc_x1 - sc_x0), max(0, sc_y1 - sc_y0)

                gl.glEnable(gl.GL_SCISSOR_TEST)
                gl.glScissor(sc_x0, sc_y0, sc_w, sc_h)

                gl.glDisable(gl.GL_BLEND)
                gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
                gl.glClearColor(0, 0, 0, 0.0)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)

                # gl.glEnable(gl.GL_BLEND)
                # gl.glBlendEquation(gl.GL_MAX)
                # gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)

                for r in subtree_rects_by_root.get(p.key, ()):
                    if r.blend_max:
                        continue
                    self._draw_mask_rect_fresh(r, dp_x, dp_y, s_x, s_y, fb_h, float(r.layer) * INV_65535,
                                               shadow_margin=0.0)

                # Copy pixels to tile
                gl.glUseProgram(self._prog_copy)
                gl.glActiveTexture(gl.GL_TEXTURE2)
                gl.glBindTexture(gl.GL_TEXTURE_2D, self._sub_mask_tex)
                gl.glUniform1i(self._loc_uSubMask, 2)

                gl.glDisable(gl.GL_BLEND)
                gl.glBlendEquation(gl.GL_FUNC_ADD)
                gl.glBlendFunc(gl.GL_ONE, gl.GL_ZERO)
                gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)

                gl.glDisable(gl.GL_SCISSOR_TEST)

                if p.tile is not None and p.tile.fbo != -1:
                    try:
                        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, p.tile.fbo)
                        gl.glViewport(0, 0, snap_int(p.tile.size[0]), snap_int(p.tile.size[1]))

                        if global_toggles is not None and getattr(global_toggles, "offscreen_debug", False):
                            gl.glUniform4f(self._loc_copy_uTint, *self.frame_tint)
                        else:
                            gl.glUniform4f(self._loc_copy_uTint, 1.0, 1.0, 1.0, 1.0)

                        gl.glUniform4f(self._loc_uSrcRectPx, float(x0), float(y0), float(x1), float(y1))
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

                        p.tile.last_clean_frame = self._frame_id
                        p.tile.dirty = self._is_dirty(p.tile)
                    except Exception as e:
                        print(
                            f"Error copying to tile {p.key}: {e} {p.tile.draw_state.to_dict()} input_value={p.tile.draw_state._input_value}")

            # ================================================================
            # PASS 4: Build tile.mask_tex for each dirty tile (full subtree)
            # ================================================================
            background_depth = 0.001
            for p in local_pending:
                x, y = p.pos
                w, h = p.size
                x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(x, y, w, h, dp_x, dp_y, s_x, s_y, fb_h)

                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._full_sub_mask_fbo)
                gl.glViewport(0, 0, fb_w, fb_h)

                sc_x0, sc_y0 = int(floor(x0)), int(floor(y0))
                sc_x1, sc_y1 = int(ceil(x1)), int(ceil(y1))
                sc_w, sc_h = max(0, sc_x1 - sc_x0), max(0, sc_y1 - sc_y0)

                gl.glEnable(gl.GL_SCISSOR_TEST)
                gl.glScissor(sc_x0, sc_y0, sc_w, sc_h)

                gl.glDisable(gl.GL_BLEND)
                gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
                gl.glClearColor(background_depth, 0, 0, 0.0)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)

                # gl.glEnable(gl.GL_BLEND)
                # gl.glBlendEquation(gl.GL_MAX)
                # gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)

                last_depth_and_layer = -1

                for r in subtree_rects_by_root.get(p.key, ()):
                    # if r.blend_max:
                    #     continue

                    draw_state = self.key_to_draw_state.get(r.key)
                    size_change = draw_state.size_change if draw_state else False
                    t_child = self._tiles.get(r.key)
                    is_self = (r.key == p.key)

                    use_child_cache = (
                            (not is_self)
                            and (t_child is not None)
                            and (t_child.mask_tex is not None)
                            and (not size_change)
                    )

                    # if r.blend_alpha and use_child_cache:
                    #     gl.glEnable(gl.GL_BLEND)
                    #     gl.glBlendEquation(gl.GL_MAX)
                    #     gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
                    # else:
                    #     gl.glDisable(gl.GL_BLEND)
                    # For cached tiles, use actual tile size from context to avoid stretching
                    if use_child_cache:
                        child_ctx = self._key_to_ctx.get(r.key)
                        if child_ctx and child_ctx.size:
                            cx, cy = child_ctx.pos
                            cw, ch = draw_state.width, draw_state.height
                            sx0, sy0, sx1, sy1 = self._screen_rect_to_fb_xyxy(cx, cy, cw, ch, dp_x, dp_y, s_x, s_y,
                                                                              fb_h)
                        else:
                            sx0, sy0, sx1, sy1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y,
                                                                              fb_h)
                    else:
                        child_ctx = self._key_to_ctx.get(r.key)
                        if child_ctx and child_ctx.size:
                            cx, cy = child_ctx.pos
                            cw, ch = draw_state.width, draw_state.height
                            sx0, sy0, sx1, sy1 = self._screen_rect_to_fb_xyxy(cx, cy, cw, ch, dp_x, dp_y, s_x, s_y,
                                                                              fb_h)
                        else:
                            sx0, sy0, sx1, sy1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y,
                                                                              fb_h)

                    ix0, iy0 = int(floor(sx0)), int(floor(sy0))
                    ix1, iy1 = int(ceil(sx1)), int(ceil(sy1))
                    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)

                    if iw <= 0 or ih <= 0:
                        continue

                    depth_and_layer = r.depth_and_layer

                    ix0, iy0 = ix0, iy0
                    ix1, iy1 = ix1, iy1
                    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)

                    if use_child_cache:
                        offset = (float(depth_and_layer) - float(t_child.mask_layer)) * float(INV_65535)
                        self._draw_mask_rect_cached(
                            t_child.mask_tex,
                            ix0,
                            iy0,
                            iw,
                            ih,
                            offset,
                            r.corner_radius,
                            r.draw_state.shadow_margin if r.draw_state is not None else 0.0
                        )
                    else:
                        rank_norm = float(depth_and_layer) / 65535.5

                        gl.glViewport(ix0, iy0, iw, ih)
                        if r.corner_radius > 0:
                            gl.glUseProgram(self._prog_mask_rounded)
                            gl.glUniform1f(self._loc_maskr_uRankNorm, rank_norm)
                            gl.glUniform2f(self._loc_maskr_uRectSize, float(iw), float(ih))
                            gl.glUniform1f(self._loc_maskr_uCornerRadius, r.corner_radius)
                            shadow_margin = r.draw_state.shadow_margin if r.draw_state is not None else 0.0
                            gl.glUniform1f(self._loc_maskr_uMargin, shadow_margin)

                        else:
                            gl.glUseProgram(self._prog_mask)
                            gl.glUniform1f(self._loc_mask_uRankNorm, rank_norm)
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

                gl.glDisable(gl.GL_SCISSOR_TEST)

                # Save _full_sub_mask_tex to tile's mask_tex and remember the layer
                if p.tile is not None and p.tile.mask_tex is not None:
                    p.tile.mask_layer = p.depth_and_layer

                    gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self._scratch_fbo)
                    gl.glFramebufferTexture2D(
                        gl.GL_FRAMEBUFFER,
                        gl.GL_COLOR_ATTACHMENT0,
                        gl.GL_TEXTURE_2D,
                        p.tile.mask_tex,
                        0,
                    )
                    gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])

                    gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self._full_sub_mask_fbo)

                    gl.glBlitFramebuffer(
                        int(x0),
                        int(y0),
                        int(x1),
                        int(y1),
                        0,
                        0,
                        int(p.tile.size[0]),
                        int(p.tile.size[1]),
                        gl.GL_COLOR_BUFFER_BIT,
                        gl.GL_NEAREST,
                    )

            # ================================================================
            # PASS 5: Build _full_mask_tex using cached subtree masks
            # ================================================================
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._full_mask_fbo)
            gl.glViewport(0, 0, fb_w, fb_h)
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glClearColor(background_depth, 0, 0, 0.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)

            gl.glDisable(gl.GL_BLEND)
            for key in subtree_rects_by_root.keys():
                for r in subtree_rects_by_root.get(key, ()):
                    draw_state = self.key_to_draw_state.get(r.key)

                    t = self._tiles.get(r.key)
                    size_change = draw_state.size_change if draw_state else False

                    can_use_cached = (t is not None) and (t.mask_tex is not None) and (not size_change)

                    clip_x0, clip_y0, clip_x1, clip_y1 = self._screen_rect_to_fb_xyxy(
                        r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h
                    )
                    clip_ix0, clip_iy0 = int(floor(clip_x0)), int(floor(clip_y0))
                    clip_ix1, clip_iy1 = int(ceil(clip_x1)), int(ceil(clip_y1))
                    clip_iw, clip_ih = max(0, clip_ix1 - clip_ix0), max(0, clip_iy1 - clip_iy0)

                    tile_ctx = self._key_to_ctx.get(r.key)
                    # gl.glEnable(gl.GL_BLEND)
                    # gl.glBlendEquation(gl.GL_MAX)
                    # gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)

                    # if size_change:
                    #     gl.glEnable(gl.GL_BLEND)
                    #     gl.glBlendEquation(gl.GL_MAX)
                    #     gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
                    # else:
                    gl.glDisable(gl.GL_BLEND)

                    # gl.glDisable(gl.GL_BLEND)

                    # gl.glDisable(gl.GL_BLEND)

                    # if r.blend_max:
                    #     gl.glEnable(gl.GL_BLEND)
                    #     gl.glBlendEquation(gl.GL_MAX)
                    #     gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
                    # else:
                    #     gl.glDisable(gl.GL_BLEND)

                    if (can_use_cached or size_change) and tile_ctx and not draw_state is None:
                        tx, ty = draw_state.left, draw_state.top
                        tw, th = draw_state.width, draw_state.height
                        x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(tx, ty, tw, th, dp_x, dp_y, s_x, s_y, fb_h)
                    else:
                        x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h)

                    ix0, iy0 = int(floor(x0)), int(floor(y0))
                    ix1, iy1 = int(ceil(x1)), int(ceil(y1))
                    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)

                    # if r.draw_state.depth_offset < -1:
                    #     ix0, iy0 = ix0 + 2, iy0 + 2
                    #     ix1, iy1 = ix1 - 2, iy1 - 2
                    #     iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)

                    if r.w <= 0 or r.h <= 0 or iw <= 0 or ih <= 0 or clip_iw <= 0 or clip_ih <= 0:
                        continue

                    gl.glEnable(gl.GL_SCISSOR_TEST)
                    gl.glScissor(clip_ix0, clip_iy0, clip_iw, clip_ih)
                    gl.glViewport(ix0, iy0, iw, ih)

                    depth_and_layer = r.depth_and_layer

                    if can_use_cached:
                        offset = (float(depth_and_layer) - float(t.mask_layer)) * float(INV_65535)

                        shadow_margin = r.draw_state.shadow_margin if r.draw_state is not None else 0.0

                        self._draw_mask_rect_cached(t.mask_tex, ix0, iy0, iw, ih, offset,
                                                    r.corner_radius, shadow_margin)
                    else:
                        rank_norm = float(depth_and_layer) / 65535.5

                        gl.glViewport(clip_ix0, clip_iy0, clip_iw, clip_ih)

                        cr = r.corner_radius
                        if cr > 0:
                            gl.glUseProgram(self._prog_mask_rounded)
                            gl.glUniform1f(self._loc_maskr_uRankNorm, rank_norm)
                            gl.glUniform2f(self._loc_maskr_uRectSize, float(clip_iw), float(clip_ih))
                            gl.glUniform1f(self._loc_maskr_uCornerRadius, cr)
                            shadow_margin = r.draw_state.shadow_margin if r.draw_state is not None else 0.0
                            gl.glUniform1f(self._loc_maskr_uMargin, shadow_margin)

                        else:
                            gl.glUseProgram(self._prog_mask)
                            gl.glUniform1f(self._loc_mask_uRankNorm, rank_norm)

                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

            gl.glDisable(gl.GL_SCISSOR_TEST)

            gl.glViewport(0, 0, fb_w, fb_h)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glUseProgram(0)

        finally:
            st.restore()
            self._pending.clear()
            self._mask_rects.clear()
            self._shadow_rects.clear()
            self._enq_mask_keys.clear()
            self._enq_copy_keys.clear()
            self._cancelled_keys.clear()
            self._recording = False
            self.apply_invalid()
            self.did_deviate.clear()
            self.seen_ids.clear()
