from __future__ import annotations

import random
from collections import deque, defaultdict
from copy import copy
from dataclasses import dataclass
from math import ceil, floor
from typing import Dict, List, Optional, Tuple, MutableMapping, Any

from OpenGL import GL as gl
import imgui
from imgui.core import _DrawList

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.core_model.core_enums import OffscreenDebugMode
from src.lsd.gl_gui.model.core_model.draw_state import TileMode
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace, get_live_frames
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.invalidation_tracker import InvalidateTracker, Note

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

# A tile backs a view with an offscreen texture sized to the view's bounds. Very
# large views (long scroll regions, oversized layouts, etc.) would allocate
# enormous textures (an 8000x8000 RGBA8 colour buffer alone is 256 MB, plus a
# mask buffer). When a view exceeds this size
# on either axis it gracefully falls back to uncached rendering instead.
MAX_TILE_DIM = 8000

# Tile textures are allocated at sizes rounded up to TILE_BUCKET so minor
# view-size jitter (hover re-measures, per-keystroke height changes) stays
# within the same allocation and costs zero GL work - no realloc, no
# crop-blit, and no "New Tile" invalidate_up cascade. Tile.size keeps meaning
# the LOGICAL view size everywhere (every equality gate, filled_bbox,
# size_change); only allocation and the raw-texel sites (PASS 3 viewport,
# PASS 4 mask blit, sampling UVRect) read Tile.alloc_size. Content is
# TOP-ANCHORED: the screen top-left corner is pinned at texel row alloc_h, so
# any logical resize within the bucket leaves existing texels aligned.
# TILE_BUCKET = 1 reverts the whole scheme to exact tile sizes.
TILE_BUCKET = 32


def _bucket(v: int) -> int:
    return min(MAX_TILE_DIM, ((int(v) + TILE_BUCKET - 1) // TILE_BUCKET) * TILE_BUCKET)


def _tile_alloc(t) -> Tuple[int, int]:
    # getattr: tolerates Tile instances created before alloc_size existed
    # (hotswap onto a live session).
    return getattr(t, "alloc_size", None) or t.size


def _tile_uv_rect(t) -> Tuple[float, float, float, float]:
    """uUVRect (xy scale, zw offset) mapping a 0..1 dest-rect UV onto the
    top-anchored logical subrect of a bucket-padded tile texture."""
    taw, tah = _tile_alloc(t)
    sx = t.size[0] / taw
    sy = t.size[1] / tah
    return (sx, sy, 0.0, 1.0 - sy)



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
    size: Tuple[int, int]  # LOGICAL view size; the texture may be larger (alloc_size)
    alloc_size: Optional[Tuple[int, int]] = None  # bucketed texture dims, None = same as size
    dirty: bool = True
    last_clean_frame: int = -1
    last_invalidated_frame: int = 3
    force_invalidate: bool = False
    mask_layer: int = 0  # Layer at which mask_tex was built (for relative depth offset)
    # Cumulative union (in tile-local coords) of regions blitted from the main
    # framebuffer during the tile's lifetime. None until the first partial blit;
    # once it covers (0,0,size) the tile is fully filled and scroll-driven
    # invalidations can be skipped. Reset on tile recreation and clamped in place
    # on a within-bucket logical resize.
    filled_bbox: Optional[Tuple[int, int, int, int]] = None


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
    corner_radius: float = 5.0  # Corner radius for rounded rectangles
    blend_max: bool = False


# ==============================
# GL helpers
# ==============================
def _create_color_tex(w: int, h: int, internal_format=gl.GL_RGBA8, clamp_to_border=False, filter=gl.GL_LINEAR) -> int:
    Melty.cache.tex_init_count += 1

    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal_format, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, filter)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, filter)
    if clamp_to_border:
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_BORDER)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_BORDER)
        gl.glTexParameterfv(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_BORDER_COLOR, [0.0, 0.0, 0.0, 0.0])
    else:
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return tex


def _create_mask_tex(w: int, h: int, clamp_to_border=False) -> int:
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R16, w, h, 0, gl.GL_RED, gl.GL_UNSIGNED_SHORT, None)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    if clamp_to_border:
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_BORDER)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_BORDER)
        gl.glTexParameterfv(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_BORDER_COLOR, [0.0, 0.0, 0.0, 0.0])
    else:
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return tex


def snap_int(v: float) -> int:
    return int(v)


def _ceil256(v: int) -> int:
    return ((int(v) + 255) // 256) * 256


def _display_max_size() -> Tuple[int, int]:
    """Largest video mode across all monitors, used to seed the one-time
    frame-global surface allocation so an OS-window resize never needs to
    reallocate. (0, 0) on any failure — the grow-only path then just rounds
    up from the current framebuffer size instead."""
    try:
        import glfw
        w = h = 0
        for m in glfw.get_monitors():
            mode = glfw.get_video_mode(m)
            w = max(w, int(mode.size.width))
            h = max(h, int(mode.size.height))
        return w, h
    except Exception:
        return 0, 0


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
        print_stack_trace()
        raise RuntimeError(f"FBO incomplete: 0x{status:04X}")
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
    return fbo, rbo


def _clear_mask_regions(mask_tex: int, rects) -> None:
    """Clear regions of an R16 tile mask to rank 0. Tile masks aren't attached
    to the tile's own FBO, so borrow the shared scratch FBO the way PASS 4
    does. rects are GL-space (x, y, w, h); the caller owns state save/restore
    (this enables scissor and rebinds GL_FRAMEBUFFER)."""
    scratch = getattr(Melty.cache, "_scratch_fbo", None)
    if not scratch:
        return
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, scratch)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, mask_tex, 0)
    gl.glEnable(gl.GL_SCISSOR_TEST)
    gl.glClearColor(0, 0, 0, 0.0)
    for x, y, cw, ch in rects:
        gl.glScissor(int(x), int(y), int(cw), int(ch))
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)


