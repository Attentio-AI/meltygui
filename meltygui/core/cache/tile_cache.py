from __future__ import annotations

import ctypes
import time
from array import array
import random
import sys
import traceback
from collections import deque, defaultdict
from copy import copy
from dataclasses import dataclass
from math import ceil, floor, radians, tan
from typing import Dict, List, Optional, Tuple, MutableMapping, Any

from OpenGL import GL as gl
import meltygui_imgui as imgui
from meltygui_imgui.core import _DrawList

from meltygui.core.melty import Melty
from meltygui.core.cache.tile_marks import snap_int
from meltygui.core.diagnostics.notifications import notify
from meltygui.core.diagnostics.notifications import capture_stack
from meltygui.state.core_enums import OffscreenDebugMode
from meltygui.state.new_core_model import TileMode
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.runtime.toggles import shadow_depth_at
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.windowing.glfw_utils import print_stack_trace
from meltygui.core.windowing.glfw_utils import get_live_frames
from meltygui.core.conversion.cache_tree import UNSET_VALUE
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.window_decoration import window
from meltygui.core.cache.invalidation_tracker import InvalidateTracker
from meltygui.core.cache.invalidation_tracker import Note

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
    # Always remember the LAST bump reason (one attr store); the slow
    # capture-pass trace reads it per captured tile, so a burst of big-tile
    # recaptures names its own trigger in the perf log.
    t._last_bump = site
    try:
        ds = getattr(t, "draw_state", None)
        if ds is not None and getattr(ds, "_bump_trace_armed", False):
            from meltygui.core.diagnostics.perf_trace import trace_rl
            trace_rl(("bump", id(t), site), f"BUMP {site} name={getattr(ds, 'name', None)!r}",
                     min_interval=0.2)
    except Exception:
        pass

def _full_mask_rects(subtree_rects_by_root):
    """Keep each stamp's last occurrence in the full-mask paint order.

    A rect is shared by its own subtree and each ancestor's. Full-mask
    stamps overwrite (blending is disabled), so earlier identical stamps
    cannot affect the final pixels, including cached masks with holes.
    Deduplicate by object identity: distinct marks with the same key remain.
    """
    seen = set()
    last = []
    # Reverse of the original paint order: reversed roots, forward rects.
    for rects in subtree_rects_by_root.values():
        for rect in reversed(rects):
            identity = id(rect)
            if identity not in seen:
                seen.add(identity)
                last.append(rect)
    return reversed(last)


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
    # Deferred descendant bars are not pixels in this tile. Replay only this
    # retained set when its body is skipped; lifetime follows the tile.
    freeze_scrollbars: tuple = ()
    overlay_views: tuple = ()


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
    # Live renders only (InvalidateTracker render times): perf_counter at
    # mark_start_offscreen, and the total time of the cached views rendered
    # inside this one, subtracted so the readout is this view's own cost.
    started: float = 0.0
    child_ms: float = 0.0
    freeze_scrollbars: tuple = ()
    overlay_views: tuple = ()


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
# Tiles hold LINEAR scRGB (hdr_color.py): RGBA16F keeps everything above 1 and
# below 0 and doesn't band in the darks the way 8 bits of linear would.
def _create_color_tex(w: int, h: int, internal_format=gl.GL_RGBA16F, clamp_to_border=False, filter=gl.GL_LINEAR) -> int:
    Melty.cache.tex_init_count += 1

    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    upload_type = gl.GL_HALF_FLOAT if internal_format in (gl.GL_RGBA16F, gl.GL_RGB16F) else (
        gl.GL_FLOAT if internal_format in (gl.GL_RGBA32F, gl.GL_RGB32F) else gl.GL_UNSIGNED_BYTE)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal_format, w, h, 0, gl.GL_RGBA, upload_type, None)
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
        import meltygui.core.windowing.window_api as glfw
        w = h = 0
        for m in glfw.get_monitors():
            mode = glfw.get_video_mode(m)
            w = max(w, int(mode.size.width))
            h = max(h, int(mode.size.height))
        return w, h
    except Exception:
        return 0, 0


def _create_fbo_with_tex(tex: int, depth_stencil: bool, w, h) -> Tuple[int, Optional[int]]:
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
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, Melty.default_framebuffer())
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
            from meltygui.core.diagnostics.perf_trace import trace_rl as _ib_trace
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
        from meltygui.core.diagnostics.perf_trace import trace as _tile_trace
        _tile_trace("tile create", name=getattr(draw_state, "name", None), size=(aw, ah),
                    logical=(w, h), previous=existing.size if existing else None)
    except Exception:
        pass
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
                    0, ah - ch, cw, ah,  # dst (new FBO, same screen corner)
                    gl.GL_COLOR_BUFFER_BIT,
                    gl.GL_NEAREST,  # No scaling -> NEAREST is exact and cheap
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


# Frames dropped from the inner side of an invalidate trace so the notified
# jump target is the CALLER of invalidate - not this file's plumbing, not the
# DrawState.invalidate* shims.
_INVALIDATE_SKIP_FILES = ("blit_offscreen.py", "draw_state.py")
_INVALIDATE_SKIP_FUNCS = ("invalidate", "invalidate_up", "invalidate_parent", "invalidate_by_obj",
                          "invalidate_up_by_obj", "invalidate_all", "invalidate_scrolled_in")


