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

Middle-drag = orbit, scroll = zoom. Four hosts/windows ship as playgrounds:
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
from src.lsd.gl_gui.shader_func import shader_func
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.view.core_views.new_core_view import draw_float, draw_any

VOXEL_FRAG = """
#version 330 core
in vec2 uv;
out vec4 FragColor;

vec3 jet(float v) {
    v = clamp(v, 0.0, 1.0);
    return clamp(vec3(1.5 - abs(4.0 * v - 3.0),
                      1.5 - abs(4.0 * v - 2.0),
                      1.5 - abs(4.0 * v - 1.0)), 0.0, 1.0);
}

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
    // Z-up orbit camera built straight from injected uniforms — tilt, spin
    // and zoom arrive as plain Python kwargs, no matrices anywhere.
    float ct = cos(tilt);
    vec3 eye = vec3(cos(spin) * ct, sin(spin) * ct, sin(tilt)) * zoom;
    vec3 fwd = normalize(-eye);
    vec3 right = normalize(cross(fwd, vec3(0.0, 0.0, 1.0)));
    vec3 up = cross(right, fwd);
    vec2 ndc = (uv * 2.0 - 1.0) * vec2(aspect, 1.0);
    vec3 rd = normalize(fwd * 1.7 + right * ndc.x + up * ndc.y);

    // volume_scale: box extents per axis, voxel-count-proportional — so each
    // VOXEL is a cube and the tensor keeps its true shape.
    vec2 hit = rayBox(eye, rd, volume_scale);
    if (hit.x > hit.y || hit.y < 0.0) { FragColor = vec4(0.0); return; }

    float t = max(hit.x, 0.0);
    vec4 acc = vec4(0.0);
    for (int i = 0; i < 2048; i++) {
        if (t > hit.y || acc.a > 0.98) break;
        vec3 p = (eye + rd * t) / volume_scale * 0.5 + 0.5;   // box -> texcoord [0,1]
        float v = texture(volume, p).r;
        // The old viewer's value pipeline, verbatim: contrast about
        // mid-grey, then brightness, on the GREYSCALE value — the heat map
        // and the opacity gate both consume the remapped value.
        v = (v - 0.5) * contrast + 0.5;
        v *= brightness;
        v = clamp(v, 0.0, 1.0);
        float d = clamp((v - threshold) * density, 0.0, 1.0);
        if (d > 0.0) {
            float a = d * step_size * 60.0;
            acc.rgb += (1.0 - acc.a) * a * jet(v);
            acc.a   += (1.0 - acc.a) * a;
        }
        t += step_size;
    }
    FragColor = acc;
}
"""


@shader_func(fragment=VOXEL_FRAG)
def voxel_pass(gl_state: GLState = None, tilt=0.5, spin=0.8, zoom=3.4,
               aspect=1.0, brightness=1.0, contrast=1.0, density=8.0,
               threshold=0.12, step_size=0.004, volume=None,
               volume_scale=(1.0, 1.0, 1.0), **kwargs):
    # Program bound, uniforms set - the body is just the draw call.
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


class VoxelParams(DictConversion):
    """Camera + render params, auto-injected per view. The field annotations
    ARE the control UI (`draw_any(params)` renders them with these ranges,
    editable from the context menus), and every annotated field is forwarded
    to the shader as a uniform."""

    tilt: draw_float(min_value=-1.45, max_value=1.45) = 0.5
    spin: draw_float(min_value=-6.3, max_value=6.3) = 0.8
    zoom: draw_float(min_value=1.4, max_value=15.0) = 3.4
    brightness: draw_float(min_value=0.0, max_value=4.0) = 1.0
    contrast: draw_float(min_value=0.1, max_value=4.0) = 1.0
    density: draw_float(min_value=0.5, max_value=30.0) = 8.0
    threshold: draw_float(min_value=0.0, max_value=1.0) = 0.12
    step_size: draw_float(min_value=0.001, max_value=0.02) = 0.004
    nearest = False   # texture filtering, applied per frame, not a uniform


# The annotated fields are exactly the scalar uniform candidates.
_UNIFORM_FIELDS = tuple(VoxelParams.__annotations__)


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


# Cube edges per DISPLAY axis: corner tuples differing only on that axis,
# ordered (negative end, positive end) so "0" labels the texcoord-0 corner.
# World x → texture x (width dim), matching the shader's p = cube*0.5+0.5.
_AXIS_EDGES = {
    "x": [((-1, y, z), (1, y, z)) for y in (-1, 1) for z in (-1, 1)],
    "y": [((x, -1, z), (x, 1, z)) for x in (-1, 1) for z in (-1, 1)],
    "z": [((x, y, -1), (x, y, 1)) for x in (-1, 1) for y in (-1, 1)],
}


