"""draw_line_graph — draw_voxels' 2-D sibling: tensor → line graph.

Same structure, same pipeline, same ownership rules as draw_voxels
(voxel_playground.py): the view owns every mapping and render decision, the
source tensor is sliced + uploaded HERE (re-keyed by gl_state deps on source
identity/_version/mapping), CUDA tensors go device-to-device through
cuda_interop into a GL texture, and a @shader_func renders into a gl_state
FBO that imgui.image shows. EVERYTHING adjustable is a parameter on the
signature (auto draw_state params: gestures and the controls panel write
draw_state.<name>, only diverged values persist/serialize).

Mapping: `x_dim` is the SAMPLE axis (the graph's horizontal), `line_dim` the
SERIES axis — one polyline per index along it (a 2-D (samples, lines) input
draws `lines` lines). Unset dims derive like draw_voxels' "last dims" rule:
x = second-to-last, lines = last; a 1-D tensor is one line. Every other dim
is pinned by `slices` (one slider per extra dim along the bottom, exactly
draw_voxels' scrubbers) or averaged via `mean_dims`.

Storage: the (lines, samples) matrix is packed into a 3-D texture
(depth = line, height = row, width = W) so a sample axis longer than
GL_MAX_3D_TEXTURE_SIZE wraps across rows — the vertex shader fetches sample
i of line k at (i % W, i / W, k). That keeps the upload on the EXACT
voxel path (cuda_interop.tensor_to_texture / GLState.texture3d, pre-flight
via texture3d_fit) with no second interop implementation.

Rendering: one instanced draw — instance = line, 6 vertices per segment —
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

import imgui
import numpy as np
import OpenGL.GL as gl

from src.lsd.gl_gui.gl_state import GLState, GLTexture, gl_limits, texture3d_fit
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.shader_func import shader_func
from src.lsd.gl_gui.toggles import SwooshMode
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
# Shared with draw_voxels on purpose: the same typed params (TensorDim /
# TensorDims / Lut route to the same pickers), the same dtype coercion, the
# same LUTs, the same error drawing / notice / footprint helpers.
from src.lsd.gl_gui.view.playground.voxel_playground import (
    LUTS, Lut, TensorDim, TensorDims, _LUT_TEXTURES, _clean_dim_name,
    _describe_tensor, _draw_image_notice, _draw_voxel_error, _ensure_host,
    _resolve_dim, _tick_values, _view_size, demo_4d, demo_5d, to_display_dtype,
    voxel_io)


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
                mean_dims=(), normalize=False):
    """tensor → (lines, samples) 2-D display matrix, PURE (every choice is an
    argument, nothing stored). Unmapped dims pin to their `slices` index
    (missing → 0) or average when in mean_dims; `normalize` min-max scales
    EACH LINE to [0, 1] (compare shapes, not magnitudes). Stays on t's
    device. Returns (lines2d, (x_dim, line_dim|None), shape)."""
    import torch
    t = to_display_dtype(t.detach())
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
    lines = lines.contiguous()
    if normalize:
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
    grid_col = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.07)
    tick_col = imgui.get_color_u32_rgba(0.85, 0.85, 0.85, 0.8)
    dim_col = imgui.get_color_u32_rgba(0.85, 0.85, 0.85, 0.55)
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

@render_func(show_bg=True, selectable=True, auto_resize=False, min_width=269,
             with_header=draw_header, bg_offset=0, min_height=293,
             disable_scroll=True, use_cache=True)
def draw_line_graph(input_value=None, gl_state: GLState = None, selectable=False,
                    draw_state=None,
                    # ── camera: zoom/pan in the shader. cam_* and zoom_* names
                    # dodge the legacy DrawState zoom field (name-colling
                    # args are excluded from auto-state). ──
                    zoom_x=1.0, zoom_y=1.0, pan_x=0.0, pan_y=0.0, fit_margin=0.92,
                    # auto_scale=False (default): x and y share ONE pixel scale
                    # and ONE zoom - the plot keeps its aspect however the
                    # window is shaped or the wheel is rolled, like the voxel
                    # box. True: each axis fits the image and zooms on its own
                    # (shift/ctrl-wheel for x/y only).
                    auto_scale=False,
                    # ── line styling ──
                    line_width=1.5, line_opacity=1.0, lut=Lut("jet"),
                    single_color=(0.35, 0.75, 1.0), max_lines=1024,
                    show_axes=True,
                    # ── data mapping: dims by index or NAME; -1 = derive
                    # (x = second-to-last, lines = last). ──
                    dim_names=("layer", "batch", "token", "feature"),
                    x_dim=TensorDim(-1), line_dim=TensorDim(-1),
                    slices=(), mean_dims=TensorDims(()), normalize=False,
                    # ── events (hover-routed wrapper kwargs) ──
                    middle_mouse_drag=None, scroll_y_changed=None,
                    left_mouse_double_clicked=None, slash_pressed=None,
                    kp_divide_pressed=None, kp_decimal_pressed=None, **kwargs):
    """The line-graph renderer — draw_voxels' sibling (see the module doc).
    Input is a tensor/ndarray (sliced + uploaded HERE) or an already-packed
    series GLTexture (rendered as-is; needs n_samples/tex_w/y_range stamped
    on it)."""
    import torch
    src = input_value
    img_origin = imgui.get_cursor_screen_pos()   # the image draws here below
    dim_names = tuple(_clean_dim_name(x, i) for i, x in enumerate(dim_names or ()))
    slices = tuple(int(v) for v in (slices or ()))
    mean_dims = tuple(int(v) for v in (mean_dims or ()))
    margin = max(0.1, min(1.0, float(fit_margin)))
    auto_scale = bool(auto_scale)

    # ── src → (lines, samples) → packed 3-D texture, parameter-driven and
    # stateless: slice_lines is a pure function of these params, the upload
    # re-runs exactly when its deps change; metadata rides the texture. ──
    if isinstance(src, GLTexture):
        tex, mapping = src, None
        source_shape = tuple(getattr(src, "source_shape", src.shape))
        n_samples = int(getattr(src, "n_samples", src.shape[2]))
        tex_w = int(getattr(src, "tex_w", src.shape[2]))
        y_range = tuple(getattr(src, "y_range", (0.0, 1.0)))
        n_lines = int(src.shape[0])
        clamp_note = getattr(src, "clamp_note", None)
    else:
        try:
            t = src if isinstance(src, torch.Tensor) else torch.from_numpy(np.asarray(src))
        except (TypeError, ValueError, RuntimeError) as e:
            _draw_voxel_error(draw_state, f"{type(src).__name__} is not tensor-shaped:\n{e}",
                              who="draw_line_graph")
            gl_state.drop("series"); gl_state.drop("series_cuda")
            return False, None
        try:
            lines, mapping, source_shape = slice_lines(
                t, dim_names, x_dim, line_dim, slices, mean_dims, normalize)
        except (ValueError, TypeError, RuntimeError, IndexError) as e:
            _draw_voxel_error(draw_state, f"can't build lines from "
                              f"{_describe_tensor(t)}:\n{e}", who="draw_line_graph")
            gl_state.drop("series"); gl_state.drop("series_cuda")
            return False, None
        n_lines, n_samples = (int(s) for s in lines.shape)
        max_3d = int(gl_limits()["max_3d"])
        notes = []
        cap = max(1, min(int(max_lines), max_3d))
        if n_lines > cap:
            notes.append(f"{n_lines} lines — showing the first {cap} "
                         f"(max_lines={int(max_lines)}, GL depth limit {max_3d})")
            lines = lines[:cap]
            n_lines = cap
        y_range = _finite_range(lines)
        vol, tex_w = pack_series(lines, max_3d)
        clamped_shape, problems = texture3d_fit(vol.shape, vol.element_size(),
                                                max_bytes=float("inf"))
        if problems:
            # Rows past the GL limit: the sample axis longer than max_3d² -
            # show the displayable prefix of samples.
            if any("GL_MAX_3D_TEXTURE_SIZE" not in p for p in problems):
                _draw_voxel_error(draw_state, f"{_describe_tensor(t)} → series "
                                  f"{tuple(int(s) for s in vol.shape)}:\n"
                                  + "\n".join(problems), who="draw_line_graph")
                gl_state.drop("series"); gl_state.drop("series_cuda")
                return False, None
            d3, h3, w3 = clamped_shape
            vol = vol[:d3, :h3, :w3].contiguous()
            n_samples = min(n_samples, h3 * w3)
            notes.append(f"{n_samples}+ samples exceed the GL texture budget; "
                         f"showing the first {h3 * w3}")
        clamp_note = "; ".join(notes) if notes else None
        version = ((id(src), getattr(src, "_version", 0)), mapping, slices,
                   mean_dims, bool(normalize), n_lines, tuple(vol.shape))
        tex = None
        try:
            if vol.is_cuda:
                from src.lsd.gl_gui import cuda_interop
                tex = cuda_interop.tensor_to_texture(gl_state, "series_cuda", vol,
                                                     version=version)
            if tex is None:
                tex = gl_state.texture3d("series", vol.cpu().numpy(), version=version)
                gl_state.drop("series_cuda")
            else:
                gl_state.drop("series")
        except Exception as e:
            _draw_voxel_error(draw_state, f"GPU upload failed for series "
                              f"{tuple(int(s) for s in vol.shape)} ({vol.dtype}):\n{e}",
                              who="draw_line_graph")
            gl_state.drop("series"); gl_state.drop("series_cuda")
            return False, None
        tex.source_shape = source_shape
        tex.source_ndim = len(source_shape)
        tex.n_samples, tex.tex_w, tex.y_range = n_samples, tex_w, y_range
        tex.clamp_note = clamp_note

    # ── labels: the x dim's name + the series caption ──
    x_label, caption = "", ""
    if mapping is not None:
        xd, ld = mapping
        x_label = dim_names[xd] if xd < len(dim_names) else f"dim{xd}"
        if ld is not None:
            ln = dim_names[ld] if ld < len(dim_names) else f"dim{ld}"
            caption = f"{ln} × {n_lines} lines"
    else:
        caption = f"{n_lines} lines" if n_lines > 1 else ""

    # ── slice sliders: one per UNMAPPED dim with extent > 1 (not plotted,
    # not averaged) - exactly draw_voxels' scrubbers. ──
    slider_dims = []
    if mapping is not None and len(source_shape) > 2:
        used = {d for d in mapping if d is not None}
        slider_dims = [d for d in range(len(source_shape))
                       if d not in used and d not in mean_dims
                       and source_shape[d] > 1]

    # ── size from the OWNING WINDOW (see _view_size) ──
    win = draw_state if draw_state.closable else (draw_state.parent_window or draw_state)
    width, height = _view_size(draw_state)
    if slider_dims:
        height = max(100, height - int(imgui.get_frame_height_with_spacing())
                     * len(slider_dims))

    # ── in-flight state values (deferred slow-source writes): re-read any
    # camera param with a timer set so drags accumulate off those values.
    _pending = getattr(draw_state, "_sa_pending", None) or {}
    _precise = getattr(draw_state, "_sa_precise", None) or {}
    _in_flight = _pending.keys() | _precise.keys()
    if _in_flight:
        def _fly(n, cur):
            if n not in _in_flight:
                return cur
            v = getattr(draw_state, "locate_" + n)
            return cur if v is None else v
        zoom_x, zoom_y = _fly("zoom_x", zoom_x), _fly("zoom_y", zoom_y)
        pan_x, pan_y = _fly("pan_x", pan_x), _fly("pan_y", pan_y)

    # ── gestures → draw_state params (auto-state: the write diverges the
    # param so it persists; events are hover-routed wrapper kwargs) ──
    unit = _unit_px(width, height, auto_scale)
    if not auto_scale and zoom_y != zoom_x:
        # Locked aspect: one zoom. A stray divergence (auto_scale is on,
        # or a panel edit to one of them) collapses onto zoom_x.
        zoom_y = zoom_x
        draw_state.locate_zoom_y = zoom_y
    if middle_mouse_drag is not None:
        # Pan tracks the cursor 1:1 at any zoom: a pixel is 1/z graph
        # units, and pan lives in pre-zoom graph units.
        pan_x -= middle_mouse_drag.dx / (unit[0] * max(zoom_x, 1e-6))
        pan_y += middle_mouse_drag.dy / (unit[1] * max(zoom_y, 1e-6))
        draw_state.locate_pan_x = pan_x
        draw_state.locate_pan_y = pan_y
    if scroll_y_changed is not None:
        # Zoom about the cursor: the data point under the mouse stays put -
        # (v·2-1)·m = c/z + pan = c/z' + pan'  →  pan' = pan + c/z - c/z'
        # with c the cursor in graph units. Locked aspect: both axes get one
        # zoom; auto_scale: shift = x only, ctrl = y only, plain = both.
        factor = math.exp(0.23 * scroll_y_changed.value)
        mx, my = imgui.get_mouse_pos()
        cx = (mx - img_origin[0] - width * 0.5) / unit[0]
        cy = (img_origin[1] + height * 0.5 - my) / unit[1]
        lim_x, lim_y = width * 0.5 / unit[0], height * 0.5 / unit[1]
        cx, cy = max(-lim_x, min(lim_x, cx)), max(-lim_y, min(lim_y, cy))
        do_x = (not auto_scale) or not scroll_y_changed.ctrl
        do_y = (not auto_scale) or not scroll_y_changed.shift
        if do_x:
            nz = min(1e6, max(1e-3, zoom_x * factor))
            pan_x += cx / zoom_x - cx / nz
            zoom_x = nz
            draw_state.locate_zoom_x = zoom_x
            draw_state.locate_pan_x = pan_x
        if do_y:
            nz = min(1e6, max(1e-3, zoom_y * factor))
            pan_y += cy / zoom_y - cy / nz
            zoom_y = nz
            draw_state.locate_zoom_y = zoom_y
            draw_state.locate_pan_y = pan_y
    from src.lsd.gl_gui.melty import Melty
    if Melty.text_focused_ds is None and (slash_pressed is not None
                                          or kp_divide_pressed is not None
                                          or kp_decimal_pressed is not None):
        zoom_x = zoom_y = 1.0
        pan_x = pan_y = 0.0
        draw_state.locate_zoom_x = 1.0
        draw_state.locate_zoom_y = 1.0
        draw_state.locate_pan_x = 0.0
        draw_state.locate_pan_y = 0.0

    # ── LUT: the shared 1D texture the LUT host materialized, else a
    # direct upload of the named list until the next is run ──
    lut_tex = _LUT_TEXTURES.get(lut)
    if lut_tex is None:
        lut_list = LUTS.get(lut, LUTS["jet"])
        lut_tex = gl_state.texture1d("lut_fallback", lut_list,
                                     version=(lut, len(lut_list)))

    # ── GL pass: every resource keyed + lifecycle-managed by gl_state ──
    fb = gl_state.fbo("target", width, height)
    depth_was_on = gl.glIsEnabled(gl.GL_DEPTH_TEST)
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        _blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_FUNC_ADD)
        gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA,
                               gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        line_pass(gl_state, series=tex, lut=lut_tex,
                  n_samples=int(n_samples), tex_w=int(tex_w), n_lines=int(n_lines),
                  zoom_x=float(zoom_x), zoom_y=float(zoom_y),
                  pan_x=float(pan_x), pan_y=float(pan_y),
                  y_min=float(y_range[0]), y_max=float(y_range[1]),
                  margin=float(margin), viewport=(float(width), float(height)),
                  unit_px=(float(unit[0]), float(unit[1])),
                  line_width=max(0.5, float(line_width)),
                  line_opacity=max(0.0, min(1.0, float(line_opacity))),
                  single_color=tuple(float(c) for c in single_color)[:3])
        if not _blend_was:
            gl.glDisable(gl.GL_BLEND)
    if depth_was_on:
        gl.glEnable(gl.GL_DEPTH_TEST)

    img_pos = imgui.get_cursor_screen_pos()
    imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))
    if show_axes:
        _draw_axes_overlay(imgui.get_window_draw_list(), img_pos, width, height,
                           int(n_samples), y_range, zoom_x, zoom_y, pan_x, pan_y,
                           margin, unit, x_label, caption, imgui.get_font_size())
    clamp_note = getattr(tex, "clamp_note", None)
    if clamp_note:
        _draw_image_notice(img_pos, width, clamp_note)

    # ── slice sliders under the image (auto-state write → re-slice) ──
    for d in slider_dims:
        label = dim_names[d] if d < len(dim_names) else f"dim{d}"
        cur = max(0, min(int(slices[d]) if d < len(slices) else 0,
                         source_shape[d] - 1))
        imgui.push_item_width(max(60, width - 110))
        s_changed, s_val = imgui.slider_int(f"{label}##slice{d}", cur,
                                            0, source_shape[d] - 1)
        imgui.pop_item_width()
        if s_changed and int(s_val) != cur:
            new_slices = list(slices) + [0] * (len(source_shape) - len(slices))
            new_slices[d] = int(s_val)
            draw_state.slices = tuple(new_slices)
            draw_state.invalidate()
            request_render()

    # ── params panel: the function's OWN params, satellite to and right
    # of the window, double-click to show/hide (draw_voxels' panel verbatim
    # - see the comments there for the mode/cursor/closed= contracts). ──
    init = "params_panel" not in draw_state.misc
    toggled = False
    if left_mouse_double_clicked is not None:
        draw_state.misc["params_panel"] = not draw_state.misc.get("params_panel", False)
        toggled = True
        draw_state.invalidate()
        request_render()
    panel_open = bool(draw_state.misc.get("params_panel", False))
    panel_kwargs = {"closed": not panel_open} if (init or toggled) else {}
    if not middle_mouse_drag and scroll_y_changed is None:
        _flow_cursor = imgui.get_cursor_screen_pos()
        _anchor_y = win.abs_top if draw_state.closable else draw_state.abs_top
        imgui.set_cursor_screen_pos((win.abs_left + (win.width or width) + 12, _anchor_y))
        changed, _, panel_ds = draw_any(draw_state.locate_params,
                                        name=f"controls##{draw_state.name}",
                                        is_tree=False,
                                        use_cache=False,
                                        show_name=False,
                                        layer_offset=7,
                                        tint=draw_state._kwargs.get("tint", None),
                                        swoosh_mode=SwooshMode.LINE,
                                        mode=Modes.WINDOW_PARAMS, show_tint=False,
                                        parent_window=win, auto_resize=True,
                                        shadow=False, return_extras=True,
                                        initial={"expanded": True},
                                        **panel_kwargs)
        imgui.set_cursor_screen_pos(_flow_cursor)
        if panel_ds is not None:
            draw_state.misc["params_panel"] = not panel_ds.closed
            if not panel_ds.closed:
                if (not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1) and not
                        imgui.is_mouse_down(0) and scroll_y_changed is None) and changed:
                    panel_ds.invalidate_up()

        if line_pass.last_error:
            imgui.text_colored(line_pass.last_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)

        if changed:
            draw_state.invalidate()
            request_render()
            return changed, input_value

    return False, None


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


@window(input_value=line_host_4d, tint=(0.20, 0.36, 0.59))
@render_func(show_bg=True, use_cache=True)
def draw_line_graph_4d(input_value=None, **kwargs):
    draw_line_graph(input_value.get("value"), name="lines_4d", mode=Modes.WINDOW,
                    dim_names=("phase", "freq", "sample", "line"))


@window(input_value=line_host_5d, tint=(0.02, 0.38, 0.11))
@render_func(show_bg=True, use_cache=True)
def draw_line_graph_5d(input_value=None, draw_state=None, **kwargs):
    t = input_value.get("value") if isinstance(input_value, dict) else input_value
    if t is None:
        imgui.text("no series yet — waiting on host")
        return
    draw_line_graph(t, name="lines_5d", dim_names=("layer", "head", "d", "h", "w"))


@window(input_value=line_host_torus, tint=(0.78, 0.67, 0.61))
@render_func(show_bg=True, use_cache=True)
def draw_line_graph_torus(input_value=None, **kwargs):
    # draw_voxel_4d's torus volume as lines, width vs x, one line per
    # height row, scrub time/depth.
    draw_line_graph(input_value.get("value"), name="lines_torus", mode=Modes.WINDOW,
                    dim_names=("time", "depth", "height", "width"))
