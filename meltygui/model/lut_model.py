"""Editable palette dictionaries and integer-like LUT texture values."""
import math
import weakref

from meltygui.core.conversion.bubbling import _BubblingDictMixin, install_bubbling
from meltygui.model.texture_model import TextureId


class Lut(str):
    """A LUT NAME that is still a str everywhere it matters (dict keys,
    comparisons, GLSL host lookups) but carries its own TYPE, so meltygui routes
    it to its own renderer — a dropdown of the available LUTs rather than a
    text field. Same contract as TensorDim: the renderer must return
    Lut(...) or the first edit stores a plain str and the row falls back to
    the generic str renderer."""

    __slots__ = ()

    def __repr__(self):
        return f"Lut({str(self)!r})"


def _bake_lut(fn, n=256, clamp=True):
    """Sample fn(v ∈ [0,1]) → (r, g, b) into the flat-list LUT shape.
    Entries are EXTENDED sRGB (hdr_color.py): `clamp=False` keeps values
    past [0, 1] — above 1 is brighter than the desktop's white, negative is
    outside the sRGB gamut (P3) — for a table that is HDR on its own."""
    out = []
    for i in range(n):
        rgb = fn(i / (n - 1))
        if clamp:
            rgb = (min(1.0, max(0.0, float(c))) for c in rgb)
        out += [float(c) for c in rgb]
    return out

def hdr_ramp(hue_at, peak=16.0, chroma=1.0, white_from=0.55, n=1024):
    """Build an HDR colour scale as a flat LUT list: ``n`` entries spaced
    UNIFORMLY in Oklab lightness from black to ``peak`` × the desktop's
    white, each the most saturated colour of hue ``hue_at(u)`` (u = the
    lightness fraction, 0 → 1; radians) that fits the P3 box that tall,
    times ``chroma``. That is what "a bigger LUT" means: an SDR scale
    (``hot``: black → red → yellow → white) runs the max-chroma edges of a
    box ONE white tall; this runs the same kind of path up a box ``peak``
    whites tall, so the scale has log2(peak) extra stops of distinguishable
    levels — not a brighter table, a longer one. Entries are extended sRGB,
    unclamped (P3 reaches below 0, HDR above 1).

    Whitening is a SECOND axis of information, not a side effect: past
    ``white_from`` the chroma eases to zero at the peak (smoothstep), so
    the top of the range reads as saturated → pale → white while the
    lightness keeps climbing — the box alone would keep a P3 yellow fully
    saturated to within a few percent of the peak and then snap to white,
    since a pure yellow fits a 16× box until its two channels hit 16. The
    ease also lands the scale inside what the panel can show: a 12× white
    saturated yellow is past any panel's peak and the compositor would
    desaturate it anyway (Hyprland's luminance-preserving rule)."""
    from meltygui.hdr_color import _cbrt
    from meltygui.hdr_color import linear_to_srgb
    from meltygui.hdr_color import oklab_max_chroma
    from meltygui.hdr_color import oklab_to_linear
    peak_lightness = _cbrt(peak)
    out = []
    for i in range(n):
        u = i / (n - 1)
        lightness = u * peak_lightness
        hue = hue_at(u)
        w = min(1.0, max(0.0, (u - white_from) / (1.0 - white_from)))
        envelope = 1.0 - w * w * (3.0 - 2.0 * w)
        c = chroma * envelope * oklab_max_chroma(lightness, hue, peak)
        lin = oklab_to_linear((lightness, c * math.cos(hue), c * math.sin(hue)))
        out += [linear_to_srgb(v) for v in lin]
    return out

