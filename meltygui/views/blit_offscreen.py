# offscreen_tiles_masked.py
from __future__ import annotations

import random
from collections import deque
from copy import copy
from dataclasses import dataclass
from math import ceil, floor
from typing import Dict, List, Optional, Tuple, MutableMapping
from OpenGL import GL as gl
import imgui

from src.lsd.gl_gui.model.core_model.core_enums import OffscreenDebugMode
from src.lsd.gl_gui.utils.glfw_utils import request_render

"""
Per-view tile caching with a post-frame mask (no ImGui draw-list replay).

Key features:
  - Per-pixel 'rank' (depth + submission order) mask via GL_MAX so the *actual*
    topmost producer wins even for overlapping siblings.
  - Subtree-aware copy: parent tiles include child regions correctly.
  - Versioned invalidation: mid-frame invalidate() takes effect next frame.
  - Only enqueue copy when tile is actually DIRTY (version-based).
  - Occlusion query: mark clean only if fragments actually copied (robust across PyOpenGL types).
  - Always record mask rects (even when showing cached) so subtree masks are complete.
  - Separate dedup sets for mask vs copy to avoid cross-suppression.
  - Visual debug modes (uv/srcpx/mask(rank=top)/layer(subtree)/checker/solid) + optional overlays.
"""


# ==============================
# Small structs
# ==============================
@dataclass
class _Tile:
    fbo: int
    tex: int
    rbo: Optional[int]
    size: Tuple[int, int]
    dirty: bool = True
    last_clean_frame: int = -1
    last_invalidated_frame: int = 0
    force_invalidate: bool = False


@dataclass
class _Ctx:
    draw_state: any
    key: str
    pos: Tuple[float, float]  # ImGui logical top-left (updated at end)
    size: Optional[Tuple[int, int]]
    layer: int
    drew_cached: bool
    auto_resize: bool


@dataclass
class _Pending:
    tile: _Tile
    pos: Tuple[float, float]
    size: Tuple[int, int]
    layer: int
    key: str  # key this pending copy belongs to


@dataclass
class _Rect:
    # stored in ImGui logical coords; converted at render time
    layer: int
    x: float
    y: float
    w: float
    h: float
    key: str  # key this rect belongs to
    order: int  # per-frame submission order (0..255), used for tiebreaker among siblings


# ==============================
# GL helpers
# ==============================
def _create_color_tex(w: int, h: int, internal_format=gl.GL_RGB8) -> int:
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal_format, w, h, 0, gl.GL_RGB, gl.GL_UNSIGNED_BYTE, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return tex


def _create_mask_tex(w: int, h: int) -> int:
    """
    Single-channel 16-bit normalized (0..65535) to store a 'rank' value.
    rank = (depth << 8) | order; GL_MAX blending selects the topmost producer.
    """
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R8, w, h, 0, gl.GL_RED, gl.GL_UNSIGNED_SHORT, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return tex


def snap_int(v: float) -> int:
    return int(v)

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


