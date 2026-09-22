"""The marks a view emits for the tile cache: shadow casters, glow emitters
and the integer snapping tile geometry uses.

This is the light half of the tile cache. Every view module imports from
here; tile_cache.py (the GL half: offscreen tiles, masks, the shadow and glow
passes) reads the marks through `Melty.cache` and imports OpenGL, so it loads
on meltygui's boot import thread (core/runtime/app.py) instead of with the
views. Each function forwards to the live TileCacheMasked when there is one;
without a cache the mark is simply dropped, so views need no guard.
"""
from meltygui.core.melty import Melty


def snap_int(v: float) -> int:
    return int(v)


def add_shadow(rect, offset=2.0, layer=None, depth=None, corner_radius=5.0,
               margin=0.0, clip=True, draw_state=None, group=None):
    """Mark a screen-space (x, y, w, h) rect as a shadow caster from anywhere
    — no @render_func, key, or draw_state required. `offset` is the signed
    depth delta from the surrounding surface: positive (default +2) lifts the
    rect so it casts a shadow, negative carves a recess so the surroundings
    cast into it. A 4-tuple (top_left, top_right, bottom_left, bottom_right)
    gives each corner its own delta and eases the depth between them across
    the quad — e.g. offset=(0, 0, 0, 8) peels the bottom-right corner up.
    layer/depth default to Melty.paint_rank/Melty.shadow_depth at call
    time. Cheap enough to call every frame. `draw_state` retains the mark
    across cache-served frames under that draw_state's `group` (None = the
    body's, cleared by clear_glows; anything else is cleared by
    clear_shadows(draw_state, group)); see TileCacheMasked.add_shadow."""
    cache = Melty.cache
    if cache is not None:
        cache.add_shadow(rect, offset=offset, layer=layer, depth=depth,
                         corner_radius=corner_radius, margin=margin, clip=clip,
                         draw_state=draw_state, group=group)


def add_shadow_strip(points, offset=2.0, layer=None, depth=None, clip=True,
                     draw_state=None, group=None):
    """add_shadow for a NON-RECT shape: `points` is a triangle strip of
    screen-space (x, y) vertices (a band between two polylines interleaves
    top0, bot0, top1, bot1, …). `offset` keeps add_shadow's signed
    semantics — positive lifts the shape so it casts, all-negative carves a
    recess — and may be a per-vertex sequence for graded depth along the
    shape. See TileCacheMasked.add_shadow_strip."""
    cache = Melty.cache
    if cache is not None and getattr(cache, "add_shadow_strip", None) is not None:
        cache.add_shadow_strip(points, offset=offset, layer=layer,
                               depth=depth, clip=clip, draw_state=draw_state,
                               group=group)


def clear_shadows(draw_state, group):
    """Open a non-body owner's retained-depth `group` for `draw_state` this
    frame: marks it retained drop at frame end unless re-emitted now. Call
    it wherever the group's owner decides whether to draw at all — before
    its early returns — the way a body calls clear_glows at its top. See
    TileCacheMasked.clear_shadows."""
    cache = Melty.cache
    if cache is not None and getattr(cache, "clear_shadows", None) is not None:
        cache.clear_shadows(draw_state, group)


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
