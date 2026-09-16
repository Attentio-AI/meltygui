"""Voxel renderer on the GLState + @shader_func stack, plugged into real data
through a RenderHost.

The pipeline — source hosting and parameter-driven tensor presentation:

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
- LUTs are flat [r,g,b, r,g,b, ...] float lists from model/lut_model.py.
  Core injects the editable host data and shared uploads into tensor and graph
  views. The LUT texture is an integer-like model, with no palette host.
- Axis labels are textured billboards IN the scene: text_texture.py bakes the
  strings via imgui's own font atlas (a private shared-atlas context + the
  screen pass's draw-list mechanics, no freetype), and a raw-GL pass draws each
  as a world-space quad in the voxel FBO — baseline along its edge, up-axis
  perpendicular, flipped per frame so it always reads upright.

Middle-drag = orbit (shift: pan, ctrl: dolly), scroll = zoom, a 3D mouse
(space_mouse_changed → voxel_camera.apply_space_mouse) orbits / pans /
dollies with the cap (Toggles.SpaceMouse), and Blender-style
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

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
import numpy as np
import OpenGL.GL as gl

from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.gl_state import GLTexture
from meltygui.core.graphics.gl_state import gl_limits
from meltygui.core.graphics.gl_state import texture3d_fit
from meltygui.core.graphics.gl_state import tight_unpack
from meltygui.core.melty import Melty
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.graphics.shader_func import shader_func
from meltygui.core.rendering.shaped import Shaped
from meltygui.core.graphics.text_texture import bake_text
from meltygui.core.graphics.text_texture import bake_texts
from meltygui.core.runtime.toggles import SwooshMode
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.windowing.glfw_utils import print_stack_trace
from meltygui.core.conversion.render_host import RenderHost
from meltygui.core.core_render import render_func
from meltygui.core.core_render import release_input_refs
from meltygui.core.rendering.window_decoration import window
from meltygui.core.rendering.modes import Modes
from meltygui.core.rendering.render_dispatch import draw_any
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.runtime.toggles import Swoosh

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

// ── shared transfer function: raw volume sample at texcoord p →
// (v: remapped LUT coordinate, m: opacity drive). The old viewer's value
// pipeline verbatim — contrast about mid-grey, then brightness, on the
// GREYSCALE value; `centered` maps signed data so raw 0 sits at the LUT
// middle (pair with a diverging LUT) and opacity keys on MAGNITUDE, so
// negatives render as strongly as positives. Takes the sampler to read
// through: `volume` (the user's nearest/linear toggle) for the color march,
// `volume_lin` (always linear — same texture, its own sampler object) for
// every shading read, where smooth beats blocky regardless of the toggle.
vec2 remapValue(sampler3D vol, vec3 p) {
    float v = texture(vol, p).r;
    if (centered) { v = v * 0.5 + 0.5; }   // signed [-1,1] -> [0,1]
    v = (v - 0.5) * contrast + 0.5;
    float m;
    if (centered) {
        v = 0.5 + (v - 0.5) * brightness;
        v = clamp(v, 0.0, 1.0);
        m = abs(v - 0.5) * 2.0;
    } else {
        v *= brightness;
        v = clamp(v, 0.0, 1.0);
        m = v;
    }
    return vec2(v, m);
}

// Extended-sRGB decode (hdr_color.py's convention): the sRGB curve
// mirrored for negatives, no ceiling — a LUT entry above 1 is brighter than
// the desktop's white, a negative one is outside the sRGB gamut (P3). A
// plain pow() turned negatives into NaN.
vec3 decodeSrgb(vec3 c) { return sign(c) * pow(abs(c), vec3(2.2)); }

// The old viewer's opacity ramp: values at/above the gate (1 - threshold)
// are FULLY opaque — a hard isosurface — and below it opacity falls off as
// (m/gate)^4, scaled by density and the volume_scale-NORMALIZED segment
// length, so optical depth is a function of the FRACTION of the volume
// traversed, not the world path length — a 4096-voxel axis viewed end-on
// accumulates the same opacity as an 8-voxel one.
float alphaFor(float m, float seg_n) {
    float gate = 1.0 - clamp(threshold, 0.0, 0.999);
    if (m >= gate) return 1.0;
    return clamp(pow(m / gate, 4.0) * density * seg_n * 50.0, 0.0, 1.0);
}

// Transmittance from a world point toward the light: a COARSE fixed-count
// march of the SAME transfer function (shadows are low-frequency — fat
// steps read clean where the primary ray needs thousands), multiplying out
// per-step opacity. The result is graded the way the render is: haze dims
// the light, the opaque core blocks it. `steps` sets the quality tier
// (the plane's cast shadow affords more than per-sample self-shading) and
// `max_dist` optionally caps the march to NEAR-FIELD occluders — for
// self-shading, what's right next to a sample is most of its shadow.
// Always reads through volume_lin: shading wants smooth fields.
// The Info variant also reports WHERE occlusion happened: (T, t_occ) with
// t_occ the distance along the light ray at which transmittance first
// dropped below 0.5 — the occluder height that drives the ground-space
// penumbra radius. Rays that never occlude report the mid-chord of their
// box span instead, so pixels just OUTSIDE the umbra blur with the same
// radius as their shadowed neighbors (the penumbra widens on BOTH sides
// of the hard edge); rays that miss the box entirely report 0.
vec2 lightVisibilityInfo(vec3 p, vec3 lp, int steps, float max_dist) {
    vec3 ld = normalize(lp - p);
    vec2 span = rayBox(p, ld, volume_scale);
    float t0 = max(span.x, 0.0);
    float t1 = min(min(span.y, length(lp - p)), max_dist);
    if (t0 >= t1) return vec2(1.0, 0.0);
    float ss = (t1 - t0) / float(steps);
    float seg_n = ss * length(ld / volume_scale);
    float T = 1.0;
    float t_occ = 0.0;
    float t = t0 + ss * 0.5;
    for (int i = 0; i < steps; i++) {
        vec3 q = (p + ld * t) / volume_scale * 0.5 + 0.5;
        T *= 1.0 - alphaFor(remapValue(volume_lin, q).y, seg_n);
        if (t_occ == 0.0 && T < 0.5) { t_occ = t; }
        if (T < 0.02) break;   // fully shadowed — stop early
        t += ss;
    }
    if (t_occ == 0.0) { t_occ = 0.5 * (t0 + t1); }
    return vec2(T, t_occ);
}

float lightVisibility(vec3 p, vec3 lp, int steps, float max_dist) {
    return lightVisibilityInfo(p, lp, steps, max_dist).x;
}

// Gradient normal at texcoord p (central differences, one voxel apart),
// mapped to WORLD space (anisotropic boxes bend gradients), plus a shading
// weight (w) that fades to 0 where the gradient is too weak to trust —
// uniform haze keeps its flat unshaded look instead of picking up noise.
vec4 volumeNormal(vec3 p) {
    // 1.75-voxel stencil: with the linear sampler this is a genuine lowpass
    // on the normal field, so hard binary edges (a 0/1 mask's staircase)
    // shade as smooth ramps instead of per-voxel facets.
    vec3 e = 1.75 / vec3(textureSize(volume_lin, 0));
    vec3 g = vec3(
        texture(volume_lin, p + vec3(e.x, 0, 0)).r - texture(volume_lin, p - vec3(e.x, 0, 0)).r,
        texture(volume_lin, p + vec3(0, e.y, 0)).r - texture(volume_lin, p - vec3(0, e.y, 0)).r,
        texture(volume_lin, p + vec3(0, 0, e.z)).r - texture(volume_lin, p - vec3(0, 0, e.z)).r);
    if (centered) { g *= sign(texture(volume_lin, p).r); }   // shade |v|'s surface
    vec3 gw = g / volume_scale;              // texcoord gradient → world
    float len = length(gw);
    // The normal points from dense toward empty — that's MINUS the gradient.
    return vec4(len > 1e-6 ? -gw / len : vec3(0.0, 0.0, 1.0),
                clamp(length(g) * 6.0, 0.0, 1.0));
}

void main() {
    // Z-up orbit camera built straight from injected uniforms — tilt, spin, roll,
    // zoom, pan and ortho arrive as plain Python kwargs, no matrices anywhere.
    // The basis is analytic in spin/tilt (not cross(fwd, world-up)) so the
    // numpad top/bottom presets (tilt = ±π/2) stay well-defined; it matches
    // the old construction everywhere else.
    float ct = cos(tilt);
    vec3 fwd = -vec3(cos(spin) * ct, sin(spin) * ct, sin(tilt));
    vec3 right0 = vec3(-sin(spin), cos(spin), 0.0);
    // roll turns right toward up about the view axis (0 = level horizon,
    // the turntable); the 3D mouse's trackball mode is what writes it.
    vec3 right = right0 * cos(roll) + cross(right0, fwd) * sin(roll);
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

    // ── shadow catcher: the plane the box rests on (z = -volume_scale.z)
    // is itself INVISIBLE — its only contribution is the shadow the volume
    // casts onto it, composited as a darkening with alpha = blocked light.
    // One-sided (backface-culled analytically): a hit only counts for rays
    // striking the TOP face — from underneath there's no shadow at all.
    float plane_t = -1.0;
    float plane_a = 0.0;
    // The caught shadow darkens toward shadow_tint — a neutral grey-black
    // (Toggles.Voxels.floor_shadow_color), decoupled from the blue-black
    // the rest of the studio's compositor shadows carry.
    vec3 plane_c = shadow_tint;
    // plane_side mirrors the catcher to the box's OTHER face when the view
    // is upside-down (+1 = floor at -z, -1 = at +z; latched between drags
    // on the Python side). The conditions are the normal ones written in
    // z' = z * plane_side; the catcher's light is mirrored to match below.
    if (draw_plane && draw_shading && rd.z * plane_side < -1e-6
            && ro.z * plane_side > -volume_scale.z) {
        plane_t = (-volume_scale.z * plane_side - ro.z) / rd.z;
        vec3 pw = ro + rd * plane_t;
        // Blocked light via the same transmittance march as the rest of the
        // shading, lifted by the ambient floor (ambient_light raises this
        // shadow like every other). The exponential radial fade bounds the
        // catcher so the darkening dies off instead of cutting.
        float ext = max(volume_scale.x, volume_scale.y);
        float r = max(length(pw.xy) - ext * 1.1, 0.0);
        // Center march gathers (visibility, occluder distance); the blur
        // then happens in FLOOR coordinates — 4 extra visibility taps on a
        // ring around the hit point, radius = shadow_softness × occluder
        // distance (higher occluders throw softer shadows), averaged with
        // the center. Skipped when the center ray misses the box (t_occ 0
        // — open floor, nothing to soften).
        // The catcher's marches use a PLANE-LOCAL light: light_pos with its
        // z mirrored to the plane's side. The real light stays fixed in
        // world space (the volume's shading uses it untouched) — this
        // mirror only makes the flipped floor catch the same silhouette
        // the bottom floor would, instead of a ceiling catching nothing.
        vec3 pl_light = vec3(light_pos.xy, light_pos.z * plane_side);
        vec2 vi = lightVisibilityInfo(pw, pl_light, 24, 1e8);
        float vis = vi.x;
        float blur_r = shadow_softness * vi.y;
        if (blur_r > 1e-4) {
            float acc_v = vis;
            for (int k = 0; k < 4; k++) {
                float ang = float(k) * 1.5707963 + 0.7853982;
                vec3 op = pw + vec3(cos(ang), sin(ang), 0.0) * blur_r;
                acc_v += lightVisibility(op, pl_light, 10, 1e8);
            }
            vis = acc_v / 5.0;
        }
        float shadow = (1.0 - ambient_light) * (1.0 - vis);
        plane_a = clamp(shadow * shadow_opacity, 0.0, 1.0) * exp(-1.5 * r / ext);
    }

    // volume_scale: box extents per axis, voxel-count-proportional — so each
    // VOXEL is a cube and the tensor keeps its true shape.
    vec2 hit = rayBox(ro, rd, volume_scale);
    bool box_hit = !(hit.x > hit.y || hit.y < 0.0);
    if (!box_hit && plane_t <= 0.0) { FragColor = vec4(0.0); return; }

    vec4 acc = vec4(0.0);
    // Plane in FRONT of the volume (looking down at foreground floor past
    // the box — possible since the fade extends beyond it): composite it
    // first. The box's bottom face lies IN the plane, so a ray never
    // crosses the plane mid-march — it's strictly before or after the box.
    if (plane_t > 0.0 && box_hit && plane_t <= max(hit.x, 0.0)) {
        acc = vec4(plane_c * plane_a, plane_a);
        plane_t = -1.0;
    }

    float t = max(hit.x, 0.0);
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
        // Value pipeline + opacity ramp live in remapValue/alphaFor (shared
        // with the shadow march). `lut` is a 1-D texture the LUT host baked
        // from a flat [r,g,b,...] float list.
        vec2 vm = remapValue(volume, p);
        float seg_n = seg * length(rd / volume_scale);
        float a = alphaFor(vm.y, seg_n * view_cos);
        if (a > 0.0) {
            // Composite in LINEAR light: the LUT tables are display-referred
            // sRGB, so decode each sample before accumulating (encode once at
            // the end). Blending in sRGB space skews mixes toward the more
            // saturated component — the old harsh/garish translucency.
            vec3 c = decodeSrgb(texture(lut, vm.x).rgb);
            if (draw_shading) {
                // Gradient-normal Lambert, weighted by gradient strength so
                // flat haze keeps its unshaded look; self_shading adds a
                // FAST near-field transmittance march toward the light —
                // 6 fat linear-filtered steps capped close to the sample,
                // since nearby occluders are most of a sample's shadow.
                // Both the normal taps and the march are skipped where they
                // can't show: sub-1% alpha samples, and (for the march)
                // gradient weight ≈ 0 — the mix would erase it anyway.
                float shade = 1.0;
                if (a > 0.01) {
                    vec4 nw = volumeNormal(p);
                    if (nw.w > 0.01) {
                        vec3 wp = ro + rd * (t + seg * 0.5);
                        // Half-Lambert wrap: (n·l/2 + 1/2)² instead of the
                        // hard max(n·l, 0). Faces pointing away from the
                        // light dim gently rather than clamping to the
                        // ambient floor — on binary data the hard clamp
                        // turned every off-facing step facet into the same
                        // flat dark block.
                        float ndl = dot(nw.xyz, normalize(light_pos - wp)) * 0.5 + 0.5;
                        float vis = self_shading
                                  ? lightVisibility(wp, light_pos, 6, 0.7) : 1.0;
                        shade = mix(1.0, ambient_light
                                    + (1.0 - ambient_light) * ndl * ndl * vis,
                                    nw.w * shading_strength);
                    }
                }
                c *= decodeSrgb(light_tint) * light_brightness * shade;
            }
            acc.rgb += (1.0 - acc.a) * a * c;
            acc.a   += (1.0 - acc.a) * a;
        }
        t += step_size;
    }
    // Plane BEHIND the volume (the usual case): composite it under
    // whatever the march accumulated.
    if (plane_t > 0.0) {
        acc.rgb += (1.0 - acc.a) * plane_c * plane_a;
        acc.a   += (1.0 - acc.a) * plane_a;
    }
    // The target is the linear fp16 scene (hdr_color.py): no sRGB encode
    // here, the presentation pass does that once. `gamma` is an artistic
    // curve on the linear image, 1.0 = untouched (the colorimetric result),
    // above 1 darkens the mids against the studio's dark UI. Mirrored for
    // negatives (P3 rides as negative scRGB). No dither: the fp16 target
    // doesn't band.
    FragColor = vec4(sign(acc.rgb) * pow(abs(acc.rgb), vec3(gamma)), acc.a);
}
"""


