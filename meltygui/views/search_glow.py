"""Search-match highlight: a circular gradient "glow" around each match.

The old search highlight was a solid translucent fill painted over the matched
text — it made white glyphs hard to read and didn't pop against busy content.
Instead we radiate a soft colored halo OUT from the matched rectangle and leave
the rectangle itself cut out, so the matched text stays perfectly readable while
a glowing ring draws the eye to it.

Geometry: the cutout is a ROUNDED rect (corner radius `cutout_radius`) and the
glow is the region between it and the same shape grown outward by `falloff` px.
Offsetting every perimeter sample by a distance `d` traces the Minkowski sum of
the rounded rect with a disk of radius `d` — a larger rounded rect — so the halo
stays smooth all the way around. The annulus is tessellated into `rings`
concentric bands; each band is a single translucent color whose alpha fades from
`opacity` at the cutout edge to 0 at the falloff distance. Bands tile the annulus
without overlap, so within one glow the alpha stays clean (no double-blending);
where two matches' glows overlap they accumulate (screen-style brighten).

The current match and the rest are drawn from two independent specs
(``Toggles.SearchSettings.ActiveElement`` / ``.InactiveElements``), so their
color, falloff, opacity, cutout shape, and outline are all tunable separately.
The gradient tint (`gradient_color`) and the outline tint (`outline_color`) are
distinct values.
"""

import math

import imgui

from src.lsd.gl_gui.toggles import Toggles


def _perimeter_samples(x0, y0, x1, y1, cseg, r):
    """Walk a rounded-rect perimeter clockwise as (base_x, base_y, nx, ny).

    `r` is the cutout corner radius. The corners are quarter-arc fans of `cseg`
    segments centred on the rect's inset corners, so each sample already sits on
    the rounded cutout; offsetting it by ``base + normal * d`` grows the rounded
    rect by `d` (the corner radius becomes ``r + d``). The straight edges need no
    interior samples: the single quad spanning one corner's exit sample to the
    next corner's entry sample (same outward normal) is geometrically exact.
    """
    r = max(0.0, min(r, (x1 - x0) * 0.5, (y1 - y0) * 0.5))
    half = math.pi / 2.0
    # (inset corner centre, start angle, end angle) - screen space, y points
    # down, so angle 0 = +x (right), +pi/2 = down, -pi/2 = up.
    corners = (
        ((x1 - r, y0 + r), -half, 0.0),               # top-right:    up    -> right
        ((x1 - r, y1 - r), 0.0, half),                # bottom-right: right -> down
        ((x0 + r, y1 - r), half, math.pi),            # bottom-left:  down  -> left
        ((x0 + r, y0 + r), math.pi, math.pi + half),  # top-left:     left  -> up
    )
    out = []
    for (cx, cy), a0, a1 in corners:
        for s in range(cseg + 1):
            ang = a0 + (a1 - a0) * (s / cseg)
            nx, ny = math.cos(ang), math.sin(ang)
            out.append((cx + nx * r, cy + ny * r, nx, ny))
    return out


def _draw_glow(draw_list, x0, y0, x1, y1, spec):
    """Radial gradient halo around (x0,y0,x1,y1) with a rounded rect cut out."""
    falloff = float(spec.falloff)
    opacity = float(spec.opacity)
    if falloff <= 0.0 or opacity <= 0.0:
        return

    cr, cg, cb = spec.gradient_color
    rings = max(1, int(spec.rings))
    exp = float(spec.falloff_exp)
    pad = float(spec.inner_pad)
    cseg = max(1, int(spec.corner_segments))
    radius = float(spec.cutout_radius)

    samples = _perimeter_samples(x0 - pad, y0 - pad, x1 + pad, y1 + pad, cseg, radius)
    n = len(samples)

    # Anti-aliased fill feathers every triangle edge, which leaves visible seams
    # where the gradient triangles meet; turn it off for a clean radial gradient
    # (same trick the swoosh ribbon uses), then restore the previous flags.
    flags = draw_list.flags
    draw_list.flags = flags & ~imgui.DRAW_LIST_ANTI_ALIASED_FILL
    try:
        inner = [(bx, by) for (bx, by, _nx, _ny) in samples]  # cutout edge, d=0
        a_inner = opacity
        for k in range(1, rings + 1):
            t = k / rings
            d = t * falloff
            a_outer = opacity * (1.0 - t) ** exp
            outer = [(bx + nx * d, by + ny * d) for (bx, by, nx, ny) in samples]
            col = imgui.get_color_u32_rgba(cr, cg, cb, (a_inner + a_outer) * 0.5)
            for i in range(n):
                j = i + 1 if i + 1 < n else 0
                ix0, iy0 = inner[i]
                ix1, iy1 = inner[j]
                ox0, oy0 = outer[i]
                ox1, oy1 = outer[j]
                draw_list.add_triangle_filled(ix0, iy0, ix1, iy1, ox1, oy1, col)
                draw_list.add_triangle_filled(ix0, iy0, ox1, oy1, ox0, oy0, col)
            inner = outer
            a_inner = a_outer
    finally:
        draw_list.flags = flags


def draw_search_highlight_multi(draw_list, segs, *, current):
    """Highlight a match made of per-line segments (a multi-line search term).
    One glow radiates around the segments' BOUNDING box — per-segment glows
    overlap into an unreadable blob — while each segment keeps its own thin
    outline so the exact matched text stays delineated."""
    if len(segs) == 1:
        x0, y0, x1, y1 = segs[0]
        draw_search_highlight(draw_list, x0, y0, x1, y1, current=current)
        return
    spec = (Toggles.SearchSettings.ActiveElement if current
            else Toggles.SearchSettings.InactiveElements)
    bx0 = min(s[0] for s in segs)
    bx1 = max(s[2] for s in segs)
    _draw_glow(draw_list, bx0, segs[0][1], bx1, segs[-1][3], spec)
    a = float(spec.outline_alpha)
    if a > 0.0:
        oc = spec.outline_color
        col = imgui.get_color_u32_rgba(oc[0], oc[1], oc[2], a)
        for x0, y0, x1, y1 in segs:
            draw_list.add_rect(x0, y0, x1, y1, col,
                               rounding=float(spec.cutout_radius),
                               thickness=float(spec.outline_thickness))


def draw_search_highlight(draw_list, x0, y0, x1, y1, *, current, rounding=0.0):
    """Highlight a search match with its glow (rect cut out) plus an optional
    thin outline. The current match uses the ActiveElement spec; the rest use
    InactiveElements, so both states are tuned independently."""
    spec = (Toggles.SearchSettings.ActiveElement if current
            else Toggles.SearchSettings.InactiveElements)
    _draw_glow(draw_list, x0, y0, x1, y1, spec)
    a = float(spec.outline_alpha)
    if a > 0.0:
        oc = spec.outline_color
        draw_list.add_rect(x0, y0, x1, y1, imgui.get_color_u32_rgba(oc[0], oc[1], oc[2], a),
                           rounding=max(float(rounding), float(spec.cutout_radius)),
                           thickness=float(spec.outline_thickness))
