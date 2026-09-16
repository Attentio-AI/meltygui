"""draw_line_graph — draw_voxels' 2-D sibling: tensor → line graph.

The view owns mapping and rendering. CUDA inputs are sampled directly from
strided storage by line_kernels; only per-line range metadata and the final
RGBA image are allocated. CPU inputs retain the packed-texture/GL path.
Adjustable settings are parameters on the signature (auto draw_state params).

Mapping: `x_dim` is the SAMPLE axis (the graph's horizontal), `line_dim` the
SERIES axis — one polyline per index along it (a 2-D (samples, lines) input
draws `lines` lines). Unset dims derive like draw_voxels' "last dims" rule:
x = second-to-last, lines = last; a 1-D tensor is one line. Every other dim
is pinned by `slices` (one slider per extra dim along the bottom, exactly
draw_voxels' scrubbers) or averaged via `mean_dims`.

CPU storage: the (lines, samples) matrix is packed into a 3-D texture
(depth = line, height = row, width = W) so a sample axis longer than
GL_MAX_3D_TEXTURE_SIZE wraps across rows — the vertex shader fetches sample
i of line k at (i % W, i / W, k). That keeps the upload on the EXACT
voxel path (cuda_interop.tensor_to_texture / GLState.texture3d, pre-flight
via texture3d_fit) with no second interop implementation.

CPU rendering: one instanced draw — instance = line, 6 vertices per segment —
where the vertex shader pulls both segment endpoints from the texture, maps
data → pixels with the zoom/pan camera, and extrudes an anti-aliased quad of
`line_width` pixels. Zoom/pan live in the shader (the camera is four
uniforms); the Python side only turns gestures into parameter writes:
middle-drag pans, wheel zooms about the cursor, `/` resets the view,
double-click toggles the controls panel. By default (auto_scale=False) x and
y share one pixel scale and one zoom — the plot's aspect is never distorted
by the window shape or the wheel, like the voxel box; auto_scale=True fits
each axis to the image and zooms them independently (shift = x only,
ctrl = y only).
"""

import math

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
import numpy
import numpy as np
import OpenGL.GL as gl

from meltygui.core.gl_state import GLState
from meltygui.core.gl_state import GLTexture
from meltygui.core.gl_state import gl_limits
from meltygui.core.gl_state import texture3d_fit
from meltygui.core.modes import Modes
from meltygui.core.shader_func import shader_func
from meltygui.core.shaped import Shaped
from meltygui.core.toggles import SwooshMode
from meltygui.core.glfw_utils import request_render
from meltygui.core.core_render import render_func
from meltygui.core.window_decoration import window
from meltygui.core.render_dispatch import draw_any
# Shared with draw_voxels on purpose: the same typed params (TensorDim /
# TensorDims / Lut route to the same pickers), the same dtype coercion, the
# same LUTs, the same error drawing / notice / footprint helpers.
from meltygui.tensor.voxel_playground import source_identity
from meltygui.tensor.voxel_playground import LUTS
from meltygui.tensor.voxel_playground import _LUT_TEXTURES
from meltygui.model.tensor_model import _clean_dim_name
from meltygui.view.tensor_view import _describe_tensor
from meltygui.view.tensor_view import _draw_image_notice
from meltygui.tensor.voxel_playground import _draw_voxel_error
from meltygui.tensor.voxel_playground import _ensure_host
from meltygui.model.tensor_model import _resolve_dim
from meltygui.view.tensor_view import _tick_values
from meltygui.view.tensor_view import _view_size
from meltygui.tensor.voxel_playground import demo_4d
from meltygui.tensor.voxel_playground import demo_5d
from meltygui.model.tensor_model import to_display_dtype
from meltygui.tensor.voxel_playground import voxel_io
from meltygui.tensor.voxel_playground import _cached_volume_texture


# ── shaders ────────────────────────────────────────────────────────────────
# Uniforms are kwargs of line_pass (shader_func splices the declarations for
# every kwarg name an identifier in the stage code); only texture samplers
# are predeclared so their types are explicit.

LINE_VERT = """
#version 330 core
uniform sampler3D series;   // texel (i % tex_w, i / tex_w, line) = sample i of line
out float v_edge;            // signed pixel distance from the line's centerline
flat out int v_line;

// data (sample index, value) → pixel. Zoom/pan ARE the camera: a fitted
// graph spans [-margin, margin] graph units at zoom 1, pan shifts in that
// space, and unit_px (pixels per graph unit, per axis) places it in the
// image — equal components keep the plot's aspect (the default), the
// image's half-extents stretch it to fill (auto_scale).
vec2 toPix(int i, float y) {
    float xn = n_samples > 1 ? float(i) / float(n_samples - 1) : 0.5;
    float yn = (y - y_min) / max(y_max - y_min, 1e-30);
    vec2 g = vec2(((xn * 2.0 - 1.0) * margin - pan_x) * zoom_x,
                  ((yn * 2.0 - 1.0) * margin - pan_y) * zoom_y);
    return viewport * 0.5 + g * unit_px;
}

float sampleAt(int i, int line) {
    return texelFetch(series, ivec3(i % tex_w, i / tex_w, line), 0).r;
}

void main() {
    int seg = gl_VertexID / 6;
    int corner = gl_VertexID % 6;
    int line = gl_InstanceID;
    v_line = line;
    float y0 = sampleAt(seg, line);
    float y1 = sampleAt(seg + 1, line);
    if (isnan(y0) || isnan(y1) || isinf(y0) || isinf(y1)) {
        // A gap in the data: park the vertex outside the clip volume.
        v_edge = 0.0;
        gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
        return;
    }
    vec2 s0 = toPix(seg, y0);
    vec2 s1 = toPix(seg + 1, y1);
    vec2 dir = s1 - s0;
    float len = length(dir);
    vec2 ext = len > 1e-6 ? dir / len : vec2(1.0, 0.0);
    vec2 nrm = vec2(-ext.y, ext.x);
    float hw = line_width * 0.5 + 1.0;       // +1px skirt for the AA ramp
    // two triangles: (s0,-)(s1,-)(s1,+) and (s0,-)(s1,+)(s0,+); the ends
    // extend by hw along the segment so consecutive segments overlap at
    // joins instead of leaving wedge gaps on sharp turns.
    bool at1 = (corner == 1 || corner == 2 || corner == 4);
    float side = (corner == 2 || corner == 4 || corner == 5) ? 1.0 : -1.0;
    vec2 p = (at1 ? s1 + ext * hw : s0 - ext * hw) + nrm * side * hw;
    v_edge = side * hw;
    gl_Position = vec4(p / viewport * 2.0 - 1.0, 0.0, 1.0);
}
"""

LINE_FRAG = """
#version 330 core
in float v_edge;
flat in int v_line;
out vec4 FragColor;
uniform sampler1D lut;

void main() {
    float d = abs(v_edge);
    float a = 1.0 - smoothstep(line_width * 0.5 - 0.5, line_width * 0.5 + 0.5, d);
    a *= line_opacity;
    vec3 c = n_lines > 1
        ? texture(lut, (float(v_line) + 0.5) / float(n_lines)).rgb
        : single_color;
    c = pow(max(c, 0.0), vec3(2.2));   // LUT / tint are display-referred sRGB; the FBO is linear (hdr_color.py)
    FragColor = vec4(c * a, a);     // premultiplied — the FBO composites ONE / 1-a
}
"""


@shader_func(fragment=LINE_FRAG, vertex=LINE_VERT)
def line_pass(gl_state: GLState = None, series=None, lut=None, n_samples=2,
              tex_w=1, n_lines=1, zoom_x=1.0, zoom_y=1.0, pan_x=0.0, pan_y=0.0,
              y_min=0.0, y_max=1.0, margin=0.92, viewport=(1.0, 1.0),
              unit_px=(0.5, 0.5), line_width=1.5, line_opacity=1.0, single_color=(0.35, 0.75, 1.0),
              **kwargs):
    # Program bound, uniforms set. Attribute-less instanced draw: the vertex
    # shader computes every segment quad from gl_VertexID / gl_InstanceID and
    # the series texture - 6 vertices per segment, one instance per line.
    if n_samples < 2 or n_lines < 1:
        return
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 6 * (int(n_samples) - 1), int(n_lines))


# ── data mapping ───────────────────────────────────────────────────────────

def _resolve_line_axes(shape, dim_names, x_dim, line_dim):
    """(x, line) tensor dims for a shape: by index or NAME, None / garbage /
    duplicates derive — x = second-to-last (the only dim for 1-D), lines =
    last. Returns line=None when there is no series axis (1-D input, or
    every other dim is claimed)."""
    n = len(shape)
    x = _resolve_dim(dim_names, x_dim, n)
    line = _resolve_dim(dim_names, line_dim, n)
    if x is None:
        x = n - 2 if n >= 2 else 0
        if line is not None and x == line:
            x = n - 1
    if line is not None and line == x:
        line = None
    if line is None and n >= 2:
        line = n - 1 if x != n - 1 else n - 2
    return x, line


def slice_lines(t, dim_names=(), x_dim=None, line_dim=None, slices=(),
                mean_dims=(), normalize=False, materialize=True):
    """tensor → (lines, samples) 2-D display matrix, PURE (every choice is an
    argument, nothing stored). Unmapped dims pin to their `slices` index
    (missing → 0) or average when in mean_dims; `normalize` min-max scales
    EACH LINE to [0, 1] (compare shapes, not magnitudes). Stays on t's
    device. Returns (lines2d, (x_dim, line_dim|None), shape)."""
    import torch
    from meltygui.model.tensor_model import _display_view_dtype
    t = (to_display_dtype if materialize else _display_view_dtype)(t.detach())
    if t.numel() == 0:
        raise ValueError(f"empty tensor (shape {tuple(t.shape)}) — nothing to plot")
    if t.dim() == 0:
        t = t.unsqueeze(0)
    n = t.dim()
    shape = tuple(int(s) for s in t.shape)
    xd, ld = _resolve_line_axes(shape, dim_names, x_dim, line_dim)
    picked = {xd} | ({ld} if ld is not None else set())
    mean_set = {int(d) for d in (mean_dims or ()) if 0 <= int(d) < n}
    for d in mean_set:
        m = t.mean(dim=d, keepdim=True)
        t = m.expand(t.shape) if d in picked else m

    def _pin(d):
        try:
            v = int(slices[d]) if d < len(slices) else 0
        except (TypeError, ValueError):
            v = 0
        return max(0, min(v, shape[d] - 1))

    index = tuple(slice(None) if d in picked
                  else (0 if d in mean_set else _pin(d))
                  for d in range(n))
    sub = t[index]
    if ld is None:
        lines = sub.reshape(1, -1)
    else:
        lines = sub.transpose(0, 1) if ld > xd else sub   # → (line, sample)
    if materialize:
        lines = lines.contiguous()
    if normalize and materialize:
        lo = lines.amin(dim=1, keepdim=True)
        hi = lines.amax(dim=1, keepdim=True)
        lines = (lines - lo) / (hi - lo + 1e-12)
    return lines, (xd, ld), shape


def pack_series(lines, max_w):
    """(lines, samples) → (lines, rows, W) volume for the series texture:
    W = min(samples, max_w), rows = ceil(samples / W), zero-padded tail.
    The shader never reads past n_samples, so the pad is inert."""
    import torch
    n_lines, n = (int(s) for s in lines.shape)
    w = max(1, min(n, int(max_w)))
    rows = (n + w - 1) // w
    if rows * w != n:
        lines = torch.nn.functional.pad(lines, (0, rows * w - n))
    return lines.reshape(n_lines, rows, w).contiguous(), w


def _finite_range(lines):
    """(min, max) over the FINITE values, (0, 1) when there are none, and a
    non-degenerate span for constant data (so a flat line sits mid-plot)."""
    import torch
    finite = lines[torch.isfinite(lines)]
    if finite.numel() == 0:
        return 0.0, 1.0
    lo, hi = torch.aminmax(finite)
    lo, hi = float(lo), float(hi)
    if hi - lo < 1e-12:
        pad = abs(lo) * 0.5 or 0.5
        return lo - pad, hi + pad
    return lo, hi


# ── camera helpers (the Python mirror of toPix, for ticks + legend) ───

def _unit_px(width, height, auto_scale):
    """Pixels per graph unit per axis — the one place the scaling policy
    lives (the shader's unit_px). auto_scale stretches the fitted graph to
    the image; otherwise both axes share min(w, h)/2 so the plot keeps its
    aspect whatever the window's shape (draw_voxels never distorts its box
    either)."""
    if auto_scale:
        return width * 0.5, height * 0.5
    u = min(width, height) * 0.5
    return u, u


def _graph_to_norm(g, zoom, pan, margin):
    """graph units (the shader's g) → normalized data coordinate [0, 1]."""
    return ((g / zoom + pan) / margin + 1.0) * 0.5


def _norm_to_px(v, zoom, pan, margin, px, unit, flip=False):
    """normalized data coordinate → pixel offset inside the image (one axis)."""
    g = ((v * 2.0 - 1.0) * margin - pan) * zoom
    return px * 0.5 - g * unit if flip else px * 0.5 + g * unit


def _nice_step(span, target_ticks):
    """1-2-5·10ᵏ step giving about `target_ticks` over `span`."""
    if span <= 0 or target_ticks <= 0:
        return 1.0
    raw = span / target_ticks
    k = 10.0 ** math.floor(math.log10(raw))
    for s in (1.0, 2.0, 5.0, 10.0):
        if s * k >= raw:
            return s * k
    return 10.0 * k


def _fmt_value(v, step):
    if step >= 1.0:
        return f"{v:.0f}"
    decimals = min(9, max(0, int(math.ceil(-math.log10(step)))))
    return f"{v:.{decimals}f}"


def _draw_axes_overlay(draw_list, img_pos, width, height, n_samples, y_range,
                       zoom_x, zoom_y, pan_x, pan_y, margin, unit, x_label,
                       caption, font_px):
    """2-D axis furniture over the image: faint grid, sample-index ticks
    along the bottom, value ticks along the left, the x dim's name, and a
    caption (series dim × line count) top-right. Pure imgui draw-list
    text, recomputed per frame from the camera params."""
    x0, y0 = img_pos
    grid_col = pack_color(1.0, 1.0, 1.0, 0.07)
    tick_col = pack_color(0.85, 0.85, 0.85, 0.8)
    dim_col = pack_color(0.85, 0.85, 0.85, 0.55)
    # x: visible index span from the inverse camera at the image edges.
    ux, uy = unit
    if n_samples > 1:
        # visible index span: the image edges are g = ±(half-extent / unit)
        gx = width * 0.5 / ux
        lo = _graph_to_norm(-gx, zoom_x, pan_x, margin) * (n_samples - 1)
        hi = _graph_to_norm(gx, zoom_x, pan_x, margin) * (n_samples - 1)
        lo, hi = max(0.0, lo), min(float(n_samples - 1), hi)
        px_per_idx = 2.0 * ux * zoom_x * margin / (n_samples - 1)
        for i in _tick_values(lo, hi, px_per_idx, font_px):
            px = x0 + _norm_to_px(i / (n_samples - 1), zoom_x, pan_x, margin, width, ux)
            draw_list.add_line(px, y0, px, y0 + height, grid_col)
            label = str(i)
            tw, th = imgui.calc_text_size(label)
            draw_list.add_text(px - tw * 0.5, y0 + height - th - 2, tick_col, label)
    # y: value ticks at a nice step over the visible value span.
    ymin, ymax = y_range
    gy = height * 0.5 / uy
    v_lo = ymin + _graph_to_norm(-gy, zoom_y, pan_y, margin) * (ymax - ymin)
    v_hi = ymin + _graph_to_norm(gy, zoom_y, pan_y, margin) * (ymax - ymin)
    step = _nice_step(v_hi - v_lo, max(2, height / 70.0))
    v = math.ceil(v_lo / step) * step
    guard = 0
    while v <= v_hi and guard < 200:
        guard += 1
        vn = (v - ymin) / (ymax - ymin)
        py = y0 + _norm_to_px(vn, zoom_y, pan_y, margin, height, uy, flip=True)
        draw_list.add_line(x0, py, x0 + width, py, grid_col)
        label = _fmt_value(v, step)
        tw, th = imgui.calc_text_size(label)
        draw_list.add_text(x0 + 4, py - th * 0.5, tick_col, label)
        v += step
    if x_label:
        tw, th = imgui.calc_text_size(x_label)
        draw_list.add_text(x0 + width - tw - 6, y0 + height - th - 2, dim_col, x_label)
    if caption:
        tw, th = imgui.calc_text_size(caption)
        draw_list.add_text(x0 + width - tw - 6, y0 + 4, dim_col, caption)


# ── the view ───────────────────────────────────────────────────────────────

from meltygui.view.graph_view import draw_line_graph


# ── playground windows: like draw_voxel_4d / _5d pair, on line graphs ──────

def demo_lines_4d():
    """(phase=8, freq=6, samples=256, lines=12): damped sines — scrub
    `phase` / `freq`, the 12 lines fan out by amplitude."""
    key = "_DEMO_LINES_4D"
    cached = globals().get(key)
    if cached is None:
        x = np.linspace(0.0, 4.0 * math.pi, 256, dtype=np.float32)
        out = np.zeros((8, 6, 256, 12), dtype=np.float32)
        for p in range(8):
            for f in range(6):
                for k in range(12):
                    out[p, f, :, k] = ((k + 1) / 12.0) * np.sin((f + 1) * 0.5 * x
                                                                 + p * math.pi / 8) \
                        * np.exp(-x * 0.08 * (k % 3))
        cached = globals()[key] = out
    return cached


line_host_4d = _ensure_host("line_host_4d", "Line 4D", demo_lines_4d(), io=voxel_io)
line_host_5d = _ensure_host("line_host_5d", "Line 5D", demo_5d(), io=voxel_io)
line_host_torus = _ensure_host("line_host_torus", "Line Torus", demo_4d(), io=voxel_io)


window(draw_line_graph, name="line_host_4d", input_value=line_host_4d, tint=(0.20, 0.36, 0.59))


window(draw_line_graph, name="line_host_5d", input_value=line_host_5d, tint=(0.09, 0.50, 0.77))


window(draw_line_graph, input_value=line_host_torus, name="line_host_torus", tint=(0.78, 0.67, 0.61))
