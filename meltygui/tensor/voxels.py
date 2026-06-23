"""Voxel renderer on the GLState + @shader_func stack, plugged into real data
through a RenderHost.

The pipeline — draw_voxels owns everything, no state objects:

    tensor / ndarray / None ──voxel_io──► tensor ──draw_voxels──► pixels
    (host input)             (resolve     (held in   (slice + upload + render,
                              source/demo) the host)  all parameter-driven)

- `voxel_io` only resolves the SOURCE (tensor/ndarray through, anything else
  → demo torus). `draw_voxels` slices via slice_volume (a pure function of
  its params), uploads through its gl_state (CUDA tensors device-to-device
  via a registered PBO, cuda_interop.py; re-upload keyed on source
  identity/`_version`/mapping deps — a tensor mutated by training streams
  in), and renders. Tensor METADATA (shape, dim count) rides the uploaded
  buffer; every camera/shading/mapping choice is a PARAMETER on the
  draw_voxels signature — auto draw_state params, so gestures and the
  controls panel write draw_state.<name> and only diverged values persist.
- The fragment shader declares NO uniforms; break it in the editor and the last
  good program keeps rendering with the remapped driver error underneath.
- LUTs are flat [r,g,b, r,g,b, ...] float lists (LUTS); `lut_host` is a
  RenderHost whose io turns them into shared 1-D textures (_LUT_TEXTURES),
  re-uploading when a list is edited. draw_voxels samples the one its `lut`
  param names — the old custom jet() GLSL is now just the baked "jet" entry.
- Axis labels are textured billboards IN the scene: text_texture.py bakes the
  strings via imgui's own font atlas (a private shared-atlas context + the
  screen pass's draw-list mechanics, no freetype), and a raw-GL pass draws each
  as a world-space quad in the voxel FBO — baseline along its edge, up-axis
  perpendicular, flipped per frame so it always reads upright.

Middle-drag = orbit (shift: pan, ctrl: dolly), scroll = zoom, and Blender-style
numpad views while hovered: 7/1/3 = top/front/right (ctrl = opposite side),
5 = perspective/ortho toggle, / (or numpad .) = recenter the pan on the origin.
Four hosts/windows ship as playgrounds:
"Voxel Volume" (unbound → torus demo), "Voxel 4D" (time-breathing torus —
scrub dim0), "Voxel 5D" (layer/head patterns — two scrubbers), and
"Voxel Flow" (a flat (96, 4096) matrix: enable neural flow, chop x along z,
chunk 128, and the feature dim unrolls into a browsable volume — the old
viewer's trick for weird-shaped tensors).
"""

import ctypes
import math

import imgui
import numpy as np
import OpenGL.GL as gl

from src.lsd.gl_gui.gl_state import GLState, GLTexture
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.shader_func import shader_func
from src.lsd.gl_gui.text_texture import bake_text, bake_texts
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any

HALF_PI = math.pi / 2

VOXEL_FRAG = """
#version 330 core
in vec2 uv;
out vec4 FragColor;

// Slab intersection with the box [-bounds, +bounds]: (t_enter, t_exit).
vec2 rayBox(vec3 ro, vec3 rd, vec3 bounds) {
    vec3 inv = 1.0 / rd;
    vec3 t0 = (-bounds - ro) * inv;
    vec3 t1 = ( bounds - ro) * inv;
    vec3 lo = min(t0, t1);
    vec3 hi = max(t0, t1);
    return vec2(max(max(lo.x, lo.y), lo.z), min(min(hi.x, hi.y), hi.z));
}

void main() {
    // Z-up orbit camera built straight from injected uniforms — tilt, spin,
    // zoom, pan and ortho arrive as plain Python kwargs, no matrices anywhere.
    // The basis is analytic in spin/tilt (not cross(fwd, world-up)) so the
    // numpad top/bottom presets (tilt = ±π/2) stay well-defined; it matches
    // the old construction everywhere else.
    float ct = cos(tilt);
    vec3 fwd = -vec3(cos(spin) * ct, sin(spin) * ct, sin(tilt));
    vec3 right = vec3(-sin(spin), cos(spin), 0.0);
    vec3 up = cross(right, fwd);
    vec3 eye = vec3(pan_x, pan_y, pan_z) - fwd * zoom;
    vec2 ndc = (uv * 2.0 - 1.0) * vec2(aspect, 1.0);
    // Perspective rays fan out from the eye; ortho rays march parallel from
    // a plane through it, sized to match the perspective frame at the target.
    vec3 ro = ortho ? eye + (right * ndc.x + up * ndc.y) * (zoom / 1.7) : eye;
    vec3 rd = ortho ? fwd : normalize(fwd * 1.7 + right * ndc.x + up * ndc.y);
    // Accumulate optical depth per unit of VIEW DEPTH, not per unit of ray
    // arc length. In perspective, edge rays cross the volume at a steeper
    // angle and a step's world length (seg) is ~1/cos(theta) longer than a
    // center ray's, so a thin slab reads denser toward the screen edges (a
    // screenspace radial artifact — hidden on cubes only because they saturate
    // the alpha break). cos(angle to fwd) cancels the extra path. Ortho rays
    // have rd == fwd, so view_cos == 1 and this is a no-op there.
    float view_cos = dot(rd, fwd);

    // volume_scale: box extents per axis, voxel-count-proportional — so each
    // VOXEL is a cube and the tensor keeps its true shape.
    vec2 hit = rayBox(ro, rd, volume_scale);
    if (hit.x > hit.y || hit.y < 0.0) { FragColor = vec4(0.0); return; }

    float t = max(hit.x, 0.0);
    vec4 acc = vec4(0.0);
    // max_steps is a watchdog: the break on hit.y is what normally ends the
    // march. The whole box is covered only while max_steps * step_size
    // exceeds the worst-case chord (2*sqrt(3) ≈ 3.46 units) — a granular
    // step_size needs a higher cap or the far side of the volume clips away.
    for (int i = 0; i < max_steps; i++) {
        if (t >= hit.y || acc.a > 0.98) break;
        // Weight each sample by the segment it actually covers (the tail is
        // partial) and sample at the segment MIDPOINT: a slab thinner than
        // one step then accumulates opacity proportional to its true path
        // length instead of jumping by whole steps as the sample count
        // changes with view angle — the concentric-ring artifact on thin
        // tensors.
        float seg = min(step_size, hit.y - t);
        vec3 p = (ro + rd * (t + seg * 0.5)) / volume_scale * 0.5 + 0.5;
        float v = texture(volume, p).r;
        if (centered) { v = v * 0.5 + 0.5; }   // signed [-1,1] -> [0,1]
        // The old viewer's value pipeline, verbatim: contrast about
        // mid-grey, then brightness, on the GREYSCALE value — the LUT lookup
        // and the opacity gate both consume the remapped value. `lut` is a
        // 1-D texture the LUT host baked from a flat [r,g,b,...] float list
        // (the old jet() is now just the "jet" entry).
        v = (v - 0.5) * contrast + 0.5;
        float m;   // opacity drive: the value, or its magnitude when centered
        if (centered) {
            // signed data: raw 0 sits at the LUT middle (pair with a
            // diverging LUT like seismic/coolwarm), brightness gains about
            // the center, and opacity keys on MAGNITUDE so negatives render
            // as strongly as positives.
            v = 0.5 + (v - 0.5) * brightness;
            v = clamp(v, 0.0, 1.0);
            m = abs(v - 0.5) * 2.0;
        } else {
            v *= brightness;
            v = clamp(v, 0.0, 1.0);
            m = v;
        }
        // The old viewer's transfer function: values at/above the gate
        // (1 - threshold) are FULLY opaque — a hard isosurface — and below
        // it opacity falls off as (m/gate)^4, scaled by density and the
        // marched segment length (seg = step_size except the partial tail).
        float gate = 1.0 - clamp(threshold, 0.0, 0.999);
        float a;
        if (m >= gate) {
            a = 1.0;
        } else {
            a = clamp(pow(m / gate, 4.0) * density * seg * view_cos * 50.0, 0.0, 1.0);
        }
        if (a > 0.0) {
            acc.rgb += (1.0 - acc.a) * a * texture(lut, v).rgb;
            acc.a   += (1.0 - acc.a) * a;
        }
        t += step_size;
    }
    FragColor = acc;
}
"""


@shader_func(fragment=VOXEL_FRAG)
def voxel_pass(gl_state: GLState = None, tilt=0.5, spin=0.8, zoom=3.4,
               pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False,
               aspect=1.0, brightness=1.0, contrast=1.0, density=1.0,
               threshold=0.1, step_size=0.0015, max_steps=4096, centered=False,
               volume=None, lut=None,
               volume_scale=(1.0, 1.0, 1.0), **kwargs):
    # Program bound, uniforms set - the body is just the draw call.
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


