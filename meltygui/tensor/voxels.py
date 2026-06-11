"""Voxel renderer on the GLState + @shader_func stack, plugged into real data
through a RenderHost.

The pipeline, each piece narrow and swappable:

    tensor / ndarray / None ──voxel_io──► GLTexture ──draw_any──► draw_voxels
    (host input)              (upload on    (held in     (routed by type:
                              render thread) the host)    is_default_for)

- `voxel_host` is a RenderHost whose io_function makes the input "look like a
  GLTexture": slice to 3-D and upload via the io's own injected gl_state (io
  runs on the render thread, so GL is legal). CUDA tensors copy device-to-device
  through a registered PBO (cuda_interop.py — no CPU round trip); anything else
  takes the cpu path. Re-uploads when the source's identity/`_version` changes —
  a tensor mutated by training streams in.
- `draw_voxels` is the renderer: input is a GLTexture, full stop. Anything that
  can become a GLTexture gets volume-rendered via plain `draw_any(tex)`.
- Controls are ONE `draw_any(params)` — VoxelParams is an annotated
  DictConversion, so ranges live as field annotations (`draw_float(min_value=…)`)
  editable from the context menus, and every annotated field whose name appears
  in the GLSL is forwarded as a uniform by shader_func.
- The fragment shader declares NO uniforms; break it in the editor and the last
  good program keeps rendering with the remapped driver error underneath.
- LUTs are flat [r,g,b, r,g,b, ...] float lists (LUTS); `lut_host` is a
  RenderHost whose io turns them into shared 1-D textures (_LUT_TEXTURES),
  re-uploading when a list is edited. draw_voxels samples the one params.lut
  names — the old custom jet() GLSL is now just the baked "jet" entry.
- Axis labels are textured billboards IN the scene: text_texture.py bakes the
  strings via imgui's own font atlas (a private shared-atlas context + the
  screen pass's draw-list mechanics, no freetype), and label_pass draws each
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

import math

import imgui
import numpy as np
import OpenGL.GL as gl

from src.lsd.gl_gui.gl_state import GLState
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.shader_func import shader_func
from src.lsd.gl_gui.text_texture import bake_text
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.view.core_views.new_core_view import draw_float, draw_any

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

    // volume_scale: box extents per axis, voxel-count-proportional — so each
    // VOXEL is a cube and the tensor keeps its true shape.
    vec2 hit = rayBox(ro, rd, volume_scale);
    if (hit.x > hit.y || hit.y < 0.0) { FragColor = vec4(0.0); return; }

    float t = max(hit.x, 0.0);
    vec4 acc = vec4(0.0);
    for (int i = 0; i < 2048; i++) {
        if (t > hit.y || acc.a > 0.98) break;
        vec3 p = (ro + rd * t) / volume_scale * 0.5 + 0.5;   // box -> texcoord [0,1]
        float v = texture(volume, p).r;
        // The old viewer's value pipeline, verbatim: contrast about
        // mid-grey, then brightness, on the GREYSCALE value — the LUT lookup
        // and the opacity gate both consume the remapped value. `lut` is a
        // 1-D texture the LUT host baked from a flat [r,g,b,...] float list
        // (the old jet() is now just the "jet" entry).
        v = (v - 0.5) * contrast + 0.5;
        v *= brightness;
        v = clamp(v, 0.0, 1.0);
        float d = clamp((v - threshold) * density, 0.0, 1.0);
        if (d > 0.0) {
            float a = d * step_size * 60.0;
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
               aspect=1.0, brightness=1.0, contrast=1.0, density=8.0,
               threshold=0.12, step_size=0.004, volume=None, lut=None,
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

LABEL_VERT = """
#version 330 core
out vec2 uv;
void main() {
    // two-triangle quad from gl_VertexID: corners in {-1,+1}²
    int id = gl_VertexID;
    vec2 q = vec2((id == 1 || id == 2 || id == 4) ? 1.0 : -1.0,
                  (id == 2 || id == 4 || id == 5) ? 1.0 : -1.0);
    uv = q * 0.5 + 0.5;   // bake puts the text TOP at v=1
    float ct = cos(tilt);
    vec3 fwd = -vec3(cos(spin) * ct, sin(spin) * ct, sin(tilt));
    vec3 right = vec3(-sin(spin), cos(spin), 0.0);
    vec3 up = cross(right, fwd);
    vec3 eye = vec3(pan_x, pan_y, pan_z) - fwd * zoom;
    // Screen-constant sizing: half_w/half_h/offs arrive in NDC units. The
    // world length that projects to one NDC unit at the ANCHOR's depth is
    // depth/1.7 (zoom/1.7 in ortho), so the label keeps its pixel size at
    // any zoom while still anchoring to and foreshortening with the scene.
    float ws = (ortho ? zoom : max(0.05, dot(quad_anchor - eye, fwd))) / 1.7;
    vec3 world = quad_anchor + (quad_out * offs
               + quad_u * (half_w * q.x) + quad_v * (half_h * q.y)) * ws;
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
in vec2 uv;
out vec4 FragColor;
void main() {
    vec4 t = texture(label, uv);
    FragColor = vec4(label_tint.rgb * t.rgb, t.a * label_tint.a);
}
"""


@shader_func(fragment=LABEL_FRAG, vertex=LABEL_VERT)
def label_pass(gl_state: GLState = None, quad_anchor=(0.0, 0.0, 0.0),
               quad_u=(1.0, 0.0, 0.0), quad_v=(0.0, 0.0, 1.0),
               quad_out=(0.0, 0.0, 1.0), half_w=0.05, half_h=0.05, offs=0.0,
               label=None, label_tint=(1.0, 1.0, 1.0, 1.0),
               tilt=0.5, spin=0.8, zoom=3.4,
               pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False, aspect=1.0,
               **kwargs):
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)


def _label_texture(gl_state, text):
    """The baked texture for one label string, cached on the view's gl_state.
    Baked at a large font size (the billboard scales it down) so angled
    sampling stays crisp."""
    from src.lsd.gl_gui.fonts import Font
    from src.lsd.gl_gui.melty import Melty
    font = Melty.font_mgr.get(Font.JETBRAINS_MONO_30) if Melty.font_mgr else None

    def delete(t):
        gl.glDeleteTextures([t.texture_id])

    return gl_state.get(("label_tex", text), lambda: bake_text(text, font=font),
                        delete, deps=(text, id(font)))


class VoxelParams(DictConversion):
    """Camera + render params, auto-injected per view. The field annotations
    ARE the control UI (`draw_any(params)` renders them with these ranges,
    editable from the context menus), and every annotated field is forwarded
    to the shader as a uniform."""

    tilt: draw_float(min_value=-1.5708, max_value=1.5708) = 0.5
    spin: draw_float(min_value=-6.3, max_value=6.3) = 0.8
    zoom: draw_float(min_value=1.4, max_value=15.0) = 3.4
    pan_x: draw_float(min_value=-4.0, max_value=4.0) = 0.0
    pan_y: draw_float(min_value=-4.0, max_value=4.0) = 0.0
    pan_z: draw_float(min_value=-4.0, max_value=4.0) = 0.0
    ortho: bool = False
    brightness: draw_float(min_value=0.0, max_value=4.0) = 1.0
    contrast: draw_float(min_value=0.1, max_value=4.0) = 1.0
    density: draw_float(min_value=0.5, max_value=30.0) = 8.0
    threshold: draw_float(min_value=0.0, max_value=1.0) = 0.12
    step_size: draw_float(min_value=0.001, max_value=0.02) = 0.004
    # screen-pixel multiplier for the axis-label billboards; 0 disables them
    # (not a uniform; applied Python-side by _billboard_specs)
    label_scale: draw_float(min_value=0.0, max_value=4.0) = 1.0
    nearest = False   # texture filtering, applied per frame, not a uniform
    lut = "jet"       # LUT name (a LUTS key); the texture itself rides in separately


# The annotated fields are exactly the scalar uniform candidates.
_UNIFORM_FIELDS = tuple(VoxelParams.__annotations__)


# ── LUTs: a LUT is just a flat [r,g,b, r,g,b, ...] float list ───────────────
# lut_host (bottom of file) converts these into shared 1-D textures; draw_voxels
# samples the one params.lut names. Editing Ls through the host re-uploads.

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


class VoxelAxes(DictConversion):
    """How a high-dim tensor maps onto the 3 display axes — the port of the
    old TensorFrame x/y/z_dim machinery. `dim_names` label the tensor's dims
    (editable), x/y/z_dim pick which dim feeds each display axis, and every
    other dim is pinned to `slice_indices[dim]` (the scrubbers — "time").
    Injected into voxel_io (slicing is the data source's job) and rides the
    GLTexture to the renderer (`tex.axes`), which draws the radio rows and
    mutates this same instance."""

    def __init__(self):
        super().__init__()
        self.dim_names = []
        self.dim_sizes = []
        self.x_dim = -1
        self.y_dim = -1
        self.z_dim = -1
        self.slice_indices = []
        # Neural flow: post-slice, chop one DISPLAY axis into `nf_chunk`-wide
        # blocks laid group-major along another - the old viewer's trick for
        # making weird high dims (Feature 4096) viewable as a volume.
        self.nf_on = False
        self.nf_chop = "x"
        self.nf_along = "z"
        self.nf_chunk = 128

    def sync(self, shape):
        """Fit state to a tensor shape. Re-derive on ndim change (defaults:
        last three dims → z/y/x, like the old viewer); only clamp on a
        same-rank shape change so user names/mapping survive resizes."""
        n = len(shape)
        if len(self.dim_names) != n:
            self.dim_names = [f"dim{i}" for i in range(n)]
            self.z_dim, self.y_dim, self.x_dim = max(0, n - 3), max(0, n - 2), n - 1
            self.slice_indices = [0] * n
        self.dim_sizes = list(shape)
        for d in range(n):
            self.slice_indices[d] = min(self.slice_indices[d], shape[d] - 1)
        for attr in ("x_dim", "y_dim", "z_dim"):
            if getattr(self, attr) >= n:
                setattr(self, attr, n - 1)

    def assign(self, axis, dim):
        """Point a display axis at a tensor dim, swapping with whichever axis
        already used it (the old radio-row conflict rule)."""
        prev = getattr(self, axis)
        for other in ("x_dim", "y_dim", "z_dim"):
            if other != axis and getattr(self, other) == dim:
                setattr(self, other, prev)
        setattr(self, axis, dim)

    def signature(self):
        return (self.x_dim, self.y_dim, self.z_dim, tuple(self.slice_indices),
                self.nf_on, self.nf_chop, self.nf_along, self.nf_chunk)

    def scrub_dims(self):
        """Dims not mapped to a display axis — these get index scrubbers."""
        shown = {self.x_dim, self.y_dim, self.z_dim}
        return [d for d in range(len(self.dim_names)) if d not in shown]


def slice_by_axes(t, axes: VoxelAxes):
    """Extract the (depth, height, width) = (z_dim, y_dim, x_dim) sub-volume,
    pinning every other dim at its slice index. Stays on t's device."""
    t = t.detach()
    while t.dim() < 3:
        t = t.unsqueeze(0)
    axes.sync(tuple(t.shape))
    import torch
    if t.dtype not in (torch.float16, torch.float32):
        t = t.float()
    picked = (axes.z_dim, axes.y_dim, axes.x_dim)
    index = tuple(slice(None) if d in picked else axes.slice_indices[d]
                  for d in range(t.dim()))
    sub = t[index]                       # first 3 dims keep original order
    remaining = sorted(picked)
    return sub.permute(remaining.index(axes.z_dim),
                       remaining.index(axes.y_dim),
                       remaining.index(axes.x_dim)).contiguous()


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
def voxel_io(input_value=None, gl_state: GLState = None, view_func=None,
             axes: VoxelAxes = None, external_change=False, **kwargs):
    """RenderHost io: make the input look like a GLTexture. Runs on the render
    thread inside the host's settings window, so GL (and CUDA interop) is
    legal here. The injected VoxelAxes maps tensor dims → display axes before
    upload; CUDA tensors go device-to-device through a registered PBO — no
    CPU round trip; everything else takes the cpu upload. Only one of the two
    textures is kept (the other path's resource is dropped)."""
    import torch
    from src.lsd.gl_gui import cuda_interop

    # Only tensor-like inputs count as a source. Everything else - None, the
    # host dict, and notably the host's OWN HELD GLTexture (an unbound io
    # resolves input to its held value, echoing last frame's value back in)
    # - means "no source": serve the demo volume.
    source = input_value if isinstance(input_value, (torch.Tensor, np.ndarray)) else None
    demo = source is None
    if demo:
        t = torch.from_numpy(demo_volume())
    elif isinstance(source, np.ndarray):
        t = torch.from_numpy(source)   # shares data; version keys on the array
    else:
        t = source

    t3 = slice_by_axes(t, axes)
    if axes.nf_on:
        t3 = neural_flow_volume(t3, axes.nf_chop, axes.nf_along, axes.nf_chunk)
    base = "demo" if demo else (id(source), getattr(source, "_version", 0))
    version = (base, axes.signature())

    tex, path = None, "cpu"
    if t3.is_cuda:
        tex = cuda_interop.tensor_to_texture(gl_state, "volume_cuda", t3, version=version)
        path = "cuda-interop"
    if tex is None:
        tex = gl_state.texture3d("volume", t3.cpu().numpy(), version=version)
        path = "demo" if demo else "cpu"
        gl_state.drop("volume_cuda")
    else:
        gl_state.drop("volume")

    # Context rides the texture (GUI philosophy): the renderer draws the
    # mapping UI against this same axes instance and labels edges from
    # axis_display (flow-aware: a flowed axis shows its original extent).
    # _host (via the bound view_func) + _io_ds let the renderer WAKE this io
    # on remap: the io body only runs when the io ENVELOPE re-runs, so the
    # wake must hit the envelope draw tiles + their gl-obj cache keys - the
    # same recipe RenderHost.draw() uses for upstream updates. Invalidating
    # just the io's tile leaves the envelope blitting and the io frozen.
    tex.axes = axes
    host = getattr(view_func, "__self__", None)
    axes._host = host if isinstance(host, RenderHost) else None
    display = []
    for axis, dim in (("x", axes.x_dim), ("y", axes.y_dim), ("z", axes.z_dim)):
        label = axes.dim_names[dim] if dim < len(axes.dim_names) else axis
        if axes.nf_on and axis == axes.nf_chop:
            label = f"{label} % {axes.nf_chunk}"          # chopped into blocks
        elif axes.nf_on and axis == axes.nf_along:
            chop_dim = getattr(axes, axes.nf_chop + "_dim")
            chop_name = (axes.dim_names[chop_dim]
                         if chop_dim < len(axes.dim_names) else axes.nf_chop)
            label = f"{label} · {chop_name}"              # carries the blocks
        display.append((label, int(t3.shape[_AXIS_POS[axis]])))
    tex.axis_display = tuple(display)
    axes._io_ds = kwargs.get("draw_state")

    imgui.text(f"{type(source).__name__} → {tex!r} via {path}")
    if view_func is None:
        return False, tex
    return view_func(input_value=tex, external_change=external_change, **kwargs)


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
    return view_func(input_value=input_value, external_change=external_change, **kwargs)


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
                if vis[(i, si)] != vis[(j, sj)]:   # the edge separates two faces
                    edges.add(frozenset((a, b)))
    return edges


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


def _draw_axis_lines(draw_list, img_pos, corners, silhouette):
    """The cube's silhouette outline as thin imgui lines, shortened near the
    corners (the original fixed_shorten look). Labels are NOT drawn here any
    more — they're textured billboards in the voxel FBO (_billboard_specs +
    label_pass), so they live in the 3-D scene."""
    line_col = imgui.get_color_u32_rgba(0.9, 0.9, 1.0, 0.5)
    SHORTEN = 14.0
    for edge in silhouette:
        a, b = tuple(edge)
        pa, pb = corners[a], corners[b]
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        length = math.hypot(dx, dy)
        if length < 2 * SHORTEN + 4:
            continue
        ux, uy = dx / length, dy / length
        draw_list.add_line(img_pos[0] + pa[0] + ux * SHORTEN, img_pos[1] + pa[1] + uy * SHORTEN,
                           img_pos[0] + pb[0] - ux * SHORTEN, img_pos[1] + pb[1] - uy * SHORTEN,
                           line_col, 1.0)


# Screen-space label metrics in PIXELS - labels keep this size at any zoom
# (the vertex shader converts at the anchor's depth). draw_voxels'
# label_scale multiplies all of them. Names sit beyond the number band so a
# mid-edge tick can't collide with the dim name.
_NAME_PX, _NUM_PX = 24.0, 16.0        # billboard heights
_NAME_OFF_PX, _NUM_OFF_PX = 46.0, 19.0   # outward offset from the edge


def _tick_values(size, px_per_idx, num_px):
    """Integer tick positions for one edge: EVERY integer when the labels
    fit, else the smallest 1-2-5·10ᵏ step whose rotated labels keep clear of
    each other (footprint ≈ the widest label's text width along the edge, in
    projected PIXELS — so zooming in fits more ticks). The end value always
    shows; the last multiple yields when it would crowd it."""
    widest = max(1, len(str(size))) * 0.62 * num_px   # ~average glyph aspect
    min_px = widest * 1.6
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


def _billboard_specs(silhouette, corners, axis_display, volume_scale, params,
                     label_scale=1.0):
    """[(text, anchor3, u_dir3, v_dir3, out_dir3, px_h, off_px, alpha)] for
    every drawn silhouette edge — the dim name beside the midpoint plus
    integer ticks (_tick_values) at their TRUE positions along the edge.
    Anchors are volume-box WORLD points ON the edge; sizes and outward
    offsets are screen PIXELS (the shader depth-converts at each anchor, so
    labels hold their size at any zoom). u runs along the edge and v outward
    from the box ("angled perpendicular to the line"); both are flipped for
    readability — the up-axis flips when the quad shows its back (un-mirrors
    without reversing the reading direction), then a 180° spin makes text
    read left-to-right, or bottom-to-top on near-vertical edges. The offset
    always rides the UNFLIPPED outward direction, so labels never land
    inside the box. Projected-length gates match the outline: <32px no
    furniture, <70px no ticks."""
    name_px, num_px = _NAME_PX * label_scale, _NUM_PX * label_scale
    name_off, num_off = _NAME_OFF_PX * label_scale, _NUM_OFF_PX * label_scale
    vis = [p for p in corners.values() if p is not None]
    if not vis:
        return []
    scx = sum(p[0] for p in vis) / len(vis)   # silhouette's screen centroid
    scy = sum(p[1] for p in vis) / len(vis)

    specs = []
    for edge in silhouette:
        a, b = tuple(edge)
        k = next(i for i in range(3) if a[i] != b[i])   # the axis it runs along
        if a[k] > b[k]:
            a, b = b, a                                  # a = the texel-0 corner
        pa, pb = corners[a], corners[b]
        px_len = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        if px_len < 32.0:
            continue
        name, size = axis_display[k]
        a3 = tuple(a[i] * volume_scale[i] for i in range(3))
        b3 = tuple(b[i] * volume_scale[i] for i in range(3))
        length = math.sqrt(sum((b3[i] - a3[i]) ** 2 for i in range(3))) or 1.0
        w = tuple((b3[i] - a3[i]) / length for i in range(3))   # a → b, for placement
        mid = tuple((a3[i] + b3[i]) * 0.5 for i in range(3))
        m_len = math.sqrt(sum(c * c for c in mid)) or 1.0
        out = tuple(c / m_len for c in mid)   # outward, ⊥ the edge (mid-w = 0)

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

        specs.append((name, mid, u, v, out, name_px, name_off, 1.0))
        if px_len >= 70.0 and size > 0:
            for idx in _tick_values(int(size), px_len / size, num_px):
                p = tuple(a3[i] + w[i] * (length * idx / size) for i in range(3))
                specs.append((str(idx), p, u, v, out, num_px, num_off, 1.0))
    return specs


def _render_label_billboards(gl_state, specs, cam, height):
    """Draw each label spec as a textured quad into the CURRENT FBO with the
    volume's camera (`cam` = the camera uniform kwargs). Pixel sizes/offsets
    convert to NDC units against the viewport height (`height`); the shader
    depth-scales them at each anchor for screen-constant labels. Quad width
    comes from the baked texture's aspect."""
    if not specs:
        return
    ndc_per_px = 2.0 / max(1.0, float(height))
    blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
    gl.glEnable(gl.GL_BLEND)
    gl.glBlendEquation(gl.GL_FUNC_ADD)
    gl.glBlendFuncSeparate(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA,
                           gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
    for text, anchor, u, v, out, px_h, off_px, alpha in specs:
        tex = _label_texture(gl_state, text)
        th, tw = tex.shape
        half_h = (px_h * 0.5) * ndc_per_px
        half_w = half_h * (tw / max(1, th))
        label_pass(gl_state, label=tex, quad_anchor=anchor,
                   quad_u=u, quad_v=v, quad_out=out,
                   half_w=half_w, half_h=half_h, offs=off_px * ndc_per_px,
                   label_tint=(1.0, 1.0, 1.0, alpha), **cam)
    if not blend_was:
        gl.glDisable(gl.GL_BLEND)


def _wake_io(axes: VoxelAxes):
    """Make the owning host's io actually re-run next frame. The io body sits
    under the host envelope window (render_host_view, blit-cached): marking
    only the io's tile leaves the envelope replaying its blit and the io
    never gets CALLED. Mirror RenderHost.draw()'s upstream-change wake:
    invalidate envelope + wrapper draw_states AND their by-obj cache keys."""
    host = getattr(axes, "_host", None)
    targets = []
    if host is not None:
        # The axes change is a "recipe change" with an UNCHANGED input - the
        # host's materialize gate would keep the old cached texture. Arm the
        # upstream-change latch so the io's fresh output (a new GLTexture
        # object) gets materialized when it lands.
        from src.lsd.gl_gui.melty import Melty
        host._pending_external = True
        host._input_change_frame = Melty.frame_count
        targets = [host._draw_state, host._wrapper_draw_state]
    elif getattr(axes, "_io_ds", None) is not None:
        targets = [axes._io_ds]
    for ds in targets:
        if ds is None:
            continue
        ds.invalidate()
        if host is not None:
            ds.invalidate_by_obj(obj=host)
    request_render()


def _draw_axis_controls(axes: VoxelAxes):
    """The remap UI: a radio row per display axis (one option per named dim,
    conflict swaps), index scrubbers for unmapped dims, editable names."""
    changed = False
    for label, attr in (("x", "x_dim"), ("y", "y_dim"), ("z", "z_dim")):
        imgui.text(f"{label}:")
        for d, dim_name in enumerate(axes.dim_names):
            imgui.same_line()
            if imgui.radio_button(f"{dim_name}##axis_{label}_{d}",
                                  getattr(axes, attr) == d):
                axes.assign(attr, d)
                changed = True
    for d in axes.scrub_dims():
        size = axes.dim_sizes[d]
        if size <= 1:
            continue
        imgui.push_item_width(160)
        scrub_changed, value = RenderFuncs.draw_int(
            axes.slice_indices[d], name=f"{axes.dim_names[d]}##scrub_{d}", min_value=0, max_value=size - 1)
        imgui.set_item_allow_overlap()

        imgui.pop_item_width()
        if scrub_changed:
            axes.slice_indices[d] = value
            changed = True

    # ── neural flow: chop a display axis into chunks laid along another ──
    nf_changed, axes.nf_on = imgui.checkbox("neural flow##nf", axes.nf_on)
    changed = changed or nf_changed
    if axes.nf_on:
        for label, attr in (("chop", "nf_chop"), ("along", "nf_along")):
            imgui.same_line()
            imgui.text(f"{label}:")
            for axis in ("x", "y", "z"):
                imgui.same_line()
                if imgui.radio_button(f"{axis}##{attr}", getattr(axes, attr) == axis):
                    setattr(axes, attr, axis)
                    changed = True
        imgui.same_line()
        imgui.push_item_width(110)
        chunk_changed, chunk = RenderFuncs.draw_int(axes.nf_chunk, name="chunk##nf", step=0)
        imgui.set_item_allow_overlap()
        imgui.pop_item_width()
        if chunk_changed and chunk > 0:
            axes.nf_chunk = chunk
            changed = True

    return changed


@render_func(show_bg=True)
def draw_voxel_controls(input_value=None, params=None, draw_state=None, **kwargs):
    """Every control that drives a voxel view, in one satellite panel:
    the VoxelParams tree, the axis remap radios + scrubbers + neural flow,
    and the editable dim names. input_value is the GLTexture (it carries
    the shared .axes); params is the OWNING VIEW's instance, passed in so
    both windows edit the same object."""
    tex = input_value
    axes = getattr(tex, "axes", None)

    changed, _ = draw_any(params, name="params", initial={"expanded": True},
                          show_add_delete=False, shadow=False)

    # ── LUT picker: one radio per list the LUT host knows about ─────────
    src = lut_host.input_value if isinstance(getattr(lut_host, "input_value", None), dict) else LUTS
    imgui.text("lut:")
    for i, lut_name in enumerate(src):
        if i % 4:
            imgui.same_line()
        if imgui.radio_button(f"{lut_name}##lut", getattr(params, "lut", "jet") == lut_name):
            params.lut = lut_name
            changed = True

    if axes is not None and axes.dim_names:
        if _draw_axis_controls(axes):
            _wake_io(axes)
            changed = True
        names_changed, new_names = draw_any(axes.dim_names, name="dim names",
                                            initial={"expanded": False},
                                            show_add_delete=False, shadow=False)
        if names_changed and isinstance(new_names, list) and len(new_names) == len(axes.dim_names):
            axes.dim_names = [str(n) for n in new_names]
            _wake_io(axes)   # labels (axis_display) are built by the io
            changed = True
    return changed, input_value


@render_func(is_default_for="GLTexture", show_bg=True, use_cache=True)
def draw_voxels(input_value=None, gl_state: GLState = None, selectable=False,
                params: VoxelParams = None, draw_state=None,
                middle_mouse_drag=None, right_mouse_drag=None,
                scroll_y_changed=None, left_mouse_double_clicked=None,
                kp_7_pressed=None, kp_1_pressed=None, kp_3_pressed=None,
                kp_5_pressed=None, slash_pressed=None, kp_divide_pressed=None,
                kp_decimal_pressed=None, **kwargs):
    """The voxel renderer: input is a GLTexture, full stop — everything else
    becomes one upstream (voxel_io / the future CUDA-interop path).
    `params.label_scale` (a slider in the controls panel) sizes the
    axis-label billboards; 0 hides them, and smaller labels also fit more
    integer ticks per edge."""
    tex = input_value

    # A 1-D texture is a LUT, not a volume - don't try to raymarch it.
    if getattr(tex, "target", None) == int(gl.GL_TEXTURE_1D):
        imgui.text(f"{tex!r} — a LUT, not a volume")
        return False, None

    # Support instances created/serialized before these fields existed.
    for field in ("pan_x", "pan_y", "pan_z", "ortho", "lut", "label_scale"):
        if not hasattr(params, field):
            setattr(params, field, getattr(VoxelParams, field))
    label_scale = params.label_scale

    # Size from the OWNING WINDOW, not this view's own draw() - a nested
    # view's height derives from what it rendered last frame (self-referential),
    # while the window's height is the user-dragged size. Reserve room for the
    # header + params tree + status line below the image.
    win = draw_state.parent_window or draw_state
    width = max(64, int(draw_state.content_width or win.content_width or 0))
    height = max(64, int(win.height or 320) - 40)

    # ── gestures → params (events are hover-routed wrapper wrappers) ──────
    if middle_mouse_drag is not None:
        if middle_mouse_drag.shift:
            # Blender-style shift-d = pan: move the orbit target so the
            # content tracks the cursor 1:1 at the target plane (world units
            # per pixel at distance zoom, / 1.7 - matches the old gen).
            wpp = 2.0 * params.zoom / (1.7 * height)
            st, ct = math.sin(params.tilt), math.cos(params.tilt)
            cs, ss = math.cos(params.spin), math.sin(params.spin)
            dx, dy = middle_mouse_drag.dx, middle_mouse_drag.dy
            params.pan_x += (ss * dx - cs * st * dy) * wpp
            params.pan_y += (-cs * dx - ss * st * dy) * wpp
            params.pan_z += ct * dy * wpp
        elif middle_mouse_drag.ctrl:
            # the old viewer's ctrl-drag: vertical = dolly zoom, horizontal
            # still orbits.
            params.zoom = min(49.1, max(0.3, params.zoom * math.exp(0.005 * middle_mouse_drag.dy)))
            params.spin -= middle_mouse_drag.dx * 0.008
        else:
            params.spin -= middle_mouse_drag.dx * 0.008
            params.tilt = min(HALF_PI, max(-HALF_PI, params.tilt + middle_mouse_drag.dy * 0.008))
    if right_mouse_drag is not None:
        # the old viewer's shading drag: horizontal = brightness, vertical =
        # contrast (up = increase). A real drag exceeds CLICK_MAX_DISTANCE,
        # so context-menu clicks don't fire alongside.
        params.brightness = min(4.0, max(0.0, params.brightness + right_mouse_drag.dx * 0.01))
        params.contrast = min(4.0, max(0.1, params.contrast - right_mouse_drag.dy * 0.008))
    if scroll_y_changed is not None:
        params.zoom = min(49.1, max(0.3, params.zoom * math.exp(-0.23 * scroll_y_changed.value)))

    # ── Blender-style numpad views (hover-routed key events): 7/1/3 = top/
    # front/right, ctrl = the opposite side, 5 = ortho toggle, / (either
    # slash, or numpad . like the old viewer) = recenter the pan on the
    # origin. A focused text editor owns the keyboard, so keys are ignored
    # while one is active. ────────────────────────────────────────────────
    from src.lsd.gl_gui.melty import Melty
    if Melty.text_focused_ds is None:
        if kp_7_pressed is not None:
            params.spin, params.tilt = -HALF_PI, (-HALF_PI if kp_7_pressed.ctrl else HALF_PI)
        if kp_1_pressed is not None:
            params.spin, params.tilt = (HALF_PI if kp_1_pressed.ctrl else -HALF_PI), 0.0
        if kp_3_pressed is not None:
            params.spin, params.tilt = (math.pi if kp_3_pressed.ctrl else 0.0), 0.0
        if kp_5_pressed is not None:
            params.ortho = not params.ortho
        if (slash_pressed is not None or kp_divide_pressed is not None
                or kp_decimal_pressed is not None):
            params.pan_x = params.pan_y = params.pan_z = 0.0

    # Filtering is sampler state on the texture, view-owned, applied per frame.
    filt = gl.GL_NEAREST if params.nearest else gl.GL_LINEAR
    gl.glBindTexture(tex.target, tex.texture_id)
    gl.glTexParameteri(tex.target, gl.GL_TEXTURE_MIN_FILTER, filt)
    gl.glTexParameteri(tex.target, gl.GL_TEXTURE_MAG_FILTER, filt)
    gl.glBindTexture(tex.target, 0)

    # Box extents proportional to voxel counts (longest axis = 1), so every
    # voxel renders as a CUBE and a (4, 32, 48) tensor reads as a flat slab -
    # tex.shape is (depth, height, width) = (z, y, x).
    t_depth, t_height, t_width = (max(1, int(s)) for s in tex.shape)
    longest = float(max(t_depth, t_height, t_width))
    volume_scale = (t_width / longest, t_height / longest, t_depth / longest)

    # ── LUT: prefer the shared 1-D texture the LUT host materialized; fall
    # back to a direct upload of the named lut until the host has time ────
    lut_tex = _LUT_TEXTURES.get(params.lut)
    if lut_tex is None:
        lut_list = LUTS.get(params.lut, LUTS["jet"])
        lut_tex = gl_state.texture1d("lut_fallback", lut_list,
                                     version=(params.lut, len(lut_list)))

    # ── axis furniture geometry: corners + silhouette are the Python mirror
    # of the OpenGL camera, computed BEFORE the GL pass - the label
    # billboards render IN the voxel FBO with the volume's own camera ────
    axes = getattr(tex, "axes", None)
    axis_display = getattr(tex, "axis_display", None)
    corners = silhouette = None
    if axis_display:
        corners = project_corners(params.tilt, params.spin, params.zoom,
                                  width / height, width, height,
                                  scale=volume_scale,
                                  pan=(params.pan_x, params.pan_y, params.pan_z),
                                  ortho=params.ortho)
        silhouette = _silhouette_edges(corners)

    # ── GL pass: every resource tracked + lifecycle-managed by gl_state ──
    fb = gl_state.fbo("target", width, height)
    depth_was_on = gl.glIsEnabled(gl.GL_DEPTH_TEST)
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        # Class-default fallback: params instances created BEFORE a hotswap
        # that added a field don't have the new attribute yet.
        uniforms = {name: getattr(params, name, getattr(VoxelParams, name, 0.0))
                    for name in _UNIFORM_FIELDS}
        voxel_pass(gl_state, volume=tex, lut=lut_tex, aspect=width / height,
                   volume_scale=volume_scale, **uniforms)
        if silhouette and label_scale and label_scale > 0:
            # Labels as in-scene textured quads. A bake/render hiccup should
            # not take down the view (or trigger the hotswap auto-revert) -
            # log it and keep rendering the volume.
            global _LABEL_WARNED
            try:
                specs = _billboard_specs(silhouette, corners, axis_display,
                                         volume_scale, params, label_scale)
                cam = {n: uniforms[n] for n in ("tilt", "spin", "zoom", "pan_x",
                                                "pan_y", "pan_z", "ortho")}
                cam["aspect"] = width / height
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

    # ── ALL controls live in a satellite panel pinned to the window's
    # right edge (params + axis remap + scrubbers + flow + dim names).
    # POPOVER window_pos is relative to the CURSOR at the call, so anchor
    # the cursor at the view's top-left and offset by the window width; the
    # panel moves along when the window is dragged. Double-click the volume
    # to show/hide. ───────────────────────────────────────────────────────
    if left_mouse_double_clicked is not None:
        draw_state.misc["params_panel"] = not draw_state.misc.get("params_panel", False)
        draw_state.invalidate()
        request_render()
    panel_open = bool(draw_state.misc.get("params_panel", False))
    imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
    changed, _, panel_ds = draw_voxel_controls(tex, params=params, name="controls",
                                     mode=Modes.WINDOW, closed=not panel_open,
                                     parent_window=win, auto_resize=False,
                                     shadow=True, return_extras=True)

    if panel_ds.closed:
        draw_state.misc["params_panel"] = False

    # ── status: error surfacing + lifecycle visibility ──────────────────
    imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
    if voxel_pass.last_error:
        imgui.text_colored(voxel_pass.last_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)
    else:
        injected = sum(1 for line in voxel_pass.last_generated.get("fragment", "").splitlines()
                       if line.startswith("uniform "))
        stats = GLState.stats()
        imgui.text_colored(
            f"{tex!r} · {injected} uniforms injected · gl: {stats['states']} states / "
            f"{stats['resources']} resources / {stats['queued_deletes']} queued · "
            f"mid-drag orbit (shift pan) · scroll zoom · numpad 7/1/3/5 · / recenter",
            0.55, 0.55, 0.55, 1.0)

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
    # input_value is the host (a dict); the GLTexture it materialized lives
    # one level down; draw_any routes it to draw_voxels by type.
    tex = input_value.get("value") if isinstance(input_value, dict) else input_value
    if tex is None:
        imgui.text("no volume yet — waiting on host")
        return
    draw_any(tex, name="volume")


@window(input_value=voxel_host, tint=(0.00, 0.22, 0.54))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_playground(input_value=None, **kwargs):
    _draw_host_volume(input_value)


@window(input_value=voxel_host_4d, tint=(0.95, 0.55, 0.15))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_4d(input_value=None, **kwargs):
    _draw_host_volume(input_value)


@window(input_value=voxel_host_5d, tint=(0.10, 0.16, 0.34))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_5d(input_value=None, **kwargs):
    _draw_host_volume(input_value)


@window(input_value=voxel_host_flow, tint=(0.20, 0.80, 0.45))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_flow(input_value=None, **kwargs):
    _draw_host_volume(input_value)
