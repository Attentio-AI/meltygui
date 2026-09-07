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
P3_TO_SRGB = _mat_mul(_mat_inv(_SRGB_TO_XYZ), _P3_TO_XYZ)


def linear_srgb_to_p3(rgb):
    return _mat_mul_vec(SRGB_TO_P3, rgb)


def linear_p3_to_srgb(rgb):
    return _mat_mul_vec(P3_TO_SRGB, rgb)


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
    from src.lsd.gl_gui.toggles import Toggles
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