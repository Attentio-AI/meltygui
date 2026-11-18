"""Melty colour: extended-sRGB tuples in Python, HDR/P3 through imgui's u32.

The convention every colour in Melty follows:

* A colour TUPLE ``(r, g, b[, a])`` is EXTENDED sRGB: the sRGB transfer
  curve, mirrored for negatives, with no ceiling. ``1.0`` is the desktop's
  SDR reference white (what an untagged sRGB window shows as white), values
  above it are brighter, negative components are colours outside the sRGB
  gamut (a P3 red is ``(1.093, -0.227, -0.150)``, the same numbers CSS gives
  for ``color(display-p3 1 0 0)`` in ``srgb``). Every existing colour stays
  valid, and the helpers ``white(n)`` / ``p3(r, g, b)`` build the rest.

* The working space on the GPU is LINEAR scRGB: sRGB primaries, linear
  light, fp16, negatives allowed. That is what the compositor (Hyprland's
  ``ext_linear`` surfaces) composites natively.

* Between the two sits imgui's 32-bit vertex colour, the one narrow pipe.
  ``pack_color`` encodes into it and the vertex shader (``GLSL_DECODE``)
  decodes out of it; the two are the only code that knows the layout:

      bit 31        : SDR bit — 1 = the RGB bytes are plain 8-bit sRGB in [0, 1]
                      (byte-identical to imgui's own packing, so an opaque
                      colour imgui converts itself — ``imgui.image``'s white
                      tint, ``0xFFFFFFFF`` — reads correctly), 0 = HDR: the
                      RGB bytes are DISPLAY P3 primaries on a log curve over
                      [0, Toggles.HDR.vertex_range] (both headroom AND gamut
                      ride the one flag; P3 contains sRGB so anything the
                      panel can show packs non-negative)
      bits 24..30   : 7-bit alpha (128 levels, both modes)
      bits 0..23    : R, G, B bytes (little-endian, R lowest — imgui's order)

  imgui itself only ever touches the alpha BYTE of a packed colour (the
  style-alpha multiply, the alpha-zero skip), never the RGB bytes, which is
  what makes re-purposing them safe. An SDR colour keeps every one of its
  256 codes per channel; an HDR colour gets its own 256 codes over the
  extended range instead of sharing them.

Style-table entries go through ``style_color`` so imgui's own float→u32
conversion reproduces these bytes exactly.
"""
from __future__ import annotations

import math

# The pickers a colour can be chosen with (`selector=` of draw_color_picker,
# view/color_picker_view.py): which of the displayable colours the field lays
# out. Public as meltygui.SRGB, meltygui.SRGB_PLUS and meltygui.HDR.
SRGB = 'srgb'           # the classic sRGB square
SRGB_PLUS = 'srgb+'     # that square, with P3 right of it and HDR above both
HDR = 'hdr'             # one Display P3 square with HDR above it, the sRGB part outlined
PERCEPTUAL = 'perceptual'  # Oklch: lightness up and chroma across in equal perceived steps, Oklch hue
DYNAMIC_PERCEPTUAL = 'dynamic perceptual'  # JzCzhz, of the nits the panel emits: its shape follows the brightness

# ---------------------------------------------------------------------------
# The curve (extended sRGB) and primaries
# ---------------------------------------------------------------------------


def srgb_to_linear(c: float) -> float:
    """Extended sRGB decode: the sRGB curve, mirrored for negatives, no ceiling."""
    s = -1.0 if c < 0.0 else 1.0
    c = abs(c)
    if c <= 0.04045:
        return s * c / 12.92
    return s * ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(v: float) -> float:
    """Extended sRGB encode: inverse of ``srgb_to_linear``."""
    s = -1.0 if v < 0.0 else 1.0
    v = abs(v)
    if v <= 0.0031308:
        return s * v * 12.92
    return s * (1.055 * v ** (1.0 / 2.4) - 0.055)