# ── label billboards: text quads IN the 3d scene ───────────────────────────
# Label text is baked into RGBA textures by label_texture.py (imgui's own font
# atlas laid out in a private shared-atlas context and baked with the same
# draw-list mechanics as the screen pass - no freetype). Each label is then a
# quad in volume-box world space, rendered into the voxel FBO with the SAME
# analytic camera as the volume, so labels foreshorten/track like scene
# geometry instead of floating like screen text.

# This pass is definitely RAW GL with INSTANCING, not @shader_func: dozens
# of label quads draw per frame on streaming volumes, and per-quad Python GL
# calls (let alone shader_func's per-call kwargs→uniform plumbing) cost real
# frame time - the 120→90fps regression. All strings for a view bake into
# ONE vertical-strip atlas (re-baked only when the tick set changes), every
# quad's placement rides a per-instance vertex buffer, and the whole label
# set is a single glDrawArraysInstanced.

LABEL_VERT = """
#version 330 core
uniform float tilt, spin, zoom, aspect;
uniform bool ortho;
uniform vec3 pan;
layout(location = 0) in vec3 a_anchor;   // world point ON the edge
layout(location = 1) in vec3 a_u;        // baseline dir (flipped for reading)
layout(location = 2) in vec3 a_v;        // text-up dir
layout(location = 3) in vec3 a_out;      // unflipped outward dir (placement)
layout(location = 4) in vec4 a_metrics;  // half_w, half_h, offs (NDC), alpha
layout(location = 5) in vec4 a_uvrect;   // u0, v0(bottom), u1, v1(top)
out vec2 uv;
out float v_alpha;
void main() {
    // two-triangle quad from gl_VertexID: corners in {-1,+1}²
    int id = gl_VertexID;
    vec2 q = vec2((id == 1 || id == 2 || id == 4) ? 1.0 : -1.0,
                  (id == 2 || id == 4 || id == 5) ? 1.0 : -1.0);
    uv = vec2(mix(a_uvrect.x, a_uvrect.z, q.x * 0.5 + 0.5),
              mix(a_uvrect.y, a_uvrect.w, q.y * 0.5 + 0.5));
    v_alpha = a_metrics.w;
    float ct = cos(tilt);
    vec3 fwd = -vec3(cos(spin) * ct, sin(spin) * ct, sin(tilt));
    vec3 right = vec3(-sin(spin), cos(spin), 0.0);
    vec3 up = cross(right, fwd);
    vec3 eye = pan - fwd * zoom;
    // Screen-constant sizing: metrics arrive in NDC units. The world length
    // that projects to one NDC unit at the ANCHOR's depth is depth/1.7
    // (zoom/1.7 in ortho), so the label keeps its pixel size at any zoom
    // while still anchoring to and foreshortening with the scene.
    float ws = (ortho ? zoom : max(0.05, dot(a_anchor - eye, fwd))) / 1.7;
    vec3 world = a_anchor + (a_out * a_metrics.z
               + a_u * (a_metrics.x * q.x) + a_v * (a_metrics.y * q.y)) * ws;
    vec3 d = world - eye;
    // The voxel ray gen, inverted (same math as project_corners): perspective
    // keeps the depth in w for the divide, ortho is a plain scale.
    if (ortho) {
        float s = zoom / 1.7;
        gl_Position = vec4(dot(d, right) / (s * aspect), dot(d, up) / s, 0.0, 1.0);
    } else {
        gl_Position = vec4(1.7 * dot(d, right) / aspect, 1.7 * dot(d, up),
                           0.0, dot(d, fwd));
    }
}
"""

LABEL_FRAG = """
#version 330 core
uniform sampler2D label;
in vec2 uv;
in float v_alpha;
out vec4 FragColor;
void main() {
    vec4 t = texture(label, uv);
    FragColor = vec4(t.rgb, t.a * v_alpha);
}
"""

_LABEL_UNIFORMS = ("tilt", "spin", "zoom", "aspect", "ortho", "pan", "label")
_LABEL_FLOATS = 20  # 4×vec3 + 2×vec4 per instance


def _label_program(gl_state):
    """The instanced label program + its uniform-location map, compiled once
    per GLState (re-created when the GLSL source changes, e.g. on hotswap)."""

    def create():
        def compile_one(kind, source):
            s = gl.glCreateShader(kind)
            gl.glShaderSource(s, source)
            gl.glCompileShader(s)
            if gl.glGetShaderiv(s, gl.GL_COMPILE_STATUS) != gl.GL_TRUE:
                raise RuntimeError(gl.glGetShaderInfoLog(s).decode(errors="replace"))
            return s

        vs = compile_one(gl.GL_VERTEX_SHADER, LABEL_VERT)
        fs = compile_one(gl.GL_FRAGMENT_SHADER, LABEL_FRAG)
        prog = gl.glCreateProgram()
        gl.glAttachShader(prog, vs)
        gl.glAttachShader(prog, fs)
        gl.glLinkProgram(prog)
        gl.glDeleteShader(vs)
        gl.glDeleteShader(fs)
        if gl.glGetProgramiv(prog, gl.GL_LINK_STATUS) != gl.GL_TRUE:
            raise RuntimeError(gl.glGetProgramInfoLog(prog).decode(errors="replace"))
        loc = {n: gl.glGetUniformLocation(prog, n) for n in _LABEL_UNIFORMS}
        return prog, loc

    def delete(value):
        gl.glDeleteProgram(value[0])

    return gl_state.get("label_prog", create, delete,
                        deps=(hash(LABEL_VERT), hash(LABEL_FRAG)))