def _silhouette_edges(corners):
    """The cube edges on the screen-space outline. Under projection a convex
    solid's silhouette IS the convex hull of its projected corners — so hull
    membership replaces the old face-visibility walk: an edge is on the
    silhouette iff its endpoints are CONSECUTIVE hull vertices."""
    pts = [(c, p) for c, p in corners.items() if p is not None]
    if len(pts) < 3:
        return set()
    pts.sort(key=lambda cp: (cp[1][0], cp[1][1]))

    def cross(o, a, b):
        return ((a[1][0] - o[1][0]) * (b[1][1] - o[1][1])
                - (a[1][1] - o[1][1]) * (b[1][0] - o[1][0]))

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = [cp[0] for cp in (lower[:-1] + upper[:-1])]
    edges = set()
    for i, a in enumerate(hull):
        b = hull[(i + 1) % len(hull)]
        # consecutive hull corners that are also cube-adjacent (differ on one axis)
        if sum(1 for k in range(3) if a[k] != b[k]) == 1:
            edges.add(frozenset((a, b)))
    return edges


def project_corners(tilt, spin, zoom, aspect, width, height, scale=(1.0, 1.0, 1.0)):
    """Screen positions of the volume BOX's 8 corners (extents = `scale`, the
    voxel-count-proportional volume_scale) — the Python mirror of the shader's
    orbit camera, so labels land exactly on the rendered edges. Keys stay the
    ±1 sign tuples; positions carry the scaling.
    Returns {corner_signs: (sx, sy) or None (behind camera)}."""
    ct = math.cos(tilt)
    eye = np.array([math.cos(spin) * ct, math.sin(spin) * ct, math.sin(tilt)]) * zoom
    fwd = -eye / np.linalg.norm(eye)
    right = np.cross(fwd, (0.0, 0.0, 1.0))
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
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
        ndx = (1.7 * (d @ right) / zc) / aspect
        ndy = 1.7 * (d @ up) / zc
        out[corner] = ((ndx * 0.5 + 0.5) * width, (1.0 - (ndy * 0.5 + 0.5)) * height)
    return out


