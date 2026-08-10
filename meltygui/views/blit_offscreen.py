from __future__ import annotations

import random
import traceback
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
from src.lsd.gl_gui.toggles import Toggles, shadow_depth_at
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


def _bucket_h(v: int) -> int:
    """Height bucket, graduated: tall tiles (a 6000px code editor) round to
    256 so per-line content growth (±23px per Enter/Backspace) stays
    within-bucket ~10 keystrokes at a time instead of realloc'ing a
    multi-MB texture every other line. Small tiles keep the tight 32px
    bucket — a 256px floor there would waste ~1MB per short row tile."""
    b = 256 if v >= 1024 else TILE_BUCKET
    return min(MAX_TILE_DIM, ((int(v) + b - 1) // b) * b)


def _bump_note(t, site):
    """DEBUG (perpetual-dirty hunt): rate-limited trace naming WHICH code path
    bumped an armed tile's last_invalidated_frame. Arm a view by setting
    `_bump_trace_armed = True` on its draw_state (the tabs instrumentation in
    new_converters does this for the structured pane). ~one getattr when
    unarmed; remove with the rest of the debug lines when the hunt closes."""
    try:
        ds = getattr(t, "draw_state", None)
        if ds is not None and getattr(ds, "_bump_trace_armed", False):
            from src.lsd.gl_gui.perf_trace import trace_rl
            trace_rl(("bump", id(t), site), f"BUMP {site} name={getattr(ds, 'name', None)!r}",
                     min_interval=0.2)
    except Exception:
        pass


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
    # Clip insets (left, top, right, bottom vs the view rect) the mask was
    # built under. Texels outside the mask clip are 0 (no depth), so when the
    # live clip recedes (a reveal) the tile must re-render before its shadow
    # can cover the newly visible area. draw_tile compares against this.
    mask_clip_insets: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    # Cumulative union (in tile-local coords) of regions blitted from the main
    # framebuffer during the tile's lifetime. None until the first partial blit;
    # once it covers (0,0,size) the tile is fully filled and scroll-driven
    # invalidations can be skipped. Reset on tile recreation and clamped in place
    # on a within-bucket logical resize.
    filled_bbox: Optional[Tuple[int, int, int, int]] = None
    # freeze_resize tiles only: per-axis high-water mark of the logical size -
    # the extent of ever-rendered content still resident in the texture.
    # Shrinks leave those texels in place (no band clear, alloc never
    # shrinks) so a grow-drag's frozen blit can reveal them instead of
    # background. None for normal tiles (logical size == content extent).
    content_size: Optional[Tuple[int, int]] = None
    # Scroll offset those beyond-logical texels were rendered at (clamped on
    # shrink). If the view scrolls away from it while small, those texels no
    # longer line up with the live content - _scrub_stale_content clears them
    # to transparent once (content_bg flags the scrub so repeated scroll
    # frames don't re-clear); the frozen blit paints the real background
    # (content_bg) underneath, so a grow-drag pops in the view's exact bg
    # instead of misaligned stale pixels.
    content_scroll: Optional[Tuple[int, int]] = None
    content_bg: bool = False
    # last_clean_frame value at which the logical-edge scrollbar/outline
    # strips were last cleared (see _scrub_view_edges). A fresh capture bumps
    # last_clean_frame past this, re-arming the clear until the next freeze.
    edge_scrub_frame: int = -1


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


# glClearTexImage (GL 4.4) availability, probed on first use. Zeroing a fresh
# tile's color+mask textures with it is two calls and no FBO/scissor/colormask
# state churn, vs the fallback's full _GLState save/restore per tile - which
# adds up when opening a window creates dozens of tiles in one frame.
_HAS_CLEAR_TEX_IMAGE = None


def _try_clear_tex_images(color_tex: int, mask_tex: int) -> bool:
    global _HAS_CLEAR_TEX_IMAGE
    if _HAS_CLEAR_TEX_IMAGE is False:
        return False
    try:
        gl.glClearTexImage(color_tex, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glClearTexImage(mask_tex, 0, gl.GL_RED, gl.GL_UNSIGNED_SHORT, None)
        _HAS_CLEAR_TEX_IMAGE = True
        return True
    except Exception:
        # Pre-4.4 context or missing entry point: remember and fall back to the
        # FBO clear path. A partial success is fine - the fallback zero-clears
        # both surfaces in full.
        _HAS_CLEAR_TEX_IMAGE = False
        return False


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
    ah = _bucket_h(h)

    # freeze_resize tiles never shrink their allocation: a shrink stays on the
    # in-place path below with the beyond-logical texels left intact, so a
    # later grow-drag can reveal them (see Tile.content_size). Recreate only
    # on a genuine grow past the ever-max alloc.
    no_shrink = bool(getattr(draw_state, "freeze_resize", False))
    if no_shrink and existing:
        eaw, eah = _tile_alloc(existing)
        aw = max(aw, snap_int(eaw))
        ah = max(ah, snap_int(eah))

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
        #
        # freeze_resize tiles deliberately break this invariant: earlier-era
        # texels beyond the logical rect are the feature (revealed during a
        # grow-drag), so the bands are kept and content_size records how far
        # they extend. The settle re-render overwrites them inside the logical
        # rect; anything beyond content_size is creation-cleared transparent.
        if (w > ow or h > oh) and not no_shrink:
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

        # DEBUG (perpetual-dirty hunt): a within-bucket logical resize bumps
        # last_invalidated_frame below with NO Note so no invalidate() call -
        # if a view's size wiggles 1px every frame this is a silent
        # self-sustaining dirty loop (+ request_render forever). Name it.
        try:
            from src.lsd.gl_gui.perf_trace import trace_rl as _ib_trace
            _ib_trace(("inbucket", id(existing)),
                      f"in-bucket resize {existing.size} -> {(w, h)} "
                      f"name={getattr(draw_state, 'name', None)!r} "
                      f"hsrc={getattr(draw_state, '_source', {}).get('height')!r}",
                      min_interval=0.5)
        except Exception:
            pass
        if no_shrink:
            cs = getattr(existing, "content_size", None) or (ow, oh)
            existing.content_size = (min(aw, max(cs[0], w)), min(ah, max(cs[1], h)))
            if w < ow or h < oh:
                # Shrink: the texels now beyond the logical rect are freshly
                # cleared - anchor them to the current scroll so a later
                # scroll can detect they've gone stale.
                so = getattr(draw_state, "scroll_offset", None) or (0, 0)
                existing.content_scroll = (snap_int(so[0]), snap_int(so[1]))
                existing.content_bg = False
        existing.size = (w, h)
        # Always stamp alloc_size: also upgrades a pre-bucketing tile whose
        # exact size happened to be bucket-aligned (getattr fallback saw
        # alloc == size for it).
        existing.alloc_size = (aw, ah)
        _bump_note(existing, "in-bucket-resize")
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
            if no_shrink:
                # Recreate under no_shrink only happens on a genuine grow
                # (aw/ah >= old alloc), so the full content extent - not just
                # the logical size - fits the new surface. Carry it over.
                cs = getattr(existing, "content_size", None) or (ow, oh)
                cw = max(0, min(snap_int(cs[0]), aw))
                ch = max(0, min(snap_int(cs[1]), ah))
            else:
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
        # glTexImage2D(None) contents are undefined; with padding, PASS 4
        # only ever overwrites the logical region, so zero both surfaces once
        # here so edge taps outside the logical boundary read a deterministic
        # transparent / rank 0.
        if not _try_clear_tex_images(new_tex, new_mask_tex):
            st = _GLState()
            try:
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, new_fbo)
                gl.glDisable(gl.GL_SCISSOR_TEST)
                gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
                gl.glClearColor(0, 0, 0, 0.0)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
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
    if no_shrink and existing is not None:
        cs = getattr(existing, "content_size", None) or (ow, oh)
        t.content_size = (min(aw, max(cs[0], w)), min(ah, max(cs[1], h)))
        t.content_scroll = getattr(existing, "content_scroll", None)
        t.content_bg = getattr(existing, "content_bg", False)
    _bump_note(t, "tile-recreate")
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

# add_shadow() marks: rank interpolated across the rect from four per-corner
# values - the peeling effect. Smoothstep-bilinear: smoothstep each UV axis,
# then bilinearly mix the corner ranks with the eased axes. That is
# C1-continuous in 2D (zero gradient at every corner, so each corner's depth
# appears as a wide flat shelf before easing toward its neighbours), monotonic
# between adjacent corners, and exactly hits the corner values. A scalar
# offset is just four equal corners, so this one program serves every case;
# uCornerRadius=0 degenerates to the sharp rect (the SDF also discards
# outside the box).
_SHADOW_GRAD_FS = """
#version 330 core
uniform vec4 uRankCorners;   // (TL, TR, BL, BR), screen orientation
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
    vec2 pixelPos = (vUV - 0.5) * uRectSize;
    vec2 halfSize = uRectSize * 0.5;
    float r = min(uCornerRadius, min(halfSize.x, halfSize.y));
    if (sdRoundedBox(pixelPos, halfSize, r) > 0.0) {
        discard;
    }
    // Ease each axis, then bilinear — see the comment block above.
    vec2 t = vUV * vUV * (3.0 - 2.0 * vUV);
    // vUV.y runs bottom-to-top in the framebuffer, while uRankCorners is
    // authored top-first in screen orientation: t.y=1 is the screen TOP row.
    float top    = mix(uRankCorners.x, uRankCorners.y, t.x);
    float bottom = mix(uRankCorners.z, uRankCorners.w, t.x);
    oColor = vec4(mix(bottom, top, t.y), 0.0, 0.0, 1.0);
}
"""

# add_glow() marks: a rounded-rect emitter with an inverse-square falloff
# skirt - the same "hot core, long faint tail" profile the old draw-list
# _blur_rect approximated with stacked rects, evaluated per-fragment from the
# SDF instead. The quad is the rect EXPANDED by uRadius on every side; inside
# the rect the profile is 1, outside it decays over uRadius pixels:
#   P(t) = ((1/(1+k*t)**2) - floor) / (1 - floor), where t = d / uRadius
# (k = uFalloff, floor = P at t=1, the tail lands exactly at 0).
#
# Depth gating happens HERE, per fragment, against the finished full depth
# mask (PASS 6 runs after PASS 5): light only lands on receivers whose rank
# sits in [uRankLo, uRankHi] - uRankLo is the emitter's ROOT-window surface
# (anything up the window chain, i.e. windows floating underneath it, gets no
# light) and uRankHi the emitter's own surface plus a hair (views floating
# above the emitter mask the glow instead of being lit through). Needing a
# depth mask per mark is why the gate lives in the stamp and not the
# composite: each mark knows its own band; a single shared glow texture
# couldn't carry per-emitter ranges through additive layers.
_GLOW_FS = """
#version 330 core
uniform vec4 uColor;         // rgb = light color, a = intensity
uniform float uRankLo;       // root-window surface rank (mask-normalized)
uniform float uRankHi;       // emitter surface rank + bias (mask-normalized)
uniform sampler2D uDepthMask;// full R16 rank mask (fb-sized)
uniform vec2 uGlowSize;      // glow buffer dims, for gl_FragCoord -> mask UV
uniform vec2 uRectSize;      // EXPANDED quad size in glow-buffer pixels
uniform float uCornerRadius; // corner radius of the inner rect, glow px
uniform float uRadius;       // falloff skirt width, glow px
uniform float uFalloff;      // inverse-square hardness k (0 = linear)
uniform int uDebugSolid;     // 1 = hard rect + 30% skirt (positioning debug)
in vec2 vUV;
out vec4 oColor;

float sdRoundedBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

void main() {
    vec2 pixelPos = (vUV - 0.5) * uRectSize;
    vec2 halfSize = uRectSize * 0.5 - vec2(uRadius);
    float r = min(uCornerRadius, min(halfSize.x, halfSize.y));
    float d = sdRoundedBox(pixelPos, halfSize, r);
    if (d >= uRadius) {
        discard;
    }
    // Receiver depth at this fragment: the glow buffer and the mask cover
    // the same logical framebuffer edge-to-edge, so the fragment's own
    // position over the glow buffer IS the mask UV. (glow_debug_no_mask
    // widens the band to [0, 1] python-side rather than branching here.)
    float receiver = texture(uDepthMask, gl_FragCoord.xy / uGlowSize).r;
    if (receiver < uRankLo || receiver > uRankHi) {
        discard;
    }
    float t = clamp(d / max(uRadius, 1.0), 0.0, 1.0);
    float p;
    if (uDebugSolid == 1) {
        p = (d <= 0.0) ? 1.0 : 0.3;
    } else if (uFalloff > 0.0) {
        float flr = 1.0 / ((1.0 + uFalloff) * (1.0 + uFalloff));
        float iv = 1.0 / ((1.0 + uFalloff * t) * (1.0 + uFalloff * t));
        p = (iv - flr) / (1.0 - flr);
    } else {
        p = 1.0 - t;
    }
    oColor = vec4(uColor.rgb * uColor.a * p, 1.0);
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
        # Rank clamp to the FULL layer budget (root band + nested band):
        # z_pos = layer * max_depth + rank, so the max layer rank is
        # nested_layer_max * max_depth (6144 with 192 * 32). Anything tied at
        # the max loses R16 depth-mask ordering - this used to sit at 2048
        # (= 64 * 32), which is exactly where deep nested layers sometimes
        # z-fought. R16 holds 65535 ranks, so there's ample headroom.
        self._LAYER_MIN = -(Melty.nested_layer_max * Melty.max_depth)
        self._LAYER_MAX = Melty.nested_layer_max * Melty.max_depth

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
        # Standalone shadow marks from add_shadow(): (x, y, w, h,
        # rank, (tl, tr, bl, br), corner_radius, margin, clip_xyxy,
        # tile_key, inset).
        # Stamped into _full_mask_tex at the end of PASS 5 (this frame's
        # shadow), and into any pending enclosing tile's cached mask in
        # PASS 4 (so the mark survives frames where the tile's mask is
        # cache-served). No key, no draw_state, never in the flat mask.
        self._shadow_rects: List[tuple] = []

        # Glow marks from add_glow(). Unlike shadow marks they carry COLOR, so
        # they can't bake into the R16 tile masks - persistence across
        # cache-served frames is CPU-side instead, keyed by the EMITTING
        # draw_state (never by tile-capture lifecycle: a window tile can
        # partially recapture without the emitting's body running, so
        # tile-keyed retention dropped marks on any sibling invalidation).
        # Protocol: an emitter's body calls clear_glows() after each run, then
        # re-emits; marks persist untouched while the body is cache-skipped.
        # Each entry: (mark_tuple, emitter_ds,_id, anchor_xy).
        self._glow_rects: List[tuple] = []
        # id(ds) -> (mark, ds, anchor): marks from the emitter's last body
        # run, with its abs pos at record time so a later move re-stamps at
        # the live offset. Entries die on clear_glows-without-re-emit or when
        # the emitter's window closes.
        self._glow_marks_by_emitter: Dict[int, tuple] = {}
        # id(ds) set: emitters whose bodies ran this frame (clear_glows) -
        # their retained entries drop before this frame's emissions re-add.
        self._glow_cleared: set = set()
        # Low-res additive light buffer (RGBA16F): rgb = accumulated glow
        # light, a = MAX-blended emitter rank (full-mask depth) so the
        # emitter can occlude glow under windows floating above the emitter.
        self._glow_tex: Optional[int] = None
        self._glow_fbo: Optional[int] = None
        self._glow_size: Tuple[int, int] = (0, 0)
        self._glow_tex_empty: bool = True
        self._prog_glow: Optional[int] = None
        self._loc_gl_uColor = None
        self._loc_gl_uRankLo = None
        self._loc_gl_uRankHi = None
        self._loc_gl_uDepthMask = None
        self._loc_gl_uGlowSize = None
        self._loc_gl_uRectSize = None
        self._loc_gl_uCornerRadius = None
        self._loc_gl_uRadius = None
        self._loc_gl_uFalloff = None

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

        self._prog_shadow_grad: Optional[int] = None
        self._loc_sg_uRankCorners = None
        self._loc_sg_uRectSize = None
        self._loc_sg_uCornerRadius = None
        self._loc_sg_uMargin = None
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
        # Scroll-settle deferral for _detect_occluder_changes: last seen
        # Melty.scroll_version and the frame it changed on.
        self._occ_scroll_version: int = -1
        self._occ_scroll_frame: int = -10**9

    @property
    def full_mask_tex(self) -> Optional[int]:
        return self._full_mask_tex

    @property
    def glow_tex(self) -> Optional[int]:
        """The low-res glow light buffer, or None before any glow stamped."""
        return getattr(self, "_glow_tex", None)

    @property
    def glow_active(self) -> bool:
        """True when the glow buffer holds any light this frame."""
        return (getattr(self, "_glow_tex", None) is not None
                and not getattr(self, "_glow_tex_empty", True))

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
        # Sort by LIVE abs_top - the same value is_inside_clip uses below -
        # not the tuple's stored top, which is a snapshot from tile
        # registration and diverges from live geometry by the accumulated
        # scroll/move since (observed: stored -892 vs live 1951). The sorted
        # walk's below-early-return is only sound when the order matches the
        # clip test's coordinates; with stale keys one wrongly-first child
        # that is live-below aborted the whole cascade, leaving every visible
        # descendant un-invalidated (stale composition a hover invalidate).
        # None tops sort first: is_inside_clip treats them as inside, and they
        # will never trigger the early-return for the children after them.
        def _live_top(entry):
            ds = entry[2]
            top = ds.abs_top if ds is not None else None
            return top if top is not None else float("-inf")
        child_keys_list.sort(key=_live_top)

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
                        _bump_note(pt, f"casc-from:{k[:48]}:{getattr(note, 'name', None)}")
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
            # Throttle: at most one trace per 100 frames. last_print_invalidate
            # is stamped only when a trace actually prints - stamping it on
            # every invalidate left frames_since == 0 for every same-frame
            # invalidate after the first, which printed a full (inspect +
            # pygments) trace per invalidated tile and dominated frame time
            # during scroll.
            frames_since_last_print = Melty.frame_count - Melty.last_print_invalidate
            if Melty.frame_count > 100 and frames_since_last_print > 100:
                if note.name != "hover change":
                    print_stack_trace()
                    if draw_state is not None:
                        print("View_func", draw_state._view_func.__name__)
                    Melty.last_print_invalidate = Melty.frame_count

        t = self._tiles.get(k)
        if t is not None:
            target_frame = self._frame_id + 1
            _bump_note(t, f"invalidate:{getattr(note, 'name', None)}")
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
                    _bump_note(pt, f"anc-of:{k[:48]}:{getattr(note, 'name', None)}")
                    pt.last_invalidated_frame = max(pt.last_invalidated_frame, self._frame_id + 1 + frame_delta)
                    pt.dirty = self._is_dirty(pt)
                    if Toggles.InvalidateTracker.enable:
                        InvalidateTracker.invalidations[k] = note
                    if stop_at_filled and self._tile_fully_filled(pt):
                        break

    def invalidate_all(self) -> None:
        for t in self._tiles.values():
            if t is not None:
                _bump_note(t, "invalidate_all")
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

        # Scroll frames translate their root windows every frame; diffing
        # occluders then would force-invalidate the moving window and every
        # window it uncovers per frame, tanking scroll fps. Hold the
        # pre-scroll baseline (_prev_occluders untouched, same in effect as
        # the mouse-down early-out above) and run one diff after the scroll
        # has settled for a few frames. request_render keeps frames pumping
        # so the delayed diff actually gets a frame to run in.
        if Melty.scroll_version != getattr(self, "_occ_scroll_version", -1):
            self._occ_scroll_version = Melty.scroll_version
            self._occ_scroll_frame = Melty.frame_count
            request_render()
            return
        if Melty.frame_count - getattr(self, "_occ_scroll_frame", -10**9) < 8:
            request_render()
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
        if getattr(self, "_glow_fbo", None):
            gl.glDeleteFramebuffers(1, [self._glow_fbo])
            self._glow_fbo = None
        if getattr(self, "_glow_tex", None):
            gl.glDeleteTextures(1, [self._glow_tex])
            self._glow_tex = None
        if getattr(self, "_prog_glow", None):
            gl.glDeleteProgram(self._prog_glow)
            self._prog_glow = None
        if getattr(self, "_glow_marks_by_emitter", None):
            self._glow_marks_by_emitter.clear()
        if getattr(self, "_glow_cleared", None):
            self._glow_cleared.clear()
        self._glow_size = (0, 0)
        self._glow_tex_empty = True

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
        self._shadow_rects.clear()
        if getattr(self, "_glow_rects", None) is not None:
            self._glow_rects.clear()
        if getattr(self, "_glow_cleared", None) is not None:
            self._glow_cleared.clear()
        if getattr(self, "_depth_frame", None) is not None:
            self._depth_frame.clear()
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

    def add_shadow(
            self, rect: Tuple[float, float, float, float], offset: float = 2.0,
            layer: int = None, depth: int = None, corner_radius: float = 5.0,
            margin: float = 0.0, clip: bool = True, draw_state=None,
    ) -> None:
        """Mark a screen-space rect as a shadow caster, for code that is not
        a @render_func (raw draw-list overlays, dock rows, drag ghosts).

        `offset` is the SIGNED depth delta from the surrounding surface
        (depth, default Melty.shadow_depth). Positive lifts the rect so it
        casts onto its surroundings (default +2, the legacy active-button
        z_offset). NEGATIVE carves a recess: the mark is MIN-blended (it can
        only lower depth), so the surrounding surface casts INTO the rect —
        the sunken-widget look.

        `offset` may also be a 4-tuple (top_left, top_right, bottom_left,
        bottom_right): each corner gets its own depth and the mark's depth
        eases between them across the quad (smoothstep-bilinear — C1-smooth
        in 2D, exact at the corners), so e.g. (0, 0, 0, 8) reads as the
        bottom-right corner peeling up off the surface with the shadow
        widening toward it. All corners <= 0 carves a graded recess
        (MIN-blended, owner-scoped like any inset mark). Mixed signs stamp
        MAX-blended: the raised portion casts, while regions easing below
        the surrounding surface clamp to it rather than carving — stamp a
        second all-negative mark if you want true mixed relief.

        Raised marks stamp into the full depth mask at the end of PASS 5,
        max-blended so they never lower an existing (higher) window mark.
        Neither kind touches the flat mask, so a mark cannot steal tile
        pixels or invalidate anything — safe to call every frame. To keep a
        recess carve from cutting into unrelated windows floating above, an
        owned negative mark lands only in its own tile's cached mask
        (PASS 4); the full mask picks it up through the cached-mask path.
        Ownerless negative marks (no tile recording) stamp the full mask
        directly and are the caller's responsibility to keep topmost.

        Persistence: if the call happens while a tile is recording, the mark
        also bakes into that tile's cached mask (PASS 4), so it keeps casting
        on frames where the caller's body is cache-served — the same
        persistence regular view marks get, and it ages out naturally on the
        owner's next fresh capture.

        rect is (x, y, w, h) in screen coords. layer defaults to
        Melty.active_layer, depth to Melty.shadow_depth, both read at call
        time. clip=True snapshots the LIVE clip rect now and scissors the
        mark with it at draw time; an explicit (x0, y0, x1, y1) tuple clips
        to that rect instead (e.g. a draw-list view's abs_clip_rect);
        False/None disables clipping.
        """
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            return
        if layer is None:
            layer = Melty.active_layer
        if depth is None:
            depth = Melty.shadow_depth
        if clip is True:
            clip_xyxy = Melty.get_clip_rect()
        elif clip:
            clip_xyxy = tuple(clip)
        else:
            clip_xyxy = None
        if clip_xyxy is not None and self._fully_clipped(x, y, w, h, clip_xyxy):
            return
        owner_key = self._stack[-1].key if self._stack else None
        if isinstance(offset, (tuple, list)):
            offs = tuple(float(o) for o in offset)
            if len(offs) != 4:
                raise ValueError(
                    "add_shadow offset must be a scalar or a 4-tuple "
                    "(top_left, top_right, bottom_left, bottom_right)")
        else:
            offs = (float(offset),) * 4
        # MIN-blend (recess) only when the whole quad is at-or-below the
        # surface; any raised corner stamps MAX so the mark doesn't carve
        # neighbours it eases across.
        inset = all(o <= 0 for o in offs) and any(o < 0 for o in offs)
        ranks = tuple(max(0.0, shadow_depth_at(depth + o, layer))
                      for o in offs)
        mark = (x, y, w, h, ranks, corner_radius, margin, clip_xyxy,
                owner_key, inset)
        self._shadow_rects.append(mark)
        # Emitter-keyed retention, mirroring add_glow: on frames where the
        # enclosing tile rebuilds its mask WITHOUT this body running (a
        # neighbour invalidated), the baked raised detail (block peels, chip
        # lifts) drops out of the full mask - shadows and the glow gate
        # stay flat until the body finally runs. Retained raised marks
        # re-stamp into the full mask every finalize (MAX blend =
        # idempotent), keeping them complete on partial-rebuild frames.
        # Insets stay unretained: MIN carves need their owner-scoped
        # cached-mask path to avoid cutting windows floating above.
        if draw_state is not None and not inset:
            self._ensure_glow_state()
            self._depth_frame.append(
                (mark, draw_state,
                 (draw_state.abs_left, draw_state.abs_top)))

    def _ensure_glow_state(self) -> None:
        """Lazily create the glow bookkeeping fields. A hotswap patches
        methods onto a live instance whose __init__ predates them — skipping
        glow silently (or crashing on a missing attr) until the next restart
        is exactly the failure mode this heals: state just starts empty."""
        if getattr(self, "_glow_rects", None) is None:
            self._glow_rects = []
        if getattr(self, "_glow_marks_by_emitter", None) is None:
            self._glow_marks_by_emitter = {}
        if getattr(self, "_glow_cleared", None) is None:
            self._glow_cleared = set()
        if getattr(self, "_glow_tex", "MISSING") == "MISSING":
            self._glow_tex = None
            self._glow_fbo = None
            self._glow_size = (0, 0)
            self._glow_tex_empty = True
        if getattr(self, "_depth_frame", None) is None:
            # Retained depth (add_shadow) marks: same lifecycle as glow -
            # frame list and per-emitter store, cleared/killed by the same
            # protocol.
            self._depth_frame = []
            self._depth_marks_by_emitter = {}
        if getattr(self, "_glow_kill_pending", None) is None:
            # Territory kills observed mid-interaction (mouse down / drag)
            # are DEFERRED here instead of executing - resize drags pass
            # over live glow territory constantly and killing there lost
            # the glow for the whole drag. Flags fire on release unless
            # the emitter re-emitted meanwhile (resize release re-renders
            # the editor, clearing its flag; a quick tab-switch never
            # re-emits, so its flag kills on release).
            self._glow_kill_pending = set()
            self._depth_kill_pending = set()

    @staticmethod
    def _glow_root_ds(ds):
        """ROOT window draw_state enclosing `ds` (ds itself if un-windowed).
        Its live `shadow_depth` property is the glow's floor anchor — the
        same scalar rank its mask rect stamps, so the floor is ALWAYS in
        mask units. (Never derive layers from z_pos: that's the fine-grained
        z clamped to 2048, a different unit from active_layer — feeding it
        into shadow_depth_at put the floor near rank 1.0 and emptied the
        receiver band.)"""
        w = ds
        hops = 0
        while w is not None and hops < 64:
            pw = getattr(w, "parent_window", None)
            if pw is None or pw is w:
                break
            w = pw
            hops += 1
        return w

    def clear_glows(self, draw_state) -> None:
        """Start of an emitter's glow group for this body run: retained marks
        from its previous run drop at finalize unless re-emitted this frame.
        Call unconditionally at the top of any body that MAY add_glow — a run
        that then emits nothing (feature toggled off, content changed) clears
        its stale glow, while cache-skipped runs never reach this and keep
        glowing."""
        self._ensure_glow_state()
        self._glow_cleared.add(id(draw_state))
        # Also drop marks this SAME ds emitted earlier THIS frame: bodies
        # can run twice a frame (double layout/render passes), and both
        # runs' emissions would otherwise accumulate into the retained
        # buffer - the glow stamped at 2x intensity because the view
        # rendered live, then snapped back to 1x for the cached blit. The
        # last body run is authoritative.
        if self._glow_rects:
            self._glow_rects[:] = [e for e in self._glow_rects
                                   if e[1] is not draw_state]
        if self._depth_frame:
            self._depth_frame[:] = [e for e in self._depth_frame
                                    if e[1] is not draw_state]

    def add_glow(
            self, rect: Tuple[float, float, float, float],
            color: Tuple[float, float, float], intensity: float = 1.0,
            radius: float = 24.0, falloff: float = 2.0, offset: float = 2.0,
            layer: int = None, depth: int = None, corner_radius: float = 3.0,
            clip: bool = True, draw_state=None,
    ) -> None:
        """Mark a screen-space rect as a GLOWING light emitter. The rect is
        rendered into the low-res glow light buffer with an inverse-square
        falloff skirt of `radius` px, and the shadow composite adds the
        result as emitted light: it brightens what it lands on and pushes
        back shadow there — the surroundings read as lit by the rect.

        `color` is the light's rgb (0-1); `intensity` scales it. `falloff` is
        the inverse-square hardness (same knob as the old draw-list blur's
        def_line_blur_falloff; 0 = linear fade). `offset`/`layer`/`depth`
        place the EMITTING surface in the depth mask's rank space exactly like
        add_shadow. The light lands only on receivers between the emitter's
        ROOT window surface and the emitter's own surface (per-fragment gate
        against the full depth mask in the stamp shader): windows behind the
        chain get nothing, views floating above the emitter mask it out.
        clip follows add_shadow's convention (True = snapshot the live clip
        now, tuple = explicit, falsy = none).

        Persistence: pass the emitting view's `draw_state` and the mark is
        retained across cache-served frames, re-stamped at the view's live
        position; pair with clear_glows(draw_state) at the top of the body so
        a run that stops emitting drops its stale glow. Without a draw_state
        the mark lasts one frame — re-call every frame, same as add_shadow's
        drag-ghost usage."""
        self._ensure_glow_state()
        x, y, w, h = rect
        if w <= 0 or h <= 0 or intensity <= 0:
            return
        if layer is None:
            layer = Melty.active_layer
        if depth is None:
            depth = Melty.shadow_depth
        if clip is True:
            clip_xyxy = Melty.get_clip_rect()
        elif clip:
            clip_xyxy = tuple(clip)
        else:
            clip_xyxy = None
        if clip_xyxy is not None and self._fully_clipped(
                x - radius, y - radius, w + 2 * radius, h + 2 * radius,
                clip_xyxy):
            return
        # Ranks are NOT frozen here - PASS 6 anchors the glow on the LIVE
        # `shadow_depth` of the emitting view and its root window (the same
        # scalar ranks their mask rects stamp), so the band always matches
        # the mask's units and tracks z changes. The mark stores only the
        # RELATIVE offset above the emitting view's surface, plus a
        # record-time absolute rank as the fallback anchor for emitterless
        # marks (no draw_state to read live).
        anchor = (0.0, 0.0)
        if draw_state is not None:
            anchor = (draw_state.abs_left, draw_state.abs_top)
        abs_rank = max(0.0, shadow_depth_at(depth + offset, layer))
        rgb = (float(color[0]), float(color[1]), float(color[2]))
        mark = (x, y, w, h, rgb, float(intensity),
                float(offset), abs_rank,
                float(radius), float(falloff), float(corner_radius),
                clip_xyxy)
        self._glow_rects.append((mark, draw_state, anchor))

    def _ensure_glow_target(self, fb_w: int, fb_h: int) -> None:
        """(Re)allocate the low-res RGBA16F glow light buffer at the current
        framebuffer size / Toggles.glow_downscale. 16F because alpha carries
        the emitter rank in full-mask units — 8 bits there would re-introduce
        the quantized-depth wobble the R16 mask migration removed."""
        ds_f = max(1, int(getattr(Toggles, "glow_downscale", 4)))
        gw = max(1, int(fb_w) // ds_f)
        gh = max(1, int(fb_h) // ds_f)
        if self._glow_tex is not None and (gw, gh) == self._glow_size:
            return
        if self._glow_tex is None:
            self._glow_tex = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._glow_tex)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_BORDER)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_BORDER)
            gl.glTexParameterfv(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_BORDER_COLOR, [0.0, 0.0, 0.0, 0.0])
        else:
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._glow_tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, gw, gh, 0,
                        gl.GL_RGBA, gl.GL_FLOAT, None)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        if self._glow_fbo is None:
            self._glow_fbo, _ = _create_fbo_with_tex(self._glow_tex, False, gw, gh)
        self._glow_size = (gw, gh)
        self._glow_tex_empty = True

    def _stamp_glow_marks(self, glows, dp_x, dp_y, s_x, s_y, fb_w, fb_h):
        """Clear the glow buffer and draw glow marks into it. Each entry is
        (mark, delta, rank, floor, live_clip): mark as built by add_glow
        (x/y/clip in screen coords), delta the emitter's live-position
        offset since record, rank/floor the mask-normalized receiver band
        resolved from LIVE draw_state ranks in PASS 6, live_clip the
        emitter's live rect. Clips apply to the ORIGIN rect only — the
        falloff skirt of whatever survives spills unclipped. Depth gating
        happens per fragment in the shader against the full mask (bound on
        unit 0 — PASS 5 is complete by now). RGB is plain additive; alpha
        is unused. Leaves blend/scissor disabled."""
        self._ensure_glow_target(fb_w, fb_h)
        gw, gh = self._glow_size
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._glow_fbo)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glViewport(0, 0, gw, gh)
        gl.glClearColor(0, 0, 0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        self._glow_tex_empty = not glows
        if not glows:
            return
        # Screen px -> glow-buffer px: the fb transform, then the downscale.
        sc_x = gw / max(1.0, float(fb_w))
        sc_y = gh / max(1.0, float(fb_h))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
        gl.glUseProgram(self._prog_glow)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._full_mask_tex)
        gl.glUniform1i(self._loc_gl_uDepthMask, 0)
        gl.glUniform2f(self._loc_gl_uGlowSize, float(gw), float(gh))
        _dbg_no_mask = getattr(Toggles, "glow_debug_no_mask", False)
        gl.glUniform1i(self._loc_gl_uDebugSolid,
                       1 if getattr(Toggles, "glow_debug_rects", False) else 0)
        # Small rank slack above the emitter so coplanar pixels (the band's
        # own glow, sibling text at the same depth) stay lit through R16
        # rounding; ~2 rank units.
        _bias = 2.0 / 65535.5
        for ((sx, sy, sw, sh, rgb, inten, _d_off, _layer_rec, radius, falloff,
              cr, clip_xyxy), delta, rank, floor_rank, live_clip) in glows:
            sx += delta[0]
            sy += delta[1]
            # Clip the ORIGIN rect, never the rendered light: intersect the
            # original rect with the recorded clip (delta-edplied) and the
            # emitter's live rect, then let the clipped rect's skirt spill
            # through - the visible part of the band still lights past a
            # column seam, and a scrolled/clipped-out band emits nothing.
            # (The live-rect term is what makes a freeze-resize column drag
            # track: the body doesn't re-run mid-drag, but resize does.)
            ex1, ey1 = sx + sw, sy + sh
            if clip_xyxy is not None:
                sx = max(sx, clip_xyxy[0] + delta[0])
                sy = max(sy, clip_xyxy[1] + delta[1])
                ex1 = min(ex1, clip_xyxy[2] + delta[0])
                ey1 = min(ey1, clip_xyxy[3] + delta[1])
            if live_clip is not None:
                sx = max(sx, live_clip[0])
                sy = max(sy, live_clip[1])
                ex1 = min(ex1, live_clip[2])
                ey1 = min(ey1, live_clip[3])
            sw, sh = ex1 - sx, ey1 - sy
            if sw <= 0 or sh <= 0:
                continue
            x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(
                sx - radius, sy - radius, sw + 2 * radius, sh + 2 * radius,
                dp_x, dp_y, s_x, s_y, fb_h)
            ix0, iy0 = int(floor(x0 * sc_x)), int(floor(y0 * sc_y))
            ix1, iy1 = int(ceil(x1 * sc_x)), int(ceil(y1 * sc_y))
            iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
            if iw <= 0 or ih <= 0:
                continue
            gl.glViewport(ix0, iy0, iw, ih)
            gl.glUniform4f(self._loc_gl_uColor, rgb[0], rgb[1], rgb[2],
                           float(inten))
            if _dbg_no_mask:
                gl.glUniform1f(self._loc_gl_uRankLo, 0.0)
                gl.glUniform1f(self._loc_gl_uRankHi, 1.0)
            else:
                gl.glUniform1f(self._loc_gl_uRankLo,
                               max(0.0, float(floor_rank) - _bias))
                gl.glUniform1f(self._loc_gl_uRankHi, float(rank) + _bias)
            gl.glUniform2f(self._loc_gl_uRectSize, float(iw), float(ih))
            gl.glUniform1f(self._loc_gl_uCornerRadius,
                           max(0.0, cr * s_x * sc_x))
            gl.glUniform1f(self._loc_gl_uRadius,
                           max(1.0, radius * s_x * sc_x))
            gl.glUniform1f(self._loc_gl_uFalloff, max(0.0, falloff))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    def _stamp_shadow_marks(self, shadows, dp_x, dp_y, s_x, s_y, fb_h,
                            scissor_fb=None):
        """Draw add_shadow() marks into the currently bound R16 mask FBO.
        Raised marks MAX-blend (can only raise depth), inset marks MIN-blend
        (can only lower it — the recess carve); either way stamping the same
        mark into several masks (tile caches in PASS 4 + the full mask in
        PASS 5) is idempotent, and the rounded shader discards outside its
        SDF so MIN never punches the quad corners. scissor_fb optionally
        intersects every mark's own clip with an outer (x0, y0, x1, y1)
        framebuffer-space rect (PASS 4's tile rect). Leaves scissor disabled
        and blend restored to FUNC_ADD/off."""
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
        for (sx, sy, sw, sh, d_and_l, cr, margin, clip_xyxy, _owner,
             inset) in shadows:
            gl.glBlendEquation(gl.GL_MIN if inset else gl.GL_MAX)
            x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(
                sx, sy, sw, sh, dp_x, dp_y, s_x, s_y, fb_h)
            ix0, iy0 = int(floor(x0)), int(floor(y0))
            ix1, iy1 = int(ceil(x1)), int(ceil(y1))
            iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
            if iw <= 0 or ih <= 0:
                continue
            sc = scissor_fb
            if clip_xyxy is not None:
                cx0, cy0, cx1, cy1 = clip_xyxy
                fx0, fy0, fx1, fy1 = self._screen_rect_to_fb_xyxy(
                    cx0, cy0, cx1 - cx0, cy1 - cy0, dp_x, dp_y, s_x, s_y, fb_h)
                sc = ((fx0, fy0, fx1, fy1) if sc is None else
                      (max(sc[0], fx0), max(sc[1], fy0),
                       min(sc[2], fx1), min(sc[3], fy1)))
            if sc is not None:
                scx0, scy0 = int(floor(sc[0])), int(floor(sc[1]))
                scw = int(ceil(sc[2])) - scx0
                sch = int(ceil(sc[3])) - scy0
                if scw <= 0 or sch <= 0:
                    continue
                gl.glEnable(gl.GL_SCISSOR_TEST)
                gl.glScissor(scx0, scy0, scw, sch)
            else:
                gl.glDisable(gl.GL_SCISSOR_TEST)
            if (iw > 4096 or ih > 4096) and sc is not None:
                # A mark the size of a whole cache block can exceed GL
                # viewport limits. Clamp it to the scissor plus a margin
                # (off-screen depth still leaks into view near the edges),
                # re-evaluating the corner ranks at the clamped edges with
                # the same smoothstep-bilinear the shader applies - exact at
                # the new corners, the shader re-eases the interior, which
                # only nudges mid-span values.
                _m = 256.0
                nx0 = max(ix0, int(floor(sc[0] - _m)))
                ny0 = max(iy0, int(floor(sc[1] - _m)))
                nx1 = min(ix1, int(ceil(sc[2] + _m)))
                ny1 = min(iy1, int(ceil(sc[3] + _m)))
                if nx1 <= nx0 or ny1 <= ny0:
                    continue

                def _ss(t):
                    t = min(1.0, max(0.0, t))
                    return t * t * (3.0 - 2.0 * t)

                _tl, _tr, _bl, _br = d_and_l

                def _ev(su, sv):
                    top = _tl + (_tr - _tl) * su
                    bot = _bl + (_br - _bl) * su
                    return bot + (top - bot) * sv
                su0, su1 = _ss((nx0 - ix0) / iw), _ss((nx1 - ix0) / iw)
                sv0, sv1 = _ss((ny0 - iy0) / ih), _ss((ny1 - iy0) / ih)
                d_and_l = (_ev(su0, sv1), _ev(su1, sv1),
                           _ev(su0, sv0), _ev(su1, sv0))
                ix0, iy0, ix1, iy1 = nx0, ny0, nx1, ny1
                iw, ih = ix1 - ix0, iy1 - iy0
            gl.glViewport(ix0, iy0, iw, ih)
            # d_and_l is the per-corner rank 4-tuple (tl, tr, bl, br); the
            # gradient shader eases between them (smoothstep-bilinear), and a
            # scalar offset arrives as four equal corners - one shader for
            # every mark, cr=0 just means sharp corners.
            gl.glUseProgram(self._prog_shadow_grad)
            gl.glUniform4f(self._loc_sg_uRankCorners,
                           float(d_and_l[0]) / 65535.5,
                           float(d_and_l[1]) / 65535.5,
                           float(d_and_l[2]) / 65535.5,
                           float(d_and_l[3]) / 65535.5)
            gl.glUniform2f(self._loc_sg_uRectSize, float(iw), float(ih))
            gl.glUniform1f(self._loc_sg_uCornerRadius, max(0.0, cr))
            gl.glUniform1f(self._loc_sg_uMargin, margin)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glBlendEquation(gl.GL_FUNC_ADD)
        gl.glDisable(gl.GL_BLEND)

    def _shadows_owned_by(self, root_key):
        """add_shadow() marks whose owner tile sits in root_key's subtree
        (owner chains up to root_key through key_to_parent_key)."""
        owned = []
        parent_of = self.key_to_parent_key
        for s in self._shadow_rects:
            k = s[8]
            hops = 0
            while k is not None and k != root_key and hops < 64:
                k = parent_of.get(k)
                hops += 1
            if k == root_key:
                owned.append(s)
        return owned

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
            # LIVE clip, never clipped_by_rect/abs_clip_rect: those are only
            # refreshed when the view actually re-renders, so after a parent
            # resize the stale clip either spills this view's cached depths
            # outside the parent (stomping windows behind it) or crops the
            # mask short of the revealed area. The mark must match what
            # imgui clips the blitted image to - the current live clip.
            clip = self._get_current_clip_rect_screen()
            clipped = self._clip_rect(x, y, w, h, clip)

            # Reveal detection: the baked mask has 0-depth texels outside the
            # clip it was built under, so a receding clip needs a re-render
            # before shadows can cover the revealed strip. Edge-triggered and
            # deferred until interaction settles - not per-frame.
            if t is not None and t.mask_tex is not None:
                if clip is not None:
                    live_insets = (max(0.0, clip[0] - x), max(0.0, clip[1] - y),
                                   max(0.0, (x + w) - clip[2]), max(0.0, (y + h) - clip[3]))
                else:
                    live_insets = (0.0, 0.0, 0.0, 0.0)
                baked_insets = getattr(t, "mask_clip_insets", (0.0, 0.0, 0.0, 0.0))
                revealed = any(li < bi - 0.5 for li, bi in zip(live_insets, baked_insets))
                settled = (not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1)
                           and not imgui.is_mouse_down(2) and not Melty.on_drag)
                if revealed and settled:
                    t.mask_clip_insets = live_insets
                    self.invalidate(rkey, note=Note(name="Clip reveal",
                                                    reason=f"insets {baked_insets} -> {live_insets}",
                                                    tint=(1, 0.5, 1)))
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

    def draw_freeze_bg(self, draw_state, left, top, width, height, live: bool):
        """Single owner of background rendering for freeze_resize views — the
        wrapper's show_bg block delegates here instead of calling draw_bg
        itself, so live and frozen frames share one code path and can never
        drift apart in color or geometry.

        live=True: called by core_render at the exact frame position the
        show_bg block draws, with the globals draw_bg reads (bg_depth ramp,
        bg_stack bleed, style tint) in their correct state — draw with them
        and capture them into _frozen_bg_kwargs. live=False: called by the
        frozen blit at mark_start time, where those globals belong to a
        different stack position — replay the captured state around the call
        (same save/restore pattern as the deferred-window pass in melty.py).

        Note: PASS 3 snapshots the framebuffer with alpha forced to 1, so the
        bg drawn on live frames is still baked into the tile like any other
        pixel. Ownership buys a single code path, not a transparent tile.
        Returns draw_bg's (changed, bg_color) or None when there is no bg to
        draw — the wrapper uses bg_color for Melty.bg_color_stack."""
        fb = getattr(draw_state, "_frozen_bg_kwargs", None)
        if (not fb or not fb.get("show_bg")
                or width is None or height is None or width <= 5 or height <= 5):
            return None
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_bg
        style_manager = Melty.global_attrs['style_manager']
        if live:
            fb.update({
                "depth": Melty.shadow_depth,
                "bg_depth": Melty.bg_depth,
                "bg_stack": copy(Melty.bg_stack),
                "style_tint": style_manager.get_tint(),
            })
        _sv_depth, _sv_stack = Melty.bg_depth, Melty.bg_stack
        _sv_tint = style_manager.get_tint()
        if not live:
            Melty.bg_depth = fb.get("bg_depth", _sv_depth)
            if fb.get("bg_stack") is not None:
                Melty.bg_stack = fb["bg_stack"]
            if fb.get("style_tint") is not None:
                style_manager.set_imgui_tint(*fb["style_tint"])
        try:
            # outline=False: freeze views render no outline at all - the baked
            # outline in the tile is what floats as a stamped ghost during
            # drags, and clipping it out proved fragile (AA feather, corner
            # arcs). No outline drawn -> none captured -> nothing to clip.
            return draw_bg(bypass=True, left=left, top=top,
                           width=width, height=height, outline=False,
                           rounding=getattr(draw_state, "corner_radius", 6),
                           bg_offset=fb.get("bg_offset", 0),
                           max_bg_depth=fb.get("max_bg_depth", None),
                           max_bg_value=fb.get("max_bg_value", None),
                           depth=fb.get("depth", Melty.shadow_depth),
                           selected=False,
                           opacity=1.0, saturation=fb.get("saturation", 1.0),
                           pressed=False,
                           style_manager=style_manager,
                           nested_bg=fb.get("nested_bg", False))
        finally:
            if not live:
                Melty.bg_depth, Melty.bg_stack = _sv_depth, _sv_stack
                style_manager.set_imgui_tint(*_sv_tint)

    def _scrub_view_edges(self, t: Tile) -> None:
        """freeze_resize tiles, on entering a frozen drag: the last live
        render baked the view's scrollbar gutter and bg outline at the
        LOGICAL (t.size) right/bottom edges — for no-shrink tiles that edge
        can sit strictly inside the resident texture (content_size is the
        high-water extent), so the content-edge trim in the frozen blit
        never reaches it and it reads as a stamped seam mid-image. Clear a
        thin strip at the logical edges to transparent; the frozen blit
        paints the real background (draw_freeze_bg) underneath, so the
        strips read as seamless bg. One-shot per capture era (keyed on
        last_clean_frame). The texels are destroyed, so the tile is also
        invalidated — the settled view re-renders once even when the drag
        releases back at the exact captured size."""
        if getattr(t, "edge_scrub_frame", -1) == t.last_clean_frame:
            return
        w, h = snap_int(t.size[0]), snap_int(t.size[1])
        # Right strip is wider: the scrollbar gutter lives there.
        trim_r = snap_int(Melty.px(20))
        trim_b = snap_int(Melty.px(5))
        if w <= trim_r or h <= trim_b:
            return
        aw, ah = _tile_alloc(t)
        bands = [
            (w - trim_r, ah - h, trim_r, h),  # right strip, full view height
            (0, ah - h, w, trim_b),           # bottom strip, full view width
        ]
        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, t.fbo)
            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glClearColor(0, 0, 0, 0.0)
            for x, y, bw, bh in bands:
                gl.glScissor(int(x), int(y), int(bw), int(bh))
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            _clear_mask_regions(t.mask_tex, bands)
        finally:
            st.restore()
        t.edge_scrub_frame = t.last_clean_frame
        t.last_invalidated_frame = max(t.last_invalidated_frame, self._frame_id)
        t.dirty = True

    def _scrub_stale_content(self, t: Tile, draw_state) -> None:
        """freeze_resize tiles: drop preserved beyond-logical texels once the
        view scrolls away from the position they were captured at. They are
        cleared to transparent; the frozen blit paints the real background
        live (draw_bg) under the stale image, so the cleared bands read as
        the view's exact bg — rounding, outline, saturation included.
        One-shot per capture era — content_bg suppresses re-clears until a
        shrink re-exposes fresh texels. Only the view's OWN scroll is
        tracked; a descendant's inner scroll can still leave stale pixels in
        the bands (acceptable for a mid-drag preview)."""
        cs = getattr(t, "content_size", None)
        if cs is None or getattr(t, "content_bg", False):
            return
        w, h = snap_int(t.size[0]), snap_int(t.size[1])
        cw, ch = snap_int(cs[0]), snap_int(cs[1])
        if cw <= w and ch <= h:
            return
        so = getattr(draw_state, "scroll_offset", None) or (0, 0)
        so = (snap_int(so[0]), snap_int(so[1]))
        anchor = getattr(t, "content_scroll", None)
        if anchor is None:
            # Pre-field tile (hotswap) or first sighting: anchor here.
            t.content_scroll = so
            return
        if anchor == so:
            return

        aw, ah = _tile_alloc(t)
        bands = []
        if ch > h:  # bottom band: screen rows [h, ch)
            bands.append((0, ah - ch, cw, ch - h))
        if cw > w:  # right band: screen cols [w, cw), full content height
            bands.append((w, ah - ch, cw - w, ch))
        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, t.fbo)
            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glClearColor(0, 0, 0, 0.0)
            for x, y, bw, bh in bands:
                gl.glScissor(int(x), int(y), int(bw), int(bh))
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            _clear_mask_regions(t.mask_tex, bands)
        finally:
            st.restore()
        t.content_scroll = so
        t.content_bg = True

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

            # Frozen resize (opt-in via freeze_resize, off by default): while a
            # mouse drag is actively changing this view's size, skip the live
            # re-render and blit the stale tile at its captured size, anchored
            # top-left and clipped to the live rect. Layout (the dummy below)
            # still advances by the LIVE size so the cursor tracks; on mouse-up
            # the size mismatch falls through to the normal live-render +
            # _ensure_tile path in mark_for_offscreen and the view redraws
            # once at the settled size. Scoped to a size mismatch so drags
            # inside the view (scroll, selection) never freeze it.
            frozen = (not use_image
                      and t is not None and has_area
                      and getattr(draw_state, "freeze_resize", False)
                      and t.size != (size[0], size[1])
                      and (imgui.is_mouse_down(0) or imgui.is_mouse_down(1)
                           or imgui.is_mouse_down(2) or Melty.on_drag))

            if use_image or frozen:
                imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))

                a = draw_state.abs_left, draw_state.abs_top
                # Frozen: draw the full resident content (the high-water
                # extent, >= logical size for no-shrink tiles) - a clip to
                # the live rect below crops it, so a grow/drag reveals
                # preserved earlier-era pixels instead of background.
                draw_size = (getattr(t, "content_size", None) or t.size) if frozen else size
                if frozen:
                    # The content's right/bottom edge has the view's own
                    # outline and scrollbar baked in; mid-drag that edge sits
                    # inside the live rect and reads as a stamped seam. Trim
                    # it off and let the live draw_bg show through the strip.
                    # (During a shrink the strip is outside the live clip
                    # anyway, so the trim only ever hides the baked edge.)
                    # Horizontal trim is wider: the scrollbar gutter lives there.
                    draw_size = (max(1, draw_size[0] - snap_int(Melty.px(20))),
                                 max(1, draw_size[1] - snap_int(Melty.px(5))))
                b = draw_state.abs_left + draw_size[0], draw_state.abs_top + draw_size[1]
                # Top-anchored subrect of the (possibly bucket-padded)
                # texture: content spans u [0, dw/aw], v [1 - dh/ah, 1].
                taw, tah = _tile_alloc(t)
                uv_a = (0.0, 1.0)
                uv_b = (draw_size[0] / taw, 1.0 - draw_size[1] / tah)

                dl = imgui.get_window_draw_list()
                if frozen:
                    # Drop the scrollbar/outline pixels baked at the logical
                    # (t.size) edges - inside the tile texture, where the
                    # content-edge trim above can't reach them.
                    self._scrub_view_edges(t)
                    # Paint the background live over the full live rect
                    # (under the frozen image) - outline-less for freeze
                    # views, so nothing baked in the tile interferes with it.
                    self.draw_freeze_bg(draw_state, a[0], a[1],
                                        size[0], size[1], live=False)
                    dl.push_clip_rect(a[0], a[1],
                                      a[0] + size[0], a[1] + size[1], True)
                dl.add_image_rounded(t.tex,
                                     a=a,
                                     b=b,
                                     uv_a=uv_a,
                                     uv_b=uv_b,
                                     rounding=getattr(draw_state, "corner_radius", 6))
                if frozen:
                    dl.pop_clip_rect()

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

        # Reveal detection (lives HERE, not in draw_tile - draw_tile is dead
        # space; this is the path that actually emits marks for cache-served
        # views): the baked mask has rank-0 texels outside the clip it was
        # built it, so when the clip RECEDES (e.g. the enclosing window is
        # resized taller and this clipped view is revealed further down) the
        # cached mask serves no depth for the revealed strip and shadows /
        # highlights stay clipped at the old height. A width change doesn't
        # need this - it changes the view's own width, which is a size_change
        # and rebuilds the tile. Edge-triggered on inset recede, deferred
        # until interaction settles; a shrinking clip needs no rebake (the
        # PASS 5 scissor crops).
        if ctx.drew_cached:
            t_reveal = self._tiles.get(ctx.key)
            if t_reveal is not None and t_reveal.mask_tex is not None:
                if clip is not None:
                    live_insets = (max(0.0, clip[0] - x), max(0.0, clip[1] - y),
                                   max(0.0, (x + w) - clip[2]), max(0.0, (y + h) - clip[3]))
                else:
                    live_insets = (0.0, 0.0, 0.0, 0.0)
                baked_insets = getattr(t_reveal, "mask_clip_insets", (0.0, 0.0, 0.0, 0.0))
                revealed = any(li < bi - 0.5 for li, bi in zip(live_insets, baked_insets))
                settled = (not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1)
                           and not imgui.is_mouse_down(2) and not Melty.on_drag)
                if revealed and settled:
                    t_reveal.mask_clip_insets = live_insets
                    self.invalidate(ctx.key, note=Note(name="Clip reveal",
                                                       reason=f"insets {baked_insets} -> {live_insets}",
                                                       tint=(1, 0.5, 1)))

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
                elif t is not None and old_t is None:
                    # Brand-new tile (first appearance of this view): the view
                    # and any visible descendants rendered fresh this frame and
                    # their blits are already enqueued below, and the parent's
                    # body must have run for this view to exist at all, so the
                    # ancestor chain is either invalid too or gets recomposed
                    # by the plain ancestor climb. The depth-4 descendant
                    # sweep would just forced the entire new subtree to render
                    # fresh a second time on the next frame - the dominant
                    # cost of opening a new window. Ancestors only.
                    self.invalidate(ctx.key, note=Note(name="New Tile", reason="fresh tile",
                                                       tint=(1, 0.5, 0)))
                elif (t is not None and old_size is not None
                      and int(old_size[0]) == int(ctx.size[0])):
                    # Cross-bucket HEIGHT-ONLY resize (typing adds/removes a
                    # line in a tall code view): every width - and so every
                    # child's wrap/layout - is unchanged; children below the
                    # edit only TRANSLATE, and their own tiles recomposite at
                    # the new positions. This view's draw already ran THIS
                    # frame (the content change invalidated it) and its blit
                    # is enqueued below, so like the fresh-tile path the
                    # depth-4 descendant sweep only forced the whole subtree
                    # (e.g. the structured code-dict pane) to re-render again
                    # next frame - ~16-20ms per Enter/Backspace. Ancestors
                    # only.
                    self.invalidate(ctx.key, note=Note(
                        name="New Tile", tint=(1, 0.5, 0),
                        reason=f"height-only {old_size} -> {ctx.size}"))
                else:
                    reason = f"New size old_size{old_size} new_size{ctx.size}" if old_size else "New tile"
                    reason = "t None" if t is None else reason
                    # DEBUG (height oscillation bug): a cross-bucket resize on a
                    # SETTLED view means two writers disagree about its height -
                    # log which writer set it this frame (ds._source provenance)
                    # so an every-frame flip names the offending involved.
                    if old_size is not None:
                        try:
                            from src.lsd.gl_gui.perf_trace import trace_rl as _nt_trace
                            _ds = ctx.draw_state
                            _nt_trace(("newtile", ctx.key),
                                      f"NEW-TILE {reason} name={getattr(_ds, 'name', None)!r} "
                                      f"hsrc={getattr(_ds, '_source', {}).get('height')!r}",
                                      min_interval=0.2)
                        except Exception:
                            pass

                    self.invalidate_up(ctx.key, max_depth=4, note=Note(name="New Tile", reason=reason, tint=(1, 0.5, 0)))
                self._tiles[ctx.key] = t

            if t is not None:
                self._scrub_stale_content(t, ctx.draw_state)

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

        if self._prog_shadow_grad is None:
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _SHADOW_GRAD_FS)
            self._prog_shadow_grad = _link(vs, fs)
            self._loc_sg_uRankCorners = gl.glGetUniformLocation(self._prog_shadow_grad, "uRankCorners")
            self._loc_sg_uRectSize = gl.glGetUniformLocation(self._prog_shadow_grad, "uRectSize")
            self._loc_sg_uCornerRadius = gl.glGetUniformLocation(self._prog_shadow_grad, "uCornerRadius")
            self._loc_sg_uMargin = gl.glGetUniformLocation(self._prog_shadow_grad, "uMargin")

        # Re-key based on the NEWEST uniform's location, not just program
        # presence: a hotswap onto a live instance can leave an OLD glow
        # program (compiled from a previous _GLOW_FS with different uniforms)
        # cached - the new stamp code would then set uniforms that were
        # never fetched and die. Bump the probed name as _GLOW_FS grows.
        if (getattr(self, "_prog_glow", None) is None
                or getattr(self, "_loc_gl_uDebugSolid", None) is None):
            if getattr(self, "_prog_glow", None):
                gl.glDeleteProgram(self._prog_glow)
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _GLOW_FS)
            self._prog_glow = _link(vs, fs)
            self._loc_gl_uColor = gl.glGetUniformLocation(self._prog_glow, "uColor")
            self._loc_gl_uRankLo = gl.glGetUniformLocation(self._prog_glow, "uRankLo")
            self._loc_gl_uRankHi = gl.glGetUniformLocation(self._prog_glow, "uRankHi")
            self._loc_gl_uDepthMask = gl.glGetUniformLocation(self._prog_glow, "uDepthMask")
            self._loc_gl_uGlowSize = gl.glGetUniformLocation(self._prog_glow, "uGlowSize")
            self._loc_gl_uRectSize = gl.glGetUniformLocation(self._prog_glow, "uRectSize")
            self._loc_gl_uCornerRadius = gl.glGetUniformLocation(self._prog_glow, "uCornerRadius")
            self._loc_gl_uRadius = gl.glGetUniformLocation(self._prog_glow, "uRadius")
            self._loc_gl_uFalloff = gl.glGetUniformLocation(self._prog_glow, "uFalloff")
            self._loc_gl_uDebugSolid = gl.glGetUniformLocation(self._prog_glow, "uDebugSolid")

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
        self.last_capture_stats = (0, 0, 0)
        if self._snapshot_fbo is None:
            return

        if (not self._pending and not self._mask_rects
                and not self._shadow_rects
                and not getattr(self, "_glow_rects", None)):
            return

        # Grab direct references (we clear at end anyway)
        local_mask_rects = self._mask_rects
        # Reversed
        local_mask_rects_rev = list(reversed(local_mask_rects))
        local_pending = self._pending
        local_pending_rev = list(reversed(local_pending))
        # TEMP perf: capture volume for next_frame's present split - how many
        # tiles this frame re-grabs and their total area. A post-reparse
        # invalidation storm shows up here as a cap_tiles/cap_px spike right
        # before the present stall.
        try:
            self.last_capture_stats = (
                len(local_pending),
                int(sum(p.size[0] * p.size[1] for p in local_pending)),
                len(local_mask_rects))
        except Exception:
            self.last_capture_stats = (len(local_pending), -1,
                                       len(local_mask_rects))

        if (not local_pending and not local_mask_rects
                and not self._shadow_rects
                and not getattr(self, "_glow_rects", None)):
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

        # TEMP perf (present-stall hunt): per-pass CPU time. The observed
        # 500ms+ captures of a handful of tiles smells like an implicit
        # present sync - the PASS 1 blit reads the backbuffer, so the GPU
        # may CPU-block there until every prior draw this frame has finished.
        # Whichever pass the stall lands in names the culprit.
        import time as _time_mod
        _cp = _time_mod.perf_counter
        _cp_t0 = _cp()
        _cp_t1 = _cp_t2 = _cp_t3 = _cp_t4 = _cp_t5 = _cp_t0

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
            _cp_t1 = _cp()

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
            _cp_t2 = _cp()

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

            _cp_t3 = _cp()
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

                    # A frozen child mid-drag: its body (and its whole
                    # subtree) skipped this frame, so the fresh-flat fallback
                    # below would render a featureless rect and erase every
                    # nested mark baked in its cached mask - nested child
                    # would visibly drop out of the drag. Serve the cached
                    # mask the way the frozen pixel blit serves the tile:
                    # captured (t_child.size) quad, top-left anchored, the
                    # live-rect scissor crops a shrink. The not-size_change
                    # below guard doesn't apply - the quad stays at the
                    # tile's own size, so uv mapping is unstretched.
                    frozen_child = (
                            (not is_self) and size_change
                            and t_child is not None
                            and t_child.mask_tex is not None
                            and draw_state is not None
                            and getattr(draw_state, "freeze_resize", False))
                    use_child_cache = (
                            (not is_self)
                            and (t_child is not None)
                            and (t_child.mask_tex is not None)
                            and (not size_change or frozen_child)
                    )

                    self.apply_blend_mode(r)

                    clip_x0, clip_y0, clip_x1, clip_y1 = self._screen_rect_to_fb_xyxy(
                        r.x, r.y, r.w, r.h, dp_x, dp_y, s_x, s_y, fb_h
                    )
                    clip_ix0, clip_iy0 = int(floor(clip_x0)), int(floor(clip_y0))
                    clip_ix1, clip_iy1 = int(ceil(clip_x1)), int(ceil(clip_y1))
                    clip_iw, clip_ih = max(0, clip_ix1 - clip_ix0), max(0, clip_iy1 - clip_iy0)

                    gl.glEnable(gl.GL_SCISSOR_TEST)
                    gl.glScissor(clip_ix0, clip_iy0, clip_iw, clip_ih)
                    gl.glDisable(gl.GL_BLEND)

                    # Quad geometry comes from the LIVE draw_state, never the
                    # _key_to_ctx: the ctx is only refreshed when a view
                    # actually re-nders (mark_end_offscreen), so after a
                    # reflow moves a cache-served sibling its ctx.pos is stale
                    # and the cached depths land at the old position while the
                    # scissor (this frame's dirty mark) sits at the new one.
                    # Mirrors PASS 5. Under the not-size_change guard the live
                    # size equals the tile's logical size, so uv_rect mapping
                    # stays unstretched.
                    if frozen_child:
                        cx, cy = draw_state.abs_left, draw_state.abs_top
                        cw, ch = t_child.size
                        sx0, sy0, sx1, sy1 = self._screen_rect_to_fb_xyxy(cx, cy, cw, ch, dp_x, dp_y, s_x, s_y,
                                                                          fb_h)
                    elif (draw_state is not None and draw_state.width is not None
                            and draw_state.height is not None):
                        cx, cy = draw_state.abs_left, draw_state.abs_top
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

                # Bake add_shadow() marks owned by this tile's subtree into
                # the cached mask, so they keep casting on frames where the
                # owner's tile is cache-served - the same persistence regular
                # view marks get from this rebuild. A body only runs when its
                # tile is dirty, so the frame a body is (re)marked is always
                # a frame its tile is in local cache; conversely the next
                # local capture without the call ages the mark out.
                if self._shadow_rects:
                    _owned = self._shadows_owned_by(p.key)
                    if _owned:
                        self._stamp_shadow_marks(_owned, dp_x, dp_y, s_x, s_y,
                                                 fb_h, scissor_fb=(x0, y0, x1, y1))

                gl.glDisable(gl.GL_SCISSOR_TEST)

                # Save _full_sub_mask_tex to tile's mask_tex and remember the layer
                if p.tile is not None and p.tile.mask_tex is not None:
                    p.tile.mask_layer = p.depth_and_layer
                    _cb = p.draw_state.clipped_by_rect if p.draw_state is not None else None
                    p.tile.mask_clip_insets = tuple(_cb) if _cb is not None else (0.0, 0.0, 0.0, 0.0)

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
            _cp_t4 = _cp()
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
                    # freeze_resize mid-drag: same serve-the-cached-mask
                    # exception as PASS 4's frozen_child - a flat fallback
                    # would flatten the whole frozen subtree's depth for the
                    # drag. Quad at the tile's logical size (unstretched),
                    # clip-rect scissor.
                    frozen_mask = (size_change and t is not None
                                   and t.mask_tex is not None
                                   and draw_state is not None
                                   and getattr(draw_state, "freeze_resize", False))
                    can_use_cached = ((t is not None) and (t.mask_tex is not None)
                                      and (not size_change or frozen_mask))

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
                        if frozen_mask:
                            tw, th = t.size
                        else:
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

            # Standalone add_shadow() marks: stamped last, MAX-blended so they
            # can only shadow depth - a shadow under an already-higher window
            # mark is a no-op, everywhere else it becomes a caster edge for
            # the post_frame shadow_cast pass. (Owned marks were also baked
            # into their pending tile's cached mask in PASS 4; MAX blend makes
            # the double-stamp idempotent.) OWNED INSET marks are excluded: a
            # MIN carve stamped over the finished full mask would cut into
            # surrounding windows floating above the recess - they reach the
            # full mask only through their tile's cached mask (PASS 4), which
            # scopes the carve to the owner's subtree.
            # ...with one exception: an owned inset whose cached-mask path
            # was skipped this frame (likely mid-resize: size_change makes
            # PASS 5 stamp the owner with a flat rank mask, or the tile has no
            # mask yet). Excluding it here would drop the recess for exactly
            # the resize frames, so stamp it again - still MIN-blended and
            # scissored to its own snapshotted clip, which keeps the
            # transient carve inside the resizing view's region.
            _standalone = []
            for s in self._shadow_rects:
                if not (s[9] and s[8] is not None):
                    _standalone.append(s)
                    continue
                _ot = self._tiles.get(s[8])
                _ods = self.key_to_draw_state.get(s[8])
                # size_change no longer voids the cached-mask path for
                # freeze_resize tiles (frozen_mask serves it above), so
                # their insets stay excluded here.
                _served = (_ot is not None and _ot.mask_tex is not None
                           and not (_ods is not None and _ods.size_change
                                    and not getattr(_ods, "freeze_resize", False)))
                if not _served:
                    _standalone.append(s)
            if _standalone:
                self._stamp_shadow_marks(_standalone,
                                         dp_x, dp_y, s_x, s_y, fb_h)

            # ================================================================
            # PASS 6: Glow stencil buffer (after PASS 5 - the stamp shader
            # samples the finished full depth mask for its depth gate).
            # Retention is keyed by EMITTING draw_state, decoupled from tile
            # captures: clear_glows(ds) ran → the emitter's body ran this
            # frame, so its retained entry drops AND this frame's emissions
            # re-add it; bodies that were cache-skipped keep their entry
            # untouched. Every retained entry re-stamps at the emitter's
            # LIVE abs pos (record-time clips go stale when a cache-skip
            # sibling reflows - see project_blit_shadow_clip). Emitterless
            # marks are one-shot.
            # ================================================================
            # Glow must never abort the capture pass (an exception here would
            # leave tiles un-processed AND read as a broken hotswap to the
            # rollback guard) - it's purely visual, so trap and report once.
            try:
                self._ensure_glow_state()
                _glow_retained = self._glow_marks_by_emitter
                _depth_retained = self._depth_marks_by_emitter
                for _eid in self._glow_cleared:
                    _glow_retained.pop(_eid, None)
                    _depth_retained.pop(_eid, None)

                # Kill evidence shared by BOTH retained stores: the rects of
                # every tile that FUTURE this frame, tagged with their
                # root emitter AND their live ancestor chain. A capture in
                # the emitter's OWN subtree that repaints its territory
                # without the emitter re-emitting means the views under the
                # marks were replaced (tab switch, jump-to-line swap, any
                # culled branch) - the marks die. Two exemptions keep
                # legitimate repaints from flickering the glow:
                # - captures INSIDE the emitter's subtree (scrolled-in
                #   widgets capturing during the parent's frame_delta window,
                #   token overlays) repaint fragments OF the glowing
                #   content, not over it - walking the CAPTURING tile's
                #   _parent chain is safe, it just rendered so its pointers
                #   are fresh (only the culled emitter's parent goes stale);
                # - the territory is the marks INTERSECTED with the
                #   emitter's LIVE rect (same origin-clip the stamp
                #   applies), so a freeze-resize drag that shrinks the view
                #   stops stale-wide marks from overlapping the sibling
                #   column's next-frame captures across the seam.
                # Captures in OTHER columns (popups and tooltips) never
                # repaint the emitter's surface - glow-root scoping keeps
                # them from killing marks beneath.
                def _anc_ids(node):
                    ids = set()
                    hops = 0
                    while node is not None and hops < 64:
                        ids.add(id(node))
                        parent = getattr(node, "_parent", None)
                        if parent is None or parent is node:
                            break
                        node = parent
                        hops += 1
                    return ids

                _pend_rects = []
                for p in local_pending:
                    try:
                        _pend_rects.append(
                            (p.pos[0], p.pos[1],
                             p.pos[0] + p.size[0], p.pos[1] + p.size[1],
                             self._glow_root_ds(p.draw_state),
                             _anc_ids(p.draw_state), p.draw_state))
                    except Exception:
                        pass

                def _live_clip_of(eds):
                    # The emitter's LIVE rect - clips glow ORIGIN rects and
                    # kill territory alike (never the rendered pixels):
                    # during a freeze-resize drag the wrapper doesn't re-run,
                    # but width/height track the drag live.
                    if eds is None:
                        return None
                    try:
                        l, t = eds.abs_left, eds.abs_top
                        w, h = eds.width, eds.height
                        if w is None or h is None:
                            return None
                        return (l, t, l + w, t + h)
                    except Exception:
                        return None

                # Kills only EXECUTE while interaction is settled: mid-drag
                # repaints (column resize, freeze resize reflows) hit live
                # glow territory constantly and immediate kills lost the
                # glow for the whole drag. Unsettled hits flag the emitter
                # (it keeps glowing); the flag executes on settle unless
                # the emitter re-emitted since (which clears it).
                _settled = not (imgui.is_mouse_down(0)
                                or imgui.is_mouse_down(1)
                                or imgui.is_mouse_down(2)
                                or Melty.on_drag)

                def _territory_hit(marks, delta, root, eds, live):
                    # Returns the pending capture that repainted the
                    # emitter's territory, or None. Exempt: captures inside
                    # the emitter's subtree (live _parent walk) and small
                    # fragment captures fully CONTAINED inside the emitter's
                    # live rect (< half its area) - inline widgets / token
                    # overlays hosted outside the emitter's parent chain
                    # repaint fragments OF the glowing content on hover; a
                    # real content swap covers the region wholesale.
                    e_id = id(eds)
                    e_area = ((live[2] - live[0]) * (live[3] - live[1])
                              if live is not None else None)
                    for m in marks:
                        mx0 = m[0] + delta[0]
                        my0 = m[1] + delta[1]
                        mx1, my1 = mx0 + m[2], my0 + m[3]
                        if live is not None:
                            mx0, my0 = max(mx0, live[0]), max(my0, live[1])
                            mx1, my1 = min(mx1, live[2]), min(my1, live[3])
                            if mx1 <= mx0 or my1 <= my0:
                                continue
                        for pr in _pend_rects:
                            px0, py0, px1, py1, proot, p_ancs, _pds = pr
                            if proot is not root or e_id in p_ancs:
                                continue
                            if (e_area
                                    and px0 >= live[0] and py0 >= live[1]
                                    and px1 <= live[2] and py1 <= live[3]
                                    and ((px1 - px0) * (py1 - py0)
                                         < 0.5 * e_area)):
                                continue
                            if (mx0 < px1 and px0 < mx1
                                    and my0 < py1 and py0 < my1):
                                return pr
                    return None

                def _log_kill(kind, eds, hit, deferred):
                    if not getattr(Toggles, "glow_debug_log", False):
                        return
                    _pds = hit[6]
                    print(f"glow kill[{kind}]"
                          f"{' DEFERRED' if deferred else ''}: "
                          f"emitter={getattr(eds, 'name', None)!r} "
                          f"by={getattr(_pds, 'name', None)!r} "
                          f"cap_rect=({hit[0]:.0f},{hit[1]:.0f},"
                          f"{hit[2]:.0f},{hit[3]:.0f})")

                # Retained DEPTH marks re-stamp into the full mask FIRST -
                # before the glow stamp samples it - so on frames when an
                # enclosing tile rebuilt its mask without this emitter's
                # body running, its interior depth change (block peels, chip
                # lifts) is retained instead of flashing. The additive blend
                # makes re-stamping marks that were also freshly emitted
                # this frame idempotent.
                _depth_emitted = defaultdict(list)
                for mark, _eds, _anchor in self._depth_frame:
                    _depth_emitted[id(_eds)].append((mark, _eds, _anchor))
                for _eid, entries in _depth_emitted.items():
                    _depth_retained[_eid] = (
                        [m for m, _d, _a in entries],
                        entries[0][1], entries[0][2])
                _depth_stamp = []
                for _eid, (marks, _eds, _anchor) in list(
                        _depth_retained.items()):
                    if _eds is None or getattr(_eds, "abs_closed", False):
                        _depth_retained.pop(_eid, None)
                        self._depth_kill_pending.discard(_eid)
                        continue
                    if _eid in _depth_emitted:
                        self._depth_kill_pending.discard(_eid)
                        continue  # stamped via the normal fresh path already
                    _delta = (_eds.abs_left - _anchor[0],
                              _eds.abs_top - _anchor[1])
                    _root_ds = self._glow_root_ds(_eds)
                    if (getattr(_eds, "last_seen", None)
                            == Melty.frame_count):
                        # Reached this frame - wrapper ran, so its pixels
                        # are on screen fresh or via its OWN cached blit. A
                        # hovered ANCESTOR rebuild around it did not
                        # replace them (the use_cache child blits inside
                        # the parent's fresh capture). Tab-switched/culled
                        # emitters are never reached, so real content
                        # swaps still kill.
                        self._depth_kill_pending.discard(_eid)
                    else:
                        _hit = _territory_hit(marks, _delta, _root_ds, _eds,
                                              _live_clip_of(_eds))
                        if _hit is not None:
                            _log_kill("depth", _eds, _hit, not _settled)
                            if _settled:
                                _depth_retained.pop(_eid, None)
                                self._depth_kill_pending.discard(_eid)
                                continue
                            self._depth_kill_pending.add(_eid)
                        elif _settled and _eid in self._depth_kill_pending:
                            _depth_retained.pop(_eid, None)
                            self._depth_kill_pending.discard(_eid)
                            continue
                    dx, dy = _delta
                    for (mx, my, mw, mh, ranks, cr, margin, mclip, _own,
                         _ins) in marks:
                        _depth_stamp.append(
                            (mx + dx, my + dy, mw, mh, ranks, cr, margin,
                             (mclip[0] + dx, mclip[1] + dy,
                              mclip[2] + dx, mclip[3] + dy)
                             if mclip is not None else None,
                             None, False))
                if _depth_stamp:
                    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,
                                         self._full_mask_fbo)
                    gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE,
                                   gl.GL_FALSE)
                    self._stamp_shadow_marks(_depth_stamp, dp_x, dp_y,
                                             s_x, s_y, fb_h)
                    gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE,
                                   gl.GL_TRUE)

                # Tunable band offsets, applied LINEARLY in rank units -
                # never by shifting shadow_depth_at's depth argument (the
                # depth term is non-monotonic: peaks ~530 rank units at d~59
                # then decreases, so a large depth offset can cross a bound
                # and collapse the band). Step unit: one shallow depth
                # step (~layer_inc * 53.42/5.975 rank units).
                _g_step = float(Melty.layer_inc) * (53.42 / 5.975)
                _g_lo_off = float(getattr(
                    Toggles, "glow_mask_lower_offset", -4.0)) * _g_step
                _g_hi_off = float(getattr(
                    Toggles, "glow_mask_upper_offset", 8.0)) * _g_step

                def _resolve_glow(m, delta, eds, root_ds):
                    # Band anchors are the LIVE shadow_depth properties -
                    # the exact same units those views' mask rects stamp
                    # (depth_and_layer through shadow_depth_at), so the band
                    # is always in the mask's own units. Emitter anchor:
                    # the emitting view's surface + the mark's relative
                    # offset; floor anchor: the root window's surface.
                    _anchor_rank = None
                    if eds is not None:
                        try:
                            _anchor_rank = (float(eds.shadow_depth)
                                            + m[6] * _g_step)
                        except Exception:
                            _anchor_rank = None
                    if _anchor_rank is None:
                        _anchor_rank = m[7]  # record-time absolute fallback
                    _lo_anchor = None
                    if root_ds is not None:
                        try:
                            _lo_anchor = float(root_ds.shadow_depth)
                        except Exception:
                            _lo_anchor = None
                    if _lo_anchor is None:
                        _lo_anchor = _anchor_rank
                    _rank = (_anchor_rank + _g_hi_off) / 65535.5
                    _floor = (_lo_anchor + _g_lo_off) / 65535.5
                    return (m, delta, min(1.0, _rank), max(0.0, _floor),
                            _live_clip_of(eds))

                _stamp_list = []
                _emitted_now = defaultdict(list)
                for mark, _eds, _anchor in self._glow_rects:
                    if _eds is None:
                        _stamp_list.append(
                            _resolve_glow(mark, (0.0, 0.0), None, None))
                    else:
                        _emitted_now[id(_eds)].append((mark, _eds, _anchor))
                for _eid, entries in _emitted_now.items():
                    _glow_retained[_eid] = (
                        [m for m, _d, _a in entries],
                        entries[0][1], entries[0][2])

                for _eid, (marks, _eds, _anchor) in list(
                        _glow_retained.items()):
                    if _eds is None or getattr(_eds, "abs_closed", False):
                        _glow_retained.pop(_eid, None)
                        self._glow_kill_pending.discard(_eid)
                        continue
                    _delta = (_eds.abs_left - _anchor[0],
                              _eds.abs_top - _anchor[1])
                    _root_ds = self._glow_root_ds(_eds)
                    # Freshly-emitted entries skip the kill: their own
                    # tile's capture legitimately overlaps their territory.
                    if _eid in _emitted_now:
                        self._glow_kill_pending.discard(_eid)
                    elif (getattr(_eds, "last_seen", None)
                          == Melty.frame_count):
                        # Reached this frame (fresh render or its own
                        # cached blit): pixels authoritative on screen - a
                        # hovered ancestor's capture around the use_cache
                        # barrier did not overlap them. Culled emitters are
                        # never reached, so this swaps to kill.
                        self._glow_kill_pending.discard(_eid)
                    else:
                        _hit = _territory_hit(marks, _delta, _root_ds, _eds,
                                              _live_clip_of(_eds))
                        if _hit is not None:
                            _log_kill("glow", _eds, _hit, not _settled)
                            if _settled:
                                _glow_retained.pop(_eid, None)
                                self._glow_kill_pending.discard(_eid)
                                continue
                            self._glow_kill_pending.add(_eid)
                        elif (_settled
                              and _eid in self._glow_kill_pending):
                            _glow_retained.pop(_eid, None)
                            self._glow_kill_pending.discard(_eid)
                            continue
                    for m in marks:
                        _stamp_list.append(
                            _resolve_glow(m, _delta, _eds, _root_ds))
                # Per-surface dedupe: identical origin rects (same pos,
                # size, color after the live-position delta) collapse to
                # ONE emission at the strongest intensity. The old
                # draw-list blur alpha-blended duplicates into
                # near-invisibility; the light field is ADDITIVE, so a
                # doubled origin (duplicate _dt_lines entries, a body
                # drawn twice through different paths) appear as a glaring
                # 2x glow.
                _dedup = {}
                _dup_count = 0
                for _se in _stamp_list:
                    _m, _d = _se[0], _se[1]
                    _k = (round(_m[0] + _d[0], 1), round(_m[1] + _d[1], 1),
                          round(_m[2], 1), round(_m[3], 1), _m[4])
                    _prev = _dedup.get(_k)
                    if _prev is None or _se[0][5] > _prev[0][5]:
                        if _prev is not None:
                            _dup_count += 1
                        _dedup[_k] = _se
                    else:
                        _dup_count += 1
                _stamp_list = list(_dedup.values())

                if (getattr(Toggles, "glow_debug_log", False)
                        and self._frame_id % 60 == 0):
                    _s0 = _stamp_list[0] if _stamp_list else None
                    print(
                        f"glow6 f{self._frame_id}: glow={getattr(Toggles, 'glow', '?')} "
                        f"frame_marks={len(self._glow_rects)} "
                        f"retained={len(_glow_retained)} "
                        f"cleared={len(self._glow_cleared)} "
                        f"stamped={len(_stamp_list)} dups={_dup_count} "
                        f"tex={getattr(self, '_glow_size', None)} "
                        f"empty={self._glow_tex_empty}"
                        + (f" first: rect={tuple(round(v, 1) for v in _s0[0][:4])}"
                           f" rank={_s0[2]:.5f} floor={_s0[3]:.5f}"
                           f" inten={_s0[0][5]:.3f}" if _s0 else ""))
                if not getattr(Toggles, "glow", False):
                    _stamp_list = []  # retained entries stay warm
                if _stamp_list or not self._glow_tex_empty:
                    self._stamp_glow_marks(_stamp_list, dp_x, dp_y, s_x, s_y,
                                           fb_w, fb_h)
            except Exception:
                if not getattr(self, "_glow_error_logged", False):
                    self._glow_error_logged = True
                    print("glow PASS 6 failed (glow disabled this frame):")
                    traceback.print_exc()

            gl.glViewport(0, 0, fb_w, fb_h)
            gl.glDisable(gl.GL_BLEND)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glUseProgram(0)
            _cp_t5 = _cp()

        finally:
            st.restore()
            # TEMP perf: emit the per-pass split when the whole pass was slow.
            # p1_blit is the draw-sync overhead (backbuffer read waits for
            # prior draws); p3_copy covers sub-mask + copy per tile; p4/p5 are
            # the mask rebuilds. Any exception mid-pass leaves later stamps at
            # their initialized values - the failing pass absorbs the tail.
            _cp_tot = (_cp() - _cp_t0) * 1000.0
            if _cp_tot >= 30.0:
                try:
                    from src.lsd.gl_gui.perf_trace import trace as _cptr
                    _cptr("capture pass split",
                          total_ms=round(_cp_tot, 1),
                          p1_blit=round((_cp_t1 - _cp_t0) * 1000.0, 1),
                          p2_mask=round((_cp_t2 - _cp_t1) * 1000.0, 1),
                          p3_copy=round((_cp_t3 - _cp_t2) * 1000.0, 1),
                          p4_tile_masks=round((_cp_t4 - _cp_t3) * 1000.0, 1),
                          p5_full_mask=round((_cp_t5 - _cp_t4) * 1000.0, 1),
                          tail=round(_cp_tot - (_cp_t5 - _cp_t0) * 1000.0, 1),
                          tiles=len(local_pending), masks=len(local_mask_rects))
                except Exception:
                    pass
            self._detect_occluder_changes(self._mask_rects)
            self._pending.clear()
            self._mask_rects.clear()
            self._shadow_rects.clear()
            if getattr(self, "_glow_rects", None) is not None:
                self._glow_rects.clear()
            if getattr(self, "_glow_cleared", None) is not None:
                self._glow_cleared.clear()
            if getattr(self, "_depth_frame", None) is not None:
                self._depth_frame.clear()
            self._enq_mask_keys.clear()
            self._enq_copy_keys.clear()
            self._cancelled_keys.clear()
            self._recording = False
            self.did_deviate.clear()
            self.seen_ids.clear()

def add_shadow(rect, offset=2.0, layer=None, depth=None, corner_radius=5.0,
               margin=0.0, clip=True, draw_state=None):
    """Mark a screen-space (x, y, w, h) rect as a shadow caster from anywhere
    — no @render_func, key, or draw_state required. `offset` is the signed
    depth delta from the surrounding surface: positive (default +2) lifts the
    rect so it casts a shadow, negative carves a recess so the surroundings
    cast into it. A 4-tuple (top_left, top_right, bottom_left, bottom_right)
    gives each corner its own delta and eases the depth between them across
    the quad — e.g. offset=(0, 0, 0, 8) peels the bottom-right corner up.
    layer/depth default to Melty.active_layer/Melty.shadow_depth at call
    time. Cheap enough to call every frame; see
    TileCacheMasked.add_shadow."""
    cache = Melty.cache
    if cache is not None:
        cache.add_shadow(rect, offset=offset, layer=layer, depth=depth,
                         corner_radius=corner_radius, margin=margin, clip=clip,
                         draw_state=draw_state)


def add_glow(rect, color, intensity=1.0, radius=24.0, falloff=2.0,
             offset=2.0, layer=None, depth=None, corner_radius=3.0,
             clip=True, draw_state=None):
    """Mark a screen-space (x, y, w, h) rect as a GLOWING light emitter. The
    rect lands in the low-res glow light buffer with an inverse-square
    falloff skirt of `radius` px; the shadow composite adds it as emitted
    light and pushes back shadow where it falls. The light only reaches
    receivers between the emitter's ROOT window surface and its own depth
    (offset/layer/depth, add_shadow semantics): nothing behind the window
    chain is lit, and views floating above the emitter mask it out.

    Pass the emitting view's `draw_state` (and call clear_glows(draw_state)
    at the top of the body) to persist the mark across cache-served frames;
    without it the mark lasts one frame — re-call every frame, like
    add_shadow's drag-ghost usage. See TileCacheMasked.add_glow."""
    cache = Melty.cache
    if cache is not None and getattr(cache, "add_glow", None) is not None:
        cache.add_glow(rect, color, intensity=intensity, radius=radius,
                       falloff=falloff, offset=offset, layer=layer,
                       depth=depth, corner_radius=corner_radius, clip=clip,
                       draw_state=draw_state)


def clear_glows(draw_state):
    """Open `draw_state`'s glow group for this body run: retained glow marks
    it emitted earlier drop at frame end unless re-emitted this frame. Call
    unconditionally at the top of any body that MAY add_glow — runs that stop
    emitting shed their stale glow, cache-skipped runs never get here and
    keep glowing."""
    cache = Melty.cache
    if cache is not None and getattr(cache, "clear_glows", None) is not None:
        cache.clear_glows(draw_state)