def _label_vao(gl_state):
    """(vao, vbo): one interleaved per-instance buffer (divisor 1 on every
    attribute — the quad corners come from gl_VertexID, no vertex attribs)."""

    def create():
        vao = gl.glGenVertexArrays(1)
        vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        stride = _LABEL_FLOATS * 4
        offset = 0
        for slot, n in ((0, 3), (1, 3), (2, 3), (3, 3), (4, 4), (5, 4)):
            gl.glEnableVertexAttribArray(slot)
            gl.glVertexAttribPointer(slot, n, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            gl.glVertexAttribDivisor(slot, 1)
            offset += n * 4
        gl.glBindVertexArray(0)
        return vao, vbo

    def delete(value):
        vao, vbo = value
        gl.glDeleteBuffers(1, [vbo])
        gl.glDeleteVertexArrays(1, [vao])

    return gl_state.get("label_vao", create, delete)


def _label_atlas(gl_state, texts):
    """The strip atlas for this view's label strings, cached until the
    string SET changes (tick sets only change at zoom thresholds, so
    re-bakes are rare). `texts` must be a sorted tuple."""
    from src.lsd.gl_gui.fonts import Font
    from src.lsd.gl_gui.melty import Melty
    font = Melty.font_mgr.get(Font.JETBRAINS_MONO_30) if Melty.font_mgr else None

    def create():
        return bake_texts(texts, font=font)

    def delete(value):
        gl.glDeleteTextures([value[0].texture_id])

    return gl_state.get("label_atlas", create, delete, deps=(texts, id(font)))

# ── LUTs: a LUT is just a flat [r,g,b, r,g,b, ...] float list ───────────────
# lut_host (bottom of file) turns these into shared 1-D textures; draw_voxels
# samples the one its `lut` param names. Editing a list re-uploads.

def _bake_lut(fn, n=256):
    """Sample fn(v ∈ [0,1]) → (r, g, b) into the flat-list LUT shape."""
    out = []
    for i in range(n):
        r, g, b = fn(i / (n - 1))
        out += [min(1.0, max(0.0, float(r))),
                min(1.0, max(0.0, float(g))),
                min(1.0, max(0.0, float(b)))]
    return out


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


LUTS = {
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
}

# Host-converted 1-D textures by LUT name - module-level (hotswap-reused) so
# every voxel view uses the SAME textures the LUT host materialized.
_LUT_TEXTURES = globals().get("_LUT_TEXTURES", {})

# Once-only warning latch for the label-billboard path (hotswap-reused).
_LABEL_WARNED = globals().get("_LABEL_WARNED", False)

# Survives hotswap re-exec (module dict is reused) so the demo volume isn't
# regenerated on every code edit.
_VOLUME = globals().get("_VOLUME")


def demo_volume():
    """Procedural (96,96,96) float32 volume: a torus around Z plus two
    gaussian blobs — enough structure to judge orientation and filtering."""
    global _VOLUME
    if _VOLUME is None:
        n = 96
        c = np.linspace(-1.0, 1.0, n, dtype=np.float32)
        z, y, x = np.meshgrid(c, c, c, indexing="ij")
        ring = np.sqrt(x * x + y * y) - 0.55
        torus = np.exp(-(ring * ring + z * z) / 0.018)
        blob1 = np.exp(-((x - 0.35) ** 2 + (y + 0.30) ** 2 + (z - 0.40) ** 2) / 0.045)
        blob2 = np.exp(-((x + 0.40) ** 2 + (y - 0.25) ** 2 + (z + 0.35) ** 2) / 0.030)
        _VOLUME = np.clip(torus + 0.9 * blob1 + 0.8 * blob2, 0.0, 1.0).astype(np.float32)
    return _VOLUME


def _clean_dim_name(x, i):
    """A dim name is a short single-line LABEL, whatever lands in the list —
    DnD/paste can drop arbitrary objects whose str() is a multi-KB code repr,
    and one of those blows up every radio row and billboard bake."""
    first = (str(x).splitlines() or [""])[0].strip()
    return first[:48] if first else f"dim{i}"


def _resolve_dim(dim_names, v, n):
    """A dim given by INDEX or by NAME (resolved through dim_names); None
    stays None, out-of-range collapses to None."""
    if v is None:
        return None
    if isinstance(v, str):
        names = list(dim_names or ())
        if v not in names:
            return None
        v = names.index(v)
    v = int(v)
    return v if 0 <= v < n else None


def _resolve_axes(shape, dim_names, x_dim, y_dim, z_dim):
    """(z, y, x) display dims for a shape: dims by index or NAME, None
    derives the default (last three dims → z/y/x, like the old viewer).
    A None fill never lands on an explicitly taken dim."""
    n = len(shape)
    zd = _resolve_dim(dim_names, z_dim, n)
    yd = _resolve_dim(dim_names, y_dim, n)
    xd = _resolve_dim(dim_names, x_dim, n)
    taken = {d for d in (zd, yd, xd) if d is not None}

    def fill(cur, default):
        if cur is not None:
            return cur
        d = default
        while d in taken and d > 0:
            d -= 1
        taken.add(d)
        return d

    zd = fill(zd, max(0, n - 3))
    yd = fill(yd, max(0, n - 2))
    xd = fill(xd, n - 1)
    return zd, yd, xd


def slice_volume(t, dim_names=(), x_dim=None, y_dim=None, z_dim=None,
                 slices=(), mean_dims=(), sort_dim=-1, normalize=False,
                 nf_on=False, nf_chop=None, nf_along=None, nf_chunk=128):
    """tensor → (depth, height, width) display volume, PURE: every choice
    arrives as an argument (the draw_voxels params), nothing is stored.
    Unmapped dims pin to their `slices` index (missing entries → 0) or
    average when listed in mean_dims (keepdim, then pinned at 0); sort
    orders fibers along a dim; normalize min-max stretches the DISPLAYED
    volume (signed data scales by max-magnitude so zero stays anchored).
    Stays on t's device. Returns (vol3, (z_dim, y_dim, x_dim), shape)."""
    import torch
    t = t.detach()
    while t.dim() < 3:
        t = t.unsqueeze(0)
    if t.dtype not in (torch.float16, torch.float32):
        t = t.float()
    n = t.dim()
    shape = tuple(int(s) for s in t.shape)
    zd, yd, xd = _resolve_axes(shape, dim_names, x_dim, y_dim, z_dim)
    if 0 <= int(sort_dim) < n:
        t = torch.sort(t, dim=int(sort_dim), descending=True).values
    picked = (zd, yd, xd)
    mean_set = {int(d) for d in (mean_dims or ())
                if 0 <= int(d) < n and int(d) not in picked}
    for d in mean_set:
        t = t.mean(dim=d, keepdim=True)
    index = tuple(
        slice(None) if d in picked
        else (0 if d in mean_set
              else min(int(slices[d]) if d < len(slices) else 0, shape[d] - 1))
        for d in range(n))
    sub = t[index]  # picked 3 dims keep original order
    remaining = sorted(picked)
    vol = sub.permute(remaining.index(zd), remaining.index(yd),
                      remaining.index(xd)).contiguous()
    if nf_on:
        # Flow is pinned to TENSOR DIMS (remapping x/y/z never changes WHICH
        # data gets chopped); unset dims default to chop=x, along=z. A chop
        # or along dim that isn't mapped makes it a no-op.
        chop_d = _resolve_dim(dim_names, nf_chop, n)
        along_d = _resolve_dim(dim_names, nf_along, n)
        dim_to_axis = {xd: "x", yd: "y", zd: "z"}
        chop = dim_to_axis.get(xd if chop_d is None else chop_d)
        along = dim_to_axis.get(zd if along_d is None else along_d)
        if chop and along and chop != along:
            vol = neural_flow_volume(vol, chop, along, int(nf_chunk))
    if normalize:
        lo, hi = vol.min(), vol.max()
        if lo < 0:
            vol = vol / (torch.maximum(hi.abs(), lo.abs()) + 1e-12)
        else:
            vol = (vol - lo) / (hi - lo + 1e-12)
    return vol, (zd, yd, xd), shape


# Display-axis position in the sliced (z, y, x) volume.
_AXIS_POS = {"z": 0, "y": 1, "x": 2}


def neural_flow_volume(vol, chop_axis, along_axis, chunk):
    """The old viewer's neural flow on the DISPLAY volume: chop one axis into
    `chunk`-wide blocks and concatenate them group-major along another —
    identical layout to the original get_neural_flow's j*orig+i ordering,
    which is exactly cat(split). No-op when the chop doesn't divide evenly
    or the axes coincide."""
    chop, along = _AXIS_POS[chop_axis], _AXIS_POS[along_axis]
    size = int(vol.shape[chop])
    if chop == along or chunk <= 0 or size <= chunk or size % chunk != 0:
        return vol
    import torch
    return torch.cat(vol.split(chunk, dim=chop), dim=along).contiguous()


@render_func(show_bg=False)
def voxel_io(input_value=None, view_func=None, external_change=False, **kwargs):
    """RenderHost io: resolve the SOURCE and pass it through — slicing and
    upload are draw_voxels' job (parameter-driven, gl_state-cached), so no
    GL happens here. Only tensor-shaped inputs count as a source; everything
    else (None, the host dict, an echoed held value) serves the demo torus."""
    import torch
    source = input_value if isinstance(input_value, (torch.Tensor, np.ndarray)) else None
    t = demo_volume() if source is None else source
    imgui.text(f"{'demo' if source is None else type(source).__name__} → draw_voxels")
    if view_func is None:
        return False, t
    return view_func(input_value=t, external_change=external_change, **kwargs)


@render_func(show_bg=False)
def lut_io(input_value=None, gl_state: GLState = None, view_func=None,
           external_change=False, **kwargs):
    """RenderHost io: {name: flat [r,g,b, ...] float list} → shared 1-D
    GLTextures (_LUT_TEXTURES). Runs on the render thread inside the host's
    settings window, so GL is legal. A list edit bubbles → host dirty → this
    re-runs and re-uploads (the texture deps key on a content hash). Draws a
    preview swatch strip per LUT."""
    luts = input_value if isinstance(input_value, dict) else {}
    draw_list = imgui.get_window_draw_list()
    bar_w, bar_h, segs = 160.0, 13.0, 48
    for name, lut in luts.items():
        if not isinstance(lut, (list, tuple)) or len(lut) < 6 or len(lut) % 3:
            imgui.text(f"{name}: not a flat [r,g,b,...] list")
            continue
        _LUT_TEXTURES[name] = gl_state.texture1d(
            f"lut_{name}", lut, version=hash(tuple(lut)))
        n = len(lut) // 3
        x, y = imgui.get_cursor_screen_pos()
        for s in range(segs):
            i = min(n - 1, int(s * (n - 1) / max(1, segs - 1))) * 3
            col = imgui.get_color_u32_rgba(lut[i], lut[i + 1], lut[i + 2], 1.0)
            draw_list.add_rect_filled(x + bar_w * s / segs, y,
                                      x + bar_w * (s + 1) / segs, y + bar_h, col)
        imgui.dummy(bar_w, bar_h)
        imgui.same_line()
        imgui.text(f"{name} ({n})")
    if view_func is None:
        return False, input_value
    return view_func(input_value=input_value, **kwargs)


def _silhouette_edges(corners):
    """The cube edges on the screen-space outline, by the classic mesh rule:
    an edge is on the silhouette iff exactly ONE of its two adjacent faces is
    front-facing. Facing comes from the projected quad's signed (shoelace)
    area — outward-wound faces flip to clockwise on screen (y grows down), so
    front-facing means a NEGATIVE sum. Edge-on faces (|area| ≈ 0 — every side
    face in an exact top view) count as back-facing, so the camera-facing
    square contributes all four sides.

    The previous convex-hull walk degenerated in the axis-aligned views: the
    depth-axis corner pairs project onto the same point (or onto collinear
    runs that interleave the front and back squares), consecutive hull
    vertices then differ on two axes, the cube-adjacency test fails, and
    outline sides vanish — the missing-lines bug."""

    def face_visible(k, s):
        # Corner quad of face (axis k, sign s), wound CCW seen from outside:
        # i + j = +k, and the s<0 loop reverses.
        i, j = (k + 1) % 3, (k + 2) % 3
        quad = ((-1, -1), (1, -1), (1, 1), (-1, 1)) if s > 0 else \
            ((-1, -1), (-1, 1), (1, 1), (1, -1))
        loop = []
        for vi, vj in quad:
            c = [0, 0, 0]
            c[k], c[i], c[j] = s, vi, vj
            p = corners[tuple(c)]
            if p is None:
                return False
            loop.append(p)
        area2 = sum(loop[m][0] * loop[(m + 1) % 4][1]
                    - loop[(m + 1) % 4][0] * loop[m][1] for m in range(4))
        return area2 < -1.0

    vis = {(k, s): face_visible(k, s) for k in range(3) for s in (-1, 1)}
    edges = set()
    all_valid = set()
    for k in range(3):
        i, j = (k + 1) % 3, (k + 2) % 3
        for si in (-1, 1):
            for sj in (-1, 1):
                a, b = [0, 0, 0], [0, 0, 0]
                a[k], b[k] = -1, 1
                a[i] = b[i] = si
                a[j] = b[j] = sj
                a, b = tuple(a), tuple(b)
                if corners[a] is None or corners[b] is None:
                    continue
                all_valid.add(frozenset((a, b)))
                if vis[(i, si)] != vis[(j, sj)]:  # the edge's adjacent faces
                    edges.add(frozenset((a, b)))
    # Camera INSIDE the box: every face is back-facing, so the original
    # silhouette (exactly one front-facing face per edge) is empty and the
    # outline + labels would vanish. Don't hide them - fall back to every edge
    # with both corners in front of the camera, so the box stays outlined and
    # labeled from the inside.
    return edges or all_valid


def project_corners(tilt, spin, zoom, aspect, width, height, scale=(1.0, 1.0, 1.0),
                    pan=(0.0, 0.0, 0.0), ortho=False):
    """Screen positions of the volume BOX's 8 corners (extents = `scale`, the
    voxel-count-proportional volume_scale) — the Python mirror of the shader's
    orbit camera, so labels land exactly on the rendered edges. Keys stay the
    ±1 sign tuples; positions carry the scaling.
    Returns {corner_signs: (sx, sy) or None (behind camera)}."""
    ct = math.cos(tilt)
    fwd = -np.array([math.cos(spin) * ct, math.sin(spin) * ct, math.sin(tilt)])
    right = np.array([-math.sin(spin), math.cos(spin), 0.0])
    up = np.cross(right, fwd)
    eye = np.asarray(pan, np.float64) - fwd * zoom
    out = {}
    for corner in ((x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)):
        world = np.asarray(corner, np.float64) * np.asarray(scale, np.float64)
        d = world - eye
        zc = d @ fwd
        if zc < 0.05:
            out[corner] = None
            continue
        # Inverse of the shader's ray gen (rd ∝ fwd*1.7 + right*ndc.x + up*ndc.y,
        # ndc.x pre-scaled for aspect): ndc = 1.7 * cam_xy / cam_z, x /= aspect.
        # Ortho divides by the fixed frame half-size (zoom/1.7) instead of the
        # corner's own depth.
        denom = zoom / 1.7 if ortho else zc / 1.7
        ndx = ((d @ right) / denom) / aspect
        ndy = (d @ up) / denom
        out[corner] = ((ndx * 0.5 + 0.5) * width, (1.0 - (ndy * 0.5 + 0.5)) * height)
    return out


# Outline lines draw shortened by this many screen px at each end (the
# original fixed_shorten look); tick placement compresses into the remaining
# span so the 0 and max labels align with the visible line ends.
_EDGE_SHORTEN_PX = 14.0


def _draw_axis_lines(draw_list, img_pos, corners, silhouette):
    """The cube's silhouette outline as thin imgui lines, shortened near the
    corners (the original fixed_shorten look). Labels are NOT drawn here any
    more — they're textured billboards in the voxel FBO (_billboard_specs +
    _render_label_billboards), so they live in the 3-D scene."""
    line_col = imgui.get_color_u32_rgba(0.9, 0.9, 1.0, 0.5)
    for edge in silhouette:
        a, b = tuple(edge)
        pa, pb = corners[a], corners[b]
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        length = math.hypot(dx, dy)
        if length < 0.5:
            continue   # zero-area edge: nothing to draw, skip the div
        # Short edges shorten proportionally instead of vanishing - the
        # outline only ever skips sub-2px degenerates.
        shorten = min(_EDGE_SHORTEN_PX, length * 0.25)
        ux, uy = dx / length, dy / length
        draw_list.add_line(img_pos[0] + pa[0] + ux * shorten, img_pos[1] + pa[1] + uy * shorten,
                           img_pos[0] + pb[0] - ux * shorten, img_pos[1] + pb[1] - uy * shorten,
                           line_col, 1.0)


def _tick_values(size, px_per_idx, num_px, spacing=1.6):
    """Integer tick positions for one edge: EVERY integer when the labels
    fit, else the smallest 1-2-5·10ᵏ step whose rotated labels keep clear of
    each other (footprint ≈ the widest label's text width along the edge, in
    projected PIXELS — so zooming in fits more ticks). `spacing` is the
    minimum gap between tick centers in widest-label widths. The end value
    always shows; the last multiple yields when it would crowd it."""
    widest = max(1, len(str(size))) * 0.62 * num_px  # ~avg glyph aspect
    min_px = widest * spacing
    step, k = None, 1
    while step is None and k <= 10 ** 9:
        for s in (1, 2, 5):
            if s * k * px_per_idx >= min_px:
                step = s * k
                break
        else:
            k *= 10
    if step is None or step > size:
        return [0, size] if size > 0 else [0]
    ticks = list(range(0, size + 1, step))
    if ticks[-1] != size:
        if size - ticks[-1] < 0.6 * step and len(ticks) > 1:
            ticks.pop()
        ticks.append(size)
    return ticks


def _billboard_specs(silhouette, corners, axis_display, volume_scale,
                     name_size=24.0, name_padding=34.0, name_opacity=1.0,
                     num_size=16.0, num_padding=11.0, num_opacity=1.0,
                     num_spacing=1.6, num_angle=0.0):
    """[(text, anchor3, u_dir3, v_dir3, out_dir3, px_h, off_px, alpha)] for
    every drawn silhouette edge — the dim name beside the midpoint plus
    integer ticks (_tick_values) at their TRUE positions along the edge.
    Anchors are volume-box WORLD points ON the edge. All metrics are screen
    PIXELS, held at any zoom (the shader depth-converts at each anchor):
    `*_size` is the label height (0 hides that label type), `*_padding` the
    GAP between the line and the label's near edge (independent of size),
    `*_opacity` the tint alpha. u runs along the edge and v outward from
    the box ("angled perpendicular to the line"); both are flipped for
    readability — the up-axis flips when the quad shows its back (un-mirrors
    without reversing the reading direction), then a 180° spin makes text
    read left-to-right, or bottom-to-top on near-vertical edges. The offset
    always rides the UNFLIPPED outward direction, so labels never land
    inside the box. Nothing hides by projected size any more — the
    face-visibility silhouette already culls truly invisible edges, and
    _tick_values degrades to just 0/max on short edges."""
    name_off = name_padding + name_size * 0.5  # anchor -> label CENTER
    num_off = num_padding + num_size * 0.5
    # tick label slant (optional, not the label plane - matplotlib-style)
    ca, sa = math.cos(math.radians(num_angle)), math.sin(math.radians(num_angle))
    vis = [p for p in corners.values() if p is not None]
    if not vis:
        return []
    scx = sum(p[0] for p in vis) / len(vis)  # silhouette's screen centroid
    scy = sum(p[1] for p in vis) / len(vis)

    specs = []
    for edge in silhouette:
        a, b = tuple(edge)
        k = next(i for i in range(3) if a[i] != b[i])  # the axis it runs along
        if a[k] > b[k]:
            a, b = b, a  # a is the texcoord-0 end
        pa, pb = corners[a], corners[b]
        px_len = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        if px_len < 0.5:
            continue   # zero-area edge: direction math requires a length
        name, size = axis_display[k]
        a3 = tuple(a[i] * volume_scale[i] for i in range(3))
        b3 = tuple(b[i] * volume_scale[i] for i in range(3))
        length = math.sqrt(sum((b3[i] - a3[i]) ** 2 for i in range(3))) or 1.0
        w = tuple((b3[i] - a3[i]) / length for i in range(3))  # a → b, for placement
        mid = tuple((a3[i] + b3[i]) * 0.5 for i in range(3))
        m_len = math.sqrt(sum(c * c for c in mid)) or 1.0
        out = tuple(c / m_len for c in mid)  # normalized, ⊥ the edge (mid-w = 0)

        # TRUE screen directions, not the camera-basis approximation (which
        # skews under perspective for off-center edges and mirrors oblique
        # labels): the baseline from the edge's mean projected points, the
        # outward axis as its perpendicular pointing away from the
        # silhouette's screen centroid (the world `out` projects into that
        # half-space for any silhouette edge, so the signs match).
        u_s = ((pb[0] - pa[0]) / px_len, (pb[1] - pa[1]) / px_len)
        mxs, mys = (pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5
        ox, oy = mxs - scx, mys - scy
        along = ox * u_s[0] + oy * u_s[1]
        nx, ny = ox - along * u_s[0], oy - along * u_s[1]
        nl = math.hypot(nx, ny) or 1.0
        v_s = (nx / nl, ny / nl)

        u, v = w, out
        # Chirality - readable text needs cross(u_s, v_s) < 0 on a y-down
        # screen. When the quad shows its back, flip the UP axis - that
        # un-mirrors top/bottom without reversing the reading direction.
        if u_s[0] * v_s[1] - u_s[1] * v_s[0] > 0:
            v = tuple(-c for c in v)
        # 180° flipping (chirality-preserving): read left-to-right, or
        # bottom-to-top when the baseline is near-vertical on screen.
        if u_s[0] < -0.2 * abs(u_s[1]) or (
                abs(u_s[0]) <= 0.2 * abs(u_s[1]) and u_s[1] > 0):
            u = tuple(-c for c in u)
            v = tuple(-c for c in v)

        if name_size > 0:
            specs.append((name, mid, u, v, out, name_size, name_off, name_opacity))
        if num_size > 0 and size > 0:
            # Ticks convert into the VISIBLE line span (edges draw shortened
            # adaptively per end), so 0 sits at the edge's start and the max
            # value at its end instead of out at the corners. _tick_values
            # self-limits on short edges (degrades to just 0 and max).
            inset_px = min(_EDGE_SHORTEN_PX, px_len * 0.25)
            inset = inset_px * length / px_len
            span = max(0.0, length - 2.0 * inset)
            if num_angle:
                ut = tuple(ca * u[i] + sa * v[i] for i in range(3))
                vt = tuple(ca * v[i] - sa * u[i] for i in range(3))
            else:
                ut, vt = u, v
            for idx in _tick_values(int(size), (px_len - 2 * inset_px) / size,
                                    num_size, num_spacing):
                p = tuple(a3[i] + w[i] * (inset + span * idx / size) for i in range(3))
                specs.append((str(idx), p, ut, vt, out, num_size, num_off, num_opacity))
    return specs


def _render_label_billboards(gl_state, specs, cam, height):
    """Draw every label spec into the CURRENT FBO with the volume's camera
    (`cam` = the camera uniform kwargs) in ONE instanced draw: assemble the
    per-instance buffer (anchor/axes/metrics/uv-rect per label), upload,
    glDrawArraysInstanced. Pixel sizes/offsets convert to NDC units against
    the viewport height (`height`); the shader depth-scales them at each
    anchor for screen-constant labels."""
    if not specs:
        return
    texts = tuple(sorted({s[0] for s in specs}))
    atlas, rects = _label_atlas(gl_state, texts)
    prog, loc = _label_program(gl_state)
    vao, vbo = _label_vao(gl_state)

    ndc_per_px = 2.0 / max(1.0, float(height))
    data = np.empty((len(specs), _LABEL_FLOATS), np.float32)
    for i, (text, anchor, u, v, out, px_h, off_px, alpha) in enumerate(specs):
        u0, v0, u1, v1, tw, th = rects[text]
        half_h = (px_h * 0.5) * ndc_per_px
        row = data[i]
        row[0:3] = anchor
        row[3:6] = u
        row[6:9] = v
        row[9:12] = out
        row[12] = half_h * (tw / max(1, th))
        row[13] = half_h
        row[14] = off_px * ndc_per_px
        row[15] = alpha
        row[16:20] = (u0, v0, u1, v1)

    blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
    prev_prog = gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM)
    gl.glEnable(gl.GL_BLEND)
    gl.glBlendEquation(gl.GL_FUNC_ADD)
    gl.glBlendFuncSeparate(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA,
                           gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
    gl.glUseProgram(prog)
    gl.glUniform1f(loc["tilt"], cam["tilt"])
    gl.glUniform1f(loc["spin"], cam["spin"])
    gl.glUniform1f(loc["zoom"], cam["zoom"])
    gl.glUniform1f(loc["aspect"], cam["aspect"])
    gl.glUniform1i(loc["ortho"], 1 if cam["ortho"] else 0)
    gl.glUniform3f(loc["pan"], cam["pan_x"], cam["pan_y"], cam["pan_z"])
    gl.glUniform1i(loc["label"], 0)
    gl.glActiveTexture(gl.GL_TEXTURE0)
    gl.glBindTexture(gl.GL_TEXTURE_2D, atlas.texture_id)
    gl.glBindVertexArray(vao)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_STREAM_DRAW)
    gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 6, len(specs))
    gl.glBindVertexArray(0)
    gl.glUseProgram(prev_prog)
    if not blend_was:
        gl.glDisable(gl.GL_BLEND)