def _mat_mul_vec(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def _mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _mat_inv(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return (
        ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det),
        ((f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det),
        ((d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det),
    )


# RGB → XYZ (D65) for the two primaries sets. P3 here is DISPLAY P3 (the
# sRGB curve on DCI-P3 primaries with a D65 white), what the panel and CSS mean.
_SRGB_TO_XYZ = ((0.4123908, 0.3575843, 0.1804808),
                (0.2126390, 0.7151687, 0.0721923),
                (0.0193308, 0.1191948, 0.9505322))
_P3_TO_XYZ = ((0.4865709, 0.2656677, 0.1982173),
              (0.2289746, 0.6917385, 0.0792869),
              (0.0000000, 0.0451134, 1.0439444))

SRGB_TO_P3 = _mat_mul(_mat_inv(_P3_TO_XYZ), _SRGB_TO_XYZ)
BT2020_XY = (0.708, 0.292, 0.170, 0.797, 0.131, 0.046, 0.3127, 0.3290)     # red, green, blue, white as CIE xy


def srgb_to_primaries(xy):
    """The 3x3 matrix from linear sRGB to linear RGB on the primaries `xy` (red x, y, green x,
    y, blue x, y, white x, y): what a display's own channels are."""
    columns = [(xy[at] / xy[at + 1], 1.0, (1.0 - xy[at] - xy[at + 1]) / xy[at + 1]) for at in (0, 2, 4)]
    shape = tuple(tuple(columns[col][row] for col in range(3)) for row in range(3))
    white = (xy[6] / xy[7], 1.0, (1.0 - xy[6] - xy[7]) / xy[7])
    gains = _mat_mul_vec(_mat_inv(shape), white)
    to_xyz = tuple(tuple(shape[row][col] * gains[col] for col in range(3)) for row in range(3))
    return _mat_mul(_mat_inv(to_xyz), _SRGB_TO_XYZ)
P3_TO_SRGB = _mat_mul(_mat_inv(_SRGB_TO_XYZ), _P3_TO_XYZ)


def linear_srgb_to_p3(rgb):
    return _mat_mul_vec(SRGB_TO_P3, rgb)


def linear_p3_to_srgb(rgb):
    return _mat_mul_vec(P3_TO_SRGB, rgb)


# ---------------------------------------------------------------------------
# Oklab: the perceptual space HDR colour SCALES are authored in
# ---------------------------------------------------------------------------
# Björn Ottosson's Oklab on LINEAR sRGB light. It is scale-invariant (no
# absolute nits: lightness is a cube root, so 16 × white sits at L = 2.52)
# and works on extended values (negative components = P3), which is what a
# ramp that keeps climbing past the desktop's white needs. Its cube-root
# lightness is a fair match for equal perceptual steps above white; it does
# NOT model the Hunt effect (bright colours reading more saturated), so a
# uniform-L ramp is "uniform" in the CIELAB sense, not a full HDR appearance
# model.

_OKLAB_M1 = ((0.4122214708, 0.5363325363, 0.0514459929),
             (0.2119034982, 0.6806995451, 0.1073969566),
             (0.0883024619, 0.2817188376, 0.6299787005))
_OKLAB_M2 = ((0.2104542553, 0.7936177850, -0.0040720468),
             (1.9779984951, -2.4285922050, 0.4505937099),
             (0.0259040371, 0.7827717662, -0.8086757660))
_OKLAB_M1_INV = _mat_inv(_OKLAB_M1)
_OKLAB_M2_INV = _mat_inv(_OKLAB_M2)


def _cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def linear_to_oklab(rgb):
    """Linear sRGB (extended) → Oklab ``(L, a, b)``; white (1, 1, 1) is L = 1."""
    lms = _mat_mul_vec(_OKLAB_M1, rgb)
    return _mat_mul_vec(_OKLAB_M2, tuple(_cbrt(c) for c in lms))


def oklab_to_linear(lab):
    """Oklab ``(L, a, b)`` → linear sRGB (extended)."""
    lms = _mat_mul_vec(_OKLAB_M2_INV, lab)
    return _mat_mul_vec(_OKLAB_M1_INV, tuple(c * c * c for c in lms))


def oklab_hue(rgb) -> float:
    """Oklab hue angle (radians) of a linear sRGB colour."""
    _, a, b = linear_to_oklab(rgb)
    return math.atan2(b, a)


def oklab_max_chroma(lightness: float, hue: float, peak: float = 1.0,
                     steps: int = 40) -> float:
    """The largest Oklab chroma at ``(lightness, hue)`` whose colour still
    fits the DISPLAY-P3 box ``[0, peak]³`` in linear light — ``peak`` = the
    brightest white the scale may reach, in multiples of the desktop's
    white (1 = the SDR gamut). This is the gamut boundary an HDR colour
    scale rides: it pinches to grey at black AND at the peak, so a ramp of
    max-chroma colours whitens PROGRESSIVELY toward its top instead of
    switching to white. Bisection: chroma grows monotonically out of the
    box along a ray of fixed lightness and hue."""
    if lightness <= 0.0 or lightness >= _cbrt(peak):
        return 0.0
    ca, sa = math.cos(hue), math.sin(hue)

    def fits(chroma):
        p3_rgb = linear_srgb_to_p3(oklab_to_linear((lightness, chroma * ca, chroma * sa)))
        return all(-1e-6 <= c <= peak + 1e-6 for c in p3_rgb)

    lo, hi = 0.0, 1.0 + _cbrt(peak)
    if fits(hi):
        return hi
    for _ in range(steps):
        mid = (lo + hi) * 0.5
        if fits(mid):
            lo = mid
        else:
            hi = mid
    return lo


def fits_box(rgb, peak: float):
    """Which colours of an (..., 3) array lie in the box [0, peak]³. A channel
    may be under 0 by a thousandth of the colour's brightest one, which is
    rounding; a fixed allowance would let every dark colour through, whatever
    its hue, once the values are small (a display turned far up)."""
    return above_zero(rgb) & (rgb.max(-1) <= peak * (1.0 + 1e-4))


def above_zero(rgb):
    """The lower half of fits_box: no channel under 0, but for rounding."""
    import numpy as np
    return rgb.min(-1) >= -1e-3 * np.maximum(rgb.max(-1), 0.0) - 1e-9


def oklch_field_linear(hue: float, rows: int, cols: int, lightness: float, chroma: float, peak: float = 1.0):
    """One Oklch hue (radians) as a (rows, cols, 4) float32 array of LINEAR
    scRGB: lightness `lightness` at the top row down to 0, chroma 0 at the
    left column up to `chroma`. Alpha is 1 where the colour fits the
    DISPLAY-P3 box [0, peak]³ and 0 outside it; a texel outside carries the
    colour of its row's edge, so a filtered edge has no fringe. Also returns
    the (rows, cols) bool mask of the texels inside the sRGB box."""
    import numpy as np
    light = (lightness * (1.0 - (np.arange(rows, dtype=np.float32) + 0.5) / rows))[:, None]
    far = (chroma * (np.arange(cols, dtype=np.float32) + 0.5) / cols)[None, :]
    lab = np.empty((rows, cols, 3), dtype=np.float32)
    lab[..., 0] = light
    lab[..., 1] = far * math.cos(hue)
    lab[..., 2] = far * math.sin(hue)
    lms = lab @ np.asarray(_OKLAB_M2_INV, dtype=np.float32).T
    linear = (lms * lms * lms) @ np.asarray(_OKLAB_M1_INV, dtype=np.float32).T
    wide = linear @ np.asarray(SRGB_TO_P3, dtype=np.float32).T
    inside = fits_box(wide, peak)
    narrow = fits_box(linear, peak)
    # The gamut is one run from chroma 0 along a row: its length is the edge.
    edge = np.maximum(inside.sum(1) - 1, 0)
    last = np.take_along_axis(linear, edge[:, None, None].repeat(3, 2), 1)
    out = np.empty((rows, cols, 4), dtype=np.float32)
    out[..., :3] = np.where(inside[..., None], linear, last)
    out[..., 3] = inside
    return out, narrow


# ---------------------------------------------------------------------------
# Jzazbz: the perceptual space of ABSOLUTE light
# ---------------------------------------------------------------------------
# Safdar et al. 2017. Unlike Oklab it takes nits, not multiples of white: its
# lightness Jz runs on a PQ curve (0 at black, about 1 at 10,000 nits), so
# how a colour sits in it depends on how bright the display shows white.
# `white` below is the nits of a colour of 1.0. JzCzhz is its polar form:
# chroma Cz = hypot(az, bz), hue hz = atan2(bz, az).

_JZ_B, _JZ_G, _JZ_D, _JZ_D0 = 1.15, 0.66, -0.56, 1.6295499532821566e-11
_JZ_N, _JZ_P = 2610.0 / 16384.0, 1.7 * 2523.0 / 32.0
_JZ_C1, _JZ_C2, _JZ_C3 = 3424.0 / 4096.0, 2413.0 / 128.0, 2392.0 / 128.0
_JZ_LMS = ((0.41478972, 0.579999, 0.0146480),
           (-0.2015100, 1.120649, 0.0531008),
           (-0.0166008, 0.264800, 0.6684799))
_JZ_IAB = ((0.5, 0.5, 0.0),
           (3.524000, -4.066708, 0.542708),
           (0.199076, 1.096799, -1.295875))
# XYZ' is XYZ with X and Y sheared (the paper's b and g): one matrix each way.
_JZ_SHEAR = ((_JZ_B, 0.0, 1.0 - _JZ_B), (1.0 - _JZ_G, _JZ_G, 0.0), (0.0, 0.0, 1.0))
_JZ_FROM_SRGB = _mat_mul(_JZ_LMS, _mat_mul(_JZ_SHEAR, _SRGB_TO_XYZ))      # linear sRGB (nits) -> LMS
_JZ_TO_SRGB = _mat_inv(_JZ_FROM_SRGB)
_JZ_IAB_INV = _mat_inv(_JZ_IAB)


def _jz_pq(nits: float) -> float:
    y = (max(nits, 0.0) / 10000.0) ** _JZ_N
    return ((_JZ_C1 + _JZ_C2 * y) / (1.0 + _JZ_C3 * y)) ** _JZ_P


def _jz_pq_inverse(code: float) -> float:
    y = max(code, 0.0) ** (1.0 / _JZ_P)
    return 10000.0 * (max(y - _JZ_C1, 0.0) / (_JZ_C2 - _JZ_C3 * y)) ** (1.0 / _JZ_N)


def linear_to_jzazbz(rgb, white: float):
    """Linear sRGB (extended, 1.0 = `white` nits) → ``(Jz, az, bz)``."""
    lms = _mat_mul_vec(_JZ_FROM_SRGB, tuple(c * white for c in rgb))
    iz, az, bz = _mat_mul_vec(_JZ_IAB, tuple(_jz_pq(c) for c in lms))
    return (1.0 + _JZ_D) * iz / (1.0 + _JZ_D * iz) - _JZ_D0, az, bz


def jzazbz_to_linear(jab, white: float):
    """``(Jz, az, bz)`` → linear sRGB (extended, 1.0 = `white` nits)."""
    jz = jab[0] + _JZ_D0
    iz = jz / (1.0 + _JZ_D - _JZ_D * jz)
    lms = tuple(_jz_pq_inverse(c) for c in _mat_mul_vec(_JZ_IAB_INV, (iz, jab[1], jab[2])))
    return tuple(c / white for c in _mat_mul_vec(_JZ_TO_SRGB, lms))


def jzczhz_max_chroma(lightness: float, hue: float, white: float, peak: float = 1.0, steps: int = 24) -> float:
    """The largest Cz at ``(Jz, hz)`` whose colour fits the DISPLAY-P3 box
    ``[0, peak]³`` when 1.0 is `white` nits. Bisection, as oklab_max_chroma."""
    ca, sa = math.cos(hue), math.sin(hue)

    def fits(chroma):
        p3_rgb = linear_srgb_to_p3(jzazbz_to_linear((lightness, chroma * ca, chroma * sa), white))
        return all(-1e-6 <= c <= peak + 1e-6 for c in p3_rgb)

    if not fits(0.0):
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(steps):
        mid = (lo + hi) * 0.5
        if fits(mid):
            lo = mid
        else:
            hi = mid
    return lo


def jzczhz_nits(lightness, chroma, hue: float):
    """Arrays of Jz and Cz (broadcast against each other) at one hue (radians)
    → (nits, over): the light as linear sRGB in NITS, shape (..., 3), and
    the bool mask of what lies past the PQ ceiling and is no light at all."""
    import numpy as np
    light = np.asarray(lightness, dtype=np.float32) + np.float32(_JZ_D0)
    far = np.asarray(chroma, dtype=np.float32)
    iab = np.empty(np.broadcast(light, far).shape + (3,), dtype=np.float32)
    iab[..., 0] = light / (1.0 + _JZ_D - _JZ_D * light)
    iab[..., 1] = far * math.cos(hue)
    iab[..., 2] = far * math.sin(hue)
    code = np.maximum(iab @ np.asarray(_JZ_IAB_INV, dtype=np.float32).T, 0.0) ** np.float32(1.0 / _JZ_P)
    over = (code >= _JZ_C2 / _JZ_C3 * 0.999).any(-1)
    code = np.minimum(code, np.float32(_JZ_C2 / _JZ_C3 * 0.999))
    lms = np.float32(10000.0) * (np.maximum(code - _JZ_C1, 0.0) / (_JZ_C2 - _JZ_C3 * code)) ** np.float32(1.0 / _JZ_N)
    return lms @ np.asarray(_JZ_TO_SRGB, dtype=np.float32).T, over


def jzczhz_field_linear(hue: float, rows: int, cols: int, lightness: float, chroma: float, white: float,
                        peak: float = 1.0, ask=None, ceiling=None):
    """oklch_field_linear in JzCzhz: lightness Jz `lightness` at the top row
    down to 0, chroma Cz 0 at the left column up to `chroma`, for a display
    whose 1.0 is `white` nits. Returns the same (pixels, sRGB mask).
    `ask(nits)` turns the light a texel stands for into the value to send
    for it (1.0 = white); without one that is `nits / white`, which is right
    for a display that emits what it is sent. `ceiling` is the most nits
    the field shows in a P3 channel (the hardware's, the headroom's): a
    texel's light over it is outside, whatever could be sent for it."""
    import numpy as np
    light = (lightness * (1.0 - (np.arange(rows, dtype=np.float32) + 0.5) / rows))[:, None]
    far = (chroma * (np.arange(cols, dtype=np.float32) + 0.5) / cols)[None, :]
    nits, over = jzczhz_nits(light, far, hue)
    if ask is None:
        linear = nits / np.float32(white)
    else:
        linear = ask(nits).astype(np.float32, copy=False)
    # A gamut's floor is light that exists, whatever is sent for it: it is tested on the light
    # itself, so it does not move with the display. Its ceiling is the most that can be sent.
    to_p3 = np.asarray(SRGB_TO_P3, dtype=np.float32).T
    real = ~over & (linear.max(-1) <= peak * (1.0 + 1e-4))
    if ceiling is not None:
        real &= (nits @ to_p3).max(-1) <= ceiling * (1.0 + 1e-4)
    inside = real & above_zero(nits @ to_p3) & ((linear @ to_p3).max(-1) <= peak * (1.0 + 1e-4))
    narrow = real & above_zero(nits)
    edge = np.maximum(inside.sum(1) - 1, 0)
    last = np.take_along_axis(linear, edge[:, None, None].repeat(3, 2), 1)
    out = np.empty((rows, cols, 4), dtype=np.float32)
    out[..., :3] = np.where(inside[..., None], linear, last)
    out[..., 3] = inside
    return out, narrow


# ---------------------------------------------------------------------------
# Helpers for writing colours
# ---------------------------------------------------------------------------


def white(scale: float):
    """A neutral ``scale`` × the SDR reference white, as an extended-sRGB
    tuple: ``white(1)`` is sRGB white, ``white(4)`` is 1000 nits on a 250-nit
    desktop. The helper for "how bright", independent of hue."""
    e = linear_to_srgb(float(scale))
    return (e, e, e)


def p3(r: float, g: float, b: float, scale: float = 1.0):
    """A Display-P3 colour (P3 primaries, sRGB curve — the CSS ``display-p3``
    triple) as an extended-sRGB tuple. ``scale`` multiplies the linear light,
    so ``p3(1, 0, 0, scale=16)`` is P3 red at 16 × reference white."""
    lin = tuple(srgb_to_linear(c) * scale for c in (r, g, b))
    return tuple(linear_to_srgb(c) for c in linear_p3_to_srgb(lin))


def scale_saturation(saturation: float, factor: float) -> float:
    """``saturation * factor`` for an EXTENDED-HSV saturation (colorsys on an
    extended-sRGB tuple: s > 1 means a channel below 0 — outside the sRGB
    gamut), capped at the colour's OWN gamut edge: 1.0 for a tint inside
    sRGB, its own s for a wider one. A boost factor above 1 (the bg ramps'
    1.1, the gutter's 1.05) must not push an sRGB primary out past P3 —
    the packer clips per channel in linear P3, so an sRGB red tint and a
    P3 red tint landed on the SAME P3-edge colour and P3 tints looked no
    more saturated than sRGB ones (09-07). Before HDR imgui's u32 clamp
    capped every boost at the sRGB edge; this keeps that look for SDR
    tints and lets a wide tint keep exactly the chroma it was given."""
    if saturation <= 0.0:
        return 0.0
    return min(max(1.0, saturation), saturation * factor)


def scale(color, k: float):
    """``color`` (an extended-sRGB tuple) with its linear light multiplied by
    ``k``; alpha, if present, is kept."""
    rgb = tuple(linear_to_srgb(srgb_to_linear(c) * k) for c in color[:3])
    return rgb + tuple(color[3:])


# ---------------------------------------------------------------------------
# The u32 pipe
# ---------------------------------------------------------------------------

SDR_BIT = 0x80000000
ALPHA_MASK = 0x7F000000
RGB_MASK = 0x00FFFFFF


def _curve_params():
    """(range, octaves) of the HDR byte curve — Toggles.HDR, read live so a
    toggle edit reaches the packer and the shaders (uniforms) together."""
    from meltygui.core.runtime.toggles import Toggles
    return float(Toggles.HDR.vertex_range), float(Toggles.HDR.vertex_octaves)


def _encode_hdr_byte(v: float, rng: float, octaves: float) -> int:
    """Linear P3 component → byte on the log curve: code 0 is exactly 0,
    codes 1..255 span [range · 2^-octaves, range] evenly in log2."""
    if v <= 0.0:
        return 0
    t = 1.0 + math.log2(v / rng) / octaves      # 1.0 at the ceiling
    code = int(1.0 + 254.0 * t + 0.5)
    return 0 if code < 1 else (255 if code > 255 else code)


def _decode_hdr_byte(code: int, rng: float, octaves: float) -> float:
    if code <= 0:
        return 0.0
    return rng * 2.0 ** (octaves * ((code - 1) / 254.0 - 1.0))


# (r, g, b, alpha7) -> packed, HDR branch only. Cleared when the curve
# toggles change (clear_cache) - hotswap keeps the dict.
_HDR_PACK_CACHE = globals().get("_HDR_PACK_CACHE", {})


def clear_cache() -> None:
    """Forget memoized HDR packs — call after editing Toggles.HDR.vertex_*."""
    _HDR_PACK_CACHE.clear()


def _is_sdr(r, g, b) -> bool:
    return 0.0 <= r <= 1.0 and 0.0 <= g <= 1.0 and 0.0 <= b <= 1.0


def pack_color(r: float, g: float, b: float, a: float = 1.0) -> int:
    """Extended-sRGB components → imgui u32. The drop-in for
    ``imgui.get_color_u32_rgba``: identical bytes for a colour inside [0, 1]
    (bar the 7-bit alpha), the HDR layout for anything outside it."""
    if a != a:
        a = 0.0
    alpha7 = int(a * 127.0 + 0.5)
    alpha7 = 0 if alpha7 < 0 else (127 if alpha7 > 127 else alpha7)
    if _is_sdr(r, g, b):
        return (SDR_BIT | (alpha7 << 24)
                | (int(b * 255.0 + 0.5) << 16)
                | (int(g * 255.0 + 0.5) << 8)
                | int(r * 255.0 + 0.5))
    # Tiny negative noise from colour math (a computed purple tint like
    # (0, -0.005, -0.009)) is not a wide-gamut intent: snap it to SDR.
    if r >= -0.02 and g >= -0.02 and b >= -0.02 and r <= 1.0 and g <= 1.0 and b <= 1.0:
        return pack_color(max(0.0, r), max(0.0, g), max(0.0, b), a)
    # The HDR path is ~25 us of matrix + log math. HDR colours are few
    # distinct values but many samples, so memoize on the exact inputs.
    key = (r, g, b, alpha7)
    packed = _HDR_PACK_CACHE.get(key)
    if packed is not None:
        return packed
    rng, octaves = _curve_params()
    lin = linear_srgb_to_p3((srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b)))
    codes = [_encode_hdr_byte(min(rng, max(0.0, c)), rng, octaves) for c in lin]
    packed = (alpha7 << 24) | (codes[2] << 16) | (codes[1] << 8) | codes[0]
    if len(_HDR_PACK_CACHE) > 4096:
        _HDR_PACK_CACHE.clear()
    _HDR_PACK_CACHE[key] = packed
    return packed


def unpack_color(packed: int):
    """imgui u32 → extended-sRGB ``(r, g, b, a)``. Inverse of ``pack_color``
    up to the byte quantization."""
    packed &= 0xFFFFFFFF
    a = ((packed >> 24) & 0x7F) / 127.0
    rb, gb, bb = packed & 0xFF, (packed >> 8) & 0xFF, (packed >> 16) & 0xFF
    if packed & SDR_BIT:
        return (rb / 255.0, gb / 255.0, bb / 255.0, a)
    rng, octaves = _curve_params()
    lin_p3 = tuple(_decode_hdr_byte(c, rng, octaves) for c in (rb, gb, bb))
    return tuple(linear_to_srgb(c) for c in linear_p3_to_srgb(lin_p3)) + (a,)


def is_sdr_packed(packed: int) -> bool:
    return bool(packed & SDR_BIT)


def with_alpha(packed: int, a: float) -> int:
    """``packed`` with its alpha replaced — the flag and RGB bytes kept."""
    alpha7 = int(a * 127.0 + 0.5)
    alpha7 = 0 if alpha7 < 0 else (127 if alpha7 > 127 else alpha7)
    return (packed & (SDR_BIT | RGB_MASK)) | (alpha7 << 24)


def packed_alpha(packed: int) -> float:
    return ((packed >> 24) & 0x7F) / 127.0


def scale_alpha(packed: int, k: float) -> int:
    """``packed`` with its alpha multiplied by ``k`` (the fade helper)."""
    return with_alpha(packed, packed_alpha(packed) * k)


def style_color(r: float, g: float, b: float, a: float = 1.0):
    """The float4 to store in an imgui STYLE entry for this colour: imgui
    converts style floats to u32 with ``round(x * 255)`` per channel, so the
    stored floats are the packed bytes / 255 and the conversion lands on
    exactly the bytes ``pack_color`` would produce."""
    packed = pack_color(r, g, b, a)
    return ((packed & 0xFF) / 255.0, ((packed >> 8) & 0xFF) / 255.0,
            ((packed >> 16) & 0xFF) / 255.0, ((packed >> 24) & 0xFF) / 255.0)


# ---------------------------------------------------------------------------
# GLSL
# ---------------------------------------------------------------------------

# Splice this into any vertex shader that reads imgui's `Color` attribute
# (normalized ubytes). `MeltyHdrRange` / `MeltyHdrOctaves` are uniforms so a
# Toggles.HDR edit reaches the shader live - set them with
# `set_decode_uniforms(program)` each draw.
GLSL_DECODE = """
uniform float MeltyHdrRange;    // Toggles.HDR.vertex_range
uniform float MeltyHdrOctaves;  // Toggles.HDR.vertex_octaves
const mat3 MELTY_P3_TO_SRGB = mat3(%s);

vec3 melty_srgb_to_linear(vec3 c) {
    vec3 lo = c / 12.92;
    vec3 hi = pow((c + 0.055) / 1.055, vec3(2.4));
    return mix(lo, hi, step(0.04045, c));
}

// imgui vertex colour (normalized ubytes) -> linear scRGB + alpha.
vec4 melty_decode_color(vec4 c) {
    float abyte = c.a * 255.0;
    bool sdr = abyte >= 127.5;
    float alpha = (sdr ? abyte - 128.0 : abyte) / 127.0;
    vec3 rgb;
    if (sdr) {
        rgb = melty_srgb_to_linear(c.rgb);
    } else {
        vec3 code = c.rgb * 255.0;
        vec3 v = MeltyHdrRange * exp2(MeltyHdrOctaves * ((code - 1.0) / 254.0 - 1.0));
        v *= step(0.5, code);            // code 0 is exactly 0
        rgb = MELTY_P3_TO_SRGB * v;
    }
    return vec4(rgb, alpha);
}

// The VARYING form: rgb PREMULTIPLIED by alpha. imgui's anti-aliased fills
// and strokes give the feather's outer vertices the same RGB bytes with an
// alpha BYTE of 0 — which clears the SDR bit, so decoded per vertex those
// bytes read as an HDR log code (a 61/255 grey becomes linear 0.11, four
// times brighter) and the rasterizer interpolates that brightness across
// the feather: a light 1-px fringe around every rounded rect (09-07). A
// zero-alpha vertex must contribute NOTHING to the colour, which is what
// premultiplied interpolation does; the fragment stage divides the alpha
// back out (melty_unpremultiply) so the blend stays straight-alpha.
vec4 melty_decode_premultiplied(vec4 c) {
    vec4 d = melty_decode_color(c);
    return vec4(d.rgb * d.a, d.a);
}
"""

# Fragment-side inverse of melty_decode_premultiplied (the fragment shaders
# don't include GLSL_DECODE, so this is spliced on its own).
GLSL_UNPREMULTIPLY = """
vec4 melty_unpremultiply(vec4 f) {
    return vec4(f.rgb / max(f.a, 1e-6), f.a);
}
"""


def _glsl_mat3(m) -> str:
    # GLSL mat3 is column-major: list column by column.
    cols = [[m[r][c] for r in range(3)] for c in range(3)]
    return ", ".join(f"{v:.7f}" for col in cols for v in col)


GLSL_DECODE = GLSL_DECODE % _glsl_mat3(P3_TO_SRGB)

# Text brightness ceiling (Toggles.HDR.text_max_stops): scales the glyph's
# colour down uniformly - hue and saturation kept, P3 negatives ride along -
# until its brightest channel sits at MeltyTextMax (multiples of reference
# white). Spliced into the fragment stage of the draw-list renderer (glyph
# pixels only) and the text-texture bake; the uniform rides with
# set_decode_uniforms so both stay live with the toggle.
GLSL_TEXT_CLAMP = """
uniform float MeltyTextMax;     // 2^Toggles.HDR.text_max_stops

vec3 melty_clamp_text(vec3 c) {
    float peak = max(max(c.r, c.g), c.b);
    return peak > MeltyTextMax ? c * (MeltyTextMax / peak) : c;
}
"""


def set_decode_uniforms(program: int) -> None:
    """Set the curve uniforms `GLSL_DECODE` declares on a bound program."""
    import OpenGL.GL as gl
    rng, octaves = _curve_params()
    loc = gl.glGetUniformLocation(program, "MeltyHdrRange")
    if loc >= 0:
        gl.glUniform1f(loc, rng)
    loc = gl.glGetUniformLocation(program, "MeltyHdrOctaves")
    if loc >= 0:
        gl.glUniform1f(loc, octaves)
    loc = gl.glGetUniformLocation(program, "MeltyTextMax")   # GLSL_TEXT_CLAMP
    if loc >= 0:
        from meltygui.core.runtime.toggles import Toggles
        gl.glUniform1f(loc, float(2.0 ** float(Toggles.HDR.text_max_stops)))


# Linear scRGB -> display encodings, for the presentation pass.
GLSL_ENCODE = """
vec3 melty_linear_to_srgb(vec3 v) {
    v = clamp(v, 0.0, 1.0);
    vec3 lo = v * 12.92;
    vec3 hi = 1.055 * pow(v, vec3(1.0 / 2.4)) - 0.055;
    return mix(lo, hi, step(0.0031308, v));
}

// BT.709/sRGB primaries -> BT.2020 primaries (linear, D65).
const mat3 MELTY_SRGB_TO_BT2020 = mat3(
    0.6274040, 0.0690970, 0.0163916,
    0.3292820, 0.9195400, 0.0880132,
    0.0433136, 0.0113612, 0.8955950);

// SMPTE ST 2084 (PQ) encode of linear light in NITS.
vec3 melty_pq_encode(vec3 nits) {
    const float m1 = 0.1593017578125, m2 = 78.84375;
    const float c1 = 0.8359375, c2 = 18.8515625, c3 = 18.6875;
    vec3 y = clamp(nits / 10000.0, 0.0, 1.0);
    vec3 yp = pow(y, vec3(m1));
    return pow((c1 + c2 * yp) / (1.0 + c3 * yp), vec3(m2));
}
"""


# ---------------------------------------------------------------------------
# The wide-gamut picker's colour model (draw_color_picker's "Wide" tab)
# ---------------------------------------------------------------------------
#
# The picker works in DISPLAY P3 HSV plus an EXPOSURE: (h, s, v) is the
# classic square on P3 primaries (P3 saturates further than sRGB), and
# exposure is a linear multiplier above white - v = 1 with exposure 4 is
# white(4). Every displayable colour has one such tuple: the P3 gamut
# contains sRGB, so anything the display can show packs non-negative in P3.

import colorsys as _colorsys


def p3_hsv_from_extended(r: float, g: float, b: float):
    """Extended-sRGB → (h, s, v, exposure) in P3 HSV. A component outside P3
    (negative after the primaries conversion) clips to the P3 edge."""
    lin = linear_srgb_to_p3((srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b)))
    lin = tuple(max(0.0, c) for c in lin)
    peak = max(lin)
    exposure = peak if peak > 1.0 else 1.0
    enc = tuple(linear_to_srgb(c / exposure) for c in lin)      # Display P3 uses the sRGB curve
    h, s, v = _colorsys.rgb_to_hsv(*enc)
    return h, s, v, exposure


def extended_from_p3_hsv(h: float, s: float, v: float, exposure: float = 1.0):
    """(h, s, v, exposure) in P3 HSV → extended-sRGB (r, g, b)."""
    enc = _colorsys.hsv_to_rgb(h, s, v)
    lin = tuple(srgb_to_linear(c) * exposure for c in enc)
    return tuple(linear_to_srgb(c) for c in linear_p3_to_srgb(lin))


def wide_square_linear(hue: float, size: int, top_fraction: float, max_stops: float):
    """The picker square for one hue as a (size, size, 4) float32 array of
    LINEAR scRGB (row 0 = top). X is P3 saturation 0→1. The top
    `top_fraction` of the rows is exposure: 2^max_stops at the top down to
    1.0 (white) at the seam; the rest is the classic value axis 1→0."""
    import numpy as np
    n = int(size)
    top = max(1, int(round(n * top_fraction)))
    s = np.linspace(0.0, 1.0, n, dtype=np.float32)[None, :, None]           # columns
    rows = np.arange(n, dtype=np.float32)
    v = np.ones(n, dtype=np.float32)
    exposure = np.ones(n, dtype=np.float32)
    exposure[:top] = 2.0 ** (max_stops * (1.0 - rows[:top] / top))
    v[top:] = 1.0 - (rows[top:] - top) / max(1, n - top - 1)
    v = np.clip(v, 0.0, 1.0)[:, None, None]
    exposure = exposure[:, None, None]
    hue_rgb = np.asarray(_colorsys.hsv_to_rgb(hue, 1.0, 1.0), dtype=np.float32)[None, None, :]
    enc = v * (1.0 - s * (1.0 - hue_rgb))                                     # HSV at fixed hue, P3-encoded
    lin = np.where(enc <= 0.04045, enc / 12.92, ((enc + 0.055) / 1.055) ** 2.4) * exposure
    m = np.asarray(P3_TO_SRGB, dtype=np.float32)
    out = np.empty((n, n, 4), dtype=np.float32)
    out[..., :3] = lin @ m.T
    out[..., 3] = 1.0
    return out


# ---------------------------------------------------------------------------
# The "sRGB+" tab: the classic sRGB square, extended past its right edge
# ---------------------------------------------------------------------------
#
# The square is the classic sRGB HSV square, untouched. Each of its rows
# (an sRGB value v) ends at the sRGB gamut edge (hue, 1, v); the extension
# to its right carries that edge carried on in DISPLAY P3: same P3 hue,
# saturation from the edge's own P3 saturation up to 1 and value from the
# edge's P3 value up to the row's v - so the strip is colour-continuous
# at the seam, the top row's right edge is a pure P3 primary, and the
# extension shows all the colours sRGB cannot reach. Exposure
# (brightness above white) is not on the square: it rides through unchanged.


def srgb_extension_edge(hue: float, v: float):
    """The sRGB gamut edge colour (hue, s=1, v) in P3 HSV: (hp, sp, vp)."""
    r, g, b = _colorsys.hsv_to_rgb(hue, 1.0, v)
    hp, sp, vp, _exposure = p3_hsv_from_extended(r, g, b)
    return hp, sp, vp


def extended_from_srgb_extension(hue: float, s: float, v: float, x: float, exposure: float = 1.0):
    """sRGB+ coordinates → extended-sRGB (r, g, b). (hue, s, v) is the
    classic sRGB HSV square; x in [0, 1] is how far into the extension the
    marker sits (0 = the square's right edge, only meaningful at s = 1)."""
    if x <= 0.0:
        rgb = _colorsys.hsv_to_rgb(hue, s, v)
        return tuple(linear_to_srgb(srgb_to_linear(c) * exposure) for c in rgb)
    hp, sp, vp = srgb_extension_edge(hue, v)
    x = min(x, 1.0)
    return extended_from_p3_hsv(hp, sp + (1.0 - sp) * x, vp + (v - vp) * x, exposure)


def srgb_extension_coords(r: float, g: float, b: float, hue_hint: float = None, steps: int = 6):
    """Extended-sRGB → (hue, s, v, x, exposure) of the sRGB+ strip, the
    inverse of extended_from_srgb_extension. Inside sRGB it is the classic
    HSV with x = 0. Outside, s = 1: for a hue, the row v is the bisection
    root of the extension's P3 value (with x fixed by the P3 saturation)
    against the target's, and the hue is corrected by a few secant steps
    toward the target's P3 hue. hue_hint seeds that search (the cached hue keeps the marker
    steady when the colour is gray)."""
    lin = (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b))
    peak = max(lin)
    srgb_exposure = peak if peak > 1.0 else 1.0
    norm = tuple(c / srgb_exposure for c in lin)
    if all(-1e-6 <= c <= 1.0 + 1e-6 for c in norm):
        enc = tuple(linear_to_srgb(min(1.0, max(0.0, c))) for c in norm)
        h, s, v = _colorsys.rgb_to_hsv(*enc)
        return h, s, v, 0.0, srgb_exposure
    hp, sp, vp, exposure = p3_hsv_from_extended(r, g, b)
    vp_abs_lin = srgb_to_linear(vp) * exposure      # the P3 peak in absolute linear light
    vp_abs = linear_to_srgb(vp_abs_lin)             # ... and sRGB-encoded (the HSV value scale)
    if hue_hint is None:
        enc = tuple(linear_to_srgb(min(1.0, max(0.0, c / exposure))) for c in lin)
        hue_hint = _colorsys.rgb_to_hsv(*enc)[0]

    def solve_row(h):
        """(v, x, exposure) for hue h: x is fixed per row by the P3
        saturation; the row is the bisection root of the extension's P3
        value against the target's ABSOLUTE peak — an HDR colour takes the
        SDR row that reaches it when one does, else the top row with the
        rest as exposure (exposure is a free split of the same colour)."""
        def at(v):
            _hp, sp_e, vp_e = srgb_extension_edge(h, v)
            x = min(max((sp - sp_e) / max(1e-6, 1.0 - sp_e), 0.0), 1.0)
            return vp_e + (v - vp_e) * x, x
        top_value, top_x = at(1.0)
        if top_value <= vp_abs:
            # Top row + exposure. HSV saturation is not invariant under a
            # linear scale (the sRGB curve's offset), and the target's sp
            # was computed from the colour normalized to its peak - so find the
            # x whose top-row colour, normalized the same way, has that
            # saturation (monotone in x), and take the exposure from the
            # ratio of their peaks.
            hp_e, sp_e, vp_e = srgb_extension_edge(h, 1.0)

            def normalized(x):
                rgb = _colorsys.hsv_to_rgb(hp_e, sp_e + (1.0 - sp_e) * x, vp_e + (1.0 - vp_e) * x)
                lin = tuple(srgb_to_linear(c) for c in rgb)
                peak = max(lin)
                enc = tuple(linear_to_srgb(c / max(1e-9, peak)) for c in lin)
                return _colorsys.rgb_to_hsv(*enc)[1], peak
            lo, hi = 0.0, 1.0
            for _ in range(20):
                mid = 0.5 * (lo + hi)
                if normalized(mid)[0] < sp:
                    lo = mid
                else:
                    hi = mid
            x = 0.5 * (lo + hi)
            return 1.0, x, vp_abs_lin / max(1e-9, normalized(x)[1])
        lo, hi = 0.0, 1.0
        for _ in range(18):
            mid = 0.5 * (lo + hi)
            if at(mid)[0] < vp_abs:
                lo = mid
            else:
                hi = mid
        v = 0.5 * (lo + hi)
        return v, at(v)[1], 1.0

    def wrap(d):
        return (d + 0.5) % 1.0 - 0.5

    def residual(h):
        v, x, e = solve_row(h)
        return wrap(hp - srgb_extension_edge(h, v)[0]), v, x, e

    h = hue_hint
    delta, v, x, e = residual(h)
    for _ in range(steps):
        if abs(delta) < 1e-7:
            break
        # secant step: the sRGB→P3 hue slope is ~0.5 around green, ~1 elsewhere
        probe = 1e-3
        slope = wrap(srgb_extension_edge(h + probe, v)[0] - srgb_extension_edge(h, v)[0]) / probe
        h = (h + delta / min(max(slope, 0.25), 4.0)) % 1.0
        delta, v, x, e = residual(h)
    if abs(delta) > 1e-4:
        # The secant wandered (a poor hint across a hue sector): bracket the
        # sign change of the residual over the hue circle and bisect it -
        # the sRGB→P3 hue map is monotone, so exactly one bracket holds it.
        n = 24
        samples = [(i / n, residual(i / n)[0]) for i in range(n)]
        lo, hi = None, None
        for i in range(n):
            h0, d0 = samples[i]
            h1, d1 = samples[(i + 1) % n]
            if d0 >= 0.0 > d1 or (d0 >= 0.0 and d1 >= 0.0 and d1 < d0 - 0.5):
                lo, hi = h0, h0 + 1.0 / n
                break
        if lo is None:
            lo, hi = 0.0, 1.0
        for _ in range(22):
            mid = 0.5 * (lo + hi)
            if residual(mid % 1.0)[0] >= 0.0:
                lo = mid
            else:
                hi = mid
        h = (0.5 * (lo + hi)) % 1.0
        _d, v, x, e = residual(h)
    return h, 1.0, v, x, e