@shader_func(fragment=VOXEL_FRAG)
def voxel_pass(gl_state: GLState = None, tilt=0.5, spin=0.8, roll=0.0, zoom=3.4,
               pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False,
               aspect=1.0, brightness=1.0, contrast=1.0, density=1.0, gamma=1.6,
               threshold=0.1, step_size=0.0015, max_steps=4096, centered=False,
               volume=None, volume_lin=None, lut=None,
               draw_plane=True, shadow_opacity=1.0, shadow_softness=0.15,
               shadow_tint=(0.0, 0.02, 0.05), plane_side=1.0,
               draw_shading=True, self_shading=True,
               light_pos=(90.0, -90.0, 200.0), light_tint=(1.0, 1.0, 1.0),
               light_brightness=1.622, ambient_light=0.3, shading_strength=0.7,
               volume_scale=(1.0, 1.0, 1.0), program=None, **kwargs):
    # Program bound, uniforms set. volume_lin is the SAME texture as volume
    # on its own unit; a GL sampler object forces LINEAR filtering on that
    # unit so the shading reads a smooth field, while the core march keeps
    # the user's nearest/linear choice (filtering is texture-object state -
    # a bound sampler object is the rare GL mechanism that overrides it
    # per unit).
    def create():
        s = int(gl.glGenSamplers(1))
        gl.glSamplerParameteri(s, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glSamplerParameteri(s, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        for w in (gl.GL_TEXTURE_WRAP_S, gl.GL_TEXTURE_WRAP_T,
                  gl.GL_TEXTURE_WRAP_R):
            gl.glSamplerParameteri(s, w, gl.GL_CLAMP_TO_EDGE)  # = texture3d's
        return s
    sampler = gl_state.get("volume_lin_sampler", create,
                           lambda v: gl.glDeleteSamplers(1, [int(v)]))
    unit = -1
    loc = gl.glGetUniformLocation(program, "volume_lin")
    if loc >= 0:
        buf = np.zeros(1, np.int32)
        gl.glGetUniformiv(program, loc, buf)
        unit = int(buf[0])
        gl.glBindSampler(unit, sampler)
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
    if unit >= 0:
        gl.glBindSampler(unit, 0)   # sampler bindings outlive the draw call


# ── cuda_march.py path: kernel-rendered fp16 RGBA → display-GPU texture → FBO ──
IMAGE_BLIT_FRAG = """
#version 330 core
out vec4 FragColor;
uniform sampler2D image;
void main() { FragColor = texelFetch(image, ivec2(gl_FragCoord.xy), 0); }
"""


@shader_func(fragment=IMAGE_BLIT_FRAG)
def image_blit_pass(gl_state: GLState = None, image=None, program=None, **kwargs):
    """Fullscreen copy of `image` (a 2-D GLTexture the size of the target)
    into the bound FBO — the voxel_pass stand-in for cuda_march frames."""
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


_CUDA_LAST_ERROR = globals().get("_CUDA_LAST_ERROR")


def _cuda_march_ready():
    try:
        import meltygui.tensor.cuda_march as cuda_march
        return cuda_march.available()
    except Exception:
        return False


def _upload_cuda_image(gl_state, out):
    """Transfer only the finished 2-D pixels; never the source tensor."""
    import torch
    H, W = int(out.shape[0]), int(out.shape[1])
    host = gl_state.get("cuda_host",
                        lambda: torch.empty(H, W, 4, dtype=torch.float16).pin_memory(),
                        deps=(W, H))
    # D2H on torch's (legacy default) stream orders after the kernel on
    # the same device's null stream — no explicit synchronize.
    host.copy_(out)

    def create():
        tex_id = _scalar_int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        # cuda_march writes linear premultiplied fp16 — the same light
        # the GL pass writes — so the image rides into the fp16 scene
        # (hdr_color.py) unclamped: no encode, no 8-bit ceiling.
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, W, H, 0,
                        gl.GL_RGBA, gl.GL_HALF_FLOAT, None)
        for pn, pv in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                       (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                       (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                       (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, pn, pv)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        return GLTexture(tex_id, gl.GL_TEXTURE_2D, (H, W), gl.GL_RGBA16F)

    img = gl_state.get("cuda_image", create,
                       lambda tx: gl.glDeleteTextures([tx.texture_id]),
                       deps=(W, H))
    gl.glBindTexture(gl.GL_TEXTURE_2D, img.texture_id)
    with tight_unpack():
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, W, H, gl.GL_RGBA,
                           gl.GL_HALF_FLOAT, host.numpy())
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return img


def _cuda_render(gl_state, cv, width, height, lut="jet", shade=None,
                 lut_texture=None, **cam):
    """Run the CUDA raymarcher over `cv` (CudaVolumeView) at width×height
    and return a display-GPU RGBA16F GLTexture holding the premultiplied
    LINEAR image (HDR headroom and P3 negatives intact, hdr_color.py) — or
    None (error recorded in _CUDA_LAST_ERROR, drawn as status).
    The shared LUT and per-view output image live on the TENSOR's device;
    a pinned host buffer carries the image over, and `cuda_image`
    is the GL texture it lands in (all re-made only when size/device/LUT
    change)."""
    global _CUDA_LAST_ERROR
    import torch
    import meltygui.tensor.cuda_march as cuda_march
    dev = cv.view.device
    W, H = int(width), int(height)
    try:
        out = gl_state.get("cuda_out",
                           lambda: torch.empty(H, W, 4, dtype=torch.float16, device=dev),
                           deps=(W, H, str(dev)))
        if lut_texture is None:
            from meltygui.model.lut_model import LutTexture, make_luts, lut_values
            lut_texture = gl_state.get(('cuda_lut_proxy', str(lut)),
                lambda: LutTexture(lut_values(make_luts(), lut)))
        lut_t = lut_texture.cuda(dev)
        # shading params ride one small device array, re-uploaded only when
        # a value changes (deps = the values themselves)
        shade_list = list(shade) if shade is not None else cuda_march.shade_params()
        shade_t = gl_state.get("cuda_shade",
                               lambda: torch.tensor(shade_list, dtype=torch.float32, device=dev),
                               deps=(tuple(shade_list), str(dev)))
        # Shading mip: baked once per volume version (one full volume read),
        # then every shading tap reads the few-MB dense copy instead of the
        # strided source - keeping shading cost independent of tensor size.
        _transfer = (float(cam["threshold"]), float(cam["density"]),
                     float(cam["brightness"]), float(cam["contrast"]),
                     bool(cam["centered"]))
        # The (j, k) mip is the colour march's TRAVERSAL grid now (two-level
        # DDA skips empty cells through it) as well as self-shading's light
        # field; it bakes OPACITY under the current transfer, so the key
        # includes it. Always baked on the cuda path.
        if True:
            mip = gl_state.get(
                "cuda_mip",
                lambda: cuda_march.build_mip(
                    cv.view, display_shape=cv.shape, nf=cv.nf, norm=cv.norm,
                    threshold=_transfer[0], density=_transfer[1],
                    brightness=_transfer[2], contrast=_transfer[3],
                    centered=_transfer[4]),
                deps=(cv._vol_key, cv.shape, str(dev), _transfer))
        # Floor map is a BAKED full-res map - the plane's shadow depends on
        # the volume/light/transfer, never the camera, so it re-bakes only
        # when those change (slice slider, tensor version, light or
        # brightness edit), and orbiting reads it for free. Softness happens
        # live (the blur is map taps at render time).
        floor_map, floor_R = None, (0.0, 0.0)
        if shade_list[0] > 0.5 and shade_list[7] > 0.5:    # draw_floor
            _light = (tuple(shade_list[9:12]), float(shade_list[6]))  # pos, side
            vsc = cam["volume_scale"]
            floor_R = cuda_march.floor_map_extent(vsc)
            floor_map = gl_state.get(
                "cuda_floor",
                lambda: cuda_march.build_floor_map(
                    cv.view, display_shape=cv.shape, volume_scale=vsc,
                    nf=cv.nf, norm=cv.norm,
                    threshold=_transfer[0], density=_transfer[1],
                    brightness=_transfer[2], contrast=_transfer[3],
                    centered=_transfer[4],
                    light_pos=_light[0], plane_side=_light[1]),
                deps=(cv._vol_key, cv.shape, str(dev), _transfer, _light,
                      tuple(round(float(v), 5) for v in vsc)))
        cuda_march.march(cv.view, out, lut_t, display_shape=cv.shape, nf=cv.nf,
                         norm=cv.norm, aspect=W / H, shade=shade_t, mip=mip,
                         floor_map=floor_map, floor_extent=floor_R, **cam)
        img = _upload_cuda_image(gl_state, out)
        _CUDA_LAST_ERROR = None
        return img
    except Exception as e:
        msg = f"cuda_march failed: {e}"
        if msg != _CUDA_LAST_ERROR:
            print(f"[voxels] {msg}")
            print_stack_trace()
        _CUDA_LAST_ERROR = msg
        return None


def _scalar_int(v):
    try:
        return int(v)
    except TypeError:
        return int(v[0])


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


# Compatibility imports for palette values; runtime ownership lives in core.


LUTS = globals().get("LUTS")

# Retain old references until the core ownership bridge below adopts them.
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


# Dim-tab geometry, authored at ui_scale 1.0 (scaled through Melty.px at
# draw time). Tighter than draw_tab_bar's 30px tabs / 15px text pad / imgui
# item-spacing gap, which made the dim rows the tallest thing in the panel.


# Axis params that must name unique dims: `_resolve_axes` (and the line
# renderer's `_resolve_line_axes`) collapse a duplicate to unset and re-derive
# it. Picking a dim another axis row already holds therefore SWAPS the two -
# the legacy voxel_renderer behavior - instead of silently knocking the other
# axis back to its derived default. sort_dim / nf_chop / nf_along stay out:
# duplicing an axis is their whole point.


# Display-axis position in the sliced (z, y, x) volume.


def source_identity(src):
    """Cache identity of a tensor source: id + in-place version + the live
    publish generation (live_view._stamp_publish_gen). id()/_version alone
    collide across runs — the released previous generation's address is
    reused by the next run's tensor, _version 0 on both — and the cached
    texture of the OLD run was served for the new value."""
    from meltygui.code.live_view import publish_gen
    return (id(src), getattr(src, "_version", 0), publish_gen(src))


def _cached_volume_texture(gl_state, vol_key, keys=("volume_cuda", "volume", "cuda_view")):
    """The already-uploaded volume texture for `vol_key`, or None. Checks
    both upload paths (interop CudaVolume wraps its GLTexture as .texture);
    a hit means draw_voxels skips slice_volume AND the upload outright."""
    for key in keys:
        rec = gl_state.peek(key)
        if rec is None:
            continue
        tex = getattr(rec, "texture", rec)
        if getattr(tex, "_vol_key", None) == vol_key:
            return tex
    return None


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


# Near-plane depth for the axis box's Python-side projection - the old
# per-corner behind-camera cutoff; edges now CLIP here instead of vanishing.


# Outline edges draw shortened by this many screen px at each true-corner
# end (the original fixed_shorten look); clipped edges compresses into the
# same span so the end labels align with the visible span ends.


from meltygui.core.graphics.tensor_core import _voxels_cleanup


from meltygui.view.tensor_view import draw_voxels


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
        from meltygui.core.melty import Melty as _Melty
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

# Compatibility names; the palette is a model and no longer needs a RenderHost.
from meltygui.core.graphics.lut_core import get_luts
LUTS = get_luts()
_LUT_TEXTURES = LUTS._textures
lut_host = None


def _draw_host_volume(input_value):
    # input_value is the host (a dict); the SOURCE tensor it resolved comes
    # one level down. draw_voxels owns creation + upload + render - call it
    # directly (render_funcs are called directly, chain philosophy).
    t = input_value.get("value") if isinstance(input_value, dict) else input_value
    if t is None:
        imgui.text("no volume yet — waiting on host")
        return
    draw_voxels(t, name="volume")


# window(cls=voxel_host.get("value"), name="draw_voxel_playground", view_func=draw_voxels, tint=(0.00, 0.02, 0.12))
#
# @render_func(show_bg=True, use_cache=True)
# def draw_voxel_playground(input_value=None, **kwargs):
#     _draw_host_volume(input_value)


# @window(input_value=voxel_host_4d, tint=(0.20, 0.36, 0.59))
# @render_func(show_bg=True, use_cache=True)
# def draw_voxel_4d(input_value=None, **kwargs):
#     draw_voxels(input_value.get("value"), name="volume_4d", mode=Modes.WIRE)

window(draw_voxels, name="voxel_host", input_value=voxel_host, tint=(1,0,0))

window(draw_voxels, name="voxel_host_4d", input_value=voxel_host_4d, tint=(0,1,1))
window(draw_voxels, name="voxel_host_5d", input_value=voxel_host_5d, tint=(0.02, 0.38, 0.11))

window(draw_voxels, name="voxel_host_flow", input_value=voxel_host_flow, tint=(0.02, 0.38, 0.11))