def _draw_axis_controls(vox_ds, shape, mapping, dim_names):
    """The remap UI over the renderer's draw_state params: a radio row per
    display axis (conflict swaps — the displaced axis takes the old dim),
    index scrubbers for unmapped dims, mean toggles, sort + normalize,
    neural flow. Every edit writes vox_ds.<param>; auto-state persists the
    diverged values, no object of its own."""
    changed = False
    n = len(shape)
    zd, yd, xd = mapping
    current = {"x_dim": xd, "y_dim": yd, "z_dim": zd}
    names = [dim_names[d] if d < len(dim_names) else f"dim{d}" for d in range(n)]
    for label, attr in (("x", "x_dim"), ("y", "y_dim"), ("z", "z_dim")):
        imgui.text(f"{label}:")
        for d in range(n):
            imgui.same_line()
            if imgui.radio_button(f"{names[d]}##axis_{label}_{d}",
                                  current[attr] == d):
                prev = current[attr]
                for other, od in current.items():
                    if other != attr and od == d:
                        current[other] = prev
                        setattr(vox_ds, other, prev)
                current[attr] = d
                setattr(vox_ds, attr, d)
                changed = True
    shown = set(current.values())
    slices = list(getattr(vox_ds, "slices", ()) or ())
    slices += [0] * (n - len(slices))
    mean_dims = {int(m) for m in (getattr(vox_ds, "mean_dims", ()) or ())}
    for d in range(n):
        if d in shown or shape[d] <= 1:
            continue
        # mean toggle: average over this dim instead of scrubbing one slice
        mean_changed, is_mean = imgui.checkbox(f"mean##mean_{d}", d in mean_dims)
        if mean_changed:
            (mean_dims.add if is_mean else mean_dims.discard)(d)
            vox_ds.mean_dims = tuple(sorted(mean_dims))
            changed = True
        imgui.same_line()
        if is_mean:
            imgui.text(f"{names[d]} (averaged)")
            continue
        imgui.push_item_width(160)
        scrub_changed, value = RenderFuncs.draw_int(
            min(slices[d], shape[d] - 1), name=f"{names[d]}##scrub_{d}",
            min_value=0, max_value=shape[d] - 1)
        imgui.set_item_allow_overlap()
        imgui.pop_item_width()
        if scrub_changed:
            slices[d] = int(value)
            vox_ds.slices = tuple(slices)
            changed = True

    # ── normalize + sort: data transforms, dim-pinned like the others ─────
    sort_dim = int(getattr(vox_ds, "sort_dim", -1))
    norm_changed, norm = imgui.checkbox(
        "normalize##norm", bool(getattr(vox_ds, "normalize", False)))
    if norm_changed:
        vox_ds.normalize = norm
        changed = True
    imgui.same_line()
    imgui.text("sort:")
    imgui.same_line()
    if imgui.radio_button("off##sort_off", sort_dim == -1):
        vox_ds.sort_dim = -1
        changed = True
    for d in range(n):
        imgui.same_line()
        if imgui.radio_button(f"{names[d]}##sort_{d}", sort_dim == d):
            vox_ds.sort_dim = d
            changed = True

    # ── neural flow: chop one tensor dim into chunks laid along another
    # (dim-pinned: remapping x/y/z never changes which dim gets chopped;
    # only currently-displayed dims are offered, since the flow operates on
    # the sliced display volume) ─────────────────────────────────────────
    nf_on = bool(getattr(vox_ds, "nf_on", False))
    nf_changed, nf_now = imgui.checkbox("neural flow##nf", nf_on)
    if nf_changed:
        vox_ds.nf_on = nf_now
        changed = True
    if nf_now:
        chop_d = _resolve_dim(dim_names, getattr(vox_ds, "nf_chop", None), n)
        along_d = _resolve_dim(dim_names, getattr(vox_ds, "nf_along", None), n)
        cur = {"nf_chop": xd if chop_d is None else chop_d,
               "nf_along": zd if along_d is None else along_d}
        for label, attr in (("chop", "nf_chop"), ("along", "nf_along")):
            imgui.same_line()
            imgui.text(f"{label}:")
            for d in sorted(shown):
                imgui.same_line()
                if imgui.radio_button(f"{names[d]}##{attr}_{d}", cur[attr] == d):
                    setattr(vox_ds, attr, d)
                    changed = True
        imgui.same_line()
        imgui.push_item_width(110)
        chunk_changed, chunk = RenderFuncs.draw_int(
            int(getattr(vox_ds, "nf_chunk", 128)), name="chunk##nf", step=0)
        imgui.set_item_allow_overlap()
        imgui.pop_item_width()
        if chunk_changed and chunk > 0:
            vox_ds.nf_chunk = int(chunk)
            changed = True

    return changed


# Panel slider rows: (param, min, max) - UI constants, not state.
_PANEL_FLOATS = (("tilt", -3.1416, 3.1416), ("spin", -6.3, 6.3),
                 ("cam_zoom", 0.0, 137.6), ("pan_x", -4.0, 4.0),
                 ("pan_y", -4.0, 4.0), ("pan_z", -4.0, 4.0),
                 ("cam_brightness", 0.0, 4.0), ("cam_contrast", 0.1, 4.0),
                 ("density", 0.0, 10.0), ("threshold", 0.0, 1.0))
_PANEL_BOOLS = ("ortho", "centered", "nearest")


@render_func(show_bg=False, use_cache=True, auto_resize=False, min_height=600)
def draw_voxel_controls(input_value=None, vox_ds=None, mapping=None,
                        draw_state=None, hovered=None, **kwargs):
    """Every control that drives a voxel view, in one satellite panel —
    sliders/radios over the OWNING VIEW's draw_state params (auto-state:
    edits write vox_ds.<param>, diverged values persist, untouched ones
    keep flowing from the draw_voxels signature). input_value is the
    uploaded buffer (it carries the tensor metadata); `mapping` is the
    (z, y, x) dims the renderer resolved this frame.

    CACHED, live only under the cursor: the `hovered` event param keeps the
    cache bypassed while the cursor is over the panel, and draw_voxels
    invalidates it ONCE when a gesture ends, so it never refreshes per drag
    frame."""
    tex = input_value
    if vox_ds is None:
        imgui.text("no owning view")
        return False, input_value
    changed = False
    for nm, lo, hi in _PANEL_FLOATS:
        c, v = RenderFuncs.draw_float(float(getattr(vox_ds, nm, 0.0)), name=nm,
                                      min_value=lo, max_value=hi)
        if c:
            setattr(vox_ds, nm, float(v))
            changed = True
    for i, nm in enumerate(_PANEL_BOOLS):
        if i:
            imgui.same_line()
        c, v = imgui.checkbox(f"{nm}##panel", bool(getattr(vox_ds, nm, False)))
        if c:
            setattr(vox_ds, nm, v)
            changed = True

    # ── LUT picker: one radio per list the LUT host knows about ─────────
    src = lut_host.input_value if isinstance(getattr(lut_host, "input_value", None), dict) else LUTS
    imgui.text("lut:")
    for i, lut_name in enumerate(src):
        if i % 4:
            imgui.same_line()
        if imgui.radio_button(f"{lut_name}##lut", getattr(vox_ds, "lut", "jet") == lut_name):
            vox_ds.lut = lut_name
            changed = True

    # ── data mapping (only when tensor metadata rides the buffer) ───────
    shape = getattr(tex, "source_shape", None)
    if shape and mapping:
        dim_names = tuple(getattr(vox_ds, "dim_names", ()) or ())
        if _draw_axis_controls(vox_ds, shape, mapping, dim_names):
            changed = True
        names_changed, new_names = draw_any(list(dim_names), name="dim names",
                                            initial={"expanded": True},
                                            shadow=False)
        if names_changed and isinstance(new_names, list):
            # Any length list: extra names wait for bigger tensors. Names
            # must stay short LABELS - a DnD/paste can land an arbitrary
            # object whose str() is a -MB code repr.
            vox_ds.dim_names = tuple(_clean_dim_name(x, i)
                                     for i, x in enumerate(new_names))
            changed = True

    # ── metadata: pipeline + lifecycle visibility (lives here, not drawn
    # over the volume) ───────────────────────────────────────────────────
    injected = sum(1 for line in voxel_pass.last_generated.get("fragment", "").splitlines()
                   if line.startswith("uniform "))
    stats = GLState.stats()
    imgui.text_colored(
        f"{tex!r}\n"
        f"{injected} uniforms · gl: {stats['states']} states / "
        f"{stats['resources']} res / {stats['queued_deletes']} queued",
        0.21, 0.33, 0.62, 1.0)
    return changed, input_value


@render_func(is_default_for=("GLTexture", "Tensor"), show_bg=True, selectable=True, min_height=50, disable_scroll=True, use_cache=True)
def draw_voxels(input_value=None, gl_state: GLState = None, selectable=False,
                draw_state=None,
                # ── camera + shading: cam_* names dodge the legacy DrawState
                # zoom/brightness/contrast fields (name-colliding params are
                # excluded from auto-state). Gestures/panel write
                # draw_state.<name>; diverged values persist. ──
                tilt=0.5, spin=0.724, cam_zoom=3.4,
                pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False,
                cam_brightness=1.0, cam_contrast=1.0,
                # density = the old densityScale (haze gain over the opacity
                # gate); threshold = the old opacityThreshold (higher → lower
                # gate → more opaque)
                density=3.7, threshold=0.301, centered=False,
                nearest=True, lut="jet", step_size=0.0005, max_steps=4096,
                # ── data mapping: dims by INDEX or NAME, None derives a
                # default (last three → z/y/x) ──
                dim_names=("layer", "batch", "token", "feature"),
                x_dim=None, y_dim=None, z_dim=None, slices=(),
                mean_dims=(), sort_dim=-1, normalize=False,
                nf_on=False, nf_chop=None, nf_along=None, nf_chunk=128,
                # ── volume furniture (screen px) ──
                name_size=17.0, name_padding=30.1, name_opacity=1.1,
                num_size=17.1, num_padding=5.5, num_opacity=0.8,
                num_spacing=1.0, num_angle=0.0,
                middle_mouse_drag=None, double_right_mouse_drag=None,
                scroll_y_changed=None, left_mouse_double_clicked=None,
                kp_7_pressed=None, kp_1_pressed=None, kp_3_pressed=None,
                kp_5_pressed=None, slash_pressed=None, kp_divide_pressed=None,
                kp_decimal_pressed=None, **kwargs):
    """The voxel renderer — owner of every render and mapping decision.
    Input is a tensor/ndarray (sliced + uploaded HERE, re-keyed by gl_state
    deps on source identity/_version/mapping) or an already-uploaded
    GLTexture (rendered as-is). Tensor METADATA — full shape, dim count —
    rides the uploaded buffer; EVERYTHING else is a parameter on this
    signature (auto draw_state params: gestures and the controls panel
    write draw_state.<name>, only diverged values persist/serialize)."""
    src = input_value

    # A 1-D texture is a LUT, not a volume - don't try to raymarch it.
    if getattr(src, "target", None) == int(gl.GL_TEXTURE_1D):
        imgui.text(f"{src!r} — a LUT, not a volume")
        return False, None

    dim_names = tuple(_clean_dim_name(x, i) for i, x in enumerate(dim_names or ()))
    slices = tuple(int(v) for v in (slices or ()))
    mean_dims = tuple(int(v) for v in (mean_dims or ()))

    # ── source → display volume → GPU, parameter-driven and stateless:
    # slice_volume is a pure function of the params, the upload re-runs
    # exactly when its deps change, and tensor metadata rides the buffer.
    if isinstance(src, GLTexture):
        tex, mapping = src, None
        source_shape = tuple(getattr(src, "source_shape", src.shape))
    else:
        import torch
        t = src if isinstance(src, torch.Tensor) else torch.from_numpy(np.asarray(src))
        vol, mapping, source_shape = slice_volume(
            t, dim_names, x_dim, y_dim, z_dim, slices, mean_dims,
            sort_dim, normalize, nf_on, nf_chop, nf_along, nf_chunk)
        version = ((id(src), getattr(src, "_version", 0)), mapping, slices,
                   mean_dims, int(sort_dim), bool(normalize), bool(nf_on),
                   str(nf_chop), str(nf_along), int(nf_chunk))
        tex = None
        if vol.is_cuda:
            from src.lsd.gl_gui import cuda_interop
            tex = cuda_interop.tensor_to_texture(gl_state, "volume_cuda", vol,
                                                 version=version)
        if tex is None:
            tex = gl_state.texture3d("volume", vol.cpu().numpy(), version=version)
            gl_state.drop("volume_cuda")
        else:
            gl_state.drop("volume")
        tex.source_shape = source_shape       # tensor metadata on the buffer
        tex.source_ndim = len(source_shape)

    # Edge labels - the mapped dim's name (+ neural-flow decoration) and its
    # DISPLAYED size, recomputed per frame from the params.
    if mapping is not None:
        zd, yd, xd = mapping
        n = len(source_shape)
        chop_d = _resolve_dim(dim_names, nf_chop, n)
        along_d = _resolve_dim(dim_names, nf_along, n)
        chop_d = xd if chop_d is None else chop_d
        along_d = zd if along_d is None else along_d
        display = []
        for axis, dim in (("x", xd), ("y", yd), ("z", zd)):
            label = dim_names[dim] if dim < len(dim_names) else f"dim{dim}"
            if nf_on and dim == chop_d:
                label = f"{label} % {int(nf_chunk)}"   # chopped into chunks
            elif nf_on and dim == along_d:
                chop_name = (dim_names[chop_d]
                             if chop_d < len(dim_names) else f"dim{chop_d}")
                label = f"{label} · {chop_name}"        # along the blocks
            display.append((label, int(tex.shape[_AXIS_POS[axis]])))
        axis_display = tuple(display)
    else:
        d3, h3, w3 = (int(s) for s in tex.shape)
        axis_display = ((dim_names[2] if len(dim_names) > 2 else "x", w3),
                        (dim_names[1] if len(dim_names) > 1 else "y", h3),
                        (dim_names[0] if dim_names else "z", d3))

    # Size from the OWNING WINDOW, not this view's own draw() - a nested
    # view's height derives from what it rendered last frame (self-referential),
    # while the window's height is the user-dragged size. Reserve room for the
    # header + a line below the image.
    # The owning window IS this draw_state when draw_voxels is itself a window
    # (mode=WINDOW / closable); only fall back to the enclosing window for the
    # non-window child case. Using draw_state.parent_window for a closable voxel
    # grabbed an ANCESTOR (e.g. live_view_forward) that doesn't move with the
    # voxel window, so the controls panel - parented to `win` below - followed
    # the ancestor and stayed put while the voxel window was dragged.
    win = draw_state if draw_state.closable else (draw_state.parent_window or draw_state)
    width = max(64, int(draw_state.content_width or win.content_width or 0))
    height = max(100, draw_state.height - 30)

    # ── gestures → draw_state params (auto-state: the caller diverges the
    # param so it persists; events are hover-routed wrapper kwargs) ──────
    if middle_mouse_drag is not None:
        if middle_mouse_drag.shift:
            # Blender-style shift-d = pan: move the orbit target so the
            # content tracks the cursor 1:1 at the target plane (world units
            # per pixel at current cam_zoom, focal 1.7 - matches the ray gen).
            wpp = 2.0 * cam_zoom / (1.7 * height)
            st, ct = math.sin(tilt), math.cos(tilt)
            cs, ss = math.cos(spin), math.sin(spin)
            dx, dy = middle_mouse_drag.dx, middle_mouse_drag.dy
            pan_x += (ss * dx - cs * st * dy) * wpp
            pan_y += (-cs * dx - ss * st * dy) * wpp
            pan_z += ct * dy * wpp
            draw_state.pan_x, draw_state.pan_y, draw_state.pan_z = pan_x, pan_y, pan_z
        elif middle_mouse_drag.ctrl:
            # the old viewer's ctrl-drag: vertical = dolly zoom, horizontal
            # still orbits.
            cam_zoom = min(137.6, max(0.0, cam_zoom * math.exp(0.005 * middle_mouse_drag.dy)))
            spin -= middle_mouse_drag.dx * 0.008
            draw_state.cam_zoom, draw_state.spin = cam_zoom, spin
        else:
            spin -= middle_mouse_drag.dx * 0.008
            tilt = min(math.pi, max(-math.pi, tilt + middle_mouse_drag.dy * 0.008))
            draw_state.spin, draw_state.tilt = spin, tilt
    if double_right_mouse_drag is not None:
        # the old viewer's shading drag: now on a DOUBLE right-drag (the 2nd
        # press of a double right-click, held and dragged): horizontal =
        # brightness, vertical = contrast (up to increase). The plain right-
        # click stays reserved for the context menu.
        cam_brightness = min(4.0, max(0.0, cam_brightness + double_right_mouse_drag.dx * 0.01))
        cam_contrast = min(4.0, max(0.1, cam_contrast - double_right_mouse_drag.dy * 0.008))
        draw_state.cam_brightness, draw_state.cam_contrast = cam_brightness, cam_contrast
    if scroll_y_changed is not None:
        cam_zoom = min(135.5, max(0.0, cam_zoom * math.exp(-0.23 * scroll_y_changed.value)))
        draw_state.cam_zoom = cam_zoom

    # ── Blender-style numpad views (hover-routed key events): 7/1/3 = top/
    # front/right, ctrl = the opposite side, 5 = ortho toggle, / (either
    # slash, or numpad . like the old viewer) = recenter the pan on the
    # origin. A focused text editor owns the keyboard, so keys are ignored
    # while one is active. ────────────────────────────────────────────────
    from src.lsd.gl_gui.melty import Melty
    if Melty.text_focused_ds is None:
        if kp_7_pressed is not None:
            spin, tilt = -HALF_PI, (-HALF_PI if kp_7_pressed.ctrl else HALF_PI)
            draw_state.spin, draw_state.tilt = spin, tilt
        if kp_1_pressed is not None:
            spin, tilt = (HALF_PI if kp_1_pressed.ctrl else -HALF_PI), 0.0
            draw_state.spin, draw_state.tilt = spin, tilt
        if kp_3_pressed is not None:
            spin, tilt = (math.pi if kp_3_pressed.ctrl else 0.0), 0.0
            draw_state.spin, draw_state.tilt = spin, tilt
        if kp_5_pressed is not None:
            ortho = not ortho
            draw_state.ortho = ortho
        if (slash_pressed is not None or kp_divide_pressed is not None
                or kp_decimal_pressed is not None):
            pan_x = pan_y = pan_z = 0.0
            draw_state.pan_x = draw_state.pan_y = draw_state.pan_z = 0.0

    # Filtering is sampler state on the texture, view-owned, applied per frame.
    filt = gl.GL_NEAREST if nearest else gl.GL_LINEAR
    gl.glBindTexture(tex.target, tex.texture_id)
    gl.glTexParameteri(tex.target, gl.GL_TEXTURE_MIN_FILTER, filt)
    gl.glTexParameteri(tex.target, gl.GL_TEXTURE_MAG_FILTER, filt)
    gl.glBindTexture(tex.target, 0)

    # Box extents proportional to voxel counts (longest axis = 1), so every
    # voxel renders as a CUBE and a (4, 32, 48) tensor reads as a flat slab -
    # tex.shape is (depth, height, width) = (z, y, x).
    t_depth, t_height, t_width = (max(1, int(s)) for s in tex.shape)
    longest = float(max(t_depth, t_height, t_width))
    # Floor each extent so extreme aspect ratios stay visible: a 1-voxel dim
    # on a 4096 box otherwise collapses to ~0.0005 world units - far below
    # the ray step. 0.02 reads as a thin plate (labels/silhouette use the
    # same floored scale, so the furniture stays consistent).
    volume_scale = (max(0.02, t_width / longest),
                    max(0.02, t_height / longest),
                    max(0.02, t_depth / longest))

    # ── LUT: prefer the shared 1-D texture the LUT host materialized; fall
    # back to a direct upload of the named lut until the host has time ────
    lut_tex = _LUT_TEXTURES.get(lut)
    if lut_tex is None:
        lut_list = LUTS.get(lut, LUTS["jet"])
        lut_tex = gl_state.texture1d("lut_fallback", lut_list,
                                     version=(lut, len(lut_list)))

    # ── axis furniture geometry: corners + silhouette are the Python mirror
    # of the OpenGL camera, computed BEFORE the GL pass - the label
    # billboards render IN the voxel FBO with the volume's own camera ────
    corners = silhouette = None
    if axis_display:
        corners = project_corners(tilt, spin, cam_zoom,
                                  width / height, width, height,
                                  scale=volume_scale,
                                  pan=(pan_x, pan_y, pan_z),
                                  ortho=ortho)
        silhouette = _silhouette_edges(corners)

    # ── GL pass: every resource tracked + lifecycle-managed by gl_state ──
    fb = gl_state.fbo("target", width, height)
    depth_was_on = gl.glIsEnabled(gl.GL_DEPTH_TEST)
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        # int() of a UI-dragged float never matches the uniform's inferred
        # GLSL type (the loop bound must stay an int).
        voxel_pass(gl_state, volume=tex, lut=lut_tex, aspect=width / height,
                   volume_scale=volume_scale, step_size=step_size,
                   max_steps=int(max_steps), density=density,
                   threshold=threshold, tilt=tilt, spin=spin, zoom=cam_zoom,
                   pan_x=pan_x, pan_y=pan_y, pan_z=pan_z, ortho=ortho,
                   brightness=cam_brightness, contrast=cam_contrast,
                   centered=centered)
        if silhouette and (name_size > 0 or num_size > 0):
            # Labels as in-scene textured quads. A bake/render hiccup should
            # not take down the view (or trigger the hotswap auto-revert) -
            # log it and keep rendering the volume.
            global _LABEL_WARNED
            try:
                specs = _billboard_specs(silhouette, corners, axis_display,
                                         volume_scale, name_size, name_padding,
                                         name_opacity, num_size, num_padding,
                                         num_opacity, num_spacing, num_angle)
                cam = {"tilt": tilt, "spin": spin, "zoom": cam_zoom,
                       "pan_x": pan_x, "pan_y": pan_y, "pan_z": pan_z,
                       "ortho": ortho, "aspect": width / height}
                _render_label_billboards(gl_state, specs, cam, height)
            except Exception as e:
                if not _LABEL_WARNED:
                    _LABEL_WARNED = True
                    import traceback
                    print(f"label billboards disabled: {e}")
                    traceback.print_exc()
    if depth_was_on:
        gl.glEnable(gl.GL_DEPTH_TEST)

    img_pos = imgui.get_cursor_screen_pos()
    imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))

    # ── the outline stays 2-D imgui (crisp 1px outline over the volume) ────
    if silhouette:
        _draw_axis_lines(imgui.get_window_draw_list(), img_pos, corners, silhouette)

    # ── ALL controls live in a satellite panel opening to the RIGHT of
    # the window (params + LUTs + axis remap + scrubbers + flow + names +
    # metadata). POPOVER window_pos is relative to the CURSOR at the call,
    # so anchor at the window's right edge - the panel rides along if the
    # window is dragged. Double-click the volume to show/hide; `closed` is
    # only PASSED on init/toggle so the window's own X button works - the
    # framework owns the state between toggles and we mirror it back (a
    # forced closed= every call reopened the panel on the next pre-render,
    # which is why the X appeared dead). ─────────────────────────────────
    init = "params_panel" not in draw_state.misc
    toggled = False
    if left_mouse_double_clicked is not None:
        draw_state.misc["params_panel"] = not draw_state.misc.get("params_panel", False)
        toggled = True
        draw_state.invalidate()
        request_render()
    panel_open = bool(draw_state.misc.get("params_panel", False))
    # imgui.set_cursor_screen_pos((win.abs_left + (win.width or width) + 12, win.abs_top))
    panel_kwargs = {"closed": not panel_open} if (init or toggled) else {}

    if not middle_mouse_drag and not double_right_mouse_drag and scroll_y_changed is None:
        changed, _, panel_ds = draw_voxel_controls(tex, vox_ds=draw_state,
                                                   mapping=mapping, name="controls",
                                                       mode=Modes.WINDOW_PARAMS,
                                                   parent_window=win, auto_resize=True,
                                                   shadow=True, return_extras=True,
                                                   **panel_kwargs)
        if panel_ds is not None:
            draw_state.misc["params_panel"] = not panel_ds.closed
            # The panel is cached and must NOT invalidate per drag frame - it
            # rides its blit while a camera gesture writes the params, then
            # catches up ONCE at the gesture edge.
            if not panel_ds.closed:
                if (not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1) and not
                        imgui.is_mouse_down(0) and scroll_y_changed is None) and changed:
                    panel_ds.invalidate_up()

        # ── status: error surfacing only (metadata lives in the panel) ──────
        if voxel_pass.last_error:
            # imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
            imgui.text_colored(voxel_pass.last_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)

        if changed:
            draw_state.invalidate()
            request_render()
            return changed, input_value

    return False, None