def notify_invalidate_stack(note, draw_state, kind="invalidate"):
    """Toast (tag "invalidate") carrying the caller's stack — click it to open
    the call site in the editor. notify() dedupes repeats of the same site and
    shows a count, so this is safe to call per invalidate."""
    stack = capture_stack(skip_files=_INVALIDATE_SKIP_FILES, skip_funcs=_INVALIDATE_SKIP_FUNCS)
    if not stack:
        return
    name = getattr(note, "name", None) or kind
    view = getattr(getattr(draw_state, "_view_func", None), "__name__", None)
    text = f"{kind}: {name}" + (f"  [{view}]" if view else "")
    notify(text, tint=getattr(note, "tint", (1, 0.6, 0.3))[:3] or (1, 0.6, 0.3),
           tag="invalidate", stack=stack, urgent=False)


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
uniform sampler2D uWinMask;  // window-occlusion mask (_build_window_mask)
uniform float uWinZ;         // owner window's encoded rank; 1.0 = ungated
uniform vec2 uFBSize;        // full-mask dims, for gl_FragCoord -> mask UV
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
    // Window occlusion: covered by a window in front of the mark's own.
    if (texture(uWinMask, gl_FragCoord.xy / uFBSize).r > uWinZ + 0.00048) {
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

# flush_captures logs its per-pass split for any call slower than this
# (ms) - see the _fc_marks stamps. [text_color=(0.95, 0.55, 0.15)]
_FC_TRACE_MS = 1.0

# Batched rect marks (goggles.Melty.batch_shadow_stamps): the SAME gradient
# as _SHADOW_GRAD_FS, but one instanced quad per mark with the per-mark
# uniforms riding as instance attributes - ~280 marks a frame cost 2.5 ms
# of per-mark GL state on PyOpenGL; batched they are one buffer upload
# and one draw per blend equation. The quad is the mark's rect clipped to
# its scissor (aClip) but vUV stays RECT-relative, so the gradient is
# exact for any size (the per-mark path approximates a 4096 px rect by
# re-evaluating its corners at the clamped edges).
_SHADOW_BATCH_VS = """
#version 330 core
layout(location = 0) in vec4 aRect;    // x0, y0, w, h — framebuffer px, y up
layout(location = 1) in vec4 aClip;    // x0, y0, x1, y1 — rect ∩ scissor
layout(location = 2) in vec4 aRanks;   // (TL, TR, BL, BR) / 65535.5
layout(location = 3) in vec4 aParams;  // corner radius, margin, winZ, unused
uniform vec2 uFBSize;
out vec2 vUV;
flat out vec4 vRanks;
flat out vec4 vParams;
flat out vec2 vRectSize;
void main() {
    vec2 c = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1));
    vec2 p = mix(aClip.xy, aClip.zw, c);
    vUV = (p - aRect.xy) / aRect.zw;
    vRanks = aRanks;
    vParams = aParams;
    vRectSize = aRect.zw;
    gl_Position = vec4(p / uFBSize * 2.0 - 1.0, 0.0, 1.0);
}
"""

_SHADOW_BATCH_FS = """
#version 330 core
uniform sampler2D uWinMask;  // window-occlusion mask (_build_window_mask)
uniform vec2 uFBSize;        // full-mask dims, for gl_FragCoord -> mask UV
in vec2 vUV;
flat in vec4 vRanks;
flat in vec4 vParams;
flat in vec2 vRectSize;
out vec4 oColor;

float sdRoundedBox(vec2 p, vec2 b, float r, float margin) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), margin) + length(max(q, margin)) - r;
}

void main() {
    vec2 pixelPos = (vUV - 0.5) * vRectSize;
    vec2 halfSize = vRectSize * 0.5;
    float r = min(vParams.x, min(halfSize.x, halfSize.y));
    if (sdRoundedBox(pixelPos, halfSize, r, vParams.y) > 0.0) {
        discard;
    }
    if (texture(uWinMask, gl_FragCoord.xy / uFBSize).r > vParams.z + 0.00048) {
        discard;
    }
    vec2 t = vUV * vUV * (3.0 - 2.0 * vUV);
    float top    = mix(vRanks.x, vRanks.y, t.x);
    float bottom = mix(vRanks.z, vRanks.w, t.x);
    oColor = vec4(mix(bottom, top, t.y), 0.0, 0.0, 1.0);
}
"""

# add_shadow_shape() marks: arbitrary triangle-strip geometry with a rank per
# VERTEX - the complex-shape sibling of the rect gradient above (compare
# ribbons, future non-rect chrome). Positions arrive pre-transformed to NDC
# within the mark's bbox viewport; rank interpolates linearly (barycentric)
# between vertices, so a caller wanting eased grading samples its curve
# densely (the rect's smoothstep slices already do). Same MAX/MIN rank and
# window-occlusion gate as the rect path - only the geometry source differs.
_SHADOW_SHAPE_VS = """
#version 330 core
layout(location = 0) in vec2 aPos;   // NDC within the mark's bbox viewport
layout(location = 1) in float aRank; // rank pre-normalized to [0, 1]
out float vRank;
void main() {
    vRank = aRank;
    gl_Position = vec4(aPos, 0.0, 1.0);
}
"""

_SHADOW_SHAPE_FS = """
#version 330 core
uniform sampler2D uWinMask;  // window-occlusion mask (_build_window_mask)
uniform float uWinZ;         // owner window's encoded rank; 1.0 = ungated
uniform vec2 uFBSize;        // full-mask dims, for gl_FragCoord -> mask UV
in float vRank;
out vec4 oColor;
void main() {
    if (texture(uWinMask, gl_FragCoord.xy / uFBSize).r > uWinZ + 0.00048) {
        discard;
    }
    oColor = vec4(vRank, 0.0, 0.0, 1.0);
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
uniform sampler2D uWinMask;  // window-occlusion mask (_build_window_mask)
uniform float uWinZ;         // emitter window's encoded rank; 1.0 = ungated
uniform vec2 uGlowSize;      // glow buffer dims, for gl_FragCoord -> mask UV
uniform vec2 uRectSize;      // EXPANDED quad size in glow-buffer pixels
uniform float uCornerRadius; // corner radius of the inner rect, glow px
uniform float uRadius;       // falloff skirt width, glow px
uniform float uFalloff;      // inverse-square hardness k (0 = linear)
uniform int uAreaLight;      // 1 = downward-facing area-light mode
uniform float uAreaHold;     // fraction of uRadius held at full brightness
uniform float uAreaSpread;   // lateral trapezoid widening, px per px of drop
uniform float uAreaFalloff;  // falloff curve exponent past the hold point
uniform float uAreaEdgeBlur; // fan-edge penumbra width, px per px of drop
uniform float uAreaTanA;     // tan(tilt angle); + shears the fan screen-right
uniform int uAreaTopEdge;    // 1 = fan hangs from the rect's TOP edge
uniform int uAreaEdges;      // 1 = emit from the rect's left/right/bottom edges
uniform float uExpand;       // quad expansion around the rect, glow px
uniform int uDebugSolid;     // 1 = hard rect + 30% skirt (positioning debug)
in vec2 vUV;
out vec4 oColor;

float sdRoundedBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

void main() {
    vec2 pixelPos = (vUV - 0.5) * uRectSize;
    vec2 halfSize = uRectSize * 0.5 - vec2(uExpand);
    float r = min(uCornerRadius, min(halfSize.x, halfSize.y));
    float d = sdRoundedBox(pixelPos, halfSize, r);
    // Area-light mode owns its own extent (the trapezoid), so the radial
    // SDF cut must not apply — it truncated the fan's far corners at a
    // hard Euclidean edge before the vertical falloff reached zero.
    if (d >= uRadius && uAreaLight != 1) {
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
    // Window occlusion: no light on (or through) windows in front of the
    // emitter's own window — same normalized UV as the depth sample.
    if (texture(uWinMask, gl_FragCoord.xy / uGlowSize).r > uWinZ + 0.00048) {
        discard;
    }
    float t = clamp(d / max(uRadius, 1.0), 0.0, 1.0);
    float p;
    if (uDebugSolid == 1) {
        p = (d <= 0.0) ? 1.0 : 0.3;
    } else if (uAreaLight == 1) {
        // Downward-facing area light: light falls in a fan BELOW the source
        // edge (vUV.y runs bottom-to-top in the framebuffer, so screen-down
        // is -y). The source edge is the rect's TOP edge when uAreaTopEdge
        // (the fan then washes down THROUGH the rect and past it) or its
        // BOTTOM edge (rect interior stays fully lit, fan starts under it).
        // Brightness holds flat for uAreaHold of the radius, then falls as
        // smoothstep^uAreaFalloff — smooth at both ends (no hard far edge).
        // uAreaTanA shears the whole fan sideways with drop (a tilted
        // light); the side edges get a PENUMBRA that widens with drop
        // (uAreaEdgeBlur px per px, symmetric about the trapezoid edge):
        // razor-sharp at the source, progressively blurrier further down.
        // uAreaEdges instead wraps the light around the rect's LEFT, RIGHT
        // and BOTTOM edges: the same hold+smoothstep^exponent profile, but
        // run on the rounded-box SDF distance (sheared by uAreaTanA as it
        // drops below the TOP edge, so the whole skirt leans with the
        // light) and killed above the top edge — the skirt tapers to
        // nothing over the token's height approaching the top corners, so
        // the top edge itself stays dark.
        if (uAreaEdges == 1) {
            float dropTop = halfSize.y - pixelPos.y;
            if (dropTop <= 0.0) {
                discard;
            }
            if (d <= 0.0) {
                p = 1.0;
            } else {
                vec2 sheared = vec2(pixelPos.x - dropTop * uAreaTanA,
                                    pixelPos.y);
                float d2 = sdRoundedBox(sheared, halfSize, r);
                float t2 = clamp(d2 / max(uRadius, 1.0), 0.0, 1.0);
                p = pow(1.0 - smoothstep(uAreaHold, 1.0, t2),
                        max(uAreaFalloff, 0.01));
                p *= smoothstep(0.0, max(1.0, 2.0 * halfSize.y), dropTop);
                if (p <= 0.0) {
                    discard;
                }
            }
            oColor = vec4(uColor.rgb * uColor.a * p, 1.0);
            return;
        }
        float edgeY = (uAreaTopEdge == 1) ? halfSize.y : -halfSize.y;
        float drop = edgeY - pixelPos.y;
        if (uAreaTopEdge != 1 && d <= 0.0) {
            p = 1.0;
        } else {
            if (drop <= 0.0) {
                discard;
            }
            float tv = clamp(drop / max(uRadius, 1.0), 0.0, 1.0);
            float vert = pow(1.0 - smoothstep(uAreaHold, 1.0, tv),
                             max(uAreaFalloff, 0.01));
            float lat = abs(pixelPos.x - drop * uAreaTanA)
                        - (halfSize.x + drop * uAreaSpread);
            float feather = max(0.5, drop * uAreaEdgeBlur);
            p = vert * (1.0 - smoothstep(-feather, feather, lat));
            if (p <= 0.0) {
                discard;
            }
        }
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
        # nested_layer_max * max_depth (8192 with 256 * 32). Anything tied at
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
        from meltygui.core.cache.parameter_dependencies import ParameterDependencies
        self.parameter_dependencies = ParameterDependencies()
        # Maps a draw function's id() -> set of view keys it produced, so we
        # can invalidate every view drawn by a given @draw_func (e.g. draw_text).
        self.func_id_to_keys: Dict[int, set] = {}
        self.key_to_parent_key: Dict[str, str] = {}
        self.parent_key_to_child_keys: Dict[str, dict] = {}
        self.parent_key_to_child_keys_last: Dict[str, dict] = {}
        self.key_to_draw_state: Dict[str, any] = {}

        self._tiles: Dict[str, Tile] = {}
        # Key -> draw_state of every freeze_resize view served FROZEN since
        # the resize gesture began: mask_begin_frame settles them (one
        # live render at the final dimensions) on the first frame the gesture is
        # over. A mouse gesture's release already re-renders the ancestors,
        # but a compositor resize (Melty.os_resize_frame) ends silently.
        self._frozen_served: Dict[str, any] = {}
        self._resize_input_keys = set()
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

        # Window-occlusion mask for shadow andlow stamps (_build_win_mask):
        # each dispatched window's live rect at its paint-order rank.
        self._win_mask_tex: Optional[int] = None
        self._win_mask_fbo: Optional[int] = None
        self._win_mask_size = (0, 0)
        self._win_z_by_ds = {}
        # 256x1 R32F table of the same windows' fb-space rects, one
        # column per mask rank - the specular fade's per-window geometry
        # (see the upload at the end of _build_window_mask).
        self._win_rects_tex: Optional[int] = None

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
        # (id(ds), kind) -> marks emitted this frame, kind 'shadow'|'glow'.
        # Enforces the Toggles.shadow_cap per-view budget - see
        # _emit_cap_hit. Cleared every frame with the mark lists.
        self._emit_counts: Dict[tuple, int] = {}
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
        self._mask_batch = None
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
        self._occ_scroll_frame: int = -10 ** 9

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

    def _accumulate_filled(self, t: Tile, draw_state, on_screen=None) -> None:
        """Union the current frame's visible-portion (tile size minus
        ``clipped_by_rect`` insets, further cut to ``on_screen`` — the part
        of the tile rect inside the display, in tile-local xyxy) into the
        tile's cumulative ``filled_bbox``. Texels outside the display never
        get real pixels (PASS 3 samples the snapshot, black past its edge),
        so a capture of a view hanging off the screen counts only for the
        part that was on it — the serve gate (_visible_unfilled) re-renders
        the rest once the view moves into view (a context menu opens at its
        spawner's top-right corner, off the display's right edge, and is
        pinned back inside a frame later: served from that first capture
        its rows showed two letters, 09-13)."""
        if t is None or draw_state is None:
            return
        cb = draw_state.clipped_by_rect or (0, 0, 0, 0)
        w, h = t.size
        bl = max(0, int(cb[0]))
        bt = max(0, int(cb[1]))
        br = max(bl, w - max(0, int(cb[2])))
        bb = max(bt, h - max(0, int(cb[3])))
        if on_screen is not None:
            bl, bt = max(bl, int(on_screen[0])), max(bt, int(on_screen[1]))
            br, bb = min(br, int(on_screen[2])), min(bb, int(on_screen[3]))
        if br <= bl or bb <= bt:
            return  # nothing actually written this frame
        if t.filled_bbox is None:
            t.filled_bbox = (bl, bt, br, bb)
        else:
            pl, pt, pr, pbottom = t.filled_bbox
            t.filled_bbox = (min(pl, bl), min(pt, bt), max(pr, br), max(pbottom, bb))

    @staticmethod
    def _display_rect_screen():
        """The display (content) rect in screen coords, xyxy: what a
        capture can actually sample. None without draw data."""
        try:
            io = imgui.get_io()
            w, h = io.display_size
        except Exception:
            return None
        if not w or not h:
            return None
        return (0.0, 0.0, float(w), float(h))

    def _on_screen_tile_rect(self, x, y, w, h):
        """The part of the tile rect (x, y, w, h in screen coords) inside
        the display, in tile-local xyxy; the whole tile without a display."""
        disp = self._display_rect_screen()
        if disp is None:
            return (0, 0, int(w), int(h))
        return (max(0.0, disp[0] - x), max(0.0, disp[1] - y),
                min(float(w), disp[2] - x), min(float(h), disp[3] - y))

    def _visible_unfilled(self, t: Tile, draw_state) -> bool:
        """True when the part of ``draw_state``'s rect now on screen (and
        inside its clip) has texels the tile never captured (filled_bbox):
        serving it would show the black past the display's edge that an
        off-screen capture recorded. Such a tile is re-rendered instead."""
        if t is None or draw_state is None:
            return False
        if t.filled_bbox is None:
            return t.last_clean_frame >= 0
        x, y = draw_state.abs_left, draw_state.abs_top
        if x is None or y is None:
            return False
        w, h = t.size
        vl, vt, vr, vb = self._on_screen_tile_rect(x, y, w, h)
        cb = draw_state.clipped_by_rect or (0, 0, 0, 0)
        vl, vt = max(vl, cb[0] or 0), max(vt, cb[1] or 0)
        vr, vb = min(vr, w - (cb[2] or 0)), min(vb, h - (cb[3] or 0))
        if vr <= vl or vb <= vt:
            return False            # nothing of this is visible: nothing to miss
        fl, ft, fr, fb = t.filled_bbox
        return fl > vl + 0.5 or ft > vt + 0.5 or fr < vr - 0.5 or fb < vb - 0.5

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

    # The caches of the app's OS windows, each with the call that asks for its
    # window's frame (surface.Surface adds and removes its own; the studio has
    # none). An object or a render function is shown by views of any window,
    # while Melty.cache is only the window drawing now: the by-object and
    # by-function invalidations reach the others through here, and a window
    # they hit draws (it skips its frames otherwise, Surface.wants_frame).
    window_caches: dict = {}

    def _invalidate_other_windows(self, method, shows, *args, **kwargs):
        for cache, request_window_frame in list(TileCacheMasked.window_caches.items()):
            if cache is not self and shows(cache):
                getattr(cache, method)(*args, other_windows=False, **kwargs)
                request_window_frame()

    @staticmethod
    def _obj_key(obj, name) -> str:
        return f"{id(obj)}.{name}" if name is not None else f"{id(obj)}"

    def invalidate_up_by_obj(self, obj, name=None, max_depth=4, force=False, frame_delta=0, note=None,
                             other_windows=True):
        dependencies = getattr(self, 'parameter_dependencies', None)
        if dependencies is not None:
            dependencies.invalidate(self, obj, frame_delta=frame_delta, note=note)
        obj_key = self._obj_key(obj, name)
        keys = self.py_id_to_keys.get(obj_key, None)
        if keys is not None:
            for k in keys:
                self.invalidate_up(k, max_depth=max_depth, force=force, frame_delta=frame_delta, note=note)
        if other_windows:
            self._invalidate_other_windows("invalidate_up_by_obj", lambda cache: (obj_key in cache.py_id_to_keys or
                                                (getattr(cache, "parameter_dependencies", None) is not None
                                                 and cache.parameter_dependencies.subscribers(obj))),
                                           obj, name=name, max_depth=max_depth, force=force,
                                           frame_delta=frame_delta, note=note)

    def invalidate_by_obj(self, obj, name=None, frame_delta=0, note=None, other_windows=True):
        dependencies = getattr(self, 'parameter_dependencies', None)
        if dependencies is not None:
            dependencies.invalidate(self, obj, frame_delta=frame_delta, note=note)
        obj_key = self._obj_key(obj, name)
        keys = self.py_id_to_keys.get(obj_key, None)
        if keys is not None:
            for k in keys:
                self.invalidate(k, frame_delta=frame_delta, note=note)
        if other_windows:
            self._invalidate_other_windows("invalidate_by_obj", lambda cache: (obj_key in cache.py_id_to_keys or
                                                (getattr(cache, "parameter_dependencies", None) is not None
                                                 and cache.parameter_dependencies.subscribers(obj))),
                                           obj, name=name, frame_delta=frame_delta, note=note)

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

    def invalidate_by_func(self, func, frame_delta=0, note=None, other_windows=True):
        """Invalidate every view drawn by the given @render_func, e.g.
        invalidate_by_func(draw_text) rerenders all text views."""
        for k in self._keys_for_func(func):
            self.invalidate(k, frame_delta=frame_delta, note=note)
        if other_windows:
            self._invalidate_other_windows("invalidate_by_func", lambda cache: cache._keys_for_func(func),
                                           func, frame_delta=frame_delta, note=note)

    def invalidate_up_by_func(self, func, max_depth=4, force=False, frame_delta=0, note=None, other_windows=True):
        """Like invalidate_by_func, but also cascades up to parents/children."""
        for k in self._keys_for_func(func):
            self.invalidate_up(k, max_depth=max_depth, force=force, frame_delta=frame_delta, note=note)
        if other_windows:
            self._invalidate_other_windows("invalidate_up_by_func", lambda cache: cache._keys_for_func(func),
                                           func, max_depth=max_depth, force=force, frame_delta=frame_delta, note=note)

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

    # [tint=(0.72, 0.11, 0.11), show_tint=True]
    def invalidate_up(self, k: str, max_depth=4, force=False, frame_delta=0, note=None, skip_self=False,
                      stop_at_filled: bool = False, bypass_clip=False, include_windows: bool = False) -> None:
        draw_state = self.key_to_draw_state.get(k, None)
        if note is None:
            note = Note(name="Unnamed invalidate_up", reason="", tint=(1, 0, 0),
                        frame=Melty.frame_count, draw_state=draw_state)

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
            notify_invalidate_stack(note, parent_draw_state, kind="invalidate_up")
        for top, child, child_draw_state in child_keys_list:

            inside_clip, below, above = parent_draw_state.is_inside_clip(child_draw_state)
            if child_draw_state is not None and child_draw_state._print_last_invalid:
                notify_invalidate_stack(note, child_draw_state, kind="invalidate_up (child)")
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
        from meltygui.core.conversion.dict_conversion import DictConversion

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
    # [tint=(0.72, 0.11, 0.11), show_tint=True]
    def invalidate_parent(self, k: str, force=False, do_store=True, frame_delta=0, note=None,
                          stop_at_filled: bool = False) -> None:
        draw_state = self.key_to_draw_state.get(k, None)
        if draw_state is None:
            return
        parent = draw_state.parent_window if draw_state.parent_window is not None else draw_state.parent
        self.invalidate(parent._tile_id, force=force, do_store=do_store, frame_delta=frame_delta, note=note,
                        stop_at_filled=stop_at_filled)

    # [tint=(0.72, 0.11, 0.11), show_tint=True]
    def invalidate(self, k: str, force=False, do_store=True, frame_delta=0, note=None,
                   stop_at_filled: bool = False) -> None:

        draw_state = self.key_to_draw_state.get(k, None)

        # if draw_state is not None and not draw_state.inside_clip:
        #     return

        if note is None:
            # Stamp the CALLER into the name: the capture-tiles trace showed
            # the focused editor's tile re-capturing every frame with reason
            # "Unnamed invalidate" - useless for finding who. One _getframe
            # walk per unnamed invalidate (~µs, not per-pixel) names the line;
            # skip our own invalidate_parent methods.
            try:
                caller = sys._getframe(1)
                # Walk past the forwarding shims (DrawState.invalidate*,
                # our invalidate_parent) to the frame that actually decided
                # to invalidate.
                hops = 0
                while (caller is not None and hops < 4
                       and caller.f_code.co_name in (
                           "invalidate", "invalidate_up", "invalidate_parent",
                           "invalidate_by_obj", "invalidate_up_by_obj")):
                    caller = caller.f_back
                    hops += 1
                site = (f"{caller.f_code.co_filename.rsplit('/', 1)[-1]}"
                        f":{caller.f_lineno}" if caller is not None else "?")
            except Exception:
                site = "?"
            note = Note(name=f"Unnamed invalidate {site}", reason="", tint=(1, 0, 0, 0.1),
                        frame=Melty.frame_count, draw_state=draw_state)

        note.draw_state = draw_state

        if note.frame == 0:
            note.frame = Melty.frame_count

        # Same gate as InvalidateTracker.invalidations below, but the
        # "invalidate" notify column shows every invalidation the tracker
        # overlay sees (named or not, hover changes included); notify()
        # collapses repeats of one call site into a counted entry.
        if (Toggles.InvalidateTracker.enable or Toggles.InvalidateTracker.invalidate_stack_trace
                or (draw_state is not None and draw_state._print_last_invalid)):
            notify_invalidate_stack(note, draw_state)
            Melty.last_print_invalidate = Melty.frame_count

        t = self._tiles.get(k)
        if t is not None:
            target_frame = self._frame_id + 1
            _bump_note(t, f"invalidate:{getattr(note, 'name', None)}")
            self._note_invalidation(k, note, force)
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
                        notify_invalidate_stack(note, parent_draw_state, kind="invalidate (ancestor)")
                    pt.force_invalidate = True
                    _bump_note(pt, f"anc-of:{k[:48]}:{getattr(note, 'name', None)}")
                    pt.last_invalidated_frame = max(pt.last_invalidated_frame, self._frame_id + 1 + frame_delta)
                    pt.dirty = self._is_dirty(pt)
                    if Toggles.InvalidateTracker.enable:
                        InvalidateTracker.invalidations[k] = note
                    if stop_at_filled and self._tile_fully_filled(pt):
                        break
    # [tint=(0.72, 0.11, 0.11), show_tint=True]
    def invalidate_all(self, other_windows=True) -> None:
        for t in self._tiles.values():
            if t is not None:
                _bump_note(t, "invalidate_all")
                t.last_invalidated_frame = max(t.last_invalidated_frame, self._frame_id + 1)
                t.force_invalidate = True
                # self.force_invalid.append(t)
        request_render()
        if other_windows:
            self._invalidate_other_windows("invalidate_all", lambda cache: True)
    
    # [tint=(0.72, 0.11, 0.11), show_tint=True]
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
            if tile_id is None:
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

        if (imgui.is_mouse_down(0) or imgui.is_mouse_down(2) or imgui.is_mouse_down(1)
                or Melty.space_mouse_drag):
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
        if Melty.frame_count - getattr(self, "_occ_scroll_frame", -10 ** 9) < 8:
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
        if getattr(self, "_mask_batch", None) is not None:
            self._mask_batch.close()
            self._mask_batch = None
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

    # --- MCP query diagnostics (mcp_query.tile_cache) ---------------------
    # Class-level attributes so a hotswapped method finds them under the live
    # object. `_frame_stats`: one (frame, body_runs, cache_hits, captures)
    # per query frame; `_invalidation_history`: (frame, key, note name,
    # reason, force) per invalidate that destroyed a tile. Both bounded.
    _frame_body_runs = 0
    _frame_cache_hits = 0
    _frame_stats = None
    _invalidation_history = None
    FRAME_STATS_LEN = 600
    INVALIDATION_HISTORY_LEN = 4000

    def _note_frame_stats(self) -> None:
        stats = self._frame_stats
        if stats is None:
            from collections import deque
            stats = self._frame_stats = deque(maxlen=self.FRAME_STATS_LEN)
        stats.append((Melty.frame_count, self._frame_body_runs, self._frame_cache_hits,
                      len(self._pending)))
        self._frame_body_runs = 0
        self._frame_cache_hits = 0

    def _note_invalidation(self, k: str, note, force: bool) -> None:
        history = self._invalidation_history
        if history is None:
            from collections import deque
            history = self._invalidation_history = deque(maxlen=self.INVALIDATION_HISTORY_LEN)
        history.append((Melty.frame_count, k, getattr(note, "name", None),
                        getattr(note, "reason", None), bool(force)))

    def _refresh_resize_input_keys(self):
        """Keep the active drag receiver and its cached ancestors live.

        A tile resize may freeze siblings, but an internal column/row drag
        still needs its body to consume input and push enclosing edges.
        Derive the set once per frame from delivered events and cache ancestry.
        """
        from meltygui.core.input.input_handler import EventAction
        keys = set()
        for events in Melty.events.values():
            for event in events.values():
                if event.action not in (EventAction.DRAGGED, EventAction.DOUBLE_DRAGGED):
                    continue
                key = event.tile_id
                while key is not None and key not in keys:
                    keys.add(key)
                    key = self.key_to_parent_key.get(key)
        self._resize_input_keys = keys

    def mask_begin_frame(self, framebuffer_size: Tuple[int, int]) -> None:
        fb_w, fb_h = map(int, framebuffer_size)
        self._note_frame_stats()
        self._refresh_resize_input_keys()
        self._frame_id += 1
        if self._frozen_served and Melty.os_resize_live():
            # Frames until the OS resize settles: no input drives change.
            request_render()
        if self._frozen_served and not Melty.resize_gesture_live():
            for key, ds in self._frozen_served.items():
                if not getattr(ds, "closed", False):
                    self.invalidate_up(key, max_depth=4, force=True,
                                       note=Note(name="freeze settle", reason="resize gesture over",
                                                 tint=(0.5, 1.0, 0.5)))
            self._frozen_served = {}
            request_render()
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
        if getattr(self, "_depth_cleared", None) is not None:
            self._depth_cleared.clear()
        if getattr(self, "_emit_counts", None) is not None:
            self._emit_counts.clear()
        self._rect_seq = 0

    def clear_mask_outside(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        """Zero the full depth mask outside the framebuffer rect
        (x0, y0)-(x1, y1) (fb pixels, origin bottom-left) — the frameless
        window's shadow margin. Windows may overhang the content edge, and
        their marks (and their children's) overhang with them, so the
        margin's depth mask carried plateaus that cast shadows of their
        own out there: button ghosts, a window's drop shadow running on
        to the surface edge. Cleared, the margin only ever sees the
        CONTENT's silhouette — an overhanging window casts from the frame
        edge exactly like the frame does. Four scissored clears; GL state
        restored. Call after the mask is built, before the shadow pass."""
        if self._full_mask_fbo is None:
            return False
        fb_w, fb_h = self._fb_size
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(fb_w, int(x1)), min(fb_h, int(y1))
        strips = [(0, 0, fb_w, y0), (0, y1, fb_w, fb_h - y1),
                  (0, y0, x0, y1 - y0), (x1, y0, fb_w - x1, y1 - y0)]
        strips = [s for s in strips if s[2] > 0 and s[3] > 0]
        if not strips:
            return False
        st = _GLState()
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._full_mask_fbo)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glClearColor(0.0, 0.0, 0.0, 0.0)
            gl.glEnable(gl.GL_SCISSOR_TEST)
            for sx, sy, sw, sh in strips:
                gl.glScissor(sx, sy, sw, sh)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        finally:
            st.restore()
        return True

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

    def _emit_cap_hit(self, draw_state, kind: str) -> bool:
        """Per-view budget for shadow/glow marks (Toggles.shadow_cap): one
        draw_state may emit at most cap marks of each kind per frame; past
        it the mark is dropped. Without this, a view whose body emits per
        content item (draw_text per block/symbol) can leak unbounded marks
        into the retained stores, and every retained mark re-stamps every
        finalize. Counts reset each frame (and on clear_glows for that
        view, so a second same-frame body run gets a fresh budget) — the
        budget recycles, next frame's first cap marks win. Ownerless
        one-shot marks (no draw_state) are exempt: they die with the
        frame. Lazily creates the counter dict so a hotswap onto an
        instance whose __init__ predates it just starts empty."""
        if draw_state is None:
            return False
        counts = getattr(self, "_emit_counts", None)
        if counts is None:
            counts = self._emit_counts = {}
        key = (id(draw_state), kind)
        n = counts.get(key, 0)
        if n >= Toggles.shadow_cap:
            return True
        counts[key] = n + 1
        return False

    def add_shadow(
            self, rect: Tuple[float, float, float, float], offset: float = 2.0,
            layer: int = None, depth: int = None, corner_radius: float = 5.0,
            margin: float = 0.0, clip: bool = True, draw_state=None,
            group=None,
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

        Retention (`draw_state=`): a raised mark is also RETAINED under its
        emitting draw_state and re-stamped into the full mask every frame
        until its owner clears it — see clear_glows / clear_shadows. `group`
        names which owner: None is the BODY's group (a body calls
        clear_glows(ds) at its top and re-emits what it still wants), any
        other value is a group some OTHER code draws for that draw_state —
        the wrapper's scrollbar grab (core_render.SCROLLBAR_SHADOW_GROUP)
        — and clears with clear_shadows(ds, group) before re-emitting.
        Groups are independent: a served-frame scrollbar re-emit keeps the
        body's retained marks, and a body run keeps the scrollbar's.

        rect is (x, y, w, h) in screen coords. layer defaults to
        Melty.paint_rank (the paint-order rank, the same unit as
        depth_and_layer), depth to Melty.shadow_depth, both read at call
        time. clip=True snapshots the LIVE clip rect now and scissors the
        mark with it at draw time; an explicit (x0, y0, x1, y1) tuple clips
        to that rect instead (e.g. a draw-list view's abs_clip_rect);
        False/None disables clipping.
        """
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            return
        if layer is None:
            layer = Melty.paint_rank
        depth_defaulted = depth is None
        if depth_defaulted:
            depth = Melty.shadow_depth
        if clip is True:
            clip_xyxy = Melty.get_clip_rect()
        elif clip:
            clip_xyxy = tuple(clip)
        else:
            clip_xyxy = None
        if clip_xyxy is not None and self._fully_clipped(x, y, w, h, clip_xyxy):
            return
        if self._emit_cap_hit(draw_state, "shadow"):
            return
        owner_key = self._mark_owner(draw_state)
        if isinstance(offset, (tuple, list)):
            offs = tuple(float(o) for o in offset)
            if len(offs) != 4:
                raise ValueError(
                    "add_shadow offset must be a scalar or a 4-tuple "
                    "(top_left, top_right, bottom_left, bottom_right)")
            # MIN-blend (recess) only when the whole quad sits at-or-below
            # the surface; any raised corner stamps MAX so the mark can't
            # carve neighbours it eases across.
            inset = all(o <= 0 for o in offs) and any(o < 0 for o in offs)
            ranks = tuple(max(0.0, shadow_depth_at(depth + o, layer))
                          for o in offs)
        else:
            # Scalar offset - the common case, ~100 marks a frame: one rank,
            # four equal entries, no generator round trips.
            o = float(offset)
            inset = o < 0
            rank = max(0.0, shadow_depth_at(depth + o, layer))
            ranks = (rank, rank, rank, rank)
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
            # Anchor carries the emitter's CURRENT shadow rank alongside
            # its position: mark ranks are absolute, so a later z change
            # (another window raised above, this one lowered) re-stamps
            # them via the live-vs-recorded shadow_depth delta - without
            # it, retained marks kept casting at their old high ranks and
            # MAX-punched through windows now floating above. The emitter's
            # own ds only re-stamps depth_and_layer when its wrapper runs,
            # which never happens while an ancestor blit-serves - exactly
            # the frames a z reorder leaves everything cache-served. The
            # ROOT window's ds IS re-stamped every frame (the layer delta),
            # so the anchor also records the root's rank: when the
            # emitter's own delta reads 0 (stale stamp), the root's
            # live-vs-recorded delta supplies the shift instead.
            self._depth_frame.append(
                (mark, draw_state,
                 self._retain_anchor(draw_state, depth_defaulted), group))

    def _retain_anchor(self, draw_state, depth_defaulted):
        """(abs_left, abs_top, emitter_rank, root_rank, depth_defaulted)
        recorded at emit time for retained shadow/glow marks — the baseline
        the PASS 6 re-stamp shifts live ranks against. Rank reads are
        guarded: a transient depth_and_layer failure just disables the
        shift for that mark (None entries)."""
        try:
            _base_rank = float(draw_state.shadow_depth)
        except Exception:
            _base_rank = None
        _root_rank = None
        try:
            _root = self._glow_root_ds(draw_state)
            if _root is not None:
                _root_rank = float(_root.shadow_depth)
        except Exception:
            _root_rank = None
        return (draw_state.abs_left, draw_state.abs_top,
                _base_rank, _root_rank, bool(depth_defaulted))

    def _anchor_rank_shift(self, eds, root_ds, anchor):
        """Live-vs-recorded rank delta for a retained mark. Prefers the
        emitter's own shadow_depth delta (correct whenever its wrapper ran
        this frame — covers intra-window z_offset changes too). When that
        reads 0 the emitter's stamp may simply be stale (ancestor
        blit-served, wrapper never entered), so fall back to the ROOT
        window's delta — live every frame — but only for marks whose depth
        was DEFAULTED at emit time: an explicitly passed depth is the
        caller pinning absolute placement, keep the legacy behavior there.
        Guarded for hotswap-era short anchors (3-tuples predate root_rank)
        and transient shadow_depth failures."""
        try:
            if len(anchor) < 3 or anchor[2] is None:
                return 0.0
            _eds_delta = float(eds.shadow_depth) - anchor[2]
            if _eds_delta:
                return _eds_delta
            if (len(anchor) > 4 and anchor[4]
                    and anchor[3] is not None and root_ds is not None):
                return float(root_ds.shadow_depth) - anchor[3]
        except Exception:
            pass
        return 0.0

    def add_shadow_strip(
            self, points, offset: float = 2.0, layer: int = None,
            depth: int = None, clip: bool = True, draw_state=None,
            group=None,
    ) -> None:
        """add_shadow for a NON-RECT shape: `points` is a triangle strip of
        screen-space (x, y) vertices (len >= 3) — e.g. a band between two
        polylines interleaved top0, bot0, top1, bot1, … Same signed-offset
        semantics as add_shadow: positive lifts the shape so it casts onto
        its surroundings, all-negative carves a recess (MIN-blended,
        owner-scoped like any inset mark), mixed signs stamp MAX-blended.

        `offset` may also be a sequence with one entry PER VERTEX — each
        vertex gets its own depth and the mark interpolates linearly between
        them across every triangle (the strip analog of the rect's
        per-corner 4-tuple; sample your curve densely if you want eased
        grading). Everything else — layer/depth defaults, clip snapshotting,
        tile-bake persistence while recording, draw_state-keyed retention on
        raised marks — matches add_shadow exactly; corner_radius/margin
        don't apply (the strip's own edges are the shape)."""
        pts = [(float(px), float(py)) for (px, py) in points]
        if len(pts) < 3:
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
        if w <= 0 or h <= 0:
            return
        if layer is None:
            layer = Melty.paint_rank
        depth_defaulted = depth is None
        if depth_defaulted:
            depth = Melty.shadow_depth
        if clip is True:
            clip_xyxy = Melty.get_clip_rect()
        elif clip:
            clip_xyxy = tuple(clip)
        else:
            clip_xyxy = None
        if clip_xyxy is not None and self._fully_clipped(x, y, w, h, clip_xyxy):
            return
        if self._emit_cap_hit(draw_state, "shadow"):
            return
        owner_key = self._mark_owner(draw_state)
        if isinstance(offset, (tuple, list)):
            offs = tuple(float(o) for o in offset)
            if len(offs) != len(pts):
                raise ValueError(
                    "add_shadow_strip offset must be a scalar or a sequence "
                    "with one entry per vertex")
        else:
            offs = (float(offset),) * len(pts)
        inset = all(o <= 0 for o in offs) and any(o < 0 for o in offs)
        ranks = tuple(max(0.0, shadow_depth_at(depth + o, layer))
                      for o in offs)
        shape = tuple((px, py, rk) for (px, py), rk in zip(pts, ranks))
        # Same 10-slot layout as rect marks (every consumer indexes those
        # positionally) + the strip payload at [10]. The rect-only rank
        # 4-tuple is filled with first/last so generic readers see something
        # sane; the stamp code reads the per-vertex ranks from the shape.
        mark = (x, y, w, h, (ranks[0], ranks[-1], ranks[0], ranks[-1]),
                0.0, 0.0, clip_xyxy, owner_key, inset, shape)
        self._shadow_rects.append(mark)
        if draw_state is not None and not inset:
            self._ensure_glow_state()
            self._depth_frame.append(
                (mark, draw_state,
                 self._retain_anchor(draw_state, depth_defaulted), group))

    @staticmethod
    def _retain_group(entry):
        """Retention group of a `_depth_frame` entry — None (the body's
        group) for pre-group entries a hotswap left in the frame list."""
        return entry[3] if len(entry) > 3 else None

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
        if getattr(self, "_depth_cleared", None) is None:
            # (id(draw_state), group) pairs whose retained depth marks drop
            # at finalize unless re-emitted this frame - clear_shadows.
            self._depth_cleared = set()
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

    @staticmethod
    def _enclosing_window_clip(ds):
        """LIVE clip of the windows enclosing `ds`: the intersection of every
        `parent_window`'s abs_clip_rect up the chain (box ∩ its captured
        clip, shifted with the window's movement). This is the rect the
        emitter's own PIXELS are scissored to, so a retained mark must never
        stamp past it either. None when `ds` sits in no window (root-level
        overlays); an EMPTY rect (x1 <= x0 or y1 <= y0) when the windows no
        longer overlap — the caller skips the mark.

        Why windows only: a non-window ancestor's box does not bound its
        children (the wrapper pushes a clip for closable views alone, and a
        pinned float legitimately overhangs the button it hangs off), while
        a window's box ∩ clip is exactly what its content is clipped to.

        Rows culled by their window's body are the case this exists for
        (global search auto-fits its height to the result count: the
        previous query's row editors below the new bottom never run again,
        nothing repaints their territory, and their chip shadows re-stamped
        under the window every frame)."""
        clip = None
        w = ds
        hops = 0
        while w is not None and hops < 64:
            pw = getattr(w, "parent_window", None)
            if pw is None or pw is w:
                break
            try:
                rect = pw.abs_clip_rect
            except Exception:
                try:
                    _l, _t = pw.abs_left, pw.abs_top
                    _w, _h = pw.width, pw.height
                    rect = ((_l, _t, _l + _w, _t + _h)
                            if _w is not None and _h is not None else None)
                except Exception:
                    rect = None
            if rect is not None:
                clip = (rect if clip is None else
                        (max(clip[0], rect[0]), max(clip[1], rect[1]),
                         min(clip[2], rect[2]), min(clip[3], rect[3])))
            w = pw
            hops += 1
        return clip

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
            # Depth marks: the body's group only (group None). Other groups
            # (the wrapper's scrollbar grab) have their own owner, who
            # clears them with clear_shadows - dropping them here would
            # lose a bar emitted earlier this frame, and keep it.
            self._depth_frame[:] = [
                e for e in self._depth_frame
                if not (e[1] is draw_state and self._retain_group(e) is None)]
        # The dropped emissions hand their cap budget back too - the last
        # body run gets the same Toggles.shadow_cap headroom the first had.
        counts = getattr(self, "_emit_counts", None)
        if counts is not None:
            counts.pop((id(draw_state), "shadow"), None)
            counts.pop((id(draw_state), "glow"), None)

    def clear_shadows(self, draw_state, group) -> None:
        """Start of a NON-body owner's retained-depth group for this frame
        (add_shadow's `group`): the marks it retained under `draw_state`
        drop at finalize unless re-emitted this frame, and anything the
        same group emitted earlier THIS frame is dropped too (last emission
        wins, as clear_glows does for bodies). Call it unconditionally
        wherever the group's owner decides whether to draw — the scrollbar
        paths call it before their early returns, so a bar that stops
        drawing (content now fits, scroll disabled, window closed) sheds
        its grab silhouette instead of re-stamping it under the view
        every frame."""
        self._ensure_glow_state()
        self._depth_cleared.add((id(draw_state), group))
        if self._depth_frame:
            self._depth_frame[:] = [
                e for e in self._depth_frame
                if not (e[1] is draw_state and self._retain_group(e) == group)]

    def _fold_depth_retention(self):
        """PASS 6 bookkeeping for retained depth marks, GL-free: apply this
        frame's clears to the per-emitter store, then fold the frame's
        emissions in. The store is keyed (id(draw_state), group) — a body
        run (clear_glows) touches only group None, clear_shadows only its
        own group, and an emission replaces exactly its own group's marks,
        so the scrollbar re-emitting on a served frame never discards the
        body's retained block/gutter marks (and vice versa). Returns the
        set of keys emitted this frame — the re-stamp loop skips those
        (stamped via the fresh path already).

        A store from before the group keys (hotswap: plain id keys) is
        re-keyed as group None on the way in."""
        self._ensure_glow_state()
        retained = self._depth_marks_by_emitter
        for key in list(retained):
            if not isinstance(key, tuple):
                retained[(key, None)] = retained.pop(key)
        pending = self._depth_kill_pending
        for key in list(pending):
            if not isinstance(key, tuple):
                pending.discard(key)
                pending.add((key, None))
        for eid in self._glow_cleared:
            retained.pop((eid, None), None)
        for key in self._depth_cleared:
            retained.pop(key, None)
        emitted = defaultdict(list)
        for entry in self._depth_frame:
            mark, eds, anchor = entry[0], entry[1], entry[2]
            emitted[(id(eds), self._retain_group(entry))].append(
                (mark, eds, anchor))
        for key, entries in emitted.items():
            retained[key] = ([m for m, _d, _a in entries],
                             entries[0][1], entries[0][2])
        return set(emitted)

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
            layer = Melty.paint_rank
        depth_defaulted = depth is None
        if depth_defaulted:
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
        if self._emit_cap_hit(draw_state, "glow"):
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
            anchor = self._retain_anchor(draw_state, depth_defaulted)
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
        ds_f = max(1, int(Toggles.glow_downscale))
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
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D,
                         getattr(self, "_win_mask_tex", None) or 0)
        gl.glUniform1i(self._loc_gl_uWinMask, 1)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._full_mask_tex)
        gl.glUniform1i(self._loc_gl_uDepthMask, 0)
        gl.glUniform2f(self._loc_gl_uGlowSize, float(gw), float(gh))
        _dbg_no_mask = Toggles.glow_debug_no_mask
        gl.glUniform1i(self._loc_gl_uDebugSolid,
                       1 if Toggles.glow_debug_rects else 0)
        gl.glUniform1i(self._loc_gl_uAreaLight,
                       1 if Toggles.glow_area_light else 0)
        gl.glUniform1f(self._loc_gl_uAreaHold,
                       min(0.999, max(0.0, float(Toggles.glow_area_hold))))
        gl.glUniform1f(self._loc_gl_uAreaSpread,
                       max(0.0, float(Toggles.glow_area_spread)))
        gl.glUniform1f(self._loc_gl_uAreaFalloff,
                       max(0.01, float(Toggles.glow_area_falloff)))
        gl.glUniform1f(self._loc_gl_uAreaEdgeBlur,
                       max(0.0, float(Toggles.glow_area_edge_blur)))
        _tan_a = tan(radians(
            min(80.0, max(-80.0, float(Toggles.glow_area_angle)))))
        gl.glUniform1f(self._loc_gl_uAreaTanA, _tan_a)
        gl.glUniform1i(self._loc_gl_uAreaTopEdge,
                       1 if Toggles.glow_area_top_edge else 0)
        gl.glUniform1i(self._loc_gl_uAreaEdges,
                       1 if Toggles.glow_area_edges else 0)
        # Area mode: the tilt shear + spread + penumbra push the fan past
        # the usual `radius` quad expansion - widen the quad so the shear
        # never gets clipped by its own geometry. uExpand tells the shader
        # the actual expansion so it can reconstruct the inner rect.
        _area = bool(Toggles.glow_area_light)
        _ex_mult = (max(1.0, 1.0 + abs(_tan_a)
                        + float(Toggles.glow_area_spread)
                        + float(Toggles.glow_area_edge_blur))
                    if _area else 1.0)
        # Small rank slack above the emitter so coplanar pixels (the band's
        # own glow, sibling text at the same depth) stay lit through R16
        # rounding; ~2 rank units.
        _bias = 2.0 / 65535.5
        for ((sx, sy, sw, sh, rgb, inten, _d_off, _layer_rec, radius, falloff,
              cr, clip_xyxy), delta, rank, floor_rank, live_clip,
             win_z) in glows:
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
            expand = radius * _ex_mult
            x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(
                sx - expand, sy - expand, sw + 2 * expand, sh + 2 * expand,
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
                gl.glUniform1f(self._loc_gl_uWinZ, 1.0)
            else:
                gl.glUniform1f(self._loc_gl_uRankLo,
                               max(0.0, float(floor_rank) - _bias))
                gl.glUniform1f(self._loc_gl_uRankHi, float(rank) + _bias)
                gl.glUniform1f(self._loc_gl_uWinZ, float(win_z))
            gl.glUniform2f(self._loc_gl_uRectSize, float(iw), float(ih))
            gl.glUniform1f(self._loc_gl_uCornerRadius,
                           max(0.0, cr * s_x * sc_x))
            gl.glUniform1f(self._loc_gl_uRadius,
                           max(1.0, radius * s_x * sc_x))
            gl.glUniform1f(self._loc_gl_uExpand,
                           max(1.0, expand * s_x * sc_x))
            gl.glUniform1f(self._loc_gl_uFalloff, max(0.0, falloff))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    def _ensure_batch_state(self) -> None:
        """Program + instanced VAO/VBO for the batched rect marks. Re-keyed
        on the newest uniform's location (hotswap onto a live instance)."""
        if (getattr(self, "_prog_shadow_batch", None) is None
                or getattr(self, "_loc_sb_uFBSize", None) is None):
            vs = _compile(gl.GL_VERTEX_SHADER, _SHADOW_BATCH_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _SHADOW_BATCH_FS)
            self._prog_shadow_batch = _link(vs, fs)
            self._loc_sb_uWinMask = gl.glGetUniformLocation(
                self._prog_shadow_batch, "uWinMask")
            self._loc_sb_uFBSize = gl.glGetUniformLocation(
                self._prog_shadow_batch, "uFBSize")
        if getattr(self, "_batch_vao", None) is None:
            vao = gl.glGenVertexArrays(1)
            vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            stride = 16 * 4
            for loc in range(4):
                gl.glEnableVertexAttribArray(loc)
                gl.glVertexAttribPointer(loc, 4, gl.GL_FLOAT, gl.GL_FALSE,
                                         stride, ctypes.c_void_p(loc * 16))
                gl.glVertexAttribDivisor(loc, 1)
            gl.glBindVertexArray(self._dummy_vao)
            self._batch_vao = int(vao)
            self._batch_vbo = int(vbo)

    def _stamp_shadow_marks_batched(self, shadows, dp_x, dp_y, s_x, s_y, fb_h,
                                    scissor_fb=None, win_gate=True):
        """Batched twin of _stamp_shadow_marks (Toggles.Melty
        .batch_shadow_stamps): rect marks become instance records — 16
        floats each — drawn as ONE instanced quad strip per blend equation
        (GL_MAX for casters, GL_MIN for insets); strip marks (shape payload)
        still go through the per-mark path. Same GL state contract on exit."""
        # [tint=(0.95, 0.55, 0.15)]
        rank_scale = 1.0 / 65535.5
        _fbw, _fbh = self._fb_size
        max_recs, min_recs, strips = [], [], []
        floor_, ceil_ = floor, ceil
        win_z = {}     # owner key to encoded rank, resolved once per owner
        # Per-frame transform cache keyed on the shadow tuple's identity: the
        # same ~115 marks are stamped up to four times a frame (each
        # pending tile's shadow set, the standalone pass, the depth pass),
        # and only the per-call scissor and the window mask differ.
        _xf = getattr(self, '_stamp_xf_cache', (-1, None))
        if _xf[0] != self._frame_id:
            _xf = self._stamp_xf_cache = (self._frame_id, {})
        _xf = _xf[1]
        _sc_fb = scissor_fb
        _sc_key = None
        for s in shadows:
            if len(s) > 10 and s[10] is not None:
                strips.append(s)
                continue
            base = _xf.get(id(s))
            if base is None:
                (sx, sy, sw, sh, d_and_l, cr, margin, clip_xyxy, _owner,
                 inset) = s[:10]
                x0 = (sx - dp_x) * s_x
                x1 = (sx + sw - dp_x) * s_x
                y0 = fb_h - (sy + sh - dp_y) * s_y
                y1 = fb_h - (sy - dp_y) * s_y
                ix0, iy0 = floor_(x0), floor_(y0)
                ix1, iy1 = ceil_(x1), ceil_(y1)
                if clip_xyxy is not None:
                    cx0, cy0, cx1, cy1 = clip_xyxy
                    own_clip = ((cx0 - dp_x) * s_x, fb_h - (cy1 - dp_y) * s_y,
                                (cx1 - dp_x) * s_x, fb_h - (cy0 - dp_y) * s_y)
                else:
                    own_clip = None
                base = _xf[id(s)] = (
                    s, ix0, iy0, ix1, iy1, own_clip, _owner, inset,
                    (ix0, iy0, ix1 - ix0, iy1 - iy0),
                    (d_and_l[0] * rank_scale, d_and_l[1] * rank_scale,
                     d_and_l[2] * rank_scale, d_and_l[3] * rank_scale,
                     cr if cr > 0 else 0.0, margin))
            _, ix0, iy0, ix1, iy1, own_clip, _owner, inset, rect4, tail6 = base
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            sc = _sc_fb
            if own_clip is not None:
                sc = (own_clip if sc is None else
                      (max(sc[0], own_clip[0]), max(sc[1], own_clip[1]),
                       min(sc[2], own_clip[2]), min(sc[3], own_clip[3])))
            if sc is not None:
                kx0, ky0 = max(ix0, floor_(sc[0])), max(iy0, floor_(sc[1]))
                kx1, ky1 = min(ix1, ceil_(sc[2])), min(iy1, ceil_(sc[3]))
                if kx1 <= kx0 or ky1 <= ky0:
                    continue
            else:
                kx0, ky0, kx1, ky1 = ix0, iy0, ix1, iy1
            if win_gate:
                z = win_z.get(_owner)
                if z is None:
                    z = win_z[_owner] = self._win_z_for_owner(_owner)
            else:
                z = 1.0
            recs = min_recs if inset else max_recs
            recs.extend(rect4)
            recs.append(kx0); recs.append(ky0); recs.append(kx1); recs.append(ky1)
            recs.extend(tail6)
            recs.append(z); recs.append(0.0)
        if max_recs or min_recs:
            self._ensure_batch_state()
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glViewport(0, 0, max(1, _fbw), max(1, _fbh))
            gl.glUseProgram(self._prog_shadow_batch)
            _win_tex = getattr(self, "_win_mask_tex", None)
            gl.glActiveTexture(gl.GL_TEXTURE1)
            gl.glBindTexture(gl.GL_TEXTURE_2D,
                             _win_tex if (win_gate and _win_tex) else 0)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glUniform1i(self._loc_sb_uWinMask, 1)
            gl.glUniform2f(self._loc_sb_uFBSize,
                           float(max(1, _fbw)), float(max(1, _fbh)))
            gl.glBindVertexArray(self._batch_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._batch_vbo)
            for recs, equation in ((max_recs, gl.GL_MAX), (min_recs, gl.GL_MIN)):
                if not recs:
                    continue
                buf = array('f', recs).tobytes()   # C-speed pack; (c_float*n)(*recs) was 0.1 ms
                gl.glBufferData(gl.GL_ARRAY_BUFFER, len(buf), buf,
                                gl.GL_STREAM_DRAW)
                gl.glBlendEquation(equation)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4,
                                         len(recs) // 16)
            gl.glBindVertexArray(self._dummy_vao)
            gl.glBlendEquation(gl.GL_FUNC_ADD)
            gl.glDisable(gl.GL_BLEND)
        if strips:
            self._stamp_shadow_marks(strips, dp_x, dp_y, s_x, s_y, fb_h,
                                     scissor_fb=scissor_fb, win_gate=win_gate,
                                     _batched=False)

    def _ensure_shape_state(self) -> None:
        """Lazily create the strip-mark program + streaming VAO/VBO (getattr
        pattern: a hotswap patches methods onto a live instance whose
        __init__ predates these fields). Called with SOME vao bound (the
        pass's dummy vao) — creation rebinds it before returning."""
        if (getattr(self, "_prog_shadow_shape", None) is None
                or getattr(self, "_loc_ss_uWinZ", None) is None):
            vs = _compile(gl.GL_VERTEX_SHADER, _SHADOW_SHAPE_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _SHADOW_SHAPE_FS)
            self._prog_shadow_shape = _link(vs, fs)
            self._loc_ss_uWinMask = gl.glGetUniformLocation(
                self._prog_shadow_shape, "uWinMask")
            self._loc_ss_uWinZ = gl.glGetUniformLocation(
                self._prog_shadow_shape, "uWinZ")
            self._loc_ss_uFBSize = gl.glGetUniformLocation(
                self._prog_shadow_shape, "uFBSize")
        if getattr(self, "_shape_vao", None) is None:
            vao = gl.glGenVertexArrays(1)
            if isinstance(vao, (list, tuple)):
                vao = vao[0]
            vbo = gl.glGenBuffers(1)
            if isinstance(vbo, (list, tuple)):
                vbo = vbo[0]
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glEnableVertexAttribArray(0)
            gl.glEnableVertexAttribArray(1)
            gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 12,
                                     ctypes.c_void_p(0))
            gl.glVertexAttribPointer(1, 1, gl.GL_FLOAT, gl.GL_FALSE, 12,
                                     ctypes.c_void_p(8))
            gl.glBindVertexArray(self._dummy_vao)
            self._shape_vao = int(vao)
            self._shape_vbo = int(vbo)

    def _stamp_shadow_marks(self, shadows, dp_x, dp_y, s_x, s_y, fb_h,
                            scissor_fb=None, win_gate=True, _batched=None):
        """Draw add_shadow() marks into the currently bound R16 mask FBO.
        Rect marks take the batched path (_stamp_shadow_marks_batched) under
        Toggles.Melty.batch_shadow_stamps; `_batched=False` is that path
        handing the strip marks back here.
        Raised marks MAX-blend (can only raise depth), inset marks MIN-blend
        (can only lower it — the recess carve); either way stamping the same
        mark into several masks (tile caches in PASS 4 + the full mask in
        PASS 5) is idempotent, and the rounded shader discards outside its
        SDF so MIN never punches the quad corners. scissor_fb optionally
        intersects every mark's own clip with an outer (x0, y0, x1, y1)
        framebuffer-space rect (PASS 4's tile rect). win_gate: fragments
        covered by a window in front of the mark's own (per the
        _build_window_mask rects) discard — full-mask stamps only; PASS 4
        tile bakes pass False (cached masks outlive today's overlaps and are
        z-composited by PASS 5). Leaves scissor disabled and blend restored
        to FUNC_ADD/off."""
        if _batched is None:
            from meltygui.core.runtime.toggles import Toggles
            _batched = bool(Toggles.Melty.batch_shadow_stamps)
        if _batched:
            self._stamp_shadow_marks_batched(shadows, dp_x, dp_y, s_x, s_y, fb_h,
                                             scissor_fb=scissor_fb,
                                             win_gate=win_gate)
            return
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
        gl.glUseProgram(self._prog_shadow_grad)
        _win_tex = getattr(self, "_win_mask_tex", None)
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D,
                         _win_tex if (win_gate and _win_tex) else 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(self._loc_sg_uWinMask, 1)
        _fbw, _fbh = self._fb_size
        gl.glUniform2f(self._loc_sg_uFBSize,
                       float(max(1, _fbw)), float(max(1, _fbh)))
        for s in shadows:
            # Strip marks (add_shadow_strip) ride the same list with a
            # vertex payload at [10]; retained 10-tuples from a pre-strip
            # hotswap era slice cleanly to shape=None.
            (sx, sy, sw, sh, d_and_l, cr, margin, clip_xyxy, _owner,
             inset) = s[:10]
            shape = s[10] if len(s) > 10 else None
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
            if shape is not None:
                # Strip path: vertices go up as NDC within the mark's bbox
                # viewport with their pre-normalized rank; the shader
                # interpolates rank linearly and applies the same window
                # gate. No >4096 clamp - the scissor bounds fragment size
                # and strip callers are class-band sized, not class-block
                # sized.
                self._ensure_shape_state()
                verts = []
                for (px, py, rk) in shape:
                    fx = (px - dp_x) * s_x
                    fy = fb_h - (py - dp_y) * s_y
                    verts.append(2.0 * (fx - ix0) / iw - 1.0)
                    verts.append(2.0 * (fy - iy0) / ih - 1.0)
                    verts.append(rk / 65535.5)
                gl.glViewport(ix0, iy0, iw, ih)
                gl.glUseProgram(self._prog_shadow_shape)
                gl.glUniform1i(self._loc_ss_uWinMask, 1)
                gl.glUniform2f(self._loc_ss_uFBSize,
                               float(max(1, _fbw)), float(max(1, _fbh)))
                gl.glUniform1f(self._loc_ss_uWinZ,
                               self._win_z_for_owner(_owner)
                               if win_gate else 1.0)
                buf = (ctypes.c_float * len(verts))(*verts)
                gl.glBindVertexArray(self._shape_vao)
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._shape_vbo)
                gl.glBufferData(gl.GL_ARRAY_BUFFER, len(verts) * 4, buf,
                                gl.GL_STREAM_DRAW)
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, len(shape))
                gl.glBindVertexArray(self._dummy_vao)
                continue
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
            gl.glUniform1f(self._loc_sg_uWinZ,
                           self._win_z_for_owner(_owner) if win_gate else 1.0)
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

    def _build_window_mask(self, dp_x, dp_y, s_x, s_y, fb_w, fb_h):
        """Window-occlusion mask for shadow/glow stamps: every dispatched
        window (Melty.paint_ordered_ds — roots + nested, exact visual z
        order) draws its LIVE rounded rect back to front at rank (i+1)/1024
        into an R16 mask. A stamp fragment whose mask value exceeds its own
        window's rank is covered by a window in front and discards in the
        shader — the same occlusion the z-ordered PASS 5 overwrite gives
        regular view marks, but from live rects, so it holds mid-resize and
        for retained re-stamps whose recorded ranks predate a z reorder."""
        if (getattr(self, "_win_mask_tex", None) is None
                or getattr(self, "_win_mask_size", None) != (fb_w, fb_h)):
            if getattr(self, "_win_mask_tex", None):
                gl.glDeleteTextures(1, [self._win_mask_tex])
            if getattr(self, "_win_mask_fbo", None):
                gl.glDeleteFramebuffers(1, [self._win_mask_fbo])
            self._win_mask_tex = _create_mask_tex(fb_w, fb_h,
                                                  clamp_to_border=True)
            self._win_mask_fbo, _ = _create_fbo_with_tex(
                self._win_mask_tex, False, fb_w, fb_h)
            self._win_mask_size = (fb_w, fb_h)
        self._win_z_by_ds = {}
        # Per-window fb-space rects, indexed by the same z rank as mask
        # stamps ((i+1)/1024): the specular pass in ShadowComposite decodes
        # the mask rank at a fragment back to an index and texelFetches the
        # owning window's rect from this 256x1 RGBA32F texture to compute
        # its fade analytically from the window's lit corner - pre-supplied
        # geometry, not depth buffer walks (which either run per-pixel or
        # wobble as views move, depending on sample anchoring).
        _spec_rects = []
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._win_mask_fbo)
        gl.glViewport(0, 0, fb_w, fb_h)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glColorMask(gl.GL_TRUE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glUseProgram(self._prog_mask_rounded)
        gl.glUniform1f(self._loc_maskr_uMargin, 0.0)
        z = 0
        for wds in (getattr(Melty, "paint_ordered_ds", None) or ()):
            try:
                if (wds is None or wds.abs_closed or wds.closed
                        or getattr(wds, "_hidden_offscreen", False)):
                    continue
                wl, wt = wds.abs_left, wds.abs_top
                ww, wh = wds.width, wds.height
                if ww is None or wh is None or ww <= 0 or wh <= 0:
                    continue
            except Exception:
                continue
            z += 1
            zn = z / 1024.0
            self._win_z_by_ds[id(wds)] = zn
            x0, y0, x1, y1 = self._screen_rect_to_fb_xyxy(
                wl, wt, ww, wh, dp_x, dp_y, s_x, s_y, fb_h)
            if len(_spec_rects) < 256:
                _spec_rects.append((x0, y0, x1, y1))
            ix0, iy0 = int(floor(x0)), int(floor(y0))
            iw = max(0, int(ceil(x1)) - ix0)
            ih = max(0, int(ceil(y1)) - iy0)
            if iw <= 0 or ih <= 0:
                continue
            gl.glViewport(ix0, iy0, iw, ih)
            gl.glUniform1f(self._loc_maskr_uRankNorm, zn)
            gl.glUniform2f(self._loc_maskr_uRectSize, float(iw), float(ih))
            gl.glUniform1f(self._loc_maskr_uCornerRadius,
                           max(0.0, float(getattr(wds, "corner_radius", 6.0)
                                          or 0.0)))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)

        # Upload the rect table (4KB, whole 256-slot row every frame so
        # stale slots from last frame's larger window count are zeroed).
        if getattr(self, "_win_rects_tex", None) is None:
            self._win_rects_tex = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self._win_rects_tex)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                               gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER,
                               gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S,
                               gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T,
                               gl.GL_CLAMP_TO_EDGE)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA32F, 256, 1, 0,
                            gl.GL_RGBA, gl.GL_FLOAT, None)
        flat = [0.0] * (256 * 4)
        for i, (rx0, ry0, rx1, ry1) in enumerate(_spec_rects):
            flat[i * 4:i * 4 + 4] = (rx0, ry0, rx1, ry1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._win_rects_tex)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, 256, 1,
                           gl.GL_RGBA, gl.GL_FLOAT,
                           (ctypes.c_float * len(flat))(*flat))

    def _pixels_preserved(self, eds):
        """True when the retained emitter `eds`'s pixels are authoritative
        this frame WITHOUT its body having run — the false-positive shield
        for the territory kill. Walk the _parent chain up to the deepest
        node reached this frame (last_seen == frame_count):

        - it IS the emitter → alive (fresh render or its own blit);
        - it was reached and SERVED ITS CACHE (_blit_served_frame == now) →
          the blit carried the whole subtree, emitter included, intact —
          the "window re-captured around a doubly-cache-served descendant"
          case that used to read as inactive and kill the glow;
        - it was reached and ran its BODY without the emitter's branch being
          reached → the body chose different children (tab switch, content
          swap, culled branch) → NOT preserved; the kill may proceed.

        Bodies never stamp _blit_served_frame, so a swap can't shadow as a
        blit. Nodes below the deepest reached one are stale by construction,
        so the first hit decides."""
        now = Melty.frame_count
        node, hops = eds, 0
        while node is not None and hops < 64:
            if getattr(node, "last_seen", None) == now:
                return (node is eds
                        or getattr(node, "_blit_served_frame", None) == now)
            parent = getattr(node, "_parent", None)
            if parent is None or parent is node:
                break
            node = parent
            hops += 1
        return False

    def _branch_dropped(self, eds):
        """True when an ANCESTOR of the retained emitter `eds` ran its body
        this frame without reaching `eds` — the emitter's branch was not
        drawn (a tab switch, a content swap, a culled child): none of its
        pixels are on screen, so its retained marks must not cast either.
        The complement of _pixels_preserved's body-run case, returned as a
        pseudo territory hit (the ancestor's rect, its draw_state) so the
        kill takes the same settle path as a capture hit.

        Why the capture-hit test alone missed it (09-12): a tab switched
        BACK to a pane seen before serves that pane's tile from the blit
        cache — nothing is captured over the hidden pane's territory, so
        its gutter / scrollbar / symbol marks stayed retained and were
        re-stamped every frame at their old rects, under the other tab
        (the meltygui code editor's compare split showed the hidden tab's
        scrollbar grab and gutter bars). Nothing reached at all (the
        window not drawn this frame) is NOT a drop: the emitter may still
        be shown by a frozen tile."""
        now = Melty.frame_count
        node, hops = eds, 0
        while node is not None and hops < 64:
            if getattr(node, "last_seen", None) == now:
                if node is eds or getattr(node, "_blit_served_frame", None) == now:
                    return None
                try:
                    return (node.abs_left, node.abs_top,
                            node.abs_left + node.width, node.abs_top + node.height,
                            self._glow_root_ds(node), frozenset(), node)
                except Exception:
                    return None
            parent = getattr(node, "_parent", None)
            if parent is None or parent is node:
                break
            node = parent
            hops += 1
        return None

    def _win_z_for_ds(self, ds):
        """Encoded window-mask rank of the nearest enclosing dispatched
        window of `ds`; 1.0 (never masked) when unresolvable."""
        zs = getattr(self, "_win_z_by_ds", None)
        if not zs or ds is None:
            return 1.0
        hops = 0
        while ds is not None and hops < 64:
            z = zs.get(id(ds))
            if z is not None:
                return z
            pw = getattr(ds, "parent_window", None)
            if pw is None or pw is ds:
                break
            ds = pw
            hops += 1
        return 1.0

    def _mark_owner(self, draw_state):
        """What a fresh add_shadow / add_shadow_strip mark records as its
        owner (slot 8): the RECORDING tile's key while a tile records, else
        the emitting WINDOW's draw_state — the window whose body is running
        (Melty.melty_window_stack), or the emitter's own draw_state when one
        was passed. With no tile recording (the blit cache off, as a meltygui
        app runs — surface.Surface leaves it disabled) every mark used to
        be ownerless, and _win_z_for_owner reads an ownerless mark as
        TOPMOST: the panes' marks cast their shadows over a nested context
        menu (Lukas 09-12). A draw_state owner resolves through
        _win_z_for_ds exactly like a key's draw_state."""
        if self._stack:
            return self._stack[-1].key
        if draw_state is not None:
            return draw_state
        stack = Melty.melty_window_stack
        return stack[-1] if stack else None

    def _win_z_for_owner(self, owner):
        if owner is None:
            return 1.0
        if isinstance(owner, str):
            return self._win_z_for_ds(self.key_to_draw_state.get(owner))
        return self._win_z_for_ds(owner)

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
        """Screen (imgui/content) → framebuffer transform. With a shadow
        margin (Melty.frame_inset) the content sits inset in a larger
        framebuffer (Melty.framebuffer_size): the inset rides as a negative
        display_pos so _screen_rect_to_fb_xyxy lands content coords on the
        right texels, and fb_h is the REAL height for the y flip."""
        dd = imgui.get_draw_data()
        dp_x, dp_y = dd.display_pos
        s_x, s_y = 1, 1
        ox, oy = getattr(Melty, "frame_origin", None) or (0, 0)
        dp_x -= int(ox)
        dp_y -= int(oy)
        real = getattr(Melty, "framebuffer_size", None)
        if real:
            fb_w, fb_h = snap_int(real[0]), snap_int(real[1])
        else:
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

    def mask_mark_uncached_window(self, draw_state) -> None:
        """Depth mark for a closable window that renders WITHOUT a tile
        (use_cache=False: popovers, dropdown menus, the surface root). The
        tile path marks a window's rank in mark_end_offscreen / draw_tile,
        so PASS 5 overwrites the full mask under it and nothing behind the
        window shows through; an uncached window never marked, so the mask
        under it kept the windows behind — the code editor's gutter recess
        (an inset mark baked into the editor tile's cached mask) cast its
        shadow straight across the colour-picker popover floating over it
        (Lukas 09-13). Same rect / rank / live-clip recipe as draw_tile's
        blit mark; flat (no tile mask), so children with tiles of their
        own stamp their detail over it in paint order — which is why the
        wrapper calls this BEFORE the window's body runs: PASS 5 stamps
        flat marks in submission order (last wins), and a mark made after
        the body flattened every child under it (09-14)."""
        w, h = draw_state.width, draw_state.height
        if not w or not h or w <= 0 or h <= 0 or draw_state.closed:
            return
        x, y = draw_state.abs_left, draw_state.abs_top
        corner_radius = getattr(draw_state, "corner_radius", None)
        if corner_radius is None:
            corner_radius = 5.0
        clipped = self._clip_rect(x, y, w, h, self._get_current_clip_rect_screen())
        if clipped is not None:
            x, y, w, h = clipped
            if w <= 0 or h <= 0:
                return
        self.mask_mark_view(draw_state, draw_state.z_pos, draw_state.shadow_depth,
                            x, y, w, h, draw_state._tile_id, corner_radius)

    def can_replay_resize(self, draw_state):
        """A layout host may replay a captured child without entering its wrapper.

        Dirty pixels are deliberately retained during resize, just as for
        freeze_resize. External changes and incomplete first captures still
        require the normal render path. No GL resource is owned by the host.
        """
        if (not self.enabled or draw_state is None or not draw_state.use_cache
                or draw_state.closed or draw_state._external_change):
            return False
        key = draw_state._tile_id
        if key in getattr(self, '_resize_input_keys', ()):
            return False
        tile = self._tiles.get(key)
        if tile is None or not tile.tex or tile.last_clean_frame < 0:
            return False
        if key in self._frozen_served:
            return True
        filled = tile.filled_bbox
        return bool(filled and filled[0] <= 0 and filled[1] <= 0
                    and filled[2] >= tile.size[0] and filled[3] >= tile.size[1])

    def replay_resize(self, draw_state, rect, *, footer_height=0):
        """Replay one resident view; see replay_resize_batch for grouped replay."""
        return self.replay_resize_batch(((draw_state, rect, footer_height),))

    def replay_resize_batch(self, items):
        """Replay an ordered group of resident views with one shared setup.

        Preflight every member before painting anything. This lets layouts fall
        back to live rendering without leaving a partially replayed tile.
        Pixels retain native scale; overlays remain outside the captured images.
        """
        prepared = []
        for draw_state, rect, footer in items:
            if not self.can_replay_resize(draw_state):
                return False
            rect = tuple(map(snap_int, rect))
            if rect[2] <= 0 or rect[3] <= 0:
                return False
            prepared.append((draw_state, rect, footer))
        if not prepared:
            return True
        dl, clip = imgui.get_window_draw_list(), Melty.get_clip_rect()
        parent_key = self._stack[-1].key if self._stack else None
        for draw_state, rect, footer in prepared:
            self._replay_resize_prepared(draw_state, rect, footer, dl, clip, parent_key)
        return True

    def _replay_resize_prepared(self, draw_state, rect, footer_height, dl, clip, parent_key):
        from meltygui.core.rendering.view_identity import place_in_parent_window
        x, y, width, height = rect
        key = draw_state._tile_id
        tile = self._tiles[key]
        imgui.set_cursor_screen_pos((x, y))
        # Match the render wrapper's geometry bookkeeping: retain geometry
        # version updates without invalidating frozen pixels on every move.
        was_silenced = Melty.silence_invalidate
        Melty.silence_invalidate = True
        try:
            place_in_parent_window(draw_state)
            draw_state.width, draw_state.height = width, height
            draw_state.left, draw_state.top = draw_state.abs_left, draw_state.abs_top
            draw_state.last_seen = Melty.frame_count
            draw_state._blit_served_frame = Melty.frame_count
            draw_state.clip_rect = clip
            parent = draw_state.parent_window
            draw_state._clip_win_anchor = (parent.abs_left, parent.abs_top) if parent is not None else None
        finally:
            Melty.silence_invalidate = was_silenced
        draw_state.replay_surface_requests()
        self._frozen_served[key] = draw_state
        self.key_to_parent_key[key] = parent_key
        self.key_to_draw_state[key] = draw_state
        ctx = _Ctx(draw_state, key, (x, y), (width, height), draw_state.z_pos,
                   draw_state.shadow_depth, True, draw_state.auto_resize)
        self._key_to_ctx[key] = ctx

        if Melty.channels_split:
            dl.channels_set_current(Melty.get_channel(draw_state.depth))
        self.draw_freeze_bg(draw_state, x, y, width, height, live=False)
        alloc_w, alloc_h = _tile_alloc(tile)
        resident_w, resident_h = tile.content_size or tile.size
        footer = min(footer_height, tile.size[1], height)
        if footer:
            # A captured toolbar is not content: remove its old position,
            # then replay that strip against the destination's bottom edge.
            resident_w, resident_h = tile.size[0], tile.size[1] - footer
        # Crop only the native-scale image's right/bottom edges. The group's
        # enclosing draw-list clip handles the screen/parent intersection.
        visible_w, visible_h = min(resident_w, width), min(resident_h, height - footer)
        if visible_w > 0 and visible_h > 0:
            dl.add_image(tile.tex, (x, y), (x + visible_w, y + visible_h),
                         (0, 1), (visible_w / alloc_w, 1 - visible_h / alloc_h))
        if footer and visible_w > 0:
            dl.add_image(tile.tex, (x, y + height - footer), (x + visible_w, y + height),
                         (0, 1 - resident_h / alloc_h),
                         (visible_w / alloc_w, 1 - tile.size[1] / alloc_h))
        clipped = self._clip_rect(x, y, width, height, clip)
        if clipped is None:
            clipped = (x, y, width, height)
        if clipped[2] > 0 and clipped[3] > 0:
            self.mask_mark_view(draw_state, ctx.layer, ctx.depth_and_layer, *clipped, key,
                                getattr(draw_state, 'corner_radius', 0))
        from meltygui.core.rendering.overlay import finish_cached_overlays
        ctx.drew_cached = True
        finish_cached_overlays(self, ctx)
        self._frame_cache_hits += 1
        return True

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
            corner_radius = getattr(draw_state, "corner_radius", None)
            if corner_radius is None:
                corner_radius = 5.0                   # (`or 5.0` turned an explicit 0 back into 5)
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

    def draw_freeze_bg(self, draw_state, left, top, width, height, live: bool,
                       rounding=None):
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
        (same save/restore pattern as the deferred-window pass in meltygui.py).

        Note: PASS 3 snapshots the framebuffer with alpha forced to 1, so the
        bg drawn on live frames is still baked into the tile like any other
        pixel. Ownership buys a single code path, not a transparent tile.
        Returns draw_bg's (changed, bg_color) or None when there is no bg to
        draw — the wrapper uses bg_color for Melty.bg_color_stack.
        `rounding` overrides the view's corner radius (a caller replaying
        the recipe onto its own rect — the stack trace view's file card —
        wants square corners)."""
        fb = getattr(draw_state, "_frozen_bg_kwargs", None)
        if (not fb or not fb.get("show_bg")
                or width is None or height is None or width <= 5 or height <= 5):
            return None
        from meltygui.view.decoration_view import draw_bg
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
                           rounding=(rounding if rounding is not None
                                     else getattr(draw_state, "corner_radius", 6)),
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

    def draw_freeze_scrollbar(self, draw_state) -> None:
        """Draw cached freeze-resize scrollbars after tile capture.

        Live geometry, input subscriptions and the retained shadow mark are
        refreshed on body-render and cache-hit frames alike.
        """
        if not getattr(draw_state, "freeze_resize", False):
            return
        from meltygui.core.core_render import draw_overlay_scrollbar
        from meltygui.core.core_render import SCROLL_BAR_WIDTH_DEFAULT
        from meltygui.core.core_render import SCROLL_BAR_BRIGHTNESS_DEFAULT
        from meltygui.core.core_render import SCROLLBAR_SHADOW_GROUP
        # Owner of the grab's retained depth mark on these views: shed the
        # group after the early returns, so a scrollbar hidden this frame
        # (content fits after a resize / edit, view closed) drops its
        # silhouette; draw_overlay_scrollbar re-retains it while visible.
        self.clear_shadows(draw_state, SCROLLBAR_SHADOW_GROUP)
        if (not draw_state.scroll_visible or draw_state.closed
                or draw_state.just_shadow or draw_state.height is None):
            return
        kwargs = getattr(draw_state, "_kwargs", None) or {}
        bar_width = kwargs.get(
            "scroll_bar_width",
            getattr(draw_state, "scroll_bar_width", SCROLL_BAR_WIDTH_DEFAULT))
        bar_brightness = kwargs.get(
            "scroll_bar_brightness",
            getattr(draw_state, "scroll_bar_brightness", SCROLL_BAR_BRIGHTNESS_DEFAULT))
        # Same max_y the wrapper's scroll block computes; published for the
        # editor's drag-auto-scroll clamp (draw_state._max_scroll_y).
        max_scroll_y = max(0, draw_state.abs_content_height
                           - draw_state.abs_clipped_height + 1)
        draw_state._max_scroll_y = max_scroll_y
        draw_overlay_scrollbar(draw_state, max_scroll_y,
                               draw_state.height - draw_state.footer_height,
                               bar_width=bar_width, bar_brightness=bar_brightness,
                               overlay=True)

    def _draw_freeze_scrollbars(self, ctx) -> None:
        """Replay descendant overlays when an ancestor's body is cached."""
        if ctx.drew_cached:
            tile = self._tiles.get(ctx.key)
            ctx.freeze_scrollbars = tile.freeze_scrollbars if tile is not None else ()
            for child in ctx.freeze_scrollbars:
                self.draw_freeze_scrollbar(child)
        self.draw_freeze_scrollbar(ctx.draw_state)
        if self._stack:
            bars = ctx.freeze_scrollbars
            if ctx.draw_state.freeze_resize:
                bars += (ctx.draw_state,)
            self._stack[-1].freeze_scrollbars += bars

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
                    started=time.perf_counter(),
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
            if use_image and self._visible_unfilled(t, draw_state):
                # Captured while (partially) off the display: the tile holds
                # black where the screen ended. Render live and re-capture
                # now that more of the view is on screen.
                use_image = False
                t.last_invalidated_frame = max(t.last_invalidated_frame, self._frame_id)
                t.dirty = self._is_dirty(t)
                _bump_note(t, "off-screen capture")

            # Scrollbars always render after capture, so the resident tile
            # is already clean when a resize starts, including when press
            # and motion arrive together. Freeze for the resize gesture;
            # mouse-up resumes normal size validation and rendering.
            dragging = Melty.resize_gesture_live()
            frozen = False
            if (draw_state.freeze_resize and t is not None and has_area and dragging
                    and rkey not in getattr(self, '_resize_input_keys', ())
                    and (t.size != (size[0], size[1])
                         or Melty.resize_press_frame == Melty.frame_count
                         or rkey in self._frozen_served)):
                use_image = False
                frozen = True
                self._frozen_served[rkey] = draw_state

            # External-change wake (draw_state._external_change: the git
            # provider's _wake_consumers, FileWatch, MergeFiles.wake,
            # ExternalChanges, PendingSave): the body must run live THIS
            # frame - through the tile path, never around it. Refuse the
            # blit (and a frozen serve) and dirty the tile for this frame,
            # so mark_end_offscreen marks the window's mask and enqueues it for
            # capture. Skipping the tile context instead (the old use_cache
            # = False bypass) dropped the window's mask mark for the frame -
            # its shadow vanished and its cached children, still stamping
            # theirs, cast onto rank-0 background inside it - and left the
            # stale tile to serve the frame after.
            if draw_state._external_change:
                use_image = False
                frozen = False
                if t is not None:
                    t.last_invalidated_frame = max(t.last_invalidated_frame,
                                                   self._frame_id)
                    t.dirty = self._is_dirty(t)
                    _bump_note(t, "external change")

            if use_image or frozen:
                # Blit-served: wrapper runs, body (and whole subtree)
                # skipped; the subtree's pixels ride this blit intact. The
                # retained glow/shadow kill logic reads this stamp to tell
                # "tile-served under a re-rendering ancestor" (alive) from
                # "content swapped away" (body ran, branch not reached) -
                # see _pixels_preserved.
                draw_state._blit_served_frame = Melty.frame_count
                imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))

                a = draw_state.abs_left, draw_state.abs_top
                # Frozen: draw the full resident content (the high-water
                # extent, >= logical size for no-shrink tiles) - a clip to
                # the live rect below crops it, so a grow/drag reveals
                # preserved earlier-era pixels instead of background.
                # Nothing is trimmed off the stale content: freeze views
                # bake no outline (draw_freeze_bg), so every resident texel
                # draws edge to edge. (An earlier 20/5 px right/bottom trim
                # left the baked gutter/outline on-drag.) The scrollbar is
                # drawn over this image afterwards, in mark_end_offscreen
                # (draw_freeze_scrollbar, deferred overlay pass). The clip
                # crops the image only, leaving earlier-era content intact.
                draw_size = (getattr(t, "content_size", None) or t.size) if frozen else size
                b = draw_state.abs_left + draw_size[0], draw_state.abs_top + draw_size[1]
                # Top-anchored subrect of the (possibly bucket-padded)
                # texture: content spans u [0, dw/aw], v [1 - dh/ah, 1].
                taw, tah = _tile_alloc(t)
                uv_a = (0.0, 1.0)
                uv_b = (draw_size[0] / taw, 1.0 - draw_size[1] / tah)

                dl = imgui.get_window_draw_list()
                if frozen:
                    # Paint the background live over the full live rect
                    # (under the frozen image) - outline-less for freeze
                    # views, so nothing baked in the tile interferes with it.
                    self.draw_freeze_bg(draw_state, a[0], a[1],
                                        size[0], size[1], live=False)
                    # Whole resident content in one image, cropped to the
                    # live rect by the clip - same draw as the cache-hit
                    # path, but at the (possibly larger) content size.
                    dl.push_clip_rect(a[0], a[1],
                                      a[0] + size[0], a[1] + size[1], True)
                    dl.add_image_rounded(t.tex,
                                         a=a,
                                         b=b,
                                         uv_a=uv_a,
                                         uv_b=uv_b,
                                         rounding=getattr(draw_state, "corner_radius", 6))
                    dl.pop_clip_rect()
                else:
                    dl.add_image_rounded(t.tex,
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
                        import meltygui.core.input.drag_drop_core as _dnd_mod
                        if draw_state is not _dnd_mod.DragDrop.item_ds:
                            _dnd_mod.DragDrop.draw_home_blank()

                imgui.dummy(size[0], size[1])

                if draw_state.multi_line:
                    imgui.set_cursor_screen_pos((draw_state.abs_left,
                                                 draw_state.abs_top + draw_state.content_height + draw_state.header_height))
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
                self._frame_cache_hits += 1
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
                started=time.perf_counter(),
            )
        )
        self._frame_body_runs += 1

        return True

    def mark_end_offscreen(self, draw_state=None) -> None:
        if not self.enabled:
            return

        if draw_state is not None and (not draw_state.use_cache):
            return

        if len(self._stack) > 0:
            ctx = self._stack.pop()
            if ctx.started:
                total_ms = (time.perf_counter() - ctx.started) * 1000.0
                if self._stack:
                    self._stack[-1].child_ms += total_ms
                InvalidateTracker.note_render(ctx.key, ctx.draw_state, total_ms - ctx.child_ms)

        imgui.pop_id()

        imgui.end_group()
        Melty.tile_id_stack.pop()

        # The deferred overlay is excluded from this and ancestor captures.
        self._draw_freeze_scrollbars(ctx)
        from meltygui.core.rendering.overlay import finish_cached_overlays
        finish_cached_overlays(self, ctx)

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

        # The view's silhouette in the depth mask (the shadow compositor's
        # notion of its shape) - an explicit 0 means square (`or 5.0`
        # rounded it, and the corners composited as in the view).
        corner_radius = getattr(ctx.draw_state, "corner_radius", None)
        if corner_radius is None:
            corner_radius = 5.0
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
            # blit (PASS 3) unions its written region into the tile's
            # filled_bbox; once that includes the whole tile, every pixel has
            # been blitted at least once and subsequent scroll deltas don't
            # need invalidate invalidate - they just shift where the existing
            # tile is sampled. This stops the per-frame revalidation storm
            # during sustained scrolls while still letting newly-revealed
            # pixels fill in.
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

            if (((t is None) or ((int(t.size[0]), int(t.size[1])) != (
            int(ctx.size[0]), int(ctx.size[1])))) and not imgui.is_mouse_down(0)
                    and not imgui.is_mouse_down(1) and not imgui.is_mouse_down(2)
                    and not Melty.space_mouse_drag):
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
                            from meltygui.core.diagnostics.perf_trace import trace_rl as _nt_trace
                            _ds = ctx.draw_state
                            _nt_trace(("newtile", ctx.key),
                                      f"NEW-TILE {reason} name={getattr(_ds, 'name', None)!r} "
                                      f"hsrc={getattr(_ds, '_source', {}).get('height')!r}",
                                      min_interval=0.2)
                        except Exception:
                            pass

                    self.invalidate_up(ctx.key, max_depth=4,
                                       note=Note(name="New Tile", reason=reason, tint=(1, 0.5, 0)))
                self._tiles[ctx.key] = t

            if t is not None:
                t.freeze_scrollbars = ctx.freeze_scrollbars
                t.overlay_views = ctx.overlay_views
                self._scrub_stale_content(t, ctx.draw_state)

            if self._is_dirty(t) and (ctx.key not in self._enq_copy_keys):
                cap_size = ctx.size
                if t is not None and ctx.draw_state.freeze_resize and Melty.resize_gesture_live():
                    # An external edit may wake a frozen body mid-gesture.
                    # Its tile still has the pre-drag size: copy 1:1 instead
                    # of stretching the live viewport into that old extent.
                    cap_size = (snap_int(t.size[0]), snap_int(t.size[1]))
                self._pending.append(
                    _Pending(draw_state=ctx.draw_state, tile=t, pos=ctx.pos, size=cap_size, layer=ctx.layer,
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
            self._loc_sg_uWinMask = gl.glGetUniformLocation(self._prog_shadow_grad, "uWinMask")
            self._loc_sg_uWinZ = gl.glGetUniformLocation(self._prog_shadow_grad, "uWinZ")
            self._loc_sg_uFBSize = gl.glGetUniformLocation(self._prog_shadow_grad, "uFBSize")

        # Re-key based on the NEWEST uniform's location, not just program
        # presence: a hotswap onto a live instance can leave an OLD glow
        # program (compiled from a previous _GLOW_FS with different uniforms)
        # cached - the new stamp code would then set uniforms that were
        # never fetched and die. Bump the probed name as _GLOW_FS grows.
        if (getattr(self, "_prog_glow", None) is None
                or getattr(self, "_loc_gl_uAreaEdges", None) is None):
            if getattr(self, "_prog_glow", None):
                gl.glDeleteProgram(self._prog_glow)
            vs = _compile(gl.GL_VERTEX_SHADER, _FULLSCREEN_VS)
            fs = _compile(gl.GL_FRAGMENT_SHADER, _GLOW_FS)
            self._prog_glow = _link(vs, fs)
            self._loc_gl_uColor = gl.glGetUniformLocation(self._prog_glow, "uColor")
            self._loc_gl_uRankLo = gl.glGetUniformLocation(self._prog_glow, "uRankLo")
            self._loc_gl_uRankHi = gl.glGetUniformLocation(self._prog_glow, "uRankHi")
            self._loc_gl_uDepthMask = gl.glGetUniformLocation(self._prog_glow, "uDepthMask")
            self._loc_gl_uWinMask = gl.glGetUniformLocation(self._prog_glow, "uWinMask")
            self._loc_gl_uWinZ = gl.glGetUniformLocation(self._prog_glow, "uWinZ")
            self._loc_gl_uGlowSize = gl.glGetUniformLocation(self._prog_glow, "uGlowSize")
            self._loc_gl_uRectSize = gl.glGetUniformLocation(self._prog_glow, "uRectSize")
            self._loc_gl_uCornerRadius = gl.glGetUniformLocation(self._prog_glow, "uCornerRadius")
            self._loc_gl_uRadius = gl.glGetUniformLocation(self._prog_glow, "uRadius")
            self._loc_gl_uFalloff = gl.glGetUniformLocation(self._prog_glow, "uFalloff")
            self._loc_gl_uDebugSolid = gl.glGetUniformLocation(self._prog_glow, "uDebugSolid")
            self._loc_gl_uAreaLight = gl.glGetUniformLocation(self._prog_glow, "uAreaLight")
            self._loc_gl_uAreaHold = gl.glGetUniformLocation(self._prog_glow, "uAreaHold")
            self._loc_gl_uAreaSpread = gl.glGetUniformLocation(self._prog_glow, "uAreaSpread")
            self._loc_gl_uAreaFalloff = gl.glGetUniformLocation(self._prog_glow, "uAreaFalloff")
            self._loc_gl_uAreaEdgeBlur = gl.glGetUniformLocation(self._prog_glow, "uAreaEdgeBlur")
            self._loc_gl_uAreaTanA = gl.glGetUniformLocation(self._prog_glow, "uAreaTanA")
            self._loc_gl_uAreaTopEdge = gl.glGetUniformLocation(self._prog_glow, "uAreaTopEdge")
            self._loc_gl_uExpand = gl.glGetUniformLocation(self._prog_glow, "uExpand")
            self._loc_gl_uAreaEdges = gl.glGetUniformLocation(self._prog_glow, "uAreaEdges")

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
                               uv_rect=(1.0, 1.0, 0.0, 0.0), batch=None):
        """Draw a cached mask texture with offset and optional rounded corners.
        Note: preserves existing behavior (rounded path effectively always used by callers).
        """
        gl.glViewport(ix0, iy0, iw, ih)

        # A pass-local batch spans consecutive uses of this shader only.
        # Other shader draws clear it; it never survives a frame or GL owner.
        if batch is None or not batch:
            gl.glUseProgram(self._prog_mask_textured_offset_rounded)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glUniform1i(self._loc_texoffr_uTex, 0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        values = (offset, (float(iw), float(ih)), corner_radius, shadow_margin, uv_rect)
        previous = batch.get("uniforms") if batch is not None else None
        if previous is None or previous[0] != offset:
            gl.glUniform1f(self._loc_texoffr_uOffset, offset)
        if previous is None or previous[1] != values[1]:
            gl.glUniform2f(self._loc_texoffr_uRectSize, *values[1])
        if previous is None or previous[2] != corner_radius:
            gl.glUniform1f(self._loc_texoffr_uCornerRadius, corner_radius)
        if previous is None or previous[3] != shadow_margin:
            gl.glUniform1f(self._loc_texoffr_uMargin, shadow_margin)
        if previous is None or previous[4] != uv_rect:
            gl.glUniform4f(self._loc_texoffr_uUVRect, *uv_rect)
        if batch is not None:
            batch["uniforms"] = values

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
        # Per-pass wall stamps (draw_text's _pf idiom). a call over
        # _FC_TRACE_MS logs its split to the perf log (Toggles.symbol_perf_log).
        _fc_t0 = time.perf_counter()
        _fc_marks = []
        _rebuild_masks = True      # set by the rebuild-on-demand gate below
        _fc_counts = (len(self._shadow_rects), len(self._mask_rects), len(self._pending))
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
            _fc_marks.append(("setup", time.perf_counter()))
            # Capture-only passes 1–3 have no consumers without dirty tiles.
            # Display depth/shadow masks below still follow live geometry.
            # PASS 1: Snapshot the current framebuffer
            # ================================================================
            if local_pending:
                gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, Melty.default_framebuffer())
                gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self._snapshot_fbo)
                gl.glBlitFramebuffer(
                    0, 0, dd_fb_w, dd_fb_h, 0, 0, dd_fb_w, dd_fb_h, gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST
                )
            _cp_t1 = _cp()

            # ================================================================
            _fc_marks.append(("pass1_snapshot", time.perf_counter()))
            # PASS 2: Build _mask_tex (flat, fresh geometry only)
            # ================================================================
            if local_pending:
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
            _fc_marks.append(("pass2_flat_mask", time.perf_counter()))
            # PASS 3: Process dirty tiles - copy pixels using the mask
            # ================================================================
            if local_pending:
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
                            self._accumulate_filled(p.tile, p.draw_state,
                                                    on_screen=self._on_screen_tile_rect(x, y, w, h))
                        except Exception as e:
                            print(
                                f"Error copying to tile {p.key}: {e} {p.tile.draw_state.to_dict()} input_value={p.tile.draw_state._input_value}")

            _cp_t3 = _cp()
            # ================================================================
            _fc_marks.append(("pass3_copy_tiles", time.perf_counter()))
            # PASS 4: Build tile.mask_tex for each dirty tile (full subtree)
            # ================================================================
            # ── Rebuild-on-change gate (Toggles.Melty.mask_rebuild_on_change) ──
            # Passes 4–6 rebuild the pending tiles' subtree masks, the full
            # depth mask and the glow buffer from geometry alone: root rects,
            # shadow marks, retained emitters, window order. A pixel-only
            # frame (a keystroke, a selection drag) leaves all of that as it
            # was, so the rebuilt textures would be identical - compare the
            # inputs and keep last frame's textures instead (Lukas 08-31:
            # "move away from restamp everything every frame"). Compared by
            # value with ==, never hashed (mark tuples carry lists).
            _mask_sig = None
            _rebuild_masks = True
            try:
                if Toggles.Melty.mask_rebuild_on_change:
                    _sig_rects = []
                    for _rk, _rects in subtree_rects_by_root.items():
                        for r in _rects:
                            _ds = self.key_to_draw_state.get(r.key)
                            _t = self._tiles.get(r.key)
                            _sig_rects.append((
                                _rk, r.key, r.x, r.y, r.w, r.h, r.depth_and_layer,
                                r.corner_radius, getattr(r, "layer", None),
                                None if _ds is None else (
                                    _ds.abs_left, _ds.abs_top, _ds.width, _ds.height,
                                    bool(_ds.size_change), bool(getattr(_ds, "freeze_resize", False)),
                                    _ds.shadow_margin,
                                    tuple(_ds.clipped_by_rect) if _ds.clipped_by_rect is not None else None),
                                None if _t is None else (_t.mask_tex is not None, _t.mask_layer, tuple(_t.size)),
                                r.key in self._key_to_ctx))
                    _sig_windows = tuple(
                        (id(_w), _w.abs_left, _w.abs_top, _w.width, _w.height, bool(_w.closed),
                         getattr(_w, "corner_radius", None))
                        for _w in (getattr(Melty, "paint_ordered_ds", None) or ()))
                    _sig_retained = tuple(
                        (_k, len(_m), id(_e), _e.abs_left, _e.abs_top, _a)
                        for _k, (_m, _e, _a) in list(self._depth_marks_by_emitter.items())
                    ) + tuple(
                        (_k, len(_m), id(_e), _e.abs_left, _e.abs_top, _a)
                        for _k, (_m, _e, _a) in list(self._glow_marks_by_emitter.items()))
                    _settled_sig = (imgui.is_mouse_down(0), imgui.is_mouse_down(1),
                                    imgui.is_mouse_down(2), bool(Melty.on_drag))
                    _mask_sig = [
                        (fb_w, fb_h, dp_x, dp_y, s_x, s_y),
                        _sig_rects, list(self._shadow_rects), _sig_windows, _sig_retained,
                        list(self._depth_frame), list(getattr(self, "_glow_rects", ()) or ()),
                        set(self._glow_cleared), set(self._depth_cleared), _settled_sig,
                        tuple((p.key, p.depth_and_layer) for p in local_pending),
                    ]
                    _rebuild_masks = (
                        self._full_mask_tex is None
                        or getattr(self, "_mask_sig_prev", None) != _mask_sig
                        or any(p.tile is None or p.tile.mask_tex is None for p in local_pending))
            except Exception:
                _mask_sig = None
                _rebuild_masks = True
            if _rebuild_masks:
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

                        # freeze_resize child mid-drag: the body (and its whole
                        # subtree) skipped this frame, so the fresh-flat draw
                        # below would stamp one featureless rect and erase every
                        # nested mark baked in its cached mask - nested child
                        # windows visibly drop out for the drag. Serve the cached
                        # mask the way the frozen pixel blit serves the tile:
                        # captured (t_child.size) quad, top-left anchored, the
                        # live-rect scissor as a shrink. The not-size_change
                        # stretch guard does not apply - the quad is at the
                        # tile's own size, so uv mapping stays unstretched.
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

                        # Quad rect comes from the LIVE draw_state, never from
                        # _key_to_ctx: the ctx is only refreshed when a view
                        # actually re-renders (mark_end_offscreen), so after a
                        # reflow moves a cache-served sibling its ctx.pos is stale
                        # and the cached depths land at the old pos while the
                        # scissor (this frame's mask rect) sits at the new one.
                        # Mirrors PASS 5. Under the not-size_change guard the live
                        # rect equals the tile's logical size, so uv_rect mapping
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
                    # owner's tile is cache-served - the only persistence regular
                    # view marks get from this rebuild. A body only runs when its
                    # tile is dirty, so the frame a shadow is (re)marked is always
                    # a frame its tile is marked local_pending; conversely the next
                    # mask capture without the call ages the mark out.
                    if self._shadow_rects:
                        _owned = self._shadows_owned_by(p.key)
                        if _owned:
                            self._stamp_shadow_marks(_owned, dp_x, dp_y, s_x, s_y,
                                                     fb_h, scissor_fb=(x0, y0, x1, y1),
                                                     win_gate=False)

                    gl.glDisable(gl.GL_SCISSOR_TEST)

                    # Save _full_sub_mask_tex to tile's mask_tex and record the layer
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
                        # bucket-ized) mask texture, mirroring PASS 3's viewport.
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
                _fc_marks.append(("pass4_tile_masks", time.perf_counter()))
                # PASS 5: rebuild _full_mask_tex using cached subtree masks
                # ================================================================
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._full_mask_fbo)
                gl.glViewport(0, 0, fb_w, fb_h)
                gl.glDisable(gl.GL_SCISSOR_TEST)
                gl.glDisable(gl.GL_BLEND)
                gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
                gl.glClearColor(background_depth, 0, 0, 0.0)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)

                # gl.glDisable(gl.GL_BLEND)
                mask_stamps = []
                for r in _full_mask_rects(subtree_rects_by_root):
                    draw_state = self.key_to_draw_state.get(r.key)
                    t = self._tiles.get(r.key)

                    size_change = draw_state.size_change if draw_state else False
                    # freeze_resize mid-drag: same serve-the-cached-mask
                    # exception as PASS 4's frozen tiles - the flat fallback
                    # would flatten the whole frozen subtree's depth for the
                    # drag. Quad at the tile's captured size (unstretched),
                    # live-rect scissor.
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


                    depth_and_layer = r.depth_and_layer
                    if can_use_cached:

                        offset = (float(depth_and_layer) - float(t.mask_layer)) * float(INV_65535)

                        shadow_margin = r.draw_state.shadow_margin if r.draw_state is not None else 0.0

                        mask_stamps.append((t.mask_tex, (ix0, iy0, iw, ih),
                                            (clip_ix0, clip_iy0, clip_iw, clip_ih),
                                            _tile_uv_rect(t), offset, r.corner_radius, shadow_margin))
                    else:
                        clip = (clip_ix0, clip_iy0, clip_iw, clip_ih)
                        shadow_margin = r.draw_state.shadow_margin if r.draw_state is not None else 0.0
                        mask_stamps.append((None, clip, clip, (1, 1, 0, 0),
                                            float(depth_and_layer) / 65535.5,
                                            r.corner_radius, shadow_margin))

                if getattr(self, "_mask_batch", None) is None:
                    from meltygui.core.cache.mask_batch import MaskBatch
                    self._mask_batch = MaskBatch()
                self._mask_batch.draw(mask_stamps, fb_w, fb_h, restore_vao=self._dummy_vao)

                gl.glDisable(gl.GL_SCISSOR_TEST)

                # Standalone add_shadow() marks are stamped last, MAX-blended so they
                # can only raise depth - a shadow under an already-higher window
                # mark is a no-op, everywhere else it leaves a caster edge for
                # the post_frame shadow_cast pass. (Owned marks were already baked
                # into their pending tile's cached mask in PASS 4; MAX blend makes
                # the double-stamp idempotent.) OWNED INSET marks are excluded: a
                # MIN inset stamped over the finished full mask would reach into
                # other windows floating above the owner - they reach the
                # full mask only through their tile's cached mask (PASS 4), which
                # scopes the carve to the owner's subtree.
                # ... with one exception: an owned inset whose cached-mask path
                # was skipped THIS frame (owner mid-resize: size_change makes
                # PASS 5 stamp the owner as a flat rank rect, or the tile has no
                # mask yet). Excluding it there would drop the recess for exactly
                # the resize frames, so stamp it directly - still MIN-blended and
                # scissored to its own snapshotted clip, which keeps the
                # transient carve inside the owner view's region.
                # Window-occlusion mask for every stamp below (fresh standalone
                # here, retained depth re-stamps + glow in PASS 6): live clip
                # rects from Melty.paint_ordered_ds, so front windows mask
                # stamps IN REGION even mid-resize. Rebind the full-mask FBO
                # after - the build leaves its own FBO active.
                self._build_window_mask(dp_x, dp_y, s_x, s_y, fb_w, fb_h)
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._full_mask_fbo)

                _standalone = []
                for s in self._shadow_rects:
                    if not (s[9] and s[8] is not None):
                        _standalone.append(s)
                        continue
                    _ot = self._tiles.get(s[8])
                    _ods = self.key_to_draw_state.get(s[8])
                    # size_change no longer voids the cached-mask path for
                    # freeze_resize owners (frozen_mask serves it above), but
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
                _fc_marks.append(("pass5_full_mask", time.perf_counter()))
                # PASS 6: Glow light buffer (after PASS 5 - the stamp shader
                # samples the finished full depth mask for its receiver gate).
                # Retention is keyed by EMITTING draw state, decoupled from tile
                # life: clear_glows(ds) still means the emitter's body ran this
                # frame, so its retained entry dies and this frame's captures
                # re-add it; bodies that were cache-skipped keep the entry
                # untouched. Every retained entry re-stamps at the emitter's
                # LIVE clip pos (record-time coords go stale when a cache-served
                # sibling reflows - see project_blit_shadow_clip). Emitterless
                # marks are one-shot.
                # ================================================================
                # Glow must never abort a capture pass (an exception here would
                # leave tiles half-processed AND read as a state hotswap to the
                # rollback guard) - it is purely UI, so trap and report once.
                try:
                    self._ensure_glow_state()
                    _glow_retained = self._glow_marks_by_emitter
                    _depth_retained = self._depth_marks_by_emitter
                    for _eid in self._glow_cleared:
                        _glow_retained.pop(_eid, None)
                    # Depth retain fold per (emitter, group) - see
                    # _fold_depth_retention; the glow store stays id-keyed.
                    _depth_emitted = self._fold_depth_retention()

                    # Kill evidence shared by BOTH retained stores: the rects of
                    # every tile capturing FRESH this frame, tagged with their
                    # root window AND their live ancestor chain. A capture in
                    # the emitter's OWN window that repaints its territory
                    # without the emitter re-emitting tells the pixels under the
                    # glow were replaced (tab switch, jump-to content swap, any
                    # culled content) - the marks die. Two exemptions keep
                    # legitimate repaints from flickering the glow:
                    # - captures INSIDE the emitter's subtree (scrolled-in
                    #   widgets capturing during a scroll or frame_delta widget,
                    #   token overlays) repaint fragments OF the glowing
                    #   content, not over it - walking the CAPTURING view's
                    #   _parent chain is safe, it just rendered so its links
                    #   are live (only the culled emitter's chain goes stale);
                    # - the territory is the marks INTERSECTED with the
                    #   emitter's live rect (same origin-clip from stamp
                    #   below), so a freeze-resize drag that shrinks the view
                    #   stops stale-wide marks from overlapping the sibling
                    #   tile's every-frame captures across the window.
                    # Captures in OTHER windows (popups floating above) never
                    # repaint the emitter's surface - same-root scoping keeps
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
                        # The emitter's live rect - clips mark ORIGIN rects,
                        # re-stamped depth marks and kill territory alike (never
                        # the rendered light): during a freeze-resize drag the
                        # body didn't re-run, but width/height keep the drag
                        # live. Intersected with the enclosing windows' live
                        # clip (_enclosing_window_clip): an emitter its window
                        # scrolled / shrank out of view keeps its own rect, but
                        # no pixel of it shows, so none of its marks may cast -
                        # an empty rect here makes the code skip them.
                        if eds is None:
                            return None
                        try:
                            l, t = eds.abs_left, eds.abs_top
                            w, h = eds.width, eds.height
                            rect = ((l, t, l + w, t + h)
                                    if w is not None and h is not None else None)
                        except Exception:
                            rect = None
                        try:
                            win = self._enclosing_window_clip(eds)
                        except Exception:
                            win = None
                        if win is None:
                            return rect
                        if rect is None:
                            return win
                        return (max(rect[0], win[0]), max(rect[1], win[1]),
                                min(rect[2], win[2]), min(rect[3], win[3]))

                    # Kills only EXECUTE when interaction has settled: mid-drag
                    # repaints (column resize, window resize reflows) hit the
                    # glow territory constantly and immediate kills kill the
                    # glow for the whole drag. Unsettled hits flag the entry
                    # (it keeps glowing); the kill executes on settle unless
                    # the emitter re-emitted since (which clears it).
                    _settled = not (imgui.is_mouse_down(0)
                                    or imgui.is_mouse_down(1)
                                    or imgui.is_mouse_down(2)
                                    or Melty.on_drag)

                    def _territory_hit(marks, delta, root, eds, live):
                        # Returns the pending capture that repainted the
                        # emitter's territory, or None. Exempt: captures inside
                        # the emitter's subtree (live _parent walk) and small
                        # fragment captures fully CONTAINED in the emitter's
                        # live rect (< half its area) - inline children / token
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
                        if not Toggles.glow_debug_log:
                            return
                        _pds = hit[6]
                        print(f"glow kill[{kind}]"
                              f"{' DEFERRED' if deferred else ''}: "
                              f"emitter={getattr(eds, 'name', None)!r} "
                              f"by={getattr(_pds, 'name', None)!r} "
                              f"cap_rect=({hit[0]:.0f},{hit[1]:.0f},"
                              f"{hit[2]:.0f},{hit[3]:.0f})")

                    # Retained DEPTH marks re-stamp to the the mask FIRST -
                    # before the glow band samples it - so on frames where an
                    # enclosing tile rebuilt the mask without this emitter's
                    # body running, the interior depth detail (block peels, chip
                    # lifts) is preserved instead of flashing flat. MAX stability
                    # makes re-stamping marks that were also freshly emitted
                    # this frame idempotent.
                    _depth_stamp = []
                    # _key = (id(emitter), group); kill-pending is per key too,
                    # so a killed body group never takes the scrollbar's with
                    # it on a frame the bar legitimately re-emitted.
                    for _key, (marks, _eds, _anchor) in list(
                            _depth_retained.items()):
                        if _eds is None or getattr(_eds, "abs_closed", False):
                            _depth_retained.pop(_key, None)
                            self._depth_kill_pending.discard(_key)
                            continue
                        if _key in _depth_emitted:
                            self._depth_kill_pending.discard(_key)
                            continue  # stamped via the normal emit path already
                        _delta = (_eds.abs_left - _anchor[0],
                                  _eds.abs_top - _anchor[1])
                        _root_ds = self._glow_root_ds(_eds)
                        if self._pixels_preserved(_eds):
                            # Emitter reached this frame, or an ancestor was
                            # reached and blit-served (subtree pixels intact).
                            # Tab-switched/culled emitters fail both, so real
                            # content swaps still kill.
                            self._depth_kill_pending.discard(_key)
                        else:
                            _hit = _territory_hit(marks, _delta, _root_ds, _eds,
                                                  _live_clip_of(_eds))
                            if _hit is None:
                                # An ancestor ran its body and left this
                                # branch undrawn: same kill as a capture
                                # covering the territory (_branch_dropped).
                                _hit = self._branch_dropped(_eds)
                            if _hit is not None:
                                _log_kill("depth", _eds, _hit, not _settled)
                                if _settled:
                                    _depth_retained.pop(_key, None)
                                    self._depth_kill_pending.discard(_key)
                                    continue
                                self._depth_kill_pending.add(_key)
                            elif _settled and _key in self._depth_kill_pending:
                                _depth_retained.pop(_key, None)
                                self._depth_kill_pending.discard(_key)
                                continue
                        dx, dy = _delta
                        # Re-stamped marks clip to the emitter's LIVE rect -
                        # same rule as glow origin rects: during a freeze-resize
                        # drag the body doesn't re-run but width/height track
                        # the drag, so record-time clips (or clip=False marks
                        # like the gutter strip) may't stamp depth past the
                        # live clip edge.
                        _live = _live_clip_of(_eds)
                        # Live rank shift (see the anchor comment in
                        # add_shadow): re-anchor absolute mark ranks on the
                        # emitter's CURRENT surface rank so z reorders that
                        # record re-gate them; falls back to the ROOT window's
                        # delta if the emitter's rank stamp is stale (ancestor
                        # blit-served, wrapper never entered this frame).
                        _shift = self._anchor_rank_shift(_eds, _root_ds, _anchor)
                        for m in marks:
                            (mx, my, mw, mh, ranks, cr, margin, mclip, _own,
                             _ins) = m[:10]
                            _shape = m[10] if len(m) > 10 else None
                            if mclip is not None:
                                _c = (mclip[0] + dx, mclip[1] + dy,
                                      mclip[2] + dx, mclip[3] + dy)
                                if _live is not None:
                                    _c = (max(_c[0], _live[0]),
                                          max(_c[1], _live[1]),
                                          min(_c[2], _live[2]),
                                          min(_c[3], _live[3]))
                            else:
                                _c = _live
                            if _c is not None and (_c[2] <= _c[0]
                                                   or _c[3] <= _c[1]):
                                continue
                            if _shift:
                                ranks = tuple(max(0.0, rk + _shift)
                                              for rk in ranks)
                            # Shape payloads carry position AND rank per vertex -
                            # translate and re-anchor them the same way.
                            if _shape is not None and (dx or dy or _shift):
                                _shape = tuple(
                                    (vx + dx, vy + dy, max(0.0, vr + _shift))
                                    for (vx, vy, vr) in _shape)
                            # Owner key passed along so the window-occlusion gate
                            # can resolve the emitter's own clip at stamp time.
                            _depth_stamp.append(
                                (mx + dx, my + dy, mw, mh, ranks, cr, margin,
                                 _c, _own, False, _shape))
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
                    # never by shifting shadow_depth_at's depth argument (its
                    # depth term is non-monotone: spikes ~530 rank units at z~59
                    # then decreases, so a large depth offset can LOWER a mark
                    # and collapse the band). Toggle units: one shallow depth
                    # step (~layer_inc * 53.42/5.975 rank units).
                    _fc_marks.append(("pass6a_depth_retained", time.perf_counter()))
                    _g_step = float(Melty.layer_inc) * (53.42 / 5.975)
                    _g_lo_off = float(getattr(
                        Toggles, "glow_mask_lower_offset", -4.0)) * _g_step
                    _g_hi_off = float(getattr(
                        Toggles, "glow_mask_upper_offset", 8.0)) * _g_step

                    def _resolve_glow(m, delta, eds, root_ds, anchor=None):
                        # Band anchors are the LIVE shadow_depth properties -
                        # the same scalar ranks those views' mask rects stamp
                        # (depth_at_layer through shadow_depth_at), so the band
                        # is always in the mask's own units. Emitter anchor:
                        # the emitting view's surface + the mark's relative
                        # offset; floor anchor: the root window's surface. The
                        # emitter's "live" shadow_depth is only live if the
                        # was rendered this frame - resolve it through the
                        # retained mark instead (record rank + live shift), so
                        # a z reorder while everything blit-serves still moves
                        # the band via the root window's delta.
                        _anchor_rank = None
                        if eds is not None:
                            try:
                                if (anchor is not None and len(anchor) > 2
                                        and anchor[2] is not None):
                                    _anchor_rank = (
                                        anchor[2]
                                        + self._anchor_rank_shift(
                                            eds, root_ds, anchor)
                                        + m[6] * _g_step)
                                else:
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
                                _live_clip_of(eds), self._win_z_for_ds(eds))

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
                        # frame's capture legitimately overlaps their origin.
                        if _eid in _emitted_now:
                            self._glow_kill_pending.discard(_eid)
                        elif self._pixels_preserved(_eds):
                            # Emitter ran this frame, or an ancestor was
                            # reached and blit-served (its blit carried the
                            # emitter's pixels intact - the doubly-cache-served
                            # case that used to read as "inactive" and cull the
                            # glow). Culled cache-switched emitters fail here, so
                            # content swaps still kill.
                            self._glow_kill_pending.discard(_eid)
                        else:
                            _hit = _territory_hit(marks, _delta, _root_ds, _eds,
                                                  _live_clip_of(_eds))
                            if _hit is None:
                                _hit = self._branch_dropped(_eds)
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
                                _resolve_glow(m, _delta, _eds, _root_ds,
                                              _anchor))
                    # Same-surface dedupe: identical glow marks (same pos,
                    # size, color after the live-position delta) collapse to
                    # ONE emission at the strongest intensity. The old
                    # draw-list blur alpha-blended duplicates into
                    # near-invisibility; the light map is ADDITIVE, so any
                    # doubled surface (duplicate _dt_lines entries, a window
                    # drawn twice through different windows) reads as a glaring
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

                    if Toggles.glow_debug_log and self._frame_id % 60 == 0:
                        _s0 = _stamp_list[0] if _stamp_list else None
                        print(
                            f"glow6 f{self._frame_id}: glow={Toggles.glow} "
                            f"frame_marks={len(self._glow_rects)} "
                            f"retained={len(_glow_retained)} "
                            f"cleared={len(self._glow_cleared)} "
                            f"stamped={len(_stamp_list)} dups={_dup_count} "
                            f"tex={getattr(self, '_glow_size', None)} "
                            f"empty={self._glow_tex_empty}"
                            + (f" first: rect={tuple(round(v, 1) for v in _s0[0][:4])}"
                               f" rank={_s0[2]:.5f} floor={_s0[3]:.5f}"
                               f" inten={_s0[0][5]:.3f}" if _s0 else ""))
                    if not Toggles.glow:
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
                self._mask_sig_prev = _mask_sig
            else:
                # Skipped: leave the GL state just as pass 6's tail does -
                # _GLState.restore() below puts back the FBO bindings, scissor,
                # blend and colour mask, but NOT the viewport or the bound
                # program, and pass 3 exits with the last tile's viewport and
                # the copy shader current (the every-other-sideile flash).
                self._mask_skips = getattr(self, "_mask_skips", 0) + 1
                gl.glViewport(0, 0, fb_w, fb_h)
                gl.glDisable(gl.GL_BLEND)
                gl.glDisable(gl.GL_SCISSOR_TEST)
                gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
                gl.glUseProgram(0)
                gl.glActiveTexture(gl.GL_TEXTURE0)

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
                    from meltygui.core.diagnostics.perf_trace import trace as _cptr
                    _cptr("capture pass split",
                          total_ms=round(_cp_tot, 1),
                          p1_blit=round((_cp_t1 - _cp_t0) * 1000.0, 1),
                          p2_mask=round((_cp_t2 - _cp_t1) * 1000.0, 1),
                          p3_copy=round((_cp_t3 - _cp_t2) * 1000.0, 1),
                          p4_tile_masks=round((_cp_t4 - _cp_t3) * 1000.0, 1),
                          p5_full_mask=round((_cp_t5 - _cp_t4) * 1000.0, 1),
                          tail=round(_cp_tot - (_cp_t5 - _cp_t0) * 1000.0, 1),
                          tiles=len(local_pending), masks=len(local_mask_rects))
                    # Name the tiles this slow pass re-captured, with the
                    # reason each was last invalidated (_bump_note stamps
                    # _last_bump) - the "who re-bumped and why" answer for a
                    # burst of slow frames is straight in the log.
                    who = []
                    for _pt in local_pending[:8]:
                        _pds = getattr(_pt, "draw_state", None)
                        _pw, _ph = getattr(_pt, "size", (0, 0)) or (0, 0)
                        # _last_bump lives on the TILE (Pending wraps it)
                        _ptile = getattr(_pt, "tile", None)
                        who.append(f"{getattr(_pds, 'name', None)}"
                                   f"[{int(_pw)}x{int(_ph)}]"
                                   f"<{getattr(_ptile, '_last_bump', '?')}>")
                    if who:
                        _cptr("capture pass tiles", tiles=" | ".join(who))
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
            if getattr(self, "_depth_cleared", None) is not None:
                self._depth_cleared.clear()
            if getattr(self, "_emit_counts", None) is not None:
                self._emit_counts.clear()
            self._enq_mask_keys.clear()
            self._enq_copy_keys.clear()
            self._cancelled_keys.clear()
            self._recording = False
            self.did_deviate.clear()
            self.seen_ids.clear()
        try:
            _fc_marks.append(("pass6_glow+tail", time.perf_counter()))
            _fc_total = (_fc_marks[-1][1] - _fc_t0) * 1000.0
            if _fc_total >= _FC_TRACE_MS:
                from meltygui.core.diagnostics.perf_trace import trace as _fc_trace
                _fc_prev, _fc_parts = _fc_t0, []
                for _fc_lbl, _fc_t in _fc_marks:
                    _fc_parts.append(f"{_fc_lbl}={(_fc_t - _fc_prev) * 1000.0:.2f}")
                    _fc_prev = _fc_t
                _fc_trace("finalize perf", total_ms=round(_fc_total, 2),
                          shadows=_fc_counts[0], masks=_fc_counts[1], pending=_fc_counts[2],
                          rebuilt=int(_rebuild_masks),
                          breakdown=" ".join(_fc_parts))
        except Exception:
            pass