def _ensure_tile(existing: Optional[_Tile], w: int, h: int, frame_id: int = 0, tile_id=None) -> Optional[_Tile]:
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
            from src.lsd.gl_gui.melty import Melty
            bg_color = Melty.bg_color_stack[-1] if len(Melty.bg_color_stack) > 0 else (0, 0, 0, 1)

            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, existing.fbo)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, new_fbo)
            gl.glClearColor(*bg_color[:3], 1.0)  # BG=0
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)

            gl.glBlitFramebuffer(0, 0, snap_int(existing.size[0]), snap_int(existing.size[1]), 0, 0,
                                 snap_int(w), snap_int(h), gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
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
            from src.lsd.gl_gui.melty import Melty
            bg_color = Melty.bg_color_stack[-1] if len(Melty.bg_color_stack) > 0 else (0, 0, 0, 1)
            # Darkened background for offscreen tiles
            gl.glClearColor(*bg_color[:3], 1.0)  # BG=0
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        finally:
            st.restore()

    t = _Tile(fbo=new_fbo, tex=new_tex, rbo=new_rbo, size=(w, h), dirty=True)

    t.last_invalidated_frame = frame_id  # requires a copy to become clean
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
        gl.glBlendFuncSeparate(self.blend_src_rgb, self.blend_dst_rgb, self.blend_src_a, self.blend_dst_a)
        gl.glColorMask(*self.color_mask)


# ---------- Occlusion query helpers (robust to PyOpenGL return types) ----------
def _normalize_gl_id(x):
    if isinstance(x, (list, tuple)):
        x = x[0]
    try:
        return int(x)
    except Exception:
        return int(getattr(x, "value", x))


def _begin_occlusion_query():
    try:
        qid = gl.glGenQueries(1)
        qid = _normalize_gl_id(qid)
        gl.glBeginQuery(gl.GL_SAMPLES_PASSED, qid)
        return qid
    except Exception:
        return None


def _end_occlusion_query(qid):
    try:
        gl.glEndQuery(gl.GL_SAMPLES_PASSED)
        passed = gl.glGetQueryObjectuiv(qid, gl.GL_QUERY_RESULT)
        try:
            passed = int(passed)
        except Exception:
            passed = int(getattr(passed, "value", passed))
        gl.glDeleteQueries(1, [qid])
        return passed
    except Exception:
        try:
            gl.glDeleteQueries(1, [qid])
        except Exception:
            pass
        return None


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

# Writes a solid RED = rank/65535 into the mask FBO.
_MASK_FS = """
#version 330 core
uniform float uRankNorm; // rank/65535 in [0,1]
out vec4 oColor;
void main(){
  oColor = vec4(uRankNorm, 0.0, 0.0, 1.0);
}
"""

# Copy/Debug shader (uses global top mask + per-tile subtree mask)
_COPY_FS = """
#version 330 core
in vec2 vUV;

uniform int   uCopyDebugMode; // 0=off,1=uv,2=srcpx,3=mask(top),4=layer(subtree),5=checker,6=solid
uniform float uDebugScale;
uniform vec4  uTint;

uniform sampler2D uSrc;       // snapshot (final composited frame)
uniform sampler2D uTopMask;   // global top rank mask (R16, NEAREST)
uniform sampler2D uSubMask;   // subtree-max rank mask (R16, NEAREST)
uniform vec2  uFBSize;
uniform vec4  uSrcRectPx;     // x0,y0,x1,y1

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

  // --- Debug Modes ---
  if (uCopyDebugMode == 1) {                // UV gradient
    oColor = vec4(uv, 0.0, 1.0) * uTint; return;
  } else if (uCopyDebugMode == 2) {         // srcPx normalized
    vec2 sp = clamp(srcPx / max(uFBSize, vec2(1.0)), 0.0, 1.0);
    oColor = vec4(sp, 0.0, 1.0) * uTint; return;
  } else if (uCopyDebugMode == 3) {         // global top mask (grayscale, shows rank)
    float g = float(topRank) / uDebugScale;
    oColor = vec4(g, g, g, 1.0) * uTint; return;
  } else if (uCopyDebugMode == 4) {         // subtree mask (grayscale, shows rank)
    float g = float(subRank) / uDebugScale;
    oColor = vec4(g, g, g, 1.0) * uTint; return;
  } else if (uCopyDebugMode == 5) {         // checker
    float c = checker(uv);
    oColor = vec4(vec3(c), 1.0) * uTint; return;
  } else if (uCopyDebugMode == 6) {         // solid
    oColor = uTint; return;
  }

  // --- Normal Copy ---
  // Copy only where this view's subtree provides the topmost pixel.
  if (subRank > 0 && topRank == subRank) {
      oColor = texture(uSrc, uv) * uTint;
  } else {
      discard;
  }
}
"""

# Simple blit FS to draw a texture to the default framebuffer (debug overlays)
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

        # Resting to previous value should cancel invalidate
        self.initial_value = {}
        self.did_deviate = {}
        self.enabled: bool = False

        # Layer constants:
        self._LAYER_BG = 0  # reserved background in mask
        self._LAYER_MIN = 1  # first valid layer for views
        self._LAYER_MAX = 254  # leave 255 free if needed (rank packs layer << 8 | rect)

        self.offscreen_debug_mode: OffscreenDebugMode = OffscreenDebugMode.OFF
        self.offscreen_scale = 200.0

        # Copy debug toggles
        self.copy_debug_mode = OffscreenDebugMode.OFF  # "off","uv","srcpx","mask","layer","checker","solid"
        self.debug_overlay_mask_to_screen: bool = False
        self.debug_overlay_src_to_screen: bool = False

        random_float = random.Random().random
        self.frame_tint = (0.5 + 0.5 * random_float(),
                           0.5 + 0.5 * random_float(),
                           0.5 + 0.5 * random_float(), 1.0)

        # Lookup dicts for bubbling (key chains)
        self.py_id_to_keys: Dict[str, set] = {}
        self.key_to_parent_key: Dict[str, str] = {}
        self.parent_key_to_child_keys: Dict[str, set] = {}
        self.key_to_draw_state: Dict[str, any] = {}

        self._tiles: Dict[str, _Tile] = {}
        self._sizes = {}  # resolved key -> (w,h)
        self._stack: List[_Ctx] = []
        self._key_to_ctx: Dict[str, _Ctx] = {}
        self._pending: dict[str, _Pending] = {}
        self.all_keys = set()

        # mask/snapshot
        self._fb_size: Tuple[int, int] = (0, 0)
        self._mask_tex: Optional[int] = None
        self._mask_fbo: Optional[int] = None
        self._sub_mask_tex: Optional[int] = None
        self._sub_mask_fbo: Optional[int] = None
        self._snapshot_tex: Optional[int] = None
        self._snapshot_fbo: Optional[int] = None
        self._mask_rects: List[_Rect] = []

        # per-frame rect index (0..255 wraps)
        self._rect_seq: int = 0

        # programs and uniform locations
        self._prog_mask: Optional[int] = None
        self._prog_copy: Optional[int] = None
        self._prog_blit: Optional[int] = None
        self._loc_uSrc = None
        self._loc_uTopMask = None
        self._loc_uSubMask = None
        self._loc_uFBSize = None
        self._loc_uSrcRectPx = None

        # Frame-atomic bookkeeping
        self._recording: bool = False
        self._cancelled_keys: set[str] = set()  # unused normally w/ version control
        self._enq_mask_keys: set[str] = set()
        self._enq_copy_keys: set[str] = set()
        self._frame_id: int = 0

        self.pending_invalid = []

    # ----- Helpers -----
    def _is_dirty(self, t: Optional[_Tile]) -> bool:
        if t is None:
            return True
        return t.last_clean_frame < t.last_invalidated_frame

    # ----- Public toggles / lifecycle -----
    def set_enabled(self, on: bool) -> None:
        if on and not self.enabled:
            for t in self._tiles.values():
                if t is not None:
                    t.dirty = True
                    t.last_invalidated_frame = max(t.last_invalidated_frame, self._frame_id)
            request_render()
        self.enabled = on

    # Deprecated; kept so external code won't crash.
    def set_top_is_low(self, v: bool) -> None:
        try:
            print("[TileCacheMasked] set_top_is_low() is deprecated; top is always high (GL_MAX).")
        except Exception:
            pass

    # ----- Key helpers -----
    def _resolve_key(self, key: str) -> str:
        return key
        # if not self._stack:
        #     return key
        # return f"{self._stack[-1].key}>{key}"

    def invalidate_current(self, force=False):
        if len(self._stack) == 0:
            return

        self.invalidate(self._stack[-1].key, force=force)

    def invalidate_up_current(self, max_depth=9, force=False):
        if len(self._stack) == 0:
            return

        self.invalidate_up(self._stack[-1].key, max_depth=max_depth, force=force)
    # def invalidate_parent(self, obj):
    #     # if name is not None:
    #     #     keys = self.py_id_to_keys.get(f"{id(obj)}.{name}", None)
    #     #     if keys is not None:
    #     #         for k in keys:
    #     #             self.invalidate_up(k)
    #     # else:
    #     parent_key = self.key_to_parent_key.get(self._stack[-1].key, None)
    #     self.invalidate_up(parent_key, max_depth=2)

    def invalidate_up_by_obj(self, obj, name=None, max_depth=9):

        if name is not None:
            keys = self.py_id_to_keys.get(f"{id(obj)}.{name}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate_up(k, max_depth=max_depth)
        else:
            keys = self.py_id_to_keys.get(f"{id(obj)}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate_up(k, max_depth=max_depth)

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
                t.dirty = self._is_dirty(t)
        self.pending_invalid.clear()

    def get_parent_keys(self, key):
        all_keys = [key]
        parent_key = self.key_to_parent_key.get(key, None)
        if parent_key and parent_key != key:
            all_keys.extend(self.get_parent_keys(parent_key))
        return all_keys

    def get_child_keys(self, key, depth=0, max_depth=9):

        if depth >= max_depth:
            return set()

        # draw_state = self.key_to_draw_state.get(key, None)
        # if draw_state is not None and not draw_state.clipped:
        #     return set()
        child_keys = self.parent_key_to_child_keys.get(key, set())
        all_keys = set(child_keys)
        for ck in child_keys:
            all_keys.update(self.get_child_keys(ck, depth + 1, max_depth=max_depth))
        return all_keys

    # More expensive, redraws all children
    def invalidate_up(self, k: str, max_depth=9, force=False) -> None:
        if k not in self._tiles:
            k = self.key_to_parent_key.get(k, None)
        from src.lsd.gl_gui.melty import Melty
        self.invalidate(k, force=force)

        # Defer parent invalidation to next frame as well
        child_keys = self.get_child_keys(k, max_depth=max_depth)
        for child in child_keys:
            if child and child != k:
                pt = self._tiles.get(child)
                if pt is not None:
                    pt.last_invalidated_frame = max(pt.last_invalidated_frame, self._frame_id + 1)
                    pt.dirty = self._is_dirty(pt)
                    self.pending_invalid.append(pt)

    def get_hash(self, draw_state):
        from src.lsd.gl_gui.model.dict_conversion import DictConversion

        if hasattr(draw_state._input_value, "hash") or isinstance(draw_state._input_value, (dict, list, set, DictConversion,
                                                                                            tuple, int, float, str, bool, type(None))):
            input_val_hash = DictConversion.compute_hash(draw_state._input_value, exclude=draw_state.__excluded_attrs__, include_hidden=False)
        else:
            input_val_hash = 0

        return (DictConversion.compute_hash(draw_state,
                                            exclude=draw_state.__excluded_attrs__, include_hidden=False),
                input_val_hash)

    def invalidate(self, key: str, force=False) -> None:
        keys_to_touch = [self._resolve_key(key)]

        for k in keys_to_touch:
            t = self._tiles.get(k)
            if t is not None:
                target_frame = self._frame_id + 1
                t.last_invalidated_frame = max(t.last_invalidated_frame, target_frame)
                t.dirty = self._is_dirty(t)
                draw_state = self.key_to_draw_state.get(k, None)
                input_val_hash = self.get_hash(draw_state)
                # if k in self.initial_value and self.initial_value[k] != input_val_hash:
                #     self.did_deviate[k] = True

                if force:
                    t.force_invalidate = force

                if k in self.initial_value and self.initial_value[k] == input_val_hash:
                    if not t.force_invalidate:
                        # Resetting initial value cancels invalidate
                        t.last_invalidated_frame = t.last_clean_frame
                        t.dirty = False
                else:
                    self.pending_invalid.append(t)
                            # Defer parent invalidation to next frame as well

            # from src.lsd.gl_gui.melty import Melty
            parent_keys = self.get_parent_keys(k)
            for parent in parent_keys:
                if parent and parent != k:
                    pt = self._tiles.get(parent)

                    if pt is not None:
                        pt.force_invalidate = True
                        pt.last_invalidated_frame = max(pt.last_invalidated_frame, self._frame_id + 1)
                        pt.dirty = self._is_dirty(pt)
                        self.pending_invalid.append(pt)

                # Optional pre-cancel for this frame (rarely used):
                # if self._recording:
                #     self._cancelled_keys.add(k)

    def invalidate_all(self) -> None:
        # Defer everything to next frame
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
            if t.rbo is not None:
                gl.glDeleteRenderbuffers(1, [t.rbo])
        self._tiles.clear()

        if self._mask_fbo:
            gl.glDeleteFramebuffers(1, [self._mask_fbo]);
            self._mask_fbo = None
        if self._mask_tex:
            gl.glDeleteTextures(1, [self._mask_tex]);
            self._mask_tex = None
        if self._sub_mask_fbo:
            gl.glDeleteFramebuffers(1, [self._sub_mask_fbo]);
            self._sub_mask_fbo = None
        if self._sub_mask_tex:
            gl.glDeleteTextures(1, [self._sub_mask_tex]);
            self._sub_mask_tex = None
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
        if self._prog_blit:
            gl.glDeleteProgram(self._prog_blit);
            self._prog_blit = None

    # ----- Mask API (per-view rectangles) -----
    def mask_begin_frame(self, framebuffer_size: Tuple[int, int]) -> None:
        fb_w, fb_h = map(int, framebuffer_size)

        # New frame
        self._frame_id += 1
        self._recording = True
        self._cancelled_keys.clear()
        self._enq_mask_keys.clear()
        self._enq_copy_keys.clear()

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
            if self._sub_mask_tex:
                gl.glDeleteTextures(1, [self._sub_mask_tex]);
                self._sub_mask_tex = None
            if self._sub_mask_fbo:
                gl.glDeleteFramebuffers(1, [self._sub_mask_fbo]);
                self._sub_mask_fbo = None
            if self._snapshot_tex:
                gl.glDeleteTextures(1, [self._snapshot_tex]);
                self._snapshot_tex = None
            if self._snapshot_fbo:
                gl.glDeleteFramebuffers(1, [self._snapshot_fbo]);
                self._snapshot_fbo = None

            self._mask_tex = _create_mask_tex(fb_w, fb_h)
            self._mask_fbo, _ = _create_fbo_with_tex(self._mask_tex, False, fb_w, fb_h)

            self._sub_mask_tex = _create_mask_tex(fb_w, fb_h)
            self._sub_mask_fbo, _ = _create_fbo_with_tex(self._sub_mask_tex, False, fb_w, fb_h)

            self._snapshot_tex = _create_color_tex(fb_w, fb_h)
            self._snapshot_fbo, _ = _create_fbo_with_tex(self._snapshot_tex, False, fb_w, fb_h)

        self._mask_rects.clear()
        self._rect_seq = 0  # restart submission order each frame

    def mask_mark_rect(self, layer: int, x: float, y: float, w: float, h: float, key: str) -> None:
        # dedup: only one mask rect per key per frame
        if key in self._enq_mask_keys:
            return
        self._enq_mask_keys.add(key)

        # Increase submission order; wrap to 0..255
        self._rect_seq = (self._rect_seq + 1) & 0xFF
        self._mask_rects.append(_Rect(layer, x, y, w, h, key, self._rect_seq))

    def mask_mark_view(self, layer: int, x: float, y: float, w: float, h: float, key: str) -> None:
        self.mask_mark_rect(layer, x, y, w, h, key)

    # ----- Helpers for transforms & clipping -----
    def _get_current_clip_rect_screen(self) -> Tuple[float, float, float, float]:
        # dl = imgui.get_window_draw_list()
        # if hasattr(dl, "get_clip_rect_min") and hasattr(dl, "get_clip_rect_max"):
        #     minx, miny = dl.get_clip_rect_min()
        #     maxx, maxy = dl.get_clip_rect_max()
        #     return (minx, miny, maxx, maxy)
        #
        # wx, wy = imgui.get_window_position()
        # crx0, cry0 = imgui.get_window_content_region_min()
        # crx1, cry1 = imgui.get_window_content_region_max()
        # sx = imgui.get_scroll_x()
        # sy = imgui.get_scroll_y()
        # x0 = wx + crx0 - sx
        # y0 = wy + cry0 - sy
        # x1 = wx + crx1 - sx
        # y1 = wy + cry1 - sy

        from src.lsd.gl_gui.melty import Melty
        clip = Melty.get_clip_rect()

        return clip

    @staticmethod
    def _clip_rect(x: float, y: float, w: float, h: float, clip_xyxy: Tuple[float, float, float, float]
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
    def _fully_clipped(x: float, y: float, w: float, h: float, clip_xyxy: Tuple[float, float, float, float]) -> bool:
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
        dp_x, dp_y = dd.display_pos  # top-left of draw space (screen pixels)
        # s_x, s_y = dd.frame_buffer_scale  # DPI scale to framebuffer pixels

        s_x, s_y = 1, 1  # DPI scale to framebuffer pixels
        fb_w = snap_int(dd.display_size[0] * s_x)
        fb_h = snap_int(dd.display_size[1] * s_y)
        return dp_x, dp_y, s_x, s_y, fb_w, fb_h

    @staticmethod
    def _screen_rect_to_fb_xyxy(x, y, w, h, dp_x, dp_y, s_x, s_y, fb_h):
        # ImGui top-left rect -> framebuffer (top-left origin)
        x0 = (x - dp_x) * s_x
        x1 = (x + w - dp_x) * s_x
        y_top0 = (y - dp_y) * s_y
        y_top1 = (y + h - dp_y) * s_y
        y0 = fb_h - y_top1
        y1 = fb_h - y_top0
        return (x0, y0, x1, y1)

    def _collect_subtree_keys(self, root_key: str, mask_rects: List[_Rect]) -> set[str]:
        # Any rect whose key's ancestor chain contains root_key
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

    def mark_uncached(self, input_value, key: str) -> None:
        rkey = self._resolve_key(key)
        parent_ctx = self._stack[-1] if self._stack else None
        self.py_id_to_keys.setdefault(f"{id(input_value)}", set()).add(rkey)
        self.key_to_parent_key[rkey] = parent_ctx.key if parent_ctx else None

    # ----- Begin/End with per-view layer (from depth) -----
    def mark_start_offscreen(self, input_value, collection, draw_state, key: str, layer: int, name="", caller=None) -> bool:

        from src.lsd.gl_gui.melty import Melty
        Melty.tile_id_stack.append(key)
        x, y = imgui.get_cursor_screen_pos()
        # snap cursor to nearest pixel to avoid sub-pixel jitter during layout
        imgui.set_cursor_screen_pos((snap_int(x), snap_int(y)))

        # Layer derived from nesting depth; reserve 0 for background.
        layer = max(self._LAYER_MIN, min(self._LAYER_MAX, layer + 1))

        parent_ctx = self._stack[-1] if self._stack else None
        rkey = self._resolve_key(key)
        size = self._sizes.get(rkey, None)

        if key in self.all_keys:
            print("[TileCacheMasked] Warning: Duplicate key detected:", key, type(input_value).__name__)
        self.all_keys.add(rkey)

        if not draw_state.auto_resize and draw_state.width is not None and draw_state.height is not None:
            size = snap_int(draw_state.width), snap_int(draw_state.height)
            self._sizes[rkey] = size
        # if not draw_state.auto_resize and draw_state.window_size is not None:
        #     size = (snap_int(draw_state.window_size[0]), snap_int(ctx.draw_state.window_size[1]))

        # if not draw_state.auto_resize and draw_state.width is not None and draw_state.height is not None:
        #     size = snap_int(draw_state.width), snap_int(draw_state.height)
        # if not draw_state.auto_resize:
        #     size = (snap_int(draw_state.bounding_width), snap_int(draw_state.bounding_height))
        self.key_to_parent_key[rkey] = parent_ctx.key if parent_ctx else None
        self.key_to_draw_state[rkey] = draw_state

        # record child keys for parent
        parent_key = parent_ctx.key if parent_ctx else None
        if parent_key is not None:
            if parent_key not in self.parent_key_to_child_keys:
                self.parent_key_to_child_keys[parent_key] = set()
            self.parent_key_to_child_keys[parent_key].add(rkey)

        if name is not None:
            name_key = f"{id(collection)}.{name}"
            self.py_id_to_keys.setdefault(name_key, set()).add(rkey)

        if isinstance(input_value, (list, dict, set, deque, MutableMapping)) or hasattr(input_value, '__dict__'):
            self.py_id_to_keys.setdefault(f"{id(input_value)}", set()).add(rkey)

        self.py_id_to_keys.setdefault(f"{id(draw_state)}", set()).add(rkey)
        try:
            self.py_id_to_keys.setdefault(f"{id(draw_state.mouse_btn_state[0])}", set()).add(rkey)
            self.py_id_to_keys.setdefault(f"{id(draw_state.mouse_btn_state[1])}", set()).add(rkey)
            self.py_id_to_keys.setdefault(f"{id(draw_state.mouse_btn_state[2])}", set()).add(rkey)
        except Exception:
            pass

        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
        from src.lsd.gl_gui.view.core_views.core_render import push_id

        push_id(f"{rkey}_offscreen")  # UI id to keep based on caller-provided key

        from src.lsd.gl_gui.view.core_views.core_render import begin_group
        begin_group()
        imgui.pop_style_var(2)

        has_area = size is not None and size[0] != 0 and size[1] != 0

        # Try to draw cached if we have a clean tile sized correctly
        if size is not None and self.enabled and draw_state.frame_count >= 2:
            t = self._tiles.get(rkey)
            use_image = t and has_area and (t.size == (size[0], size[1])) and (not self._is_dirty(t))

            debug_mode = False
            if debug_mode:
                bounding_rect = (x, y, size[0], size[1])
                draw_list = imgui.get_window_draw_list()
                if not use_image:
                    draw_list.add_rect(
                        bounding_rect[0], bounding_rect[1],
                        bounding_rect[0] + bounding_rect[2], bounding_rect[1] + bounding_rect[3],
                        imgui.get_color_u32_rgba(1, 1, 1, 0.5),
                        thickness=2.0
                    )
                    if caller is not None:
                        caller_name = caller.__name__
                        draw_list.add_text(
                            x + 4, y + 4 + size[1],
                            imgui.get_color_u32_rgba(1, 0, 0, 0.45),
                            f"{caller_name}"
                        )

            if t and has_area and (t.size == (size[0], size[1])) and (not self._is_dirty(t)):

                imgui.image(t.tex, snap_int(size[0]), snap_int(size[1]), uv0=(0.0, 1.0), uv1=(1.0, 0.0),
                            tint_color=(1, 1, 1, 1))

                self._stack.append(
                    _Ctx(draw_state=draw_state, key=rkey, pos=(x, y), size=size, layer=layer, drew_cached=True,
                         auto_resize=draw_state.auto_resize))
                return False

        # Push context; pos/size will be updated to the *final* rect in mark_end_offscreen()
        self._stack.append(_Ctx(draw_state=draw_state, key=rkey, pos=(x, y), size=size, layer=layer, drew_cached=False,
                                auto_resize=draw_state.auto_resize))

        return True

    def mark_end_offscreen(self) -> None:
        ctx = self._stack.pop()

        from src.lsd.gl_gui.view.core_views.core_render import pop_id
        pop_id()
        from src.lsd.gl_gui.view.core_views.core_render import end_group
        end_group()

        from src.lsd.gl_gui.melty import Melty
        Melty.tile_id_stack.pop()

        # ctx.draw_state.imgui_is_edited = imgui.is_item_edited()
        # ctx.draw_state.imgui_is_active = imgui.is_item_active()
        # ctx.draw_state.imgui_is_focused = imgui.is_item_focused()
        # ctx.draw_state.imgui_scroll_y = imgui.get_scroll_y()
        # ctx.draw_state.imgui_is_hovered = imgui.is_item_hovered()
        # # if ctx.draw_state._has_popup:
        # ctx.draw_state.imgui_popover_open = (
        #     imgui.is_popup_open("", flags=imgui.POPUP_ANY_POPUP))

        # if not self._stack:
        #     return

        # Always query the *final* item rect from ImGui (post-layout)
        minx, miny = imgui.get_item_rect_min()

        size = imgui.get_item_rect_size()
        if not ctx.auto_resize and ctx.draw_state.window_size is not None:
            size = snap_int(ctx.draw_state.width), snap_int(ctx.draw_state.height)
        # if len(Melty.clip_stack) > 0:
        #     clip_width = Melty.clip_stack[-1][2] - Melty.clip_stack[-1][0]
        #     clip_height = Melty.clip_stack[-1][3] - Melty.clip_stack[-1][1]
        #     siz = (min(siz[0], clip_width), min(siz[1], clip_height))

        ctx.pos = (float(minx), float(miny))
        ctx.size = size[0], size[1]

        # --- Always record mask rects (even if we drew cached) so parents' subtree masks include children ---
        x, y = ctx.pos
        w, h = ctx.size
        clip = self._get_current_clip_rect_screen()
        clipped = self._clip_rect(x, y, w, h, clip)
        fully_clipped = self._fully_clipped(x, y, w, h, clip)
        self._key_to_ctx[ctx.key] = ctx
        if ctx.size:
            # clipped = False
            if clipped:
                cx, cy, cw, ch = clipped
                if cw > 0 and ch > 0:
                    self.mask_mark_view(ctx.layer, cx, cy, cw, ch, ctx.key)
            else:
                if w > 0 and h > 0:
                    self.mask_mark_view(ctx.layer, x, y, w, h, ctx.key)

        # If disabled or we used cached image, don't enqueue copy
        if not self.enabled or ctx.drew_cached or ctx.draw_state.frame_count < 2:
            # still keep sizes up to date
            self._sizes[ctx.key] = ctx.size
            return

        # Cache size & enqueue copy ONLY IF DIRTY (dedup per frame for copies)
        self._sizes[ctx.key] = ctx.size
        if ctx.size and ctx.size[0] > 0 and ctx.size[1] > 0:
            t = self._tiles.get(ctx.key)
            # Allocate/resize tile only if we need to copy (dirty or size changed)
            if (t is None) or (t.size != (ctx.size[0], ctx.size[1])):
                t = _ensure_tile(t, ctx.size[0], ctx.size[1], frame_id=self._frame_id)
                self.invalidate(ctx.key)
                self._tiles[ctx.key] = t

            if self._is_dirty(t):
                # parent_keys = self.get_parent_keys(ctx.key)
                # for parent in parent_keys:
                #     if parent and parent != ctx.key:
                #         parent_ctx = self._key_to_ctx.get(parent, None)
                #         parent_tile = self._tiles.get(parent, None)
                #         if parent_tile is not None:
                #             parent_tile.dirty = True
                #             parent_tile.last_clean_frame = parent_tile.last_invalidated_frame
                #             parent_tile.last_invalidated_frame = self._frame_id + 1
                #             parent_tile.force_invalidate = True
                #         if parent_tile is not None and parent_ctx is not None:
                #             self._pending[parent] = _Pending(tile=parent_tile, pos=parent_ctx.pos,
                #                                              size=parent_ctx.size, layer=parent_ctx.layer,
                #                                               key=parent_ctx.key)


                self._pending[ctx.key] = _Pending(tile=t, pos=ctx.pos, size=ctx.size, layer=ctx.layer, key=ctx.key)

    # ----- Finalize (post-frame) -----
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
            self._loc_uTopMask = gl.glGetUniformLocation(self._prog_copy, "uTopMask")
            self._loc_uSubMask = gl.glGetUniformLocation(self._prog_copy, "uSubMask")
            self._loc_uFBSize = gl.glGetUniformLocation(self._prog_copy, "uFBSize")
            self._loc_uSrcRectPx = gl.glGetUniformLocation(self._prog_copy, "uSrcRectPx")
            for name, loc in [("uSrc", self._loc_uSrc),
                              ("uTopMask", self._loc_uTopMask),
                              ("uSubMask", self._loc_uSubMask),
                              ("uFBSize", self._loc_uFBSize),
                              ("uSrcRectPx", self._loc_uSrcRectPx)]:
                assert loc != -1, f"[copy] uniform {name} missing/optimized out (loc=-1)"

        if self._prog_blit is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _BLIT_FS)
            self._prog_blit = _link(vs, fs)

    def _copy_debug_mode_to_int(self) -> int:
        table = {
            "off": 0,
            "uv": 1,
            "srcpx": 2,
            "mask": 3,  # shows global top mask (rank grayscale)
            "layer": 4,  # shows subtree mask (rank grayscale)
            "checker": 5,
            "solid": 6,
        }
        return table.get(self.copy_debug_mode.value, 0)

    def finalize_captures(self, framebuffer_size: Tuple[int, int], global_toggles=None) -> None:
        self.all_keys = set()

        if self._snapshot_fbo is None:
            return
        if not self._pending:
            return

        # Freeze worklists (with versioning, we generally do NOT cancel mid-frame)
        local_mask_rects = self._mask_rects[:]
        local_pending = list(self._pending.values())[:]

        if not local_pending:
            self._pending.clear()
            self._mask_rects.clear()
            self._enq_mask_keys.clear()
            self._enq_copy_keys.clear()
            return

        # Use ImGui sizes everywhere
        dp_x, dp_y, s_x, s_y, dd_fb_w, dd_fb_h = self._get_draw_xform()
        fb_w, fb_h = self._fb_size

        # If sizes mismatch, reallocate to match ImGui
        if (fb_w != dd_fb_w) or (fb_h != dd_fb_h):
            self.mask_begin_frame((dd_fb_w, dd_fb_h))
            fb_w, fb_h = self._fb_size

        st = _GLState()
        try:
            # 1) Capture default framebuffer -> texture (resolve MSAA via blit)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, 0)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self._snapshot_fbo)
            gl.glBlitFramebuffer(0, 0, dd_fb_w, dd_fb_h, 0, 0, dd_fb_w, dd_fb_h,
                                 gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)

            # 2) Build GLOBAL TOP mask using viewport per-rect (top=high via GL_MAX)
            self._ensure_programs()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._mask_fbo)
            gl.glViewport(0, 0, fb_w, fb_h)
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)

            gl.glClearColor(0,0,0, 1.0)  # BG=0
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)

            gl.glEnable(gl.GL_BLEND)
            gl.glBlendEquation(gl.GL_MAX)
            gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)  # factors ignored by equation

            gl.glUseProgram(self._prog_mask)
            loc_rank_norm = gl.glGetUniformLocation(self._prog_mask, "uRankNorm")
            for r in local_mask_rects:
                x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h)

                ix0 = int(snap_int(x0))
                iy0 = int(snap_int(y0))
                ix1 = int(snap_int(x1))
                iy1 = int(snap_int(y1))
                iw = max(0, ix1 - ix0)
                ih = max(0, iy1 - iy0)

                # if iw <= 0 or ih <= 0:
                #     continue

                gl.glViewport(ix0, iy0, iw, ih)

                # pack rank = (layer<<8)|order  in [0..65535]
                rank = ((r.layer & 0xFF) << 8) | (r.order & 0xFF)
                gl.glUniform1f(loc_rank_norm, float(rank) / 65535.0)

                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

            # restore state in the FBO
            gl.glViewport(0, 0, fb_w, fb_h)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glUseProgram(0)

            # 3) For each pending tile, build SUBTREE mask (max rank inside its subtree) + copy
            gl.glUseProgram(self._prog_copy)
            gl.glActiveTexture(gl.GL_TEXTURE0);
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._snapshot_tex)
            gl.glUniform1i(self._loc_uSrc, 0)
            gl.glActiveTexture(gl.GL_TEXTURE1);
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._mask_tex)
            gl.glUniform1i(self._loc_uTopMask, 1)
            gl.glUniform2f(self._loc_uFBSize, float(fb_w), float(fb_h))
            gl.glUniform1f(gl.glGetUniformLocation(self._prog_copy, "uDebugScale"), float(self.offscreen_scale))
            gl.glUniform1i(gl.glGetUniformLocation(self._prog_copy, "uCopyDebugMode"), self._copy_debug_mode_to_int())

            for p in local_pending:
                x, y = p.pos
                w, h = p.size
                x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(x, y, w, h, dp_x, dp_y, s_x, s_y, fb_h)
                # 3a) Build subtree mask for this tile
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._sub_mask_fbo)
                gl.glViewport(0, 0, fb_w, fb_h)
                gl.glDisable(gl.GL_SCISSOR_TEST)
                gl.glDisable(gl.GL_BLEND)
                gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
                from src.lsd.gl_gui.melty import Melty
                bg_color = Melty.bg_stack[-1] if len(Melty.bg_stack) > 0 else (0, 0, 0, 1)
                gl.glClearColor(*bg_color[:3], 1.0)  # BG=0

                # gl.glClearColor(0.0, 0.0, 0.0, 1.0)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)

                gl.glEnable(gl.GL_BLEND)
                gl.glBlendEquation(gl.GL_MAX)
                gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
                gl.glUseProgram(self._prog_mask)

                subtree_keys = self._collect_subtree_keys(p.key, local_mask_rects)

                for r in local_mask_rects:
                    if r.key not in subtree_keys:
                        continue
                    sx0, sy0, sx1, sy1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h)
                    ix0 = int(floor(sx0));
                    iy0 = int(floor(sy0))
                    ix1 = int(ceil(sx1));
                    iy1 = int(ceil(sy1))
                    iw = max(0, ix1 - ix0);
                    ih = max(0, iy1 - iy0)
                    if iw <= 0 or ih <= 0:
                        continue
                    gl.glViewport(ix0, iy0, iw, ih)

                    # same rank packing
                    rank = ((r.layer & 0xFF) << 8) | (r.order & 0xFF)
                    gl.glUniform1f(loc_rank_norm, float(rank) / 65535.0)

                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

                # Bind back copy program + subtree mask texture
                gl.glUseProgram(self._prog_copy)
                gl.glActiveTexture(gl.GL_TEXTURE2);
                gl.glBindTexture(gl.GL_TEXTURE_2D, self._sub_mask_tex)
                gl.glUniform1i(self._loc_uSubMask, 2)

                # Reset state before copying into tiles
                gl.glDisable(gl.GL_BLEND)
                gl.glBlendEquation(gl.GL_FUNC_ADD)
                gl.glBlendFunc(gl.GL_ONE, gl.GL_ZERO)
                gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)

                # 3b) Copy to the tile itself (mask == subtree)
                if p.tile is not None:
                    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, p.tile.fbo)
                    gl.glViewport(0, 0, snap_int(p.tile.size[0]), snap_int(p.tile.size[1]))

                    # Tint
                    if global_toggles is not None and getattr(global_toggles, "offscreen_debug", False):
                        gl.glUniform4f(gl.glGetUniformLocation(self._prog_copy, "uTint"), *self.frame_tint)
                    else:
                        gl.glUniform4f(gl.glGetUniformLocation(self._prog_copy, "uTint"), 1.0, 1.0, 1.0, 1.0)

                    gl.glUniform4f(self._loc_uSrcRectPx, float(x0), float(y0), float(x1), float(y1))

                    # Occlusion query
                    qid = _begin_occlusion_query()
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
                    passed = _end_occlusion_query(qid) if qid is not None else None
                    # if passed == 0:
                    #     # fully occluded -> invalid dirty
                    #     # p.tile.last_clean_frame = self._frame_id
                    #     self.invalidate(p.key)

                    # if isinstance(passed, int):
                    #     from src.lsd.gl_gui.mutty import Mutty
                    #
                    #     p.tile.last_clean_frame = self._frame_id
                    #     p.tile.dirty = self._is_dirty(p.tile)
                    #
                    # else:
                        # fallback (no queries) -> optimistic clean
                    p.tile.last_clean_frame = self._frame_id
                    p.tile.dirty = self._is_dirty(p.tile)
                    p.tile.force_invalidate = False

                    draw_state = self.key_to_draw_state.get(p.key)
                    self.initial_value[p.key] = self.get_hash(draw_state)

                    # self.initial_value.pop(p.key, None)

            gl.glUseProgram(0)

            # # 4) Optional: draw mask/snapshot overlays to default framebuffer for eyeballing
            # if self.debug_overlay_mask_to_screen or self.debug_overlay_src_to_screen:
            #     gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            #     gl.glViewport(0, 0, dd_fb_w, dd_fb_h)
            #     gl.glDisable(gl.GL_BLEND)
            #     gl.glUseProgram(self._prog_blit)
            #
            #     def blit_tex(tex, x, y, w, h):
            #         gl.glActiveTexture(gl.GL_TEXTURE0);
            #         gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
            #         gl.glUniform1i(gl.glGetUniformLocation(self._prog_blit, "uTex"), 0)
            #         gl.glViewport(x, y, w, h)
            #         gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
            #
            #     small_w = max(64, dd_fb_w // 6)
            #     small_h = max(64, dd_fb_h // 6)
            #
            #     # bottom-left stack
            #     ox, oy = 8, 8
            #     if self.debug_overlay_mask_to_screen:
            #         blit_tex(self._mask_tex, ox, oy, small_w, small_h)
            #         oy += small_h + 8
            #
            #     if self.debug_overlay_src_to_screen:
            #         blit_tex(self._snapshot_tex, ox, oy, small_w, small_h)
            #
            #     # restore viewport
            #     gl.glViewport(0, 0, dd_fb_w, dd_fb_h)
            #     gl.glUseProgram(0)

        finally:
            st.restore()
            # Clear per-frame state atomically
            self._pending.clear()
            self._mask_rects.clear()
            self._enq_mask_keys.clear()
            self._enq_copy_keys.clear()
            self._cancelled_keys.clear()
            self._recording = False
            self.apply_invalid()
            # self.initial_value.clear()
            self.did_deviate.clear()