def srgb_extension_linear(hue: float, rows: int, cols: int):
    """The extension strip for one sRGB hue as a (rows, cols, 4) float32
    array of LINEAR scRGB (row 0 = top = v 1, bottom = black; column 0 =
    the sRGB edge, last column = P3 saturation 1 at the row's value)."""
    import numpy as np
    n_rows, n_cols = int(rows), int(cols)
    x = np.linspace(0.0, 1.0, n_cols, dtype=np.float32)[None, :, None]
    v_row = np.empty(n_rows, dtype=np.float32)
    hp = np.empty(n_rows, dtype=np.float32)
    sp = np.empty(n_rows, dtype=np.float32)
    vp = np.empty(n_rows, dtype=np.float32)
    for i in range(n_rows):
        v_row[i] = 1.0 - i / max(1, n_rows - 1)
        hp[i], sp[i], vp[i] = srgb_extension_edge(hue, float(v_row[i]))
    hue_rgb = np.asarray([_colorsys.hsv_to_rgb(float(h), 1.0, 1.0) for h in hp],
                         dtype=np.float32)[:, None, :]                          # per-row P3 hue
    s = sp[:, None, None] + (1.0 - sp[:, None, None]) * x                       # P3 saturation past the edge
    vv = vp[:, None, None] + (v_row[:, None, None] - vp[:, None, None]) * x     # P3 value up to the row's v
    enc = vv * (1.0 - s * (1.0 - hue_rgb))                                      # HSV at that position, P3-encoded
    lin = np.where(enc <= 0.04045, enc / 12.92, ((enc + 0.055) / 1.055) ** 2.4)
    m = np.asarray(P3_TO_SRGB, dtype=np.float32)
    out = np.empty((n_rows, n_cols, 4), dtype=np.float32)
    out[..., :3] = lin @ m.T
    out[..., 3] = 1.0
    return out


