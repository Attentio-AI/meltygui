"""Color model functions and supporting definitions."""
from meltygui.hdr_color import scale_saturation


def _brightness_clamp(r, g, b, min_b, max_b):
    """Clamp PERCEIVED brightness (0.299r + 0.587g + 0.114b) — the
    legibility guard for tinted colors. Both directions SCALE the channels,
    which preserves their ratios and therefore saturation — a dark
    high-saturation tint lifts to a dark high-saturation color, it does NOT
    wash toward gray (a uniform add did, which made lowering the value
    factor also lose saturation). Only true near-black — no hue left to
    preserve — falls back to the uniform add."""
    # Inverted clamp (min above max - mid-drag or experimental toggle values)
    # collapses to the floor: without this, dark colors LIFT to min while
    # bright ones CRUSH to max < min, inverting brightness ordering and pinning
    # every wash to near-identical luminance ("tints stopped responding").
    if 0 < max_b < min_b:
        max_b = min_b
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if lum < min_b:
        if lum > 1e-4:
            k = min_b / lum
            return min(1.0, r * k), min(1.0, g * k), min(1.0, b * k)
        d = min_b - lum
        return min(1.0, r + d), min(1.0, g + d), min(1.0, b + d)
    if lum > max_b > 0 and lum > 0:
        k = max_b / lum
        return r * k, g * k, b * k
    return r, g, b


def _clamp_bg_value(color, max_bg_value):
    """Cap a background color's VALUE (max channel, as in HSV) at `max_bg_value`,
    keeping hue exact and BOOSTING saturation by the same factor the value was
    cut by (s / k, clamped to 1.0). A plain uniform channel scale holds HSV
    saturation constant but still reads as washed out once it's dark, so the
    boost buys the colorfulness back — the color only ever gets darker and
    *more* saturated, never grayer.

    This is the LAST thing applied to a bg color — it bounds the color actually
    painted, not the depth ramp that fed it, so whatever the depth/tint/bleed
    chain produced, `max_bg_value=0` is black and `0.2` is at most 20% value.
    Deliberately not text_editor's _brightness_clamp: that one is a perceptual
    (luma) guard and no-ops at max_b == 0, which would break the black case.

    Done in raw channel arithmetic rather than a colorsys round trip: hue is
    just the position of the mid channel in the [min, max] span, so rebuilding
    against the new chroma preserves it without ever naming an angle."""
    if max_bg_value is None or color is None or len(color) < 3:
        return color
    rest = tuple(color[3:])
    red, green, blue = color[0], color[1], color[2]
    value = max(red, green, blue)
    if value <= max_bg_value:
        return color
    if max_bg_value <= 0 or value <= 0:
        return (0.0, 0.0, 0.0) + rest

    low = min(red, green, blue)
    if low >= value:  # achromatic - no hue to preserve, just darken
        return (max_bg_value, max_bg_value, max_bg_value) + rest

    # k is the cut applied to the value; undo it with saturation.
    k = max_bg_value / value
    saturation = scale_saturation((value - low) / value, 1.0 / k)
    chroma = max_bg_value * saturation
    new_low = max_bg_value - chroma
    span = value - low
    return (new_low + (red - low) / span * chroma,
            new_low + (green - low) / span * chroma,
            new_low + (blue - low) / span * chroma) + rest


def _wide_pick(fx, fy, top_fraction, max_stops):
    """Square fractions (x right, y down) → (s, v, exposure)."""
    fx = min(max(fx, 0.0), 1.0)
    fy = min(max(fy, 0.0), 1.0)
    if fy < top_fraction:
        return fx, 1.0, 2.0 ** (max_stops * (1.0 - fy / top_fraction))
    v = 1.0 - (fy - top_fraction) / max(1e-6, 1.0 - top_fraction)
    return fx, min(max(v, 0.0), 1.0), 1.0


def _wide_marker(s, v, exposure, top_fraction, max_stops):
    """(s, v, exposure) → square fractions; the inverse of _wide_pick."""
    import math
    if exposure > 1.0:
        fy = top_fraction * (1.0 - min(1.0, math.log2(exposure) / max_stops))
    else:
        fy = top_fraction + (1.0 - v) * (1.0 - top_fraction)
    return min(max(s, 0.0), 1.0), min(max(fy, 0.0), 1.0)


def _srgb_plus_pick(px, py, square, ext, band, max_stops):
    """Cursor offset from the SQUARE's top-left, in px (negative y = over
    the exposure band, x past `square` = over the P3 strip) →
    (s, x, v, exposure)."""
    if px <= square:
        s, x = max(px / square, 0.0), 0.0
    else:
        s, x = 1.0, min(max((px - square) / max(1e-6, ext), 0.0), 1.0)
    if py < 0.0:
        fy = min(max(-py / max(1e-6, band), 0.0), 1.0)      # 0 at the seam, 1 at the top
        return s, x, 1.0, 2.0 ** (max_stops * fy)
    v = 1.0 - min(max(py / square, 0.0), 1.0)
    return s, x, v, 1.0


def _srgb_plus_marker(s, x, v, exposure, square, ext, band, max_stops):
    """(s, x, v, exposure) → marker offset from the square's top-left, in
    px; the inverse of _srgb_plus_pick."""
    import math
    px = s * square if x <= 0.0 else square + x * ext
    if exposure > 1.0:
        py = -band * min(1.0, math.log2(exposure) / max_stops)
    else:
        py = (1.0 - v) * square
    return px, py


def _extension_pick(fx, ext_fraction):
    """Cursor x as a fraction of the SQUARE's width (past 1 = over the
    extension, whose width is ext_fraction squares) → (s, x): the classic
    saturation inside the square, s = 1 and the P3 depth x past the seam."""
    if fx <= 1.0:
        return max(fx, 0.0), 0.0
    return 1.0, min(max((fx - 1.0) / max(1e-6, ext_fraction), 0.0), 1.0)