def _hot_hdr_hue(u):
    """`hot`'s hue path for hdr_ramp: P3 red through the lower half of the
    lightness range, turning to P3 yellow across the middle, yellow above
    (the box then whitens it toward the peak). The SDR `hot` is untouched:
    an HDR scale is its own picker entry, never a scaled SDR one, so a
    colour scale people know keeps meaning what it meant (Lukas 09-08)."""
    from meltygui.hdr_color import linear_p3_to_srgb
    from meltygui.hdr_color import oklab_hue
    # [tint=(0.95, 0.35, 0.1)]
    red_until = 0.4        # lightness fraction that stays pure red
    # [tint=(0.95, 0.75, 0.2)]
    yellow_from = 0.7      # ... and where it has fully turned yellow
    red = oklab_hue(linear_p3_to_srgb((1.0, 0.0, 0.0)))
    yellow = oklab_hue(linear_p3_to_srgb((1.0, 1.0, 0.0)))
    t = min(1.0, max(0.0, (u - red_until) / (yellow_from - red_until)))
    t = t * t * (3.0 - 2.0 * t)                      # smoothstep
    return red + (yellow - red) * t

def _poly(coeffs):
    """Per-channel polynomial in t (Horner); rows are (r, g, b) coefficients
    in ascending order — the shape of Matt Zucker's matplotlib colormap fits
    (shadertoy WlfXRN) and of Google's turbo fit."""

    def fn(t):
        r = g = b = 0.0
        for cr, cg, cb in reversed(coeffs):
            r, g, b = r * t + cr, g * t + cg, b * t + cb
        return r, g, b

    return fn

def _seismic(v):
    """Diverging blue-white-red with dark ends (matplotlib's seismic) —
    strong negative coverage: zero is white, sign maps to hue, magnitude
    to saturation/darkness. Pair with the `centered` param."""
    if v < 0.25:
        t = v / 0.25
        return (0.0, 0.0, 0.3 + 0.7 * t)
    if v < 0.5:
        t = (v - 0.25) / 0.25
        return (t, t, 1.0)
    if v < 0.75:
        t = (v - 0.5) / 0.25
        return (1.0, 1.0 - t, 1.0 - t)
    t = (v - 0.75) / 0.25
    return (1.0 - 0.5 * t, 0.0, 0.0)

def _coolwarm(v):
    """Cool-to-warm diverging ramp: blue → near-white → red."""
    if v < 0.5:
        t, a, b = v * 2.0, (0.230, 0.299, 0.754), (0.865, 0.865, 0.865)
    else:
        t, a, b = v * 2.0 - 1.0, (0.865, 0.865, 0.865), (0.706, 0.016, 0.150)
    return tuple(x + (y - x) * t for x, y in zip(a, b))

_VIRIDIS = [
    (0.2777273272234177, 0.005407344544966578, 0.3340998053353061),
    (0.1050930431085774, 1.404613529898575, 1.384590162594685),
    (-0.3308618287255563, 0.214847559468213, 0.09509516302823659),
    (-4.634230498983486, -5.799100973351585, -19.33244095627987),
    (6.228269936347081, 14.17993336680509, 56.69055260068105),
    (4.776384997670288, -13.74514537774601, -65.35303263337234),
    (-5.435455855934631, 4.645852612178535, 26.3124352495832),
]

_PLASMA = [
    (0.05873234392399702, 0.02333670892565664, 0.5433401826748754),
    (2.176514634195958, 0.2383834171260182, 0.7539604599784036),
    (-2.689460476458034, -7.455851135738909, 3.110799939717086),
    (6.130348345893603, 42.3461881477227, -28.51885465332158),
    (-11.10743619062271, -82.66631109428045, 60.13984767418263),
    (10.02306557647065, 71.41361770095349, -54.07218655560067),
    (-3.658713842777788, -22.93153465461149, 18.19190778539828),
]

_MAGMA = [
    (-0.002136485053939582, -0.000749655052795221, -0.005386127855323933),
    (0.2516605407371642, 0.6775232436837668, 2.494026599312351),
    (8.353717279216625, -3.577719514958484, 0.3144679030132573),
    (-27.66873308576866, 14.26473078096533, -13.64921318813922),
    (52.17613981234068, -27.94360607168351, 12.94416944238394),
    (-50.76852536473588, 29.04658282127291, 4.23415299384598),
    (18.65570506591883, -11.48977351997711, -5.601961508734096),
]

_INFERNO = [
    (0.0002189403691192265, 0.001651004631001012, -0.01948089843709184),
    (0.1065134194856116, 0.5639564367884091, 3.932712388889277),
    (11.60249308247187, -3.972853965665698, -15.9423941062914),
    (-41.70399613139459, 17.43639888205313, 44.35414519872813),
    (77.162935699427, -33.40235894210092, -81.80730925738993),
    (-71.31942824499214, 32.62606426397723, 73.20951985803202),
    (25.13112622477341, -12.24266895238567, -23.07032500287172),
]

_TURBO = [
    (0.13572138, 0.09140261, 0.10667330),
    (4.61539260, 2.19418839, 12.64194608),
    (-42.66032258, 4.84296658, -60.58204836),
    (132.13108234, -14.18503333, 110.36276771),
    (-152.94239396, 4.27729857, -89.90310912),
    (59.28637943, 2.82956604, 27.34824973),
]


def make_luts():
    """A fresh editable dictionary of flat RGB lists, including HDR palettes."""
    return {
        # the original custom jet, baked from the exact formula the shader uses
        "jet": _bake_lut(lambda v: (1.5 - abs(4.0 * v - 3.0),
                                    1.5 - abs(4.0 * v - 2.0),
                                    1.5 - abs(4.0 * v - 1.0))),
        "viridis": _bake_lut(_poly(_VIRIDIS)),
        "plasma": _bake_lut(_poly(_PLASMA)),
        "magma": _bake_lut(_poly(_MAGMA)),
        "inferno": _bake_lut(_poly(_INFERNO)),
        "turbo": _bake_lut(_poly(_TURBO)),
        "grey": _bake_lut(lambda v: (v, v, v)),
        "hot": _bake_lut(lambda v: (3.0 * v, 3.0 * v - 1.0, 3.0 * v - 2.0)),
        "coolwarm": _bake_lut(_coolwarm),
        "seismic": _bake_lut(_seismic),
        # ── HDR ramps get their own entries (hdr_ramp - uniform Oklab lightness
        # up a 16× box, max chroma that fits, 4 stops longer than the SDR one)
        "hot_hdr": hdr_ramp(_hot_hdr_hue, peak=16.0),
    }


def lut_values(luts, name):
    """Resolve a named palette from supplied data, falling back to its jet entry."""
    return luts.get(str(name), luts.get("jet", (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)))


class LutTexture(TextureId):
    """A flat RGB palette that behaves as its current OpenGL texture ID."""

    def __init__(self, colors):
        from OpenGL.GL import GL_TEXTURE_1D
        super().__init__(GL_TEXTURE_1D)
        self.colors = colors

    def _upload(self, state):
        return state.texture1d('texture', self.colors, version=hash(tuple(self.colors)))

    def cuda(self, device):
        """The same palette on the tensor's device, refreshed when colours change."""
        import torch
        return self._state().get(
            ('cuda_lut', str(device)),
            lambda: torch.tensor(self.colors, dtype=torch.float32, device=device).reshape(-1, 3),
            deps=tuple(self.colors))


class LutPalette(_BubblingDictMixin, dict):
    """An ordinary editable palette dictionary with stable texture proxies.

    List/dictionary edits bubble through the existing mutation mechanism.
    Subscribers are weak bound callbacks; no host, rendering loop or GL work
    is needed to hold or edit the values.
    """

    def __init__(self, colors=None):
        dict.__init__(self, make_luts() if colors is None else colors)
        self._textures = {}
        self._callbacks = {}
        install_bubbling(self, self)

    def texture(self, name):
        name = str(name)
        colors = lut_values(self, name)
        texture = self._textures.get(name)
        if texture is None:
            texture = LutTexture(colors)
            self._textures[name] = texture
        else:
            texture.colors = colors
        return texture

    def watch(self, callback, **kwargs):
        key = (id(callback.__self__), callback.__func__)
        self._callbacks[key] = (weakref.WeakMethod(callback), kwargs)

    def _mark_changed(self):
        for name in list(self._textures):
            if name not in self:
                del self._textures[name]
            else:
                self._textures[name].colors = self[name]
        for key, (reference, kwargs) in list(self._callbacks.items()):
            callback = reference()
            if callback is None:
                del self._callbacks[key]
            else:
                callback(**kwargs)
