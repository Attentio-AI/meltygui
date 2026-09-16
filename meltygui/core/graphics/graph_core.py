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

from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.gl_state import GLTexture
from meltygui.core.graphics.gl_state import gl_limits
from meltygui.core.graphics.gl_state import texture3d_fit
from meltygui.core.rendering.modes import Modes
from meltygui.core.graphics.shader_func import shader_func
from meltygui.core.rendering.shaped import Shaped
from meltygui.core.runtime.toggles import SwooshMode
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window
from meltygui.core.rendering.render_dispatch import draw_any
# Shared with draw_voxels on purpose: the same typed params (TensorDim /
# TensorDims / Lut route to the same pickers), the same dtype coercion, the
# same LUTs, the same error drawing / notice / footprint helpers.
from meltygui.tensor.voxel_playground import source_identity
from meltygui.tensor.voxel_playground import _ensure_host
from meltygui.tensor.voxel_playground import demo_4d
from meltygui.tensor.voxel_playground import demo_5d
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


# ── camera helpers (the Python mirror of toPix, for ticks + legend) ───


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