def demo_4d():
    """(time=8, depth=24, height=32, width=40): a torus whose radius breathes
    across the time dim — scrub `dim0` to watch it."""
    key = "_DEMO_4D"
    cached = globals().get(key)
    if cached is None:
        c = lambda n: np.linspace(-1.0, 1.0, n, dtype=np.float32)
        z, y, x = np.meshgrid(c(24), c(32), c(40), indexing="ij")
        frames = []
        for ti in range(8):
            ring = np.sqrt(x * x + y * y) - (0.35 + 0.05 * ti)
            frames.append(np.exp(-(ring * ring + z * z) / 0.02))
        cached = globals()[key] = np.stack(frames).astype(np.float32)
    return cached


def demo_5d():
    """(layer=3, head=4, d=16, h=24, w=32): per-layer/head frequency pattern —
    two scrubbers."""
    key = "_DEMO_5D"
    cached = globals().get(key)
    if cached is None:
        c = lambda n: np.linspace(0.0, 1.0, n, dtype=np.float32)
        z, y, x = np.meshgrid(c(16), c(24), c(32), indexing="ij")
        vols = [[np.abs(np.sin((layer + 1) * 3 * x + head) * np.cos((head + 1) * 3 * y) *
                        np.sin((layer + head + 1) * 2 * z))
                 for head in range(4)] for layer in range(3)]
        cached = globals()[key] = np.asarray(vols, dtype=np.float32)
    return cached


def demo_flat():
    """(seq=96, feature=4096): the neuralflow showcase — a 2-D matrix whose
    feature dim has per-128-chunk structure. Raw it's a 1-deep slab; turn on
    neural flow (chop x, along z, chunk 128) and the 32 chunks become a
    browsable volume."""
    key = "_DEMO_FLAT"
    cached = globals().get(key)
    if cached is None:
        seq = np.linspace(0.0, 6.0, 96, dtype=np.float32)[:, None]
        feat = np.arange(4096, dtype=np.float32)[None, :]
        chunk_id = np.floor(feat / 128.0)
        cached = globals()[key] = np.abs(
            np.sin(seq + chunk_id * 0.7) * np.cos(feat * (0.05 + 0.01 * chunk_id))
        ).astype(np.float32)
    return cached


def _ensure_host(var_name, host_name, demo_input, io=None):
    """ONE host per (process, name), found three ways: the module global
    (survives hotswap re-exec — checked `is None`, NOT truthiness: a
    RenderHost is a dict and an EMPTY host is falsy), the registry by NAME
    (both module identities exec this body, and render_host_view resolves
    hosts BY NAME), else freshly created. Always rebinds the
    freshly-compiled io."""
    host = globals().get(var_name)
    if host is None:
        from src.lsd.gl_gui.melty import Melty as _Melty
        host = next((h for h in _Melty.render_hosts.values()
                     if getattr(h, "name", None) == host_name), None)
    if host is None:
        host = RenderHost(io_function=io, input_value=demo_input, name=host_name)
    host.io_function = io
    return host


voxel_host = _ensure_host("voxel_host", "Voxel Volume", None, io=voxel_io)
voxel_host_4d = _ensure_host("voxel_host_4d", "Voxel 4D", demo_4d(), io=voxel_io)
voxel_host_5d = _ensure_host("voxel_host_5d", "Voxel 5D", demo_5d(), io=voxel_io)
voxel_host_flow = _ensure_host("voxel_host_flow", "Voxel Flow", demo_flat(), io=voxel_io)

# LUT lists → GL 1-D textures. A surviving host keeps ITS dict across
# hotswap (user edits intact); LUTs newly added in code merge in by name.
lut_host = _ensure_host("lut_host", "LUTs", LUTS, io=lut_io)
if lut_host.input_value is not LUTS and isinstance(lut_host.input_value, dict):
    for _name in LUTS:
        lut_host.input_value.setdefault(_name, LUTS[_name])


def _draw_host_volume(input_value):
    # input_value is the host (a dict); the SOURCE tensor it resolved comes
    # one level down. draw_voxels owns creation + upload + render - call it
    # directly (render_funcs are called directly, chain philosophy).
    t = input_value.get("value") if isinstance(input_value, dict) else input_value
    if t is None:
        imgui.text("no volume yet — waiting on host")
        return
    draw_voxels(t, name="volume")


window(cls=voxel_host.get("value"), name="draw_voxel_playground", view_func=draw_voxels, tint=(0.00, 0.02, 0.12))
#
# @render_func(show_bg=True, use_cache=True)
# def draw_voxel_playground(input_value=None, **kwargs):
#     _draw_host_volume(input_value)


@window(input_value=voxel_host_4d, tint=(0.20, 0.36, 0.59))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_4d(input_value=None, **kwargs):
    draw_voxels(input_value.get("value"), name="volume_4d", mode=Modes.WINDOW)


@window(input_value=voxel_host_5d, tint=(0.02, 0.38, 0.11))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_5d(input_value=None, **kwargs):
    _draw_host_volume(input_value)


@window(input_value=voxel_host_flow, tint=(0.91, 0.39, 0.00))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_flow(input_value=None, **kwargs):
    _draw_host_volume(input_value)
