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
from src.lsd.gl_gui.shader_func import shader_func
from src.lsd.gl_gui.text_texture import bake_text, bake_texts
from src.lsd.gl_gui.toggles import SwooshMode
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any, draw_tab_bar, draw_dropdown
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.toggles import Swoosh

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

    // ── shadow catcher: the plane the box rests on (z = -volume_scale.z)
    // is itself INVISIBLE — its only contribution is the shadow the volume
    // casts onto it, composited as a darkening with alpha = blocked light.
    // One-sided (backface-culled analytically): a hit only counts for rays
    // striking the TOP face — from underneath there's no shadow at all.
    float plane_t = -1.0;
    float plane_a = 0.0;
    // The caught shadow darkens toward the UI's compositor shadow color
    // (Toggles.shadow_color rides in as shadow_tint) — the same slight
    // blue the rest of the studio's shadows carry, instead of pure black.
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
            vec3 c = pow(texture(lut, vm.x).rgb, vec3(2.2));
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
                c *= pow(light_tint, vec3(2.2)) * light_brightness * shade;
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
    // Output gamma on the finished 2-D image, folded into the sRGB encode:
    // gamma 1.0 = pure sRGB encode (brightest, colorimetrically "correct"),
    // 2.2 = raw linear out (darkest). The default sits between — the encode
    // alone reads too bright/washed against the studio's dark UI.
    // Dither ±half an 8-bit quantum (interleaved gradient noise, Jimenez):
    // the RGBA8 target snaps smooth dark gradients — the plane's exponential
    // falloff especially, post-gamma — into visible contour bands; sub-LSB
    // noise makes adjacent quanta average out instead. Alpha too: the fade
    // is largely an ALPHA ramp composited over the UI.
    float dither = (fract(52.9829189 * fract(
        dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715)))) - 0.5) / 255.0;
    FragColor = vec4(pow(acc.rgb, vec3(gamma / 2.2)) + dither, acc.a + dither);
}
"""


@shader_func(fragment=VOXEL_FRAG)
def voxel_pass(gl_state: GLState = None, tilt=0.5, spin=0.8, zoom=3.4,
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
    // The voxel ray gen, inverted (same math as _axis_edges): perspective
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


class TensorDim(int):
    """A tensor dim index that is still an int everywhere it matters
    (indexing, comparisons, arithmetic, `int()`, pickling) but carries its own
    TYPE, so melty routes it to its own renderer instead of the plain int one
    — a dim picker rather than a number field.

    Values only stay TensorDim if whatever writes them keeps the type: a
    renderer registered `@render_func(is_default_for=TensorDim)` should return
    TensorDim(...), otherwise the first edit stores a plain int and the row
    falls back to the int renderer."""

    __slots__ = ()

    def __repr__(self):
        return f"TensorDim({int(self)})"


class TensorDims(tuple):
    """A SET of tensor dim indices (`mean_dims`) — tuple everywhere it
    matters, but typed so it routes to the same dim picker as TensorDim
    (multi-select tabs). A tuple needs SOME type to route by; this is the
    minimal one, and the renderer is shared."""

    __slots__ = ()

    def __repr__(self):
        return f"TensorDims({tuple(int(v) for v in self)})"


class Lut(str):
    """A LUT NAME that is still a str everywhere it matters (dict keys,
    comparisons, GLSL host lookups) but carries its own TYPE, so melty routes
    it to its own renderer — a dropdown of the available LUTs rather than a
    text field. Same contract as TensorDim: the renderer must return
    Lut(...) or the first edit stores a plain str and the row falls back to
    the generic str renderer."""

    __slots__ = ()

    def __repr__(self):
        return f"Lut({str(self)!r})"


@render_func(is_default_for="Lut", show_bg=False, is_tree=False,
             header_same_line=True, with_header=draw_header)
def draw_lut(input_value=None, draw_state=None, unique=0, **kwargs):
    """THE lut picker — a dropdown of the LUT names the lut host knows about
    (live host dict when it's up, baked LUTS otherwise), shared by every
    lut-typed param. Returns Lut(...) so the value keeps routing here."""
    host_val = getattr(globals().get("lut_host"), "input_value", None)
    luts = host_val if isinstance(host_val, dict) and host_val else LUTS
    names = [str(k) for k in luts]
    current = str(input_value) if input_value else "jet"
    changed, picked = draw_dropdown(
        current, collection={n: n for n in names},
        name=f"lut##{unique}", show_header=False, width=140)
    if changed and picked:
        return True, Lut(picked)
    return False, input_value


def _row_collection(draw_state, kwargs):
    """The collection this row renders in (the params panel's locate_params
    proxy) — sibling params like dim_names / x_dim live there."""
    col = kwargs.get("collection")
    if not isinstance(col, dict):
        col = getattr(draw_state, "_collection", None)
    return col if isinstance(col, dict) else None


def _collection_dim_labels(col):
    """Dim-name labels from the collection's `dim_names` entry; [] when no
    names are in reach."""
    raw_names = col.get("dim_names", ()) if col is not None else ()
    return [_clean_dim_name(x, i) for i, x in enumerate(raw_names or ())]


@render_func(is_default_for=("TensorDim", "TensorDims"), show_bg=False, is_tree=False,
             header_same_line=True, with_header=draw_header)
def draw_tensor_dim(input_value=None, draw_state=None, unique=0, **kwargs):
    """THE dim picker — one TAB per dim NAME instead of a bare number field,
    shared by every dim-typed param. A TensorDim renders single-select with
    a leading "off" tab that maps to -1 (unset: sort disabled, nf/axis dims
    derived), so sort_dim and nf_chop/nf_along reuse it as-is. A TensorDims
    renders the same tabs multi-select (mean_dims). The names come
    from the sibling `dim_names` entry of the collection this row renders
    in (the params panel's locate_params proxy); with no names in reach it
    falls back to a plain int edit. Returns the SAME type it was given so
    the value keeps routing here (a plain int/tuple would drop back to the
    generic renderer next frame)."""
    multi = isinstance(input_value, tuple)
    col = _row_collection(draw_state, kwargs)
    labels = _collection_dim_labels(col)
    if not labels:
        if multi:
            imgui.text(f"dims: {tuple(int(v) for v in input_value)}")
            return False, input_value
        changed, v = RenderFuncs.draw_int(
            0 if input_value is None else int(input_value),
            name=f"dim##{unique}")
        if changed and v is not None:
            return True, TensorDim(int(v))
        return False, input_value
    n = len(labels)
    if multi:
        cur = [int(v) for v in input_value if isinstance(v, int)]
        changed, selected = draw_tab_bar(
            [d for d in cur if 0 <= d < n],
            collection=list(range(n)), names=labels,
            name=f"dims##{unique}", wrap=True, z_offset=-1, rounding=5,
            as_toggles=True, bg_offset=-3)
        if changed:
            return True, TensorDims(sorted(int(s) for s in selected))
        return False, input_value
    # Single-select: a leading "off" tab maps to -1 (unset - sort disabled,
    # nf/axis dims derived), so unsetting doesn't rely on double-click.
    cur = int(input_value) if input_value is not None else -1
    if not (0 <= cur < n):
        cur = -1
    changed, selected = draw_tab_bar(
        [cur], collection=[-1] + list(range(n)), names=["off"] + labels,
        name=f"dims##{unique}", wrap=True, z_offset=-1, rounding=5,
        as_toggles=False, bg_offset=-3)
    if changed:
        return True, TensorDim(int(selected[0]) if selected else -1)
    return False, input_value


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
    try:
        v = int(v)
    except (TypeError, ValueError):
        # params are user-editable from the panel and from source, so a dim
        # can arrive as anything at all. Unusable = unset.
        return None
    return v if 0 <= v < n else None


def _resolve_axes(shape, dim_names, x_dim, y_dim, z_dim):
    """(z, y, x) display dims for a shape: dims by index or NAME, None
    derives the default (last three dims → z/y/x, like the old viewer).

    ALWAYS returns three DISTINCT in-range dims (for n >= 3) — the params are
    editable from the panel and from source, so two axes can name the same dim
    or a garbage one. A dim already claimed by an earlier axis is treated as
    unset and re-derived, which keeps the slicing downstream well-formed
    (duplicate picks collapse the sliced volume to 2 dims and the permute
    blows up). z wins over y wins over x, so the LAST axis you retarget onto a
    taken dim is the one that moves."""
    n = len(shape)
    resolved = []
    taken = set()
    for cur in (z_dim, y_dim, x_dim):
        d = _resolve_dim(dim_names, cur, n)
        if d is None or d in taken:
            resolved.append(None)       # unset, or a duplicate: re-derive
        else:
            taken.add(d)
            resolved.append(d)

    def fill(default):
        # The default dim, else the nearest free one scanning down then up.
        # (The old walk stopped at 0 and could hand back a taken 0.)
        if default not in taken:
            return default
        for d in range(default - 1, -1, -1):
            if d not in taken:
                return d
        for d in range(default + 1, n):
            if d not in taken:
                return d
        return default                  # n < 3: nothing free left

    for i, default in enumerate((max(0, n - 3), max(0, n - 2), max(0, n - 1))):
        if resolved[i] is None:
            resolved[i] = fill(default)
            taken.add(resolved[i])
    return tuple(resolved)


def slice_volume(t, dim_names=(), x_dim=None, y_dim=None, z_dim=None,
                 slices=(), mean_dims=(), sort_dim=-1, normalize=False,
                 nf_on=False, nf_chop=None, nf_along=None, nf_chunk=128):
    """tensor → (depth, height, width) display volume, PURE: every choice
    arrives as an argument (the draw_voxels params), nothing is stored.
    Unmapped dims pin to their `slices` index (missing entries → 0) or
    average when listed in mean_dims (keepdim, then pinned at 0); a
    DISPLAYED dim in mean_dims keeps its extent with the mean broadcast
    along it (the value repeats across the plot); sort
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
    mean_set = {int(d) for d in (mean_dims or ()) if 0 <= int(d) < n}
    for d in mean_set:
        m = t.mean(dim=d, keepdim=True)
        # A DISPLAYED dim keeps its extent with the mean BROADCAST along it
        # (the same value repeats across the plot - visual convenience);
        # an unmapped dim stays collapsed and pins at 0 below.
        t = m.expand(t.shape) if d in picked else m
    def _pin(d):
        # A pinned index from `slices` can be anything the panel/source lets;
        # clamp into range instead of letting torch raise (or silently wrap on
        # a negative).
        try:
            v = int(slices[d]) if d < len(slices) else 0
        except (TypeError, ValueError):
            v = 0
        return max(0, min(v, shape[d] - 1))

    index = tuple(
        slice(None) if d in picked
        else (0 if d in mean_set else _pin(d))
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


# Near-plane depth for the axis box's Python-side projection - the old
# per-corner behind-camera cutoff; edges now CLIP here instead of vanishing.
_AXIS_NEAR = 0.05


def _axis_edges(tilt, spin, zoom, aspect, width, height,
                scale=(1.0, 1.0, 1.0), pan=(0.0, 0.0, 0.0), ortho=False):
    """The volume box's silhouette edges, each clipped to its VISIBLE span —
    the Python mirror of the shader's orbit camera (extents = `scale`, the
    voxel-count-proportional volume_scale), so lines and labels land exactly
    on the rendered edges.

    Face visibility is decided in WORLD space: front-facing iff the eye is
    outside the face's plane (ortho: iff the view direction looks against
    its normal) — never from projected corner geometry. The old test used
    the projected quad's shoelace area against an absolute px² threshold and
    needed all four corners in front of the near plane; on a wide-skinny
    volume (a (1, 96, 4096) slab is a 1.0 × 0.023 × 0.02 box) any zoom that
    makes the data readable puts the camera INSIDE the box's long span, the
    near corners fell to the behind-camera cutoff, and every face and edge
    touching them vanished — the axis hid exactly when you zoomed in to
    read it, and orbiting changed which corners died.

    An edge is on the silhouette iff exactly one adjacent face is front-
    facing (edge-on faces count as back-facing, so the camera-facing square
    contributes all four sides in an exact top view); eye inside the box —
    no face front-facing — keeps all 12, so the box stays outlined and
    labeled from the inside. Each edge then clips against the near plane in
    camera space and the image rect in screen space (screen params map back
    through the perspective-correct 1/z interpolation), so a partially-
    behind or partially-offscreen axis keeps its on-screen portion.

    Returns [(a, b, pa, pb, t0, t1, z0, z1)]: the ±1 corner sign tuples, the
    screen endpoints of the visible span, its world-param range over a→b
    (exactly 0.0 / 1.0 when that end is the true corner), and the camera
    depths at the visible ends (equal under ortho) for perspective-correct
    tick placement downstream."""
    ct = math.cos(tilt)
    fwd = -np.array([math.cos(spin) * ct, math.sin(spin) * ct, math.sin(tilt)])
    right = np.array([-math.sin(spin), math.cos(spin), 0.0])
    up = np.cross(right, fwd)
    eye = np.asarray(pan, np.float64) - fwd * zoom
    sc = np.asarray(scale, np.float64)
    # Inverse of the shader's ray gen (rd ∝ fwd*1.7 + right*ndc.x + up*ndc.y,
    # ndc.x pre-scaled by aspect): ndc = 1.7 * cam_xy / cam_z, x /= aspect.
    # Ortho divides by the fixed frame half-size (zoom/1.7) instead of the
    # point's own depth.
    ortho_denom = max(zoom, 1e-6) / 1.7

    def to_screen(cx, cy, cz):
        denom = ortho_denom if ortho else cz / 1.7
        ndx = (cx / denom) / aspect
        ndy = cy / denom
        return ((ndx * 0.5 + 0.5) * width, (1.0 - (ndy * 0.5 + 0.5)) * height)

    def face_visible(k, s):
        # The box is centered on the ORIGIN (pan is the camera target).
        return (-s * fwd[k] > 1e-12) if ortho else (s * eye[k] > sc[k])

    vis = {(k, s): face_visible(k, s) for k in range(3) for s in (-1, 1)}
    any_vis = any(vis.values())

    def clip(a, b):
        # World → camera space (right/up/depth) at both corners.
        da = np.asarray(a, np.float64) * sc - eye
        db = np.asarray(b, np.float64) * sc - eye
        az, bz = float(da @ fwd), float(db @ fwd)
        if az < _AXIS_NEAR and bz < _AXIS_NEAR:
            return None
        t0, t1 = 0.0, 1.0
        if az < _AXIS_NEAR:
            t0 = (_AXIS_NEAR - az) / (bz - az)
        elif bz < _AXIS_NEAR:
            t1 = (_AXIS_NEAR - az) / (bz - az)
        ax, ay = float(da @ right), float(da @ up)
        bx, by = float(db @ right), float(db @ up)
        cx0, cy0, cz0 = ax + (bx - ax) * t0, ay + (by - ay) * t0, az + (bz - az) * t0
        cx1, cy1, cz1 = ax + (bx - ax) * t1, ay + (by - ay) * t1, az + (bz - az) * t1
        pa, pb = to_screen(cx0, cy0, cz0), to_screen(cx1, cy1, cz1)
        # Liang-Barsky against the image rect.
        s0, s1 = 0.0, 1.0
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        for p, q in ((-dx, pa[0]), (dx, width - pa[0]),
                     (-dy, pa[1]), (dy, height - pa[1])):
            if abs(p) < 1e-9:
                if q < 0.0:
                    return None
                continue
            r = q / p
            if p < 0.0:
                if r > s1:
                    return None
                if r > s0:
                    s0 = r
            else:
                if r < s0:
                    return None
                if r < s1:
                    s1 = r

        def world_u(s):
            # Screen param → world param over the near-clipped span: 1/z
            # interpolates linearly in screen space, so u = s-z0/(z1+s-(z0-z1));
            # ortho z is affine (u = s).
            return s if ortho else s * cz0 / (cz1 + s * (cz0 - cz1))

        u0, u1 = world_u(s0), world_u(s1)
        return (a, b,
                (pa[0] + dx * s0, pa[1] + dy * s0),
                (pa[0] + dx * s1, pa[1] + dy * s1),
                t0 + (t1 - t0) * u0, t0 + (t1 - t0) * u1,
                cz0 + (cz1 - cz0) * u0, cz0 + (cz1 - cz0) * u1)

    edges = []
    for k in range(3):
        i, j = (k + 1) % 3, (k + 2) % 3
        for si in (-1, 1):
            for sj in (-1, 1):
                if any_vis and vis[(i, si)] == vis[(j, sj)]:
                    continue  # the edge's two faces agree → not visible
                a, b = [0, 0, 0], [0, 0, 0]
                a[k], b[k] = -1, 1
                a[i] = b[i] = si
                a[j] = b[j] = sj
                rec = clip(tuple(a), tuple(b))
                if rec is not None:
                    edges.append(rec)
    return edges


# Outline edges draw shortened by this many screen px at each true-corner
# end (the original fixed_shorten look); clipped edges compresses into the
# same span so the end labels align with the visible span ends.
_EDGE_SHORTEN_PX = 14.0


def _draw_axis_lines(draw_list, img_pos, edges):
    """The visible silhouette spans as thin imgui lines, shortened near true
    CORNERS (the original fixed_shorten look); a clipped end (near plane /
    screen border) runs to its cut, since the edge continues past it. Labels
    are NOT drawn here any more — they're textured billboards in the voxel
    FBO (_billboard_specs + _render_label_billboards), so they live in the
    3-D scene."""
    line_col = imgui.get_color_u32_rgba(0.9, 0.9, 1.0, 0.5)
    for a, b, pa, pb, t0, t1, z0, z1 in edges:
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        length = math.hypot(dx, dy)
        if length < 0.5:
            continue   # zero-area edge: nothing to draw, skip the div
        # Short edges shorten proportionally instead of vanishing - the
        # outline only ever skips sub-2px degenerates.
        shorten = min(_EDGE_SHORTEN_PX, length * 0.25)
        sh_a = shorten if t0 == 0.0 else 0.0
        sh_b = shorten if t1 == 1.0 else 0.0
        ux, uy = dx / length, dy / length
        draw_list.add_line(img_pos[0] + pa[0] + ux * sh_a, img_pos[1] + pa[1] + uy * sh_a,
                           img_pos[0] + pb[0] - ux * sh_b, img_pos[1] + pb[1] - uy * sh_b,
                           line_col, 1.0)


def _tick_values(lo, hi, px_per_idx, num_px, spacing=1.6):
    """Integer tick positions for the VISIBLE [lo, hi] index span of one
    edge: EVERY integer when the labels fit, else the smallest 1-2-5·10ᵏ
    step whose rotated labels keep clear of each other (footprint ≈ the
    widest label's text width along the edge, in projected PIXELS — so
    zooming in fits more ticks). `spacing` is the minimum gap between tick
    centers in widest-label widths. The span's end values always show —
    0/max on an unclipped edge, the boundary indices (a scrollbar-like
    readout of where you are along the axis) on a clipped one; interior
    step multiples stay GLOBAL multiples (they don't jitter as the clip
    end moves) and yield when they would crowd an end."""
    e0, e1 = int(math.ceil(lo - 1e-9)), int(math.floor(hi + 1e-9))
    if e1 < e0:
        return []
    if e1 == e0:
        return [e0]
    widest = max(1, len(str(e1))) * 0.62 * num_px  # ~max glyph aspect
    min_px = widest * spacing
    step, k = None, 1
    while step is None and k <= 10 ** 9:
        for s in (1, 2, 5):
            if s * k * px_per_idx >= min_px:
                step = s * k
                break
        else:
            k *= 10
    if step is None or step > e1 - e0:
        return [e0, e1]
    ticks = [e0]
    m = int(math.ceil((e0 + 0.6 * step) / step)) * step
    while m <= e1 - 0.6 * step:
        ticks.append(m)
        m += step
    ticks.append(e1)
    return ticks


def _billboard_specs(edges, axis_display, volume_scale,
                     name_size=24.0, name_padding=34.0, name_opacity=1.0,
                     num_size=16.0, num_padding=11.0, num_opacity=1.0,
                     num_spacing=1.6, num_angle=0.0):
    """[(text, anchor3, u_dir3, v_dir3, out_dir3, px_h, off_px, alpha)] for
    every visible silhouette span — the dim name beside the SPAN's midpoint
    (always on screen, unlike a clipped edge's full midpoint, which can sit
    behind the camera) plus integer ticks (_tick_values) at their TRUE
    positions along the edge; a clipped edge labels only its on-screen index
    range, so a zoomed-in wide volume reads like a scrolled ruler. Anchors
    are volume-box WORLD points ON the edge. All metrics are screen PIXELS,
    held at any zoom (the shader depth-converts at each anchor): `*_size` is
    the label height (0 hides that label type), `*_padding` the GAP between
    the line and the label's near edge (independent of size), `*_opacity`
    the tint alpha. u runs along the edge and v outward from the box
    ("angled perpendicular to the line"); both are flipped for readability —
    the up-axis flips when the quad shows its back (un-mirrors without
    reversing the reading direction), then a 180° spin makes text read
    left-to-right, or bottom-to-top on near-vertical edges. The offset
    always rides the UNFLIPPED outward direction, so labels never land
    inside the box. Nothing hides by projected size any more — the
    face-visibility silhouette already culls truly invisible edges, and
    _tick_values degrades to just the end values on short edges."""
    name_off = name_padding + name_size * 0.5  # anchor -> label CENTER
    num_off = num_padding + num_size * 0.5
    # tick label slant (optional, not the label plane - matplotlib-style)
    ca, sa = math.cos(math.radians(num_angle)), math.sin(math.radians(num_angle))
    pts = [p for e in edges for p in (e[2], e[3])]
    if not pts:
        return []
    scx = sum(p[0] for p in pts) / len(pts)  # silhouette's screen centroid
    scy = sum(p[1] for p in pts) / len(pts)

    specs = []
    for a, b, pa, pb, t0, t1, z0, z1 in edges:
        k = next(i for i in range(3) if a[i] != b[i])  # the axis it runs along
        if a[k] > b[k]:  # a = the texcoord-0 end (visible span flips with it)
            a, b = b, a
            pa, pb = pb, pa
            t0, t1 = 1.0 - t1, 1.0 - t0
            z0, z1 = z1, z0
        px_len = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        if px_len < 0.5:
            continue   # zero-area edge: direction math requires a length
        name, size = axis_display[k]
        a3 = tuple(a[i] * volume_scale[i] for i in range(3))
        b3 = tuple(b[i] * volume_scale[i] for i in range(3))
        length = math.sqrt(sum((b3[i] - a3[i]) ** 2 for i in range(3))) or 1.0
        w = tuple((b3[i] - a3[i]) / length for i in range(3))  # a → b, for placement
        mid_full = tuple((a3[i] + b3[i]) * 0.5 for i in range(3))
        m_len = math.sqrt(sum(c * c for c in mid_full)) or 1.0
        out = tuple(c / m_len for c in mid_full)  # outward, ⊥ the edge (mid-w = 0)
        # World endpoints + midpoint of the VISIBLE span (the label anchors).
        va = tuple(a3[i] + (b3[i] - a3[i]) * t0 for i in range(3))
        vb = tuple(a3[i] + (b3[i] - a3[i]) * t1 for i in range(3))
        mid = tuple((va[i] + vb[i]) * 0.5 for i in range(3))

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
        i0, i1 = t0 * size, t1 * size  # the visible index range
        if num_size > 0 and size > 0 and i1 - i0 > 1e-9:
            # Ticks compress into the DRAWN line span (true-corner ends draw
            # shortened; clipped ends need to be cut), and the end labels
            # sit at the visible line ends. Screen px and world params go
            # through the perspective-correct 1/z map (affine when z0 == z1,
            # i.e. ortho or an edge parallel to the screen).
            def u_of(s):
                return s if z0 == z1 else s * z0 / (z1 + s * (z0 - z1))

            def s_of(up):
                return up if z0 == z1 else up * z1 / (z0 + up * (z1 - z0))

            inset_px = min(_EDGE_SHORTEN_PX, px_len * 0.25)
            u_lo = u_of(inset_px / px_len if t0 == 0.0 else 0.0)
            u_hi = u_of(1.0 - (inset_px / px_len if t1 == 1.0 else 0.0))
            if num_angle:
                ut = tuple(ca * u[i] + sa * v[i] for i in range(3))
                vt = tuple(ca * v[i] - sa * u[i] for i in range(3))
            else:
                ut, vt = u, v
            # Step from the span's AVERAGE screen density; perspective
            # compresses the far end, so greedily skip interior ticks whose
            # SCREEN positions crowd the previous one or the end label.
            ticks = _tick_values(i0, i1, px_len / (i1 - i0), num_size,
                                 num_spacing)  # [] on an integer-free sliver
            min_gap = max(1, len(str(ticks[-1] if ticks else 0))) \
                      * 0.62 * num_size * num_spacing
            placed = []
            for n, idx in enumerate(ticks):
                up = u_lo + (u_hi - u_lo) * ((idx - i0) / (i1 - i0))
                s_px = s_of(up) * px_len
                if 0 < n < len(ticks) - 1 and placed and (
                        s_px - placed[-1] < min_gap
                        or s_of(u_hi) * px_len - s_px < min_gap):
                    continue
                placed.append(s_px)
                p = tuple(va[i] + (vb[i] - va[i]) * up for i in range(3))
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


@render_func(is_default_for=("GLTexture", "Tensor"), show_bg=True, selectable=True,
             auto_resize=False, min_width=269, with_header=draw_header, tint=(0.126, 0.267, 0.083),
             bg_offset=0, min_height=293, disable_scroll=True, use_cache=True)
def draw_voxels(input_value=None, gl_state: GLState = None, selectable=False,
                draw_state=None,
                # ── camera + shading: cam_* names dodge the legacy DrawState
                # zoom/brightness/contrast fields (name-colliding params are
                # excluded from auto-state). Gestures/panel write
                # draw_state.<name>; diverged values persist. ──
                tilt=0.283, spin=0.724, cam_zoom=3.4,
                # [tint=(0.084, 0.472, 0.148, 1.0)]
                pan_x=0.0, pan_y=0.0, pan_z=0.0, ortho=False,
                cam_brightness=1.332, cam_contrast=1.0,
                # density = the old densityScale (haze gain over the opacity
                # gate); threshold = the old opacityThreshold (higher → lower
                # gate → more opaque)
                density=0.7, threshold=0.297, centered=False,
                nearest=True, lut=Lut("jet"), step_size=0.0005, max_steps=4096,
                # ── shadow catcher: the invisible plane the box rests on -
                # it renders nothing but the volume's cast shadow (one-sided:
                # no shadow from below). shadow_opacity scales how dark the
                # caught shadow composites; shadow_softness scales the
                # screen-space penumbra blur (radius grows with occluder
                # height) - 0 = hard edge, bigger = wider penumbra. ──
                draw_plane=True, shadow_opacity=1.0, shadow_softness=0.15,
                # ── lighting: draw_shading lights the floor (per-s., the
                # raymarched cast shadow) and the volume (gradient-normal
                # Lambert); self_shading adds the per-sample transmittance
                # march inside the volume - the expensive tier. ambient_light
                # is the shadow floor: how much light survives everywhere.
                # shading_strength scales how much the volume's cast normal
                # may darken its LUT color (0 = shading off, plane still
                # catches). ──
                draw_shading=True, self_shading=True,
                light_pos=(50.0, -50.0, 200.0), light_tint=(1.0, 1.0, 1.0),
                light_brightness=1.622, ambient_light=0.3, shading_strength=0.7,
                # ── axis mapping: dims by index OR NAME. The first three dims
                # by default; None still means "derive" (last three → z/y/x)
                # for anything that clears one. ──
                dim_names=("layer", "batch", "token", "feature"),
                x_dim=TensorDim(0), y_dim=TensorDim(2), z_dim=TensorDim(2),
                slices=(),
                mean_dims=TensorDims(()), sort_dim=TensorDim(-1),
                normalize=False, nf_on=False, nf_chop=TensorDim(-1),
                nf_along=TensorDim(-1), nf_chunk=128,
                # ── volume furniture (screen px) ──
                name_size=17.0, name_padding=30.1, name_opacity=1.1,
                num_size=17.1, num_padding=5.5, num_opacity=0.8,
                num_spacing=1.0, num_angle=0.0, z_offset=1,
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

    # ── slice sliders: >3-dim tensors get one slider per UNMAPPED dim (not
    # displayed, not averaged, extent > 1) along the bottom of the view to
    # choose which slice is pinned. GLTexture inputs arrive pre-sliced
    # (mapping is None) - nothing to scrub. ─────────────────────────────
    slider_dims = []
    if mapping is not None and len(source_shape) > 3:
        slider_dims = [d for d in range(len(source_shape))
                       if d not in mapping and d not in mean_dims
                       and source_shape[d] > 1]

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
    # Vertical reserve: the actual header height (0 when hidden) + a little
    # slack for the controls/status margin (the old hardcoded 30 was the
    # 23px header + some slack).
    _reserve = int(draw_state.header_height or 0) + 7
    if draw_state.closable:
        height = max(100, draw_state.height - _reserve)
    else:
        # Nested in a parent's flow, draw_state.height is only trustworthy
        # when something authoritative wrote it - a resize callback ("initial
        # size..."), a passed height kwarg, fill_height. The auto_resize
        # measurement path ("... item_rect[1]") is what this view drew last
        # frame - sizing the image from it is a feedback loop that sustains
        # any spike forever (image = height-30 → measures back ≈ height →
        # committed again); fall back to the design height (min_height,
        # overridable per call site) for that case, and a bad committed
        # height self-heals on the next live render.
        _h_src = str(draw_state._source.get("height", ""))
        if draw_state.height and "item_rect" not in _h_src:
            height = max(100, int(draw_state.height) - _reserve)
        else:
            height = max(100, int(draw_state.min_height or 293) - _reserve)
    if slider_dims:
        # The sliders live INSIDE the view's box - give them their rows by
        # shrinking the image, not by growing past the window.
        height = max(100, height - int(imgui.get_frame_height_with_spacing())
                     * len(slider_dims))

    # ── in-flight locate values: a locate_* write to a SLOW source (e.g. a
    # `# [cam_brightness=...]` comment) is deferred during drags and lands
    # multi-frame after; until then the injected kwarg is stale. Re-read any
    # camera param with a value set through locate_* (which also clears the
    # entry once the trip lands) so drags accumulate off the latest state.
    # _sa_precise rides the same way: a low-precision source has a 4dp
    # rounding, so locate_* serves the full-precision overlay over it. ──
    _pending = getattr(draw_state, "_sa_pending", None) or {}
    _precise = getattr(draw_state, "_sa_precise", None) or {}
    _in_flight = _pending.keys() | _precise.keys()
    if _in_flight:
        def _fly(n, cur):
            if n not in _in_flight:
                return cur
            v = getattr(draw_state, "locate_" + n)
            return cur if v is None else v
        tilt, spin, cam_zoom = _fly("tilt", tilt), _fly("spin", spin), _fly("cam_zoom", cam_zoom)
        pan_x, pan_y, pan_z = _fly("pan_x", pan_x), _fly("pan_y", pan_y), _fly("pan_z", pan_z)
        cam_brightness = _fly("cam_brightness", cam_brightness)
        cam_contrast = _fly("cam_contrast", cam_contrast)
        ortho = _fly("ortho", ortho)

    # ── gestures → draw_state params (auto-state: the caller diverges the
    # param so it persists; events are hover-routed wrapper kwargs) ──────
    if middle_mouse_drag is not None:
        # Ortable chirality, latched per GESTURE: upside down (cos(tilt)<0,
        # world-up pointing down the screen) a rightward move must spin the
        # other way to keep tracking the cursor. Latching at drag start keeps
        # the direction stable when a drag tilts across the pole mid-gesture;
        # the latch clears on release so the next drag re-reads orientation.
        spin_sign = getattr(draw_state, "_orbit_spin_sign", None)
        if spin_sign is None:
            spin_sign = -1.0 if math.cos(tilt) < 0.0 else 1.0
            draw_state._orbit_spin_sign = spin_sign
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
            draw_state.locate_pan_x = pan_x
            draw_state.locate_pan_y = pan_y
            draw_state.locate_pan_z = pan_z
        elif middle_mouse_drag.ctrl:
            # the old viewer's ctrl-drag: vertical = dolly zoom, horizontal
            # still orbits.
            cam_zoom = min(137.6, max(0.0, cam_zoom * math.exp(0.005 * middle_mouse_drag.dy)))
            spin -= middle_mouse_drag.dx * 0.008 * spin_sign
            draw_state.locate_cam_zoom = cam_zoom
            draw_state.locate_spin = spin
        else:
            spin -= middle_mouse_drag.dx * 0.008 * spin_sign
            # Tilt is UNRESTRICTED - orbit straight over the poles and keep
            # going. remainder() re-wraps into [-pi, pi] (same orientation,
            # cos/sin-continuous) so the stored angle never runs away.
            tilt = math.remainder(tilt + middle_mouse_drag.dy * 0.008, math.tau)
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
    else:
        draw_state._orbit_spin_sign = None
    if double_right_mouse_drag is not None:
        # the old viewer's shading drag: now on a DOUBLE right-drag (the 2nd
        # press of a double right-click, held and dragged): horizontal =
        # brightness, vertical = contrast (up to increase). The plain right-
        # click stays reserved for the context menu.
        cam_brightness = min(4.0, max(0.0, cam_brightness + double_right_mouse_drag.dx * 0.01))
        cam_contrast = min(5.0, max(0.01, cam_contrast - double_right_mouse_drag.dy * 0.008))
        draw_state.locate_cam_brightness = cam_brightness
        draw_state.locate_cam_contrast = cam_contrast
    if scroll_y_changed is not None:
        cam_zoom = min(135.5, max(0.0, cam_zoom * math.exp(-0.23 * scroll_y_changed.value)))
        draw_state.locate_cam_zoom = cam_zoom

    # ── Blender-style numpad views (hover-routed key events): 7/1/3 = top/
    # front/right, ctrl = the opposite side, 5 = ortho toggle, / (either
    # slash, or numpad . like the old viewer) = recenter the pan on the
    # origin. A focused text editor owns the keyboard, so keys are ignored
    # while one is active. ────────────────────────────────────────────────
    from src.lsd.gl_gui.melty import Melty
    if Melty.text_focused_ds is None:
        if kp_7_pressed is not None:
            spin, tilt = -HALF_PI, (-HALF_PI if kp_7_pressed.ctrl else HALF_PI)
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
        if kp_1_pressed is not None:
            spin, tilt = (HALF_PI if kp_1_pressed.ctrl else -HALF_PI), 0.0
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
        if kp_3_pressed is not None:
            spin, tilt = (math.pi if kp_3_pressed.ctrl else 0.0), 0.0
            draw_state.locate_spin = spin
            draw_state.locate_tilt = tilt
        if kp_5_pressed is not None:
            ortho = not ortho
            draw_state.locate_ortho = ortho
        if (slash_pressed is not None or kp_divide_pressed is not None
                or kp_decimal_pressed is not None):
            pan_x = pan_y = pan_z = 0.0
            draw_state.locate_pan_x = 0.0
            draw_state.locate_pan_y = 0.0
            draw_state.locate_pan_z = 0.0

    # ── plane side latch: when the view is upside-down the target plane
    # belongs on the box's OTHER face (the floor light stays fixed in world
    # space; only the catcher's marches see a mirrored box so the flipped
    # floor still catches a shadow). Same idea as _orbit_spin_sign: the side
    # only re-reads orientation while NO drag is active - mid-drag the floor
    # holds put, and the flip lands when you let go.
    if middle_mouse_drag is None:
        draw_state._plane_side = -1.0 if math.cos(tilt) < 0.0 else 1.0
    plane_side = getattr(draw_state, "_plane_side", None) or (
        -1.0 if math.cos(tilt) < 0.0 else 1.0)

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

    # ── axis coordinate positions: visible silhouette spans via the Python
    # mirror of the shader camera, computed BEFORE the GL pass - the label
    # billboards render INTO the voxel FBO with the volume's own camera ────
    axis_edges = None
    if axis_display:
        axis_edges = _axis_edges(tilt, spin, cam_zoom,
                                 width / height, width, height,
                                 scale=volume_scale,
                                 pan=(pan_x, pan_y, pan_z),
                                 ortho=ortho)

    # ── GL pass: every resource tracked + lifecycle-managed by gl_state ──
    fb = gl_state.fbo("target", width, height)
    depth_was_on = gl.glIsEnabled(gl.GL_DEPTH_TEST)
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        # Labels FIRST, so the volume pass composites OVER them - the floor
        # shadow (and the floor itself) darkens the labels beneath it
        # instead of the labels floating on top of the shadow.
        if axis_edges and (name_size > 0 or num_size > 0):
            # Labels as in-scene textured quads. A bake/render hiccup should
            # not take down the view (or trigger the hotswap auto-revert) -
            # log it and keep rendering the volume.
            global _LABEL_WARNED
            try:
                specs = _billboard_specs(axis_edges, axis_display,
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
        # The volume shader outputs PREMULTIPLIED alpha (the shader
        # composites with (1-a) weights), so it layers over the labels with
        # ONE / ONE_MINUS_SRC_ALPHA - over label-free (transparent) pixels
        # this is bit-identical to the old unblended write.
        _blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_FUNC_ADD)
        gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA,
                               gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        # int() so a UI-dragged float never flips the uniform's inferred
        # GLSL type (the loop bound must be an int).
        voxel_pass(gl_state, volume=tex, volume_lin=tex, lut=lut_tex,
                   aspect=width / height,
                   volume_scale=volume_scale, step_size=step_size,
                   max_steps=int(max_steps), density=density,
                   threshold=threshold, tilt=tilt, spin=spin, zoom=cam_zoom,
                   pan_x=pan_x, pan_y=pan_y, pan_z=pan_z, ortho=ortho,
                   brightness=cam_brightness, contrast=cam_contrast,
                   gamma=float(Toggles.Voxels.gamma), centered=centered,
                   draw_plane=bool(draw_plane),
                   shadow_opacity=float(shadow_opacity),
                   shadow_softness=float(shadow_softness),
                   # The studio-wide compositor shadow color: the floor
                   # shadow matches whatever the UI's shadows are tinted.
                   shadow_tint=tuple(float(c) for c in Toggles.shadow_color),
                   draw_shading=bool(draw_shading),
                   self_shading=bool(self_shading),
                   plane_side=float(plane_side),
                   light_pos=tuple(float(c) for c in light_pos),
                   light_tint=tuple(float(c) for c in light_tint),
                   light_brightness=float(light_brightness),
                   ambient_light=float(ambient_light),
                   shading_strength=float(shading_strength))
        if not _blend_was:
            gl.glDisable(gl.GL_BLEND)
    if depth_was_on:
        gl.glEnable(gl.GL_DEPTH_TEST)

    img_pos = imgui.get_cursor_screen_pos()
    imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))

    # ── the outline stays 2-D imgui (crisp 1px outline over the volume) ────
    if axis_edges:
        _draw_axis_lines(imgui.get_window_draw_list(), img_pos, axis_edges)

    # ── the slice sliders, one slider per unmapped dim under the volume. An
    # edit writes the full-length slices tuple to draw_state (auto-state:
    # it diverges the param, persists, and resets slice_volume's version so
    # the volume re-slices + re-uploads on the next frame). ──────────────
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

    # ── ALL controls live in a satellite panel opening to the RIGHT of
    # the window: the renderer's full params, rendered automatically -
    # draw_state.locate_params is a live dict over this signature, each row
    # reads its framework-resolved value and an edit goes through
    # set_anywhere (draw_state by default; a higher-pri source like an
    # annotation comment claims the write when it drives the param).
    # POPOVER window_pos is relative to the CURSOR at the call,
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
    # Anchor the panel at the window's RIGHT edge (+12px gap). Set every frame
    # so left_offset/top_offset track the right edge and the panel rides along
    # when the window is dragged; the panel's own drag accumulates into
    # window_pos on top of that, so it stays draggable.
    # Save/restore the flow cursor around the panel jump; nested in a scroll
    # view, `win` is the ENCLOSING window, so this teleports the cursor far
    # from this view's box - left unrestored, poisons the parent rect and
    # the group measure (views popped in with a huge height, then the -30
    # self-reference shrank them back 29px a frame).
    panel_kwargs = {"closed": not panel_open} if (init or toggled) else {}
    if not middle_mouse_drag and not double_right_mouse_drag and scroll_y_changed is None:
        _flow_cursor = imgui.get_cursor_screen_pos()
        # Anchor y: the enclosing window's top for a window voxel, this ROW's
        # top for a nested one. The panel call emits an inline item at the
        # cursor, and that item is committed into THIS view's group - an
        # anchor at win.abs_top made a nested view's rect span from the row to
        # the window top, so committed heights scaled with scroll distance
        # (the scrollbar jitter as rows crossed the viewport).
        _anchor_y = win.abs_top if draw_state.closable else draw_state.abs_top
        imgui.set_cursor_screen_pos((win.abs_left + (win.width or width) + 12, _anchor_y))
        
        # [tint=(0.003, 0.172, 0.031), show_tint=True]
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
            # The panel is cached and must NOT invalidate per drag frame - it
            # rides its blit while a camera gesture writes the params, then
            # catches up ONCE at the gesture edge.
            if not panel_ds.closed:
                if (not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1) and not
                imgui.is_mouse_down(0) and scroll_y_changed is None) and changed:
                    panel_ds.invalidate_up()

        # ── status bar error surfacing only ────────────────────────────────────
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
def draw_voxel_5d(input_value=None, draw_state=None, **kwargs):
    _draw_host_volume(input_value)


@window(input_value=voxel_host_flow, tint=(0.30, 0.20, 0.14))
@render_func(show_bg=True, use_cache=True)
def draw_voxel_flow(input_value=None, **kwargs):
    _draw_host_volume(input_value)