def _draw_axis_labels(draw_list, img_pos, corners, axis_display):
    """The old view's edge furniture: the cube's silhouette outline drawn as
    thin lines (shortened near corners, the original fixed_shorten look), and
    per display axis ONE labeled silhouette edge — the (flow-aware) dim name
    centered beside its midpoint, 0 → size at the ends. Text is centered via
    calc_text_size and offset PERPENDICULAR to the edge so labels sit beside
    their line instead of colliding at shared corners."""
    visible = [p for p in corners.values() if p is not None]
    if len(visible) < 4:
        return
    cx = sum(p[0] for p in visible) / len(visible)
    cy = sum(p[1] for p in visible) / len(visible)
    silhouette = _silhouette_edges(corners)
    line_col = imgui.get_color_u32_rgba(0.9, 0.9, 1.0, 0.5)
    name_col = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.85)
    num_col = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.45)
    SHORTEN = 14.0

    def put_text(text, x, y, nx, ny, dist, color):
        ts = imgui.calc_text_size(text)
        draw_list.add_text(img_pos[0] + x + nx * dist - ts.x / 2,
                           img_pos[1] + y + ny * dist - ts.y / 2, color, text)

    # ── the silhouette ──────────────────────────────────────────────────────
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

    # ── one labeled edge per axis (prefer silhouette edges) ─────────────
    for axis, (name, size) in zip(("x", "y", "z"), axis_display):
        best, best_score = None, -1.0
        for a, b in _AXIS_EDGES[axis]:
            pa, pb = corners[a], corners[b]
            if pa is None or pb is None:
                continue
            mx, my = (pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5
            score = math.hypot(mx - cx, my - cy)
            if frozenset((a, b)) in silhouette:
                score += 1e5   # silhouette edges first, outermost edges farthest
            if score > best_score:
                best, best_score = (pa, pb, mx, my), score
        if best is None:
            continue
        pa, pb, mx, my = best
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        # label direction, reduced to its component PERPENDICULAR to the edge
        ox, oy = mx - cx, my - cy
        along = ox * ux + oy * uy
        nx, ny = ox - along * ux, oy - along * uy
        norm = math.hypot(nx, ny) or 1.0
        nx, ny = nx / norm, ny / norm
        put_text(name, mx, my, nx, ny, 16, name_col)
        if length >= 70:
            # End numbers sit a little way IN along their own edge - shared
            # corners would stack each edge's number at one point.
            inset = min(26.0, length * 0.16)
            put_text("0", pa[0] + ux * inset, pa[1] + uy * inset, nx, ny, 11, num_col)
            put_text(str(size), pb[0] - ux * inset, pb[1] - uy * inset, nx, ny, 11, num_col)


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
        scrub_changed, value = imgui.slider_int(
            f"{axes.dim_names[d]}##scrub_{d}", axes.slice_indices[d], 0, size - 1)
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
        chunk_changed, chunk = imgui.input_int("chunk##nf", axes.nf_chunk, step=0)
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
                **kwargs):
    """The voxel renderer: input is a GLTexture, full stop — everything else
    becomes one upstream (voxel_io / the future CUDA-interop path)."""
    tex = input_value

    # ── gestures → params (events are hover-routed wrapper kwargs) ──────
    dirty = False
    if middle_mouse_drag is not None:
        params.spin -= middle_mouse_drag.dx * 0.008
        params.tilt = min(1.45, max(-1.45, params.tilt + middle_mouse_drag.dy * 0.008))
        dirty = True
    if right_mouse_drag is not None:
        # the old viewer's shading drag: horizontal = brightness, vertical =
        # contrast (up to saturation). A real click exceeds CLICK_MAX_DISTANCE,
        # so context-menu clicks don't fire alongside.
        params.brightness = min(4.0, max(0.0, params.brightness + right_mouse_drag.dx * 0.01))
        params.contrast = min(4.0, max(0.1, params.contrast - right_mouse_drag.dy * 0.008))
        dirty = True
    if scroll_y_changed is not None:
        params.zoom = min(49.1, max(0.3, params.zoom * math.exp(-0.23 * scroll_y_changed.value)))
        dirty = True

    # Size from the OWNING WINDOW, not this view's own draw() - a nested
    # view's height derives from what it rendered last frame (self-referential),
    # while the window's height is the user-dragged size. Reserve room for the
    # header + params tree + status line below the image.
    win = draw_state.parent_window or draw_state
    width = max(64, int(draw_state.content_width or win.content_width or 0))
    height = max(64, int(win.height or 320) - 40)

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
        voxel_pass(gl_state, volume=tex, aspect=width / height,
                   volume_scale=volume_scale, **uniforms)
    if depth_was_on:
        gl.glEnable(gl.GL_DEPTH_TEST)

    img_pos = imgui.get_cursor_screen_pos()
    imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))

    # ── axis labels: dim names + extents along the box's outermost corners,
    # drawn with the Python mirror of the shader camera ────────────────
    axes = getattr(tex, "axes", None)
    axis_display = getattr(tex, "axis_display", None)
    if axis_display:
        corners = project_corners(params.tilt, params.spin, params.zoom,
                                  width / height, width, height,
                                  scale=volume_scale)
        _draw_axis_labels(imgui.get_window_draw_list(), img_pos, corners, axis_display)

    # ── ALL controls live in a satellite panel pinned to the window's
    # right edge (params + axis remap + scrubbers + flow + dim names).
    # POPOVER window_pos is relative to the CURSOR at the call, so anchor
    # the cursor at the view's top-left and offset by the window width; the
    # panel moves along when the window is dragged. Double-click the volume
    # to show/hide. ───────────────────────────────────────────────────────
    if left_mouse_double_clicked is not None:
        draw_state.misc["params_panel"] = not draw_state.misc.get("params_panel", True)
        draw_state.invalidate()
        request_render()
    panel_open = bool(draw_state.misc.get("params_panel", True))
    imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
    changed, _ = draw_voxel_controls(tex, params=params, name="controls",
                                     mode=Modes.WINDOW, closed=not panel_open,
                                     parent_window=win, auto_resize=False,
       
                                     shadow=True)

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
            f"mid-drag orbit · scroll zoom",
            0.55, 0.55, 0.55, 1.0)

    if dirty or changed:
        draw_state.invalidate()
        request_render()


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


def _ensure_host(var_name, host_name, demo_input):
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
        host = RenderHost(io_function=voxel_io, input_value=demo_input, name=host_name)
    host.io_function = voxel_io
    return host


voxel_host = _ensure_host("voxel_host", "Voxel Volume", None)
voxel_host_4d = _ensure_host("voxel_host_4d", "Voxel 4D", demo_4d())
voxel_host_5d = _ensure_host("voxel_host_5d", "Voxel 5D", demo_5d())
voxel_host_flow = _ensure_host("voxel_host_flow", "Voxel Flow", demo_flat())


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


@window(input_value=voxel_host_5d, tint=(0.55, 0.25, 0.85))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_5d(input_value=None, **kwargs):
    _draw_host_volume(input_value)


@window(input_value=voxel_host_flow, tint=(0.20, 0.80, 0.45))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_flow(input_value=None, **kwargs):
    _draw_host_volume(input_value)