def srgb_plus_linear(hue: float, square: int, ext: int, band: int, max_stops: float):
    """The sRGB+ tab's whole picking area for one hue as a
    (band + square, square + ext, 4) float32 array of LINEAR scRGB: the
    classic sRGB square (rows band.., cols ..square), the P3 extension to
    its right (srgb_extension_linear), and above both an exposure band —
    the top row (v = 1) of each lifted by 2^max_stops at the top down to
    1.0 at the seam, so the band's bottom row IS the square's top row."""
    import numpy as np
    n_sq, n_ext, n_band = int(square), int(ext), int(band)
    out = np.empty((n_band + n_sq, n_sq + n_ext, 4), dtype=np.float32)
    out[..., 3] = 1.0
    # the classic square, sRGB-encoded (row 0 = v 1, col 0 = s 0)
    s_axis = np.linspace(0.0, 1.0, n_sq, dtype=np.float32)[None, :, None]
    v_axis = np.linspace(1.0, 0.0, n_sq, dtype=np.float32)[:, None, None]
    hue_rgb = np.asarray(_colorsys.hsv_to_rgb(hue, 1.0, 1.0), dtype=np.float32)[None, None, :]
    enc = v_axis * (1.0 - s_axis * (1.0 - hue_rgb))
    square_lin = np.where(enc <= 0.04045, enc / 12.92, ((enc + 0.055) / 1.055) ** 2.4)
    out[n_band:, :n_sq, :3] = square_lin
    if n_ext:
        out[n_band:, n_sq:, :] = srgb_extension_linear(hue, n_sq, n_ext)
    if n_band:
        rows = np.arange(n_band, dtype=np.float32)
        exposure = (2.0 ** (max_stops * (1.0 - rows / n_band)))[:, None, None]
        out[:n_band, :, :3] = out[n_band, :, :3][None, :, :] * exposure
    return out


def srgb_saturation_limit(hue: float, v: float, steps: int = 14) -> float:
    """Largest P3 saturation at (hue, v) whose colour still sits inside the
    sRGB gamut (bisection; 1.0 when every saturation fits, e.g. at black)."""
    def inside(s):
        lin = linear_p3_to_srgb(tuple(srgb_to_linear(c) for c in _colorsys.hsv_to_rgb(hue, s, v)))
        return all(-1e-4 <= c <= 1.0 + 1e-4 for c in lin)
    if inside(1.0):
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(steps):
        mid = 0.5 * (lo + hi)
        if inside(mid):
            lo = mid
        else:
            hi = mid
    return lo


def srgb_region_outline(hue: float, top_fraction: float, samples: int = 32):
    """The sRGB-reachable region of the wide square for one hue, as a list
    of (x, y) fractions of the square: along the white seam from the left
    edge to the gamut edge, then down the gamut edge to black."""
    points = [(0.0, top_fraction)]
    for i in range(samples + 1):
        v = 1.0 - i / samples
        x = srgb_saturation_limit(hue, v)
        y = top_fraction + (1.0 - v) * (1.0 - top_fraction)
        points.append((x, y))
    return points