def _ensure_tile(existing: Optional[Tile], w: int, h: int, frame_id: int = 0, draw_state=None, tile_id=None) -> \
        Optional[Tile]:
    # Layout occasionally hands us fractional or negative dims (e.g. a
    # midline is 0.333... width). glTexImage2D coerces those to int and lands on
    # 0, producing an incomplete FBO attachment (0x8CD6). Snap to integer
    # coords up front and bail before any GL work if it reduces to zero.
    w = int(w) if w and w > 0 else 0
    h = int(h) if h and h > 0 else 0
    if w <= 0 or h <= 0:
        return None
    if existing and existing.size == (w, h):
        return existing

    aw = _bucket(w)
    ah = _bucket(h)

    if existing and _tile_alloc(existing) == (aw, ah):
        # Same bucket: update the logical size in place - no GL realloc, no
        # crop-blit (top-anchored content keeps the screen-top-left corner on
        # the same texels). Returning the SAME object is the caller's signal
        # to issue a direct ancestor invalidate instead of the "New Tile"
        # invalidate_up cascade.
        ow = snap_int(existing.size[0])
        oh = snap_int(existing.size[1])

        # Invariant: texels outside the current logical rect stay transparent
        # (color) and rank 0 (mask), so a grow reveals new pixels - PASS 3's
        # mask-gated copy discards where the tile has no fresh geometry, so it
        # would NOT overwrite stale texels left by an earlier larger logical
        # era. Clear the newly exposed bands on grow.
        if w > ow or h > oh:
            st = _GLState()
            try:
                bands = []
                if h > oh:  # bottom band: screen rows [oh, h)
                    bands.append((0, ah - h, w, h - oh))
                if w > ow:  # right band: screen cols [ow, w), full new height
                    bands.append((ow, ah - h, w - ow, h))
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, existing.fbo)
                gl.glEnable(gl.GL_SCISSOR_TEST)
                # Pin the colormask: a leaked R-only one (the mask passes use
                # one) would leave stale G/B/A inside the new logical rect.
                gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
                gl.glClearColor(0, 0, 0, 0.0)
                for x, y, cw, ch in bands:
                    gl.glScissor(int(x), int(y), int(cw), int(ch))
                    gl.glClear(gl.GL_COLOR_BUFFER_BIT)
                _clear_mask_regions(existing.mask_tex, bands)
            finally:
                st.restore()

        existing.size = (w, h)
        # Always stamp alloc_size: also upgrades a pre-bucketing tile whose
        # exact size happened to be bucket-aligned (getattr fallback saw
        # alloc == size for it).
        existing.alloc_size = (aw, ah)
        if existing.filled_bbox is not None:
            # Clamp to the new logical dims. On a shrink an old full-coverage
            # bbox now covers the new logical rect (reads fully filled, the
            # scroll-invalidate gate stops immediately); on a grow the old
            # coverage stays valid in top-left coords and the revealed border
            # reads unfilled - both matching the recreate path's seeding.
            l, t_, r, b = existing.filled_bbox
            r, b = min(r, w), min(b, h)
            existing.filled_bbox = (l, t_, r, b) if r > l and b > t_ else None
        existing.last_invalidated_frame = max(existing.last_invalidated_frame, frame_id + 1)
        request_render()
        return existing

    try:
        new_tex = _create_color_tex(aw, ah)
    except Exception as e:
        existing_size = existing.size if existing else None
        reset = "\033[0m"
        pink = "\033[95m"
        print(f"{pink}{draw_state.to_dict()}\n{'=' * 10} "
              f"Failed to create color texture for tile (size {aw}x{ah}): {e}"
              f"\nCurrent size {existing_size}\n{'=' * 10}{reset}")
        return None

    new_mask_tex = _create_mask_tex(aw, ah)
    # No depth-stencil renderbuffer: tiles are only ever written by the PASS 3
    # copy shader and the crop-blit, neither of which depth/stencil-tests, and
    # the D24S8 attachment was 4 B/px of VRAM plus the slowest part of the
    # create/destroy/delete cycle. Tile.rbo stays None; the guarded delete
    # sites will free RBOs on tiles created before this change.
    new_fbo, new_rbo = _create_fbo_with_tex(new_tex, False, aw, ah)

    if existing:
        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, existing.fbo)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, new_fbo)
            # Inherited scissor box clips both glClear and glBlitFramebuffer;
            # without this the new surface keeps undefined texels outside
            # whatever scissor box the frame happened to leave behind.
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glClearColor(0, 0, 0, 0.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            # Crop, don't stretch. The previous stretch-blit produced ugly
            # squished/streched content until the partial blits caught up. We
            # copy 1:1 from the old surface's screen-top-left corner into the
            # new surface's screen-top-left corner; the uncovered remainder
            # stays transparent until Stage 3 fills it.
            #
            # Y is flipped relative to screen coords, so content is
            # TOP-ANCHORED: screen-top maps to FBO y = alloc_h on each
            # surface. The crop dims are the LOGICAL dims; the crop source
            # runs from alloc_h - crop_h to alloc_h on both surfaces.
            old_aw, old_ah = _tile_alloc(existing)
            ow = snap_int(existing.size[0])
            oh = snap_int(existing.size[1])
            cw = max(0, min(ow, w))
            ch = max(0, min(oh, h))
            if cw > 0 and ch > 0:
                gl.glBlitFramebuffer(
                    0, snap_int(old_ah) - ch, cw, snap_int(old_ah),  # src (old FBO, top-left in screen)
                    0, ah - ch, cw, ah,                              # dst (new FBO, same screen corner)
                    gl.GL_COLOR_BUFFER_BIT,
                    gl.GL_NEAREST,                # no scaling -> NEAREST is exact and cheap
                )
            _clear_mask_regions(new_mask_tex, [(0, 0, aw, ah)])
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
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glClearColor(0, 0, 0, 0.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            # glTexImage2D(None) values are undefined; the padding for PASS 4
            # only ever rewrites the logical region, so zero the mask once here
            # so edge taps at the logical boundary read as mask rank 0.
            _clear_mask_regions(new_mask_tex, [(0, 0, aw, ah)])
        finally:
            st.restore()

    t = Tile(draw_state=draw_state, fbo=new_fbo, tex=new_tex, mask_tex=new_mask_tex, rbo=new_rbo, size=(w, h),
             alloc_size=(aw, ah), dirty=True)
    # Seed filled_bbox to what the crop-copy above covered (in tile-local
    # top-left coords). On a shrink it's the whole tile -> tile reads are fully
    # covered and the scroll-invalidate gate stops early; on a grow it's
    # the old corner, so only the newly-revealed border still needs to fill in.
    # A new tile -> None (untouched, scroll-invalidate proceeds).
    if existing is not None:
        ow = snap_int(existing.size[0])
        oh = snap_int(existing.size[1])
        cw = max(0, min(ow, w))
        ch = max(0, min(oh, h))
        t.filled_bbox = (0, 0, cw, ch) if cw > 0 and ch > 0 else None
    else:
        t.filled_bbox = None
    t.last_invalidated_frame = max(t.last_invalidated_frame, frame_id + 1)
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
        # gl.glViewport(*self.viewport)
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
        discard;
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
    if (val > 0.0) {
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
uniform vec4 uUVRect;        // xy = UV scale, zw = UV offset: maps the dest
                             // rect onto the top-anchored logical subrect of a
                             // bucket-padded tile mask. (1,1,0,0) = whole tex.
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

    float val = texture(uTex, vUV * uUVRect.xy + uUVRect.zw).r;
    if (val > 0.0) {
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

_SOLID_FS = """
#version 330 core
uniform vec4 uColor;
out vec4 oColor;
void main() { oColor = uColor; }
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
        # Maps a draw function's id() -> set of view keys it produced, so we
        # can invalidate every view drawn by a given @draw_func (e.g. draw_text).
        self.func_id_to_keys: Dict[int, set] = {}
        self.key_to_parent_key: Dict[str, str] = {}
        self.parent_key_to_child_keys: Dict[str, dict] = {}
        self.parent_key_to_child_keys_last: Dict[str, dict] = {}
        self.key_to_draw_state: Dict[str, any] = {}

        self._tiles: Dict[str, Tile] = {}
        self._stack: List[_Ctx] = []
        self._key_to_ctx: Dict[str, _Ctx] = {}
        self._pending: List[_Pending] = []
        # Last (clipped_rect, transform) each view marked its tile with. When this
        # changes the cached subtree masks (Tile.mask_tex) of this tile and its
        # ancestors are wrong, so we invalidate them once interaction has settled.
        self._last_mark_clip: Dict[str, tuple] = {}
        # Frame number of the most recent scroll-delta per scroll container.
        # The BVH-driven scroll-in sweep runs one extra frame after that so a
        # view that registers its updated bbox during its own rendering (i.e.
        # after the scroller's mark_end already ran this frame) still gets
        # caught next frame, when the BVH is current.
        self._last_scroll_change_frame: Dict[str, int] = {}
        self.all_keys = set()

        self._fb_size: Tuple[int, int] = (0, 0)
        # Allocated dims of the four co-sized internal surfaces (_mask,
        # _sub_mask, _full_sub_mask, snapshot). For monitor only, grow-only.
        # _fb_size stays the LOGICAL framebuffer size everywhere.
        self._fb_alloc_size: Tuple[int, int] = (0, 0)

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
        self._prog_solid: Optional[int] = None
        self._loc_solid_uColor = None

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
        self._loc_texoffr_uUVRect = None

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

        # self.pending_invalid = []
        self._prev_occluders: Dict[str, frozenset] = {}  # tile_key -> frozenset of (key, x, y, w, h)

    @property
    def full_mask_tex(self) -> Optional[int]:
        return self._full_mask_tex

    def _is_dirty(self, t: Optional[Tile]) -> bool:
        if t is None:
            return True
        return t.last_clean_frame < t.last_invalidated_frame

    def _tile_fully_filled(self, t: Optional[Tile]) -> bool:
        """True when every pixel of the tile has been blitted from the main
        framebuffer at least once. Partial blits accumulate into ``filled_bbox``
        in PASS 3; this is the test that lets scroll-driven invalidation stop
        once the tile is complete (the original purpose of that invalidation
        is just to keep scrolling tiles filling in)."""
        if t is None or t.filled_bbox is None:
            return False
        w, h = t.size
        # Chicken-and-egg: draw_state.width/height update before the tile gets
        # re-ensured at the new size, so the tile can be on screen with stale
        # contents we'd otherwise treat as "filled". Check against the live
        # draw_state and drop the cached fill if they diverge - the scroll
        # path then re-engages until _ensure_tile catches up next frame.
        ds = t.draw_state
        if ds is not None and ds.width is not None and ds.height is not None:
            if int(ds.width) != w or int(ds.height) != h:
                t.filled_bbox = None
                return False
        l, top, r, b = t.filled_bbox
        # And defense against any in-place tile-size mutation (unlikely today, but
        # cheap insurance for future code paths).
        if r > w or b > h:
            t.filled_bbox = None
            return False
        return l <= 0 and top <= 0 and r >= w and b >= h

    def _accumulate_filled(self, t: Tile, draw_state) -> None:
        """Union the current frame's visible-portion (tile size minus
        ``clipped_by_rect`` insets) into the tile's cumulative ``filled_bbox``."""
        if t is None or draw_state is None:
            return
        cb = draw_state.clipped_by_rect or (0, 0, 0, 0)
        w, h = t.size
        bl = max(0, int(cb[0]))
        bt = max(0, int(cb[1]))
        br = max(bl, w - max(0, int(cb[2])))
        bb = max(bt, h - max(0, int(cb[3])))
        if br <= bl or bb <= bt:
            return  # nothing actually written this frame
        if t.filled_bbox is None:
            t.filled_bbox = (bl, bt, br, bb)
        else:
            pl, pt, pr, pbottom = t.filled_bbox
            t.filled_bbox = (min(pl, bl), min(pt, bt), max(pr, br), max(pbottom, bb))

    @staticmethod
    def _oversized(size: Optional[Tuple[int, int]]) -> bool:
        """True if a view this size is too large to back with an offscreen tile."""
        return size is not None and (size[0] > MAX_TILE_DIM or size[1] > MAX_TILE_DIM)

    def _discard_tile(self, key: str) -> None:
        """Drop a tile and free its GL resources (e.g. a view grew too large to cache)."""
        t = self._tiles.pop(key, None)
        if t is None:
            return
        gl.glDeleteFramebuffers(1, [t.fbo])
        gl.glDeleteTextures(1, [t.tex])
        if t.mask_tex:
            gl.glDeleteTextures(1, [t.mask_tex])
        if t.rbo:
            gl.glDeleteRenderbuffers(1, [t.rbo])

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

    def invalidate_current(self, force=False, note=None):
        if len(self._stack) == 0:
            return
        self.invalidate(self._stack[-1].key, force=force, note=note)

    def invalidate_up_current(self, max_depth=4, force=False, note=None):
        if len(self._stack) == 0:
            return
        self.invalidate_up(self._stack[-1].key, max_depth=max_depth, force=force, note=note)

    def invalidate_up_by_obj(self, obj, name=None, max_depth=4, force=False, frame_delta=0, note=None):

        if name is not None:
            keys = self.py_id_to_keys.get(f"{id(obj)}.{name}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate_up(k, max_depth=max_depth, force=force, frame_delta=frame_delta, note=note)
        else:
            keys = self.py_id_to_keys.get(f"{id(obj)}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate_up(k, max_depth=max_depth, force=force, frame_delta=frame_delta, note=note)

    def invalidate_by_obj(self, obj, name=None, frame_delta=0, note=None):
        if name is not None:
            keys = self.py_id_to_keys.get(f"{id(obj)}.{name}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate(k, frame_delta=frame_delta, note=note)
        else:
            keys = self.py_id_to_keys.get(f"{id(obj)}", None)
            if keys is not None:
                for k in keys:
                    self.invalidate(k, frame_delta=frame_delta, note=note)

    @staticmethod
    def _func_ids(func) -> set:
        """Candidate ids for a render function: the function itself plus the raw
        function it wraps. @render_func sets wrapper.__wrapped__ to the original
        (via functools.wraps), so callers can pass either the imported wrapper
        (e.g. draw_text) or the undecorated function and still match."""
        ids = set()
        if func is None:
            return ids
        ids.add(id(func))
        wrapped = getattr(func, "__wrapped__", None)
        if wrapped is not None:
            ids.add(id(wrapped))
        return ids

    def _register_func_keys(self, draw_state, rkey) -> None:
        """Record which render function produced this view key (both the wrapper
        and the underlying function) so invalidate_by_func can find it later."""
        for fn in (getattr(draw_state, "_wrapper", None), getattr(draw_state, "_view_func", None)):
            for fid in self._func_ids(fn):
                self.func_id_to_keys.setdefault(fid, set()).add(rkey)

    def register_func_key(self, func, key) -> None:
        """Associate an extra render function with an existing view key. Used for
        helper functions that draw into another view's tile rather than owning a
        tile themselves (e.g. draw_bg painting a view's background), so that
        invalidate_by_func(draw_bg) reaches every view it backed."""
        if key is None:
            return
        for fid in self._func_ids(func):
            self.func_id_to_keys.setdefault(fid, set()).add(key)

    def _keys_for_func(self, func) -> set:
        keys = set()
        for fid in self._func_ids(func):
            keys |= self.func_id_to_keys.get(fid, set())
        return keys

    def invalidate_by_func(self, func, frame_delta=0, note=None):
        """Invalidate every view drawn by the given @render_func, e.g.
        invalidate_by_func(draw_text) rerenders all text views."""
        for k in self._keys_for_func(func):
            self.invalidate(k, frame_delta=frame_delta, note=note)

    def invalidate_up_by_func(self, func, max_depth=4, force=False, frame_delta=0, note=None):
        """Like invalidate_by_func, but also cascades up to parents/children."""
        for k in self._keys_for_func(func):
            self.invalidate_up(k, max_depth=max_depth, force=force, frame_delta=frame_delta, note=note)

    # def apply_invalid(self):
        # for t in self.pending_invalid:
        #     if t is not None:
        #         t.dirty = self._is_dirty(t)
        # self.pending_invalid.clear()

    def get_parent_keys(self, key):
        all_keys = [key]
        parent_key = self.key_to_parent_key.get(key, None)
        if parent_key and parent_key != key:
            all_keys.extend(self.get_parent_keys(parent_key))
        return all_keys

    def get_child_keys(self, key, depth=0, max_depth=4, stop_at_filled: bool = False,
                       include_windows: bool = True):
        if depth >= max_depth:
            return {}
        child_keys = self.parent_key_to_child_keys.get(key, {})
        if not stop_at_filled:
            all_keys = {}
            for k_inner, ck in child_keys.items():
                # Terminate the chain on nested windows when asked: a closable
                # window owns its own tile, so invalidating an ancestor
                # (e.g. a scroll) needn't re-invalidate the window or its
                # subtree. Skip the window entirely - don't add it, don't recurse.
                if not include_windows and ck[2] is not None and ck[2].closable:
                    continue
                all_keys[k_inner] = ck
                all_keys.update(self.get_child_keys(ck[1], depth + 1, max_depth=max_depth,
                                                    include_windows=include_windows))
            return all_keys

        # stop_at_filled: include the boundary child (so it still gets
        # invalidated and recomposes) but don't recurse into its subtree -
        # the descendants below a filled tile already have their contents
        # composed into it and don't need re-invalidation. Without the "one
        # past" case, the last unfilled child above a filled boundary gets
        # stranded with stale composition.
        all_keys = {}
        for k_inner, ck in child_keys.items():
            child_key = ck[1]
            if not include_windows and ck[2] is not None and ck[2].closable:
                continue
            all_keys[k_inner] = ck
            if self._tile_fully_filled(self._tiles.get(child_key)):
                continue  # one past: included above, but don't walk the subtree
            all_keys.update(self.get_child_keys(child_key, depth + 1, max_depth=max_depth,
                                                stop_at_filled=stop_at_filled,
                                                include_windows=include_windows))
        return all_keys

    def invalidate_up(self, k: str, max_depth=4, force=False, frame_delta=0, note=None, skip_self=False,
                      stop_at_filled: bool = False, bypass_clip=False, include_windows: bool = False) -> None:
        draw_state = self.key_to_draw_state.get(k, None)
        if note is None:
            note = Note(name="Unnamed invalidate_up", reason="", tint=(1, 0, 0),
                        frame=Melty.frame_count, draw_state=draw_state)
            if Melty.frame_count > 100 and Melty.frame_count % 30 == 0:
                if Toggles.InvalidateTracker.enable:
                    print_stack_trace()

        note.draw_state = draw_state

        if k not in self._tiles:
            k = self.key_to_parent_key.get(k, None)

        self.invalidate(k, force=force, note=note, stop_at_filled=stop_at_filled)
        child_keys = self.get_child_keys(k, max_depth=max_depth,
                                          stop_at_filled=stop_at_filled,
                                          include_windows=include_windows).values()
        child_keys_list = list(child_keys)
        child_keys_list.sort(key=lambda x: x[0] if x[0] is not None else 0)

        parent_draw_state = self.key_to_draw_state.get(k, None)
        if parent_draw_state is not None and parent_draw_state._print_last_invalid:
            print_stack_trace()
        for top, child, child_draw_state in child_keys_list:

            inside_clip, below, above = parent_draw_state.is_inside_clip(child_draw_state)
            if child_draw_state is not None and child_draw_state._print_last_invalid:
                print_stack_trace()
            if (child_draw_state.inside_clip and inside_clip) or bypass_clip:
                if child != k:
                    pt = self._tiles.get(child)
                    if pt is not None:
                        pt.last_invalidated_frame = max(pt.last_invalidated_frame, self._frame_id + 1 + frame_delta)
                        pt.dirty = self._is_dirty(pt)
                        pt.force_invalidate = True
                        # self.pending_invalid.append(pt)
            if above:
                continue
            if below:
                return

    def apply_blend_mode(self, r):
        if r.draw_state is None:
            gl.glDisable(gl.GL_BLEND)
            return

        if r.draw_state.tile_mode == TileMode.MIN:
            # gl.glDisable(gl.GL_BLEND)
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendEquation(gl.GL_MIN)
            gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
        elif r.draw_state.tile_mode == TileMode.MAX:
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendEquation(gl.GL_MAX)
            gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
        else:
            gl.glDisable(gl.GL_BLEND)

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

    def invalidate_parent(self, k: str, force=False, do_store=True, frame_delta=0, note=None,
                   stop_at_filled: bool = False) -> None:
        draw_state = self.key_to_draw_state.get(k, None)
        if draw_state is None:
            return
        parent = draw_state.parent_window if draw_state.parent_window is not None else draw_state.parent
        self.invalidate(parent._tile_id, force=force, do_store=do_store, frame_delta=frame_delta, note=note, stop_at_filled=stop_at_filled)

    def invalidate(self, k: str, force=False, do_store=True, frame_delta=0, note=None,
                   stop_at_filled: bool = False) -> None:

        draw_state = self.key_to_draw_state.get(k, None)

        # if draw_state is not None and not draw_state.inside_clip:
        #     return

        if note is None:
            note = Note(name="Unnamed invalidate", reason="", tint=(1, 0, 0, 0.1),
                        frame=Melty.frame_count, draw_state=draw_state)
            if Melty.frame_count > 100 and Melty.frame_count % 30 == 0:
                if Toggles.InvalidateTracker.enable:
                    print_stack_trace()

        note.draw_state = draw_state

        if note.frame == 0:
            note.frame = Melty.frame_count

        if Melty.frame_count > 100 and Melty.frame_count % 30 == 0:
            if draw_state is not None and draw_state._print_last_invalid:
                print_stack_trace()


        if Toggles.InvalidateTracker.invalidate_stack_trace:
            frames_since_last_print = Melty.frame_count - Melty.last_print_invalidate
            if Melty.frame_count > 100 and (frames_since_last_print > 100 or frames_since_last_print == 0):
                if note.name != "hover change":
                    print_stack_trace()
                    if draw_state is not None:
                        print("View_func", draw_state._view_func.__name__)

                # else:
                #     print("hover change")

            Melty.last_print_invalidate = Melty.frame_count

        t = self._tiles.get(k)
        if t is not None:
            target_frame = self._frame_id + 1
            t.last_invalidated_frame = max(t.last_invalidated_frame, target_frame)
            t.dirty = self._is_dirty(t)

            if Toggles.InvalidateTracker.enable:
                InvalidateTracker.invalidations[k] = note

            if force:
                t.force_invalidate = True

        parent_keys = self.get_parent_keys(k)
        for parent in parent_keys:
            if parent and parent != k:
                pt = self._tiles.get(parent)
                if pt is not None:
                    # Stop climbing once an ancestor's tile is fully filled -
                    # but invalidate that boundary ancestor first, then stop.
                    # Without the "one more" step, the boundary tile is the
                    # one that has to recompose to cover the just-invalidated
                    # descendant, and it gets stranded with stale composition.
                    parent_draw_state = self.key_to_draw_state.get(parent, None)
                    if parent_draw_state is not None and parent_draw_state._print_last_invalid:
                        print_stack_trace()
                    pt.force_invalidate = True
                    pt.last_invalidated_frame = max(pt.last_invalidated_frame, self._frame_id + 1 + frame_delta)
                    pt.dirty = self._is_dirty(pt)
                    if Toggles.InvalidateTracker.enable:
                        InvalidateTracker.invalidations[k] = note
                    if stop_at_filled and self._tile_fully_filled(pt):
                        break

    def invalidate_all(self) -> None:
        for t in self._tiles.values():
            if t is not None:
                t.last_invalidated_frame = max(t.last_invalidated_frame, self._frame_id + 1)
                t.force_invalidate = True
                # self.force_invalid.append(t)
        request_render()

    def invalidate_scrolled_in(self, draw_state, on_change: bool = False) -> None:
        """Query the BVH for views currently overlapping the given scroll
        view's clip rect and invalidate any whose tile isn't fully filled at
        the live draw_state size. Catches views scrolled into view with stale
        tiles (the chicken-and-egg) without per-view bookkeeping.

        ``on_change=True`` records the current frame for use by a follow-up
        catchup sweep one frame later — a child that just entered the clip
        rect may register its updated bbox during its own render after this
        method already ran for the current frame, so the second sweep catches
        what the BVH didn't have yet. Call with ``on_change=False`` for that
        follow-up so the timestamp isn't kept moving forward.
        """
        if draw_state is None:
            return
        clip = draw_state.abs_clip_rect
        if clip is None:
            return

        # Walk the live child tree instead of querying the BVH. The BVH boxes
        # only catch up when a child re-renders, so a child that just scrolled
        # in still carries its previous box and a clip query misses it - the
        # very views this needs to find. children_in_clip binary searches the
        # ordered child dict using live abs_top, so it sees the current layout.
        # Nested windows are pruned: a window doesn't translate with our
        # scroll, so it can never be "scrolled in" and invalidating it (and its
        # subtree) here just added lag to every scroll event.
        children_in_clip = draw_state.children_in_clip(clip, max_depth=5,
                                                       include_windows=False)
        for ds in children_in_clip:
            if Toggles.InvalidateTracker.draw_bvh:
                InvalidateTracker.invalidations[f"{ds.name} in {draw_state.name} ds"] = Note(name="ds rect",
                                                                                             reason="bvh",
                                                                                             tint=(0, 1, 1),
                                                                                             frame=Melty.frame_count,
                                                                                             draw_state=ds,
                                                                                             rect=(ds.abs_left,
                                                                                                   ds.abs_top,
                                                                                                   ds.abs_left + ds.width,
                                                                                                   ds.abs_top + ds.height))

            tile_id = getattr(ds, "_tile_id", None)
            if tile_id is  None:
                continue
            # Only stale tiles need re-rendering; a fully-filled tile just
            # translates, so leave the cache alone (its bbox was refreshed above).
            if not self._tile_fully_filled(self._tiles.get(tile_id)):
                self.invalidate(tile_id, frame_delta=0,
                                   stop_at_filled=False,
                                   note=Note(name="Scrolled in",
                                             reason="bvh", tint=(0.3, 1, 0.5)))


    def _detect_occluder_changes(self, mask_rects):
        """Invalidate root windows whose occluder set changed (reveals stale cached pixels).

        Only checks tiles with closable=True (root windows). When an occluding
        root window is removed or moves, invalidate_up cascades to children.
        """
        if not mask_rects:
            self._prev_occluders = {}
            return

        if imgui.is_mouse_down(0) or imgui.is_mouse_down(2) or imgui.is_mouse_down(1):
            return

        # Collect only closable root window rects (both as targets and occluders)
        root_rects = {}
        for r in mask_rects:
            if r.key in self._shadow_mask_keys:
                continue
            ds = self.key_to_draw_state.get(r.key)

            if ds is not None and ds.closable:
                root_rects[r.key] = r

        new_occluders = {}
        needs_invalidate = []

        for key, my_rect in root_rects.items():
            mx0, my0 = my_rect.x, my_rect.y
            mx1, my1 = mx0 + my_rect.w, my0 + my_rect.h
            my_depth = my_rect.depth_and_layer

            occluders = []
            for r in root_rects.values():
                if r.key == key:
                    continue
                if r.depth_and_layer <= my_depth:
                    continue
                # AABB overlap test
                rx0, ry0 = r.x, r.y
                rx1, ry1 = rx0 + r.w, ry0 + r.h
                if rx0 < mx1 and mx0 < rx1 and ry0 < my1 and my0 < ry1:
                    # print(f"{r.draw_state.name}  From {rx0},{ry0},{rx1},{ry1} to {mx0},{my0},{mx1},{my1}")
                    occluders.append((r.key, snap_int(r.x), snap_int(r.y),
                                      snap_int(r.w), snap_int(r.h)))

            occ_frozen = frozenset(occluders)
            new_occluders[key] = occ_frozen

            if not occluders:
                ds = my_rect.draw_state
                if ds is not None and not ds.has_full_tile:
                    ds.has_full_tile = True

            prev = self._prev_occluders.get(key)
            if prev is not None and occ_frozen != prev:
                # Check if any occluder was removed or changed position
                prev_map = {o[0]: o for o in prev}
                curr_map = {o[0]: o for o in occ_frozen}

                for pk in prev_map:
                    if pk not in curr_map or prev_map[pk] != curr_map[pk]:
                        needs_invalidate.append(key)
                        break

        for key in needs_invalidate:
            draw_state = self.key_to_draw_state.get(key, None)

            note = Note(name="Occlude change", reason="",
                        tint=(1, 0.5, 0), frame=Melty.frame_count, draw_state=draw_state)
            self.invalidate_up(key, force=True, max_depth=20, note=note)

        if needs_invalidate:
            request_render()

        self._prev_occluders = new_occluders

    def get_texture_id(self, key: str) -> Optional[int]:
        # Returned texture may be zero-padded: content is the top-anchored
        # logical subrect (u [0, size/alloc], v [1 - size/alloc, 1]) - sample
        # via _tile_uv_rect, not 0..1.
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
        if self._prog_solid:
            gl.glDeleteProgram(self._prog_solid)
            self._prog_solid = None

    def mask_begin_frame(self, framebuffer_size: Tuple[int, int]) -> None:
        fb_w, fb_h = map(int, framebuffer_size)
        self._frame_id += 1
        self._recording = True
        self._cancelled_keys.clear()
        self._enq_mask_keys.clear()
        self._enq_copy_keys.clear()

        rf = self._rand
        self.frame_tint = (0.5 + 0.5 * rf(), 0.5 + 0.5 * rf(), 0.5 + 0.5 * rf(), 1.0)

        # The four internal surfaces (_mask/_sub_mask/_full_sub_mask R16 +
        # snapshot RGBA8) are allocated ONCE at monitor-max (256px-rounded,
        # grow-only); content renders into the GL (0,0,fb_w,fb_h) corner, so
        # an interactive OS-window resize no longer destroys and recreates
        # ~146MB of surfaces per drag step. They are only ever sampled through
        # _COPY_FS's uv = srcPx / uFBSize, so finalize_captures passes the
        # ALLOCATED size in uFBSize. _full_mask_tex is the exception: the
        # shader_library filters in Melty.post_frame (normalize/shadowmap)
        # process it edge-to-edge in UV space, so it must be the logical fb
        # size - resized in-place via glTexImage2D rebind, which keeps the
        # texture name and its FBO attachment valid.
        self._fb_alloc_size = getattr(self, "_fb_alloc_size", (0, 0))
        if fb_w > 0 and fb_h > 0:
            aw, ah = self._fb_alloc_size
            if fb_w > aw or fb_h > ah or self._snapshot_fbo is None:
                def safe_del_tex(t):
                    if t:
                        gl.glDeleteTextures(1, [t])

                def safe_del_fbo(f):
                    if f:
                        gl.glDeleteFramebuffers(1, [f])

                safe_del_tex(self._mask_tex)
                safe_del_fbo(self._mask_fbo)
                safe_del_tex(self._sub_mask_tex)
                safe_del_fbo(self._sub_mask_fbo)
                safe_del_tex(self._full_sub_mask_tex)
                safe_del_fbo(self._full_sub_mask_fbo)
                safe_del_tex(self.snapshot_tex)
                safe_del_fbo(self._snapshot_fbo)

                mon_w, mon_h = _display_max_size()
                max_tex = int(gl.glGetIntegerv(gl.GL_MAX_TEXTURE_SIZE))
                aw = max(min(_ceil256(max(fb_w, mon_w)), max_tex), fb_w)
                ah = max(min(_ceil256(max(fb_h, mon_h)), max_tex), fb_h)

                self._mask_tex = _create_mask_tex(aw, ah, clamp_to_border=True)
                self._mask_fbo, _ = _create_fbo_with_tex(self._mask_tex, False, aw, ah)

                self._sub_mask_tex = _create_mask_tex(aw, ah, clamp_to_border=True)
                self._sub_mask_fbo, _ = _create_fbo_with_tex(self._sub_mask_tex, False, aw, ah)

                self._full_sub_mask_tex = _create_mask_tex(aw, ah, clamp_to_border=True)
                self._full_sub_mask_fbo, _ = _create_fbo_with_tex(self._full_sub_mask_tex, False, aw, ah)

                self.snapshot_tex = _create_color_tex(aw, ah, clamp_to_border=True, filter=gl.GL_NEAREST)
                self._snapshot_fbo, _ = _create_fbo_with_tex(self.snapshot_tex, False, aw, ah)

                # An FBO has no storage - bound to bind, create it once.
                if self._scratch_fbo is None:
                    self._scratch_fbo = gl.glGenFramebuffers(1)

                # glTexImage2D(None) contents are undefined, and the copy gate
                # (topRank == maxRank > 0) relies on padding beyond the logical
                # fb reading rank 0. This may also be re-entered mid-frame by
                # finalize_captures under arbitrary leftover GL state, so pin
                # scissor/colormask explicitly around the clears.
                st = _GLState()
                try:
                    gl.glDisable(gl.GL_SCISSOR_TEST)
                    gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
                    gl.glClearColor(0, 0, 0, 0.0)
                    for fbo in (self._mask_fbo, self._sub_mask_fbo,
                                self._full_sub_mask_fbo, self._snapshot_fbo):
                        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
                        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
                finally:
                    st.restore()

                self._fb_alloc_size = (aw, ah)

            if (fb_w, fb_h) != self._fb_size or self._full_mask_fbo is None:
                self._fb_size = (fb_w, fb_h)
                if self._full_mask_tex is None or self._full_mask_fbo is None:
                    if self._full_mask_tex:
                        gl.glDeleteTextures(1, [self._full_mask_tex])
                    if self._full_mask_fbo:
                        gl.glDeleteFramebuffers(1, [self._full_mask_fbo])
                    self._full_mask_tex = _create_mask_tex(fb_w, fb_h, clamp_to_border=True)
                    self._full_mask_fbo, _ = _create_fbo_with_tex(self._full_mask_tex, False, fb_w, fb_h)
                else:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, self._full_mask_tex)
                    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R16, fb_w, fb_h, 0,
                                    gl.GL_RED, gl.GL_UNSIGNED_SHORT, None)
                    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

        self._mask_rects.clear()
        self._rect_seq = 0

    def mask_mark_rect(
            self, draw_state: any, layer: int, depth_and_layer: any, x: float, y: float, w: float, h: float,
            key: str, corner_radius: float = 5.0
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
            corner_radius: float = 5.0, draw_state=None, parent_key=None,
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
            h: float, key: str, corner_radius: float = 5.0
    ) -> None:
        self.mask_mark_rect(draw_state, layer, copy(depth_and_layer), x, y, w, h, key, corner_radius)

    def _get_current_clip_rect_screen(self) -> Tuple[float, float, float, float]:
        clip = Melty.get_clip_rect()
        return clip

    @staticmethod
    def _clip_rect(
            x: any,
            y: any,
            w: any,
            h: any,
            clip_xyxy: Tuple[float, float, float, float],
    ) -> Optional[Tuple[float, float, float, float]]:
        if clip_xyxy is None:
            return None
        cx0, cy0, cx1, cy1 = clip_xyxy
        x0 = max(x, cx0)
        y0 = max(y, cy0)
        x1 = min(x + w, cx1)
        y1 = min(y + h, cy1)

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
            self.parent_key_to_child_keys[parent_key][rkey] = (draw_state.abs_top, rkey, draw_state)

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
        self._register_func_keys(draw_state, rkey)
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
            corner_radius = getattr(draw_state, "corner_radius", 6) or 5.0
            x, y = draw_state.abs_left, draw_state.abs_top
            w, h = draw_state.width, draw_state.height
            cb = draw_state.clipped_by_rect
            clip = draw_state.abs_clip_rect if (cb is not None and any(cb)) else None
            clipped = self._clip_rect(x, y, w, h, clip)
            if clipped:
                cx, cy, cw, ch = clipped
                if cw > 0 and ch > 0:
                    self.mask_mark_view(
                        draw_state,
                        layer,
                        draw_state.shadow_depth,
                        cx,
                        cy,
                        cw,
                        ch,
                        draw_state._tile_id,
                        corner_radius,
                    )
            else:
                self.mask_mark_view(
                    draw_state,
                    layer,
                    draw_state.shadow_depth,
                    x,
                    y,
                    w,
                    h,
                    draw_state._tile_id,
                    corner_radius,
                )

        if use_image:
            a = draw_state.abs_left, draw_state.abs_top
            b = draw_state.abs_left + size[0], draw_state.abs_top + size[1]
            # Top-anchored logical subrect of the (possibly bucket-padded)
            # texture: content spans u [0, lw/aw], v [1 - lh/ah, 1].
            taw, tah = _tile_alloc(t)
            uv_a = (0.0, 1.0)
            uv_b = (t.size[0] / taw, 1.0 - t.size[1] / tah)

            imgui.get_window_draw_list().add_image_rounded(t.tex,
                                                           a=a,
                                                           b=b,
                                                           uv_a=uv_a,
                                                           uv_b=uv_b,
                                                           rounding=max(0.0, getattr(draw_state, "corner_radius", 6)))

        imgui.pop_id()
        draw_state.last_seen = Melty.frame_count

    def mark_start_offscreen(self, draw_state) -> bool:



        draw_state._input_value_cache = draw_state._input_value

        if not self.enabled:
            return True

        if not draw_state.use_cache or draw_state._external_change:
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

        if draw_state.width is not None and draw_state.height is not None:
            size = snap_int(draw_state.width), snap_int(draw_state.height)

        self.key_to_parent_key[rkey] = parent_ctx.key if parent_ctx else None
        self.key_to_draw_state[rkey] = draw_state
        parent_key = parent_ctx.key if parent_ctx else None
        if parent_key is not None:
            if parent_key not in self.parent_key_to_child_keys:
                self.parent_key_to_child_keys[parent_key] = {}
            top = draw_state.abs_top
            self.parent_key_to_child_keys[parent_key][rkey] = (top, rkey, draw_state)

        if name is not None:
            name_key = f"{id(collection)}.{name}"
            self.py_id_to_keys.setdefault(name_key, set()).add(rkey)

        if isinstance(input_value, (list, dict, set, deque, MutableMapping)) or hasattr(input_value, "__dict__"):
            self.py_id_to_keys.setdefault(f"{id(input_value)}", set()).add(rkey)

        if f"{id(draw_state)}" not in self.py_id_to_keys:
            self.py_id_to_keys[f"{id(draw_state)}"] = set()
        self.py_id_to_keys[f"{id(draw_state)}"].add(rkey)
        self._register_func_keys(draw_state, rkey)

        imgui.push_id(f"{rkey}{layer}_offscreen")
        imgui.begin_group()
        has_area = size is not None and size[0] != 0 and size[1] != 0

        if draw_state._input_value is UNSET_VALUE or draw_state._bypass_cache:
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

        if draw_state.just_shadow:
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
            return False

        if size is not None and self.enabled and draw_state.frame_count >= 2 and not self._oversized(size):
            t = self._tiles.get(rkey)
            use_image = (t and has_area
                         and (t.size == (size[0], size[1]))
                         and (not self._is_dirty(t))
                         and (not draw_state.size_change))

            if use_image:
                imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))

                a = draw_state.abs_left, draw_state.abs_top
                b = draw_state.abs_left + size[0], draw_state.abs_top + size[1]
                # Top-anchored logical subrect of the (possibly zero-padded)
                # texture: it spans u [0, lw/aw], v [1 - lh/ah, 1].
                taw, tah = _tile_alloc(t)
                uv_a = (0.0, 1.0)
                uv_b = (t.size[0] / taw, 1.0 - t.size[1] / tah)

                imgui.get_window_draw_list().add_image_rounded(t.tex,
                                                               a=a,
                                                               b=b,
                                                               uv_a=uv_a,
                                                               uv_b=uv_b,
                                                               rounding=getattr(draw_state, "corner_radius", 6))

                # Drag-n-drop home slot: when this cached tile contains the
                # dragged item's slot, its pixels there can be stale (the
                # floating window overlapped the slot when the tile was
                # captured). Repaint the blank socket live over the image.
                # Exactly one tile per level contains it - the level that
                # actually blitted; deeper levels were skipped.
                _home = Melty.dnd_home_rect
                if _home is not None:
                    _hx, _hy, _hw, _hh = _home
                    if (a[0] <= _hx and a[1] <= _hy
                            and _hx + _hw <= b[0] and _hy + _hh <= b[1]):
                        from src.lsd.gl_gui.view.core_views import drag_drop as _dnd_mod
                        if draw_state is not _dnd_mod.DragDrop.item_ds:
                            _dnd_mod.DragDrop.draw_home_blank()

                imgui.dummy(size[0], size[1])

                if draw_state.multi_line:
                    imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top + draw_state.content_height + draw_state.header_height))
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

        if draw_state is not None and (not draw_state.use_cache):
            return

        if draw_state is not None and (draw_state._external_change):
            return

        if len(self._stack) > 0:
            ctx = self._stack.pop()

        imgui.pop_id()

        imgui.end_group()
        Melty.tile_id_stack.pop()

        minx, miny = int(ctx.draw_state.abs_left), int(ctx.draw_state.abs_top)

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
        cb = ctx.draw_state.clipped_by_rect if ctx.draw_state is not None else None
        if cb is not None and any(cb):
            clip = ctx.draw_state.abs_clip_rect
        else:
            clip = self._get_current_clip_rect_screen()
        clipped = self._clip_rect(x, y, w, h, clip)
        self._key_to_ctx[ctx.key] = ctx

        corner_radius = getattr(ctx.draw_state, "corner_radius", 6) or 5.0
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

            # If this view's clipped rect or layer changed, the cached layer
            # masks of this view and its non-closable ancestors are stale. Refresh
            # them when interaction has settled so we don't thrash during
            # scroll/resize (mid-drag the size update path already regenerates
            # from fresh rects).
            # settled = (not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1)
            #            and not imgui.is_mouse_down(2) and not Melty.on_drag)
            # if settled:

                # Stop invalidating once the tile is fully filled. Each partial
                # blit (PASS_3) adds its written region into the tile's
                # frame_bbox; once that covers the whole tile, every pixel has
                # been blitted at least once and subsequent frame deltas don't
                # need another pass - they just shift where the cached
                # tile is sampled. This kills the per-frame revalidation storm
                # during sustained scrolls while still letting newly-revealed
                # tiles fill in.
                # self_tile = self._tiles.get(ctx.key)
                # self_unfilled = not self._tile_fully_filled(self_tile)
                #
                # if ctx.draw_state.scroll_visible:
                #     rect = ctx.draw_state.scroll_offset
                #     new_mark_state = (tuple(int(v) for v in rect))
                #     prev_mark_state = self._last_mark_clip.get(ctx.key)
                #     if (prev_mark_state is not None and prev_mark_state != new_mark_state
                #             and self_unfilled):
                #         self.invalidate(ctx.draw_state._tile_id, frame_delta=0,
                #                         note=Note(name="Clip change", reason="",
                #                                   tint=(1, 0.5, 1)))
                #     self._last_mark_clip[ctx.key] = new_mark_state
                #
                # if (ctx.draw_state._parent.scroll_visible
                #         and not ctx.draw_state.scroll_visible):
                #     rect = ctx.draw_state._parent.scroll_offset
                #     new_mark_state = (tuple(int(v) for v in rect))
                #     prev_mark_state = self._last_mark_clip.get(ctx.key)
                #     if (prev_mark_state is not None and prev_mark_state != new_mark_state
                #             and self_unfilled):
                #         self.invalidate_up(ctx.draw_state._tile_id, max_depth=8, frame_delta=2,
                #                            stop_at_filled=True,
                #                            note=Note(name="Clip change",reason="",tint=(1, 0.5, 1)))

                    # self._last_mark_clip[ctx.key] = new_mark_state


        if not self.enabled or ctx.drew_cached or ctx.draw_state.frame_count < 1:
            return

        if ctx.size and ctx.size[0] > 0 and ctx.size[1] > 0:
            if self._oversized(ctx.size):
                # Too large to back with an offscreen texture: drop any stale
                # tile and render this view uncached from here on.
                self._discard_tile(ctx.key)
                return
            t = self._tiles.get(ctx.key)
            if self._dummy_vao is None:
                vao = gl.glGenVertexArrays(1)
                if isinstance(vao, (list, tuple)):
                    vao = vao[0]
                self._dummy_vao = int(vao)

            gl.glBindVertexArray(self._dummy_vao)
            old_size = t.size if t else None

            if (((t is None) or ((int(t.size[0]), int(t.size[1])) != (int(ctx.size[0]), int(ctx.size[1])))) and not imgui.is_mouse_down(0)
                    and not imgui.is_mouse_down(1) and not imgui.is_mouse_down(2)):
                old_t = t
                t = _ensure_tile(t, ctx.size[0], ctx.size[1], frame_id=self._frame_id, draw_state=ctx.draw_state)

                if t is not None and t is old_t:
                    # Within-bucket logical resize: the tile was updated in
                    # place. Ancestors must recompose (their cached pixels and
                    # masks show the old extent), but there is no need for the
                    # depth-4 descendant sweep: skipping it is safe because
                    # whatever caused the size change already invalidated this
                    # view, and invalidate_up force-marks every ancestor on the
                    # parent resize path.
                    self.invalidate(ctx.key, note=Note(name="Logical resize",
                                                       reason=f"{old_size} -> {ctx.size} in bucket",
                                                       tint=(0.5, 1, 0.5)))
                else:
                    reason = f"New size old_size{old_size} new_size{ctx.size}" if old_size else "New tile"
                    reason = "t None" if t is None else reason

                    self.invalidate_up(ctx.key, max_depth=4, note=Note(name="New Tile", reason=reason, tint=(1, 0.5, 0)))
                self._tiles[ctx.key] = t

            if self._is_dirty(t) and (ctx.key not in self._enq_copy_keys):
                self._pending.append(
                    _Pending(draw_state=ctx.draw_state, tile=t, pos=ctx.pos, size=ctx.size, layer=ctx.layer,
                             depth_and_layer=ctx.depth_and_layer, key=ctx.key))
                self._enq_copy_keys.add(ctx.key)

    def _ensure_programs(self):
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

        # Fallback in the compile guard so a hotswap onto a live instance
        # (which already compiled from the pre-uUVRect source) still resolves
        # the location: -1 there, and glUniform4f(-1, ...) is a legal no-op.
        if getattr(self, "_loc_texoffr_uUVRect", None) is None and self._prog_mask_textured_offset_rounded is not None:
            self._loc_texoffr_uUVRect = gl.glGetUniformLocation(
                self._prog_mask_textured_offset_rounded, "uUVRect"
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

        if self._prog_solid is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _SOLID_FS)
            self._prog_solid = _link(vs, fs)
            self._loc_solid_uColor = gl.glGetUniformLocation(self._prog_solid, "uColor")

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
                               corner_radius: float, shadow_margin: float = 0.0,
                               uv_rect=(1.0, 1.0, 0.0, 0.0)):
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
        gl.glUniform1f(self._loc_texoffr_uMargin, shadow_margin)
        gl.glUniform4f(self._loc_texoffr_uUVRect, *uv_rect)

        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

    def _draw_mask_rect(self, r: _Rect, dp_x, dp_y, s_x, s_y, fb_h, use_cached: bool, shadow_margin=0.0):
        """Helper to draw a single mask rect, optionally using cached subtree mask."""
        x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h)
        ix0, iy0 = int(floor(x0)), int(floor(y0))
        ix1, iy1 = int(ceil(x1)), int(ceil(y1))
        iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)

        if iw <= 0 or ih <= 0:
            return

        gl.glViewport(ix0, iy0, iw, ih)

        t = self._tiles.get(r.key)
        can_use_cached = use_cached and (t is not None) and (not self._is_dirty(t)) and (t.mask_tex is not None)

        if can_use_cached:
            # Dead in practice: the sole caller (PASS 2) passes
            # use_cached=False. If ever revived, callers must remap sampling to
            # the tile's logical subrect (see uUVRect in _draw_mask_rect_cached)
            # - bucket-padded textures sampled 0..1 here will render stretched.
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

    def finalize_captures(self, framebuffer_size: Tuple[int, int]) -> None:


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
        for r in local_mask_rects_rev:
            k = r.key

            while k is not None:
                if r.draw_state is not None:
                    subtree_rects_by_root[k].append(r)
                    if not r.draw_state.closable:
                        k = parent_of.get(k)
                    else:
                        k = None

        subtree_pending = []

        # Reverse
        # subtree_rects_by_root_rev = {k: list(reversed(v)) for k, v in subtree_rects_by_root.items()}

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

            for r in local_mask_rects_rev:
                self.apply_blend_mode(r)
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

            # uFBSize is the divisor turning framebuffer-pixel coords into UVs
            # for uSrc/uTopMask/uSubMask. Those textures are allocated at
            # _fb_alloc_size with content corner-anchored at (0,0), so the
            # divisor is the ALLOCATED size, not the logical framebuffer size.
            alloc_w, alloc_h = self._fb_alloc_size
            gl.glUniform2f(self._loc_uFBSize, float(alloc_w), float(alloc_h))
            gl.glUniform1f(self._loc_copy_uDebugScale, float(self.offscreen_scale))
            gl.glUniform1i(self._loc_copy_uCopyDebugMode, self._copy_debug_mode_to_int())

            for p in local_pending_rev:
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

                for r in subtree_rects_by_root.get(p.key, ()):
                    self.apply_blend_mode(r)
                    self._draw_mask_rect_fresh(r, dp_x, dp_y, s_x, s_y, fb_h, float(r.layer) * INV_65535)

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
                        # Write into the top-anchored logical subrect of the
                        # (possibly bucket-padded) texture.
                        t_lw, t_lh = snap_int(p.tile.size[0]), snap_int(p.tile.size[1])
                        t_ah = snap_int(_tile_alloc(p.tile)[1])
                        gl.glViewport(0, t_ah - t_lh, t_lw, t_lh)

                        # Pre-tint: multiplicative blending drifts stale pixels toward blue
                        if Toggles.debug_stale_tint:
                            gl.glEnable(gl.GL_BLEND)
                            gl.glBlendEquation(gl.GL_FUNC_ADD)
                            gl.glBlendFunc(gl.GL_ZERO, gl.GL_SRC_COLOR)
                            gl.glUseProgram(self._prog_solid)
                            gl.glUniform4f(self._loc_solid_uColor, 0.8, 0.8, 1.0, 1.0)
                            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
                            gl.glDisable(gl.GL_BLEND)

                        gl.glUseProgram(self._prog_copy)
                        gl.glActiveTexture(gl.GL_TEXTURE2)
                        gl.glBindTexture(gl.GL_TEXTURE_2D, self._sub_mask_tex)
                        gl.glUniform1i(self._loc_uSubMask, 2)

                        gl.glUniform4f(self._loc_copy_uTint, 1.0, 1.0, 1.0, 1.0)

                        gl.glUniform4f(self._loc_uSrcRectPx, float(x0), float(y0), float(x1), float(y1))
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

                        p.tile.last_clean_frame = self._frame_id
                        p.tile.dirty = self._is_dirty(p.tile)
                        # Track which portion of the tile got pixels this frame
                        # so scroll frame invalidations can stop once the union
                        # covers the whole tile (see _tile_fully_filled).
                        self._accumulate_filled(p.tile, p.draw_state)
                    except Exception as e:
                        print(
                            f"Error copying to tile {p.key}: {e} {p.tile.draw_state.to_dict()} input_value={p.tile.draw_state._input_value}")

            # ================================================================
            # PASS 4: Build tile.mask_tex for each dirty tile (full subtree)
            # ================================================================
            background_depth = 0.001
            background_depth = 0

            for p in local_pending_rev:
                x, y = p.pos
                w, h = p.size
                x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(x, y, w, h, dp_x, dp_y, s_x, s_y, fb_h)

                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._full_sub_mask_fbo)
                gl.glViewport(0, 0, fb_w, fb_h)

                sc_x0, sc_y0 = int(floor(x0)), int(floor(y0))
                sc_x1, sc_y1 = int(ceil(x1)), int(ceil(y1))
                sc_w, sc_h = max(0, sc_x1 - sc_x0), max(0, sc_y1 - sc_y0)

                # clip = p.draw_state.clip_rect
                # clipped = self._clip_rect(x, y, w, h, clip)
                # if clipped:
                #     cx, cy, cw, ch = clipped
                #     cx, cy, cw, ch = self._screen_rect_to_fb_xyxy(cx, cy, cw, ch, dp_x, dp_y, s_x, s_y, fb_h)
                #     sc_x0, sc_y0 = max(0, int(cx)), max(0, int(cy))
                #     sc_w, sc_h = max(1, int(cw)), max(1, int(ch))

                gl.glEnable(gl.GL_SCISSOR_TEST)
                gl.glScissor(sc_x0, sc_y0, sc_w, sc_h)

                gl.glDisable(gl.GL_BLEND)
                gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
                gl.glClearColor(background_depth, 0, 0, 0.0)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)

                for r in subtree_rects_by_root.get(p.key, ()):
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

                    self.apply_blend_mode(r)

                    clip_x0, clip_y0, clip_x1, clip_y1 = self._screen_rect_to_fb_xyxy(
                        r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h
                    )
                    clip_ix0, clip_iy0 = int(floor(clip_x0)), int(floor(clip_y0))
                    clip_ix1, clip_iy1 = int(ceil(clip_x1)), int(ceil(clip_y1))
                    clip_iw, clip_ih = max(0, clip_ix1 - clip_ix0), max(0, clip_iy1 - clip_iy0)

                    # For cached tiles, use actual tile size from context to avoid stretching
                    if use_child_cache:
                        gl.glEnable(gl.GL_SCISSOR_TEST)
                        gl.glScissor(clip_ix0, clip_iy0, clip_iw, clip_ih)
                        gl.glDisable(gl.GL_BLEND)

                        child_ctx = self._key_to_ctx.get(r.key)
                        if child_ctx and child_ctx.size:
                            cx, cy = child_ctx.pos
                            cw, ch = child_ctx.size
                            sx0, sy0, sx1, sy1 = self._screen_rect_to_fb_xyxy(cx, cy, cw, ch, dp_x, dp_y, s_x, s_y,
                                                                              fb_h)
                        else:
                            sx0, sy0, sx1, sy1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y,
                                                                              fb_h)
                    else:
                        gl.glEnable(gl.GL_SCISSOR_TEST)
                        gl.glScissor(clip_ix0, clip_iy0, clip_iw, clip_ih)
                        gl.glDisable(gl.GL_BLEND)
                        child_ctx = self._key_to_ctx.get(r.key)
                        if child_ctx and child_ctx.size:
                            cx, cy = child_ctx.pos
                            cw, ch = child_ctx.size
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
                            r.draw_state.shadow_margin if r.draw_state is not None else 0.0,
                            uv_rect=_tile_uv_rect(t_child),
                        )
                    else:
                        if r.draw_state.parent_window is not None:
                            if r.draw_state.parent_window._is_nested:
                                gl.glEnable(gl.GL_SCISSOR_TEST)
                                gl.glScissor(clip_ix0, clip_iy0, clip_iw, clip_ih)
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

                    # Dst is the top-anchored logical subrect of the (possibly
                    # bucket-padded) mask texture, mirroring PASS 3's viewport.
                    m_lw, m_lh = int(p.tile.size[0]), int(p.tile.size[1])
                    m_ah = int(_tile_alloc(p.tile)[1])
                    gl.glBlitFramebuffer(
                        int(x0),
                        int(y0),
                        int(x1),
                        int(y1),
                        0,
                        m_ah - m_lh,
                        m_lw,
                        m_ah,
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

            # gl.glDisable(gl.GL_BLEND)
            for key in reversed(list((subtree_rects_by_root.keys()))):
                for r in subtree_rects_by_root.get(key, ()):
                    draw_state = self.key_to_draw_state.get(r.key)
                    t = self._tiles.get(r.key)

                    size_change = draw_state.size_change if draw_state else False
                    can_use_cached = (t is not None) and (t.mask_tex is not None) and (not size_change)

                    # abs_clip = draw_state.abs_clip_rect if draw_state else None
                    # abs_clip_w = abs_clip[2] - abs_clip[0] if abs_clip is not None else r.w
                    # abs_clip_h = abs_clip[3] if abs_clip else r.h
                    clip_x0, clip_y0, clip_x1, clip_y1 = self._screen_rect_to_fb_xyxy(
                        r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h
                    )
                    clip_ix0, clip_iy0 = int(floor(clip_x0)), int(floor(clip_y0))
                    clip_ix1, clip_iy1 = int(ceil(clip_x1)), int(ceil(clip_y1))
                    clip_iw, clip_ih = max(0, clip_ix1 - clip_ix0), max(0, clip_iy1 - clip_iy0)

                    tile_ctx = self._key_to_ctx.get(r.key)

                    if (can_use_cached or size_change) and tile_ctx and not draw_state is None:
                        tx, ty = draw_state.abs_left, draw_state.abs_top
                        # if can_use_cached and t is not None:
                        #     tw, th = t.size
                        # else:
                        tw, th = draw_state.width, draw_state.height
                        x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(tx, ty, tw, th, dp_x, dp_y, s_x, s_y, fb_h)
                    else:
                        x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h)

                    ix0, iy0 = int(floor(x0)), int(floor(y0))
                    ix1, iy1 = int(ceil(x1)), int(ceil(y1))
                    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)

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
                                                    r.corner_radius, shadow_margin,
                                                    uv_rect=_tile_uv_rect(t))
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
            self._detect_occluder_changes(self._mask_rects)
            self._pending.clear()
            self._mask_rects.clear()
            self._shadow_rects.clear()
            self._enq_mask_keys.clear()
            self._enq_copy_keys.clear()
            self._cancelled_keys.clear()
            self._recording = False
            self.did_deviate.clear()
            self.seen_ids.clear()
