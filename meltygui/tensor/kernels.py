"""CUDA raymarcher that reads a torch tensor IN PLACE — the experimental
"compute" sibling of voxel_playground's GL fragment-shader path.

The GL path has to own a 3-D texture, which means every volume is a COPY of
the tensor (dtype-coerced, made contiguous, neural-flow repacked, uploaded)
— and a live tensor whose values or shape change pays that whole-tensor pass
per change. This module flips the data flow: the kernel runs ON THE TENSOR'S
GPU and samples the tensor's memory directly through its strides, so

  * a slice / axis remap is just a torch VIEW (data_ptr + strides, no copy),
  * any dtype is decoded in the load (bf16, fp16, ints, bool, f64 — no f32 copy),
  * neural flow is index math inside the sampler,
  * a shape change is new kernel args,
  * the only thing that ever crosses GPUs is the finished 2-D image (a few
    MB), so the tensor may live on ANY device — a 50 GB H100 tensor renders
    where it is and ships ~9 MB to the display GPU.

Cost: nearest sampling only, and a linear strided buffer has none of a 3-D
texture's swizzled locality — per-sample reads are slower than the texture
path. Worth it exactly when the data moves.

Mechanics: PyCUDA SourceModule, compiled once per DEVICE (its primary
context is retained and pushed around the launch only — never around GL
calls, see cuda_interop's context rules), launched on the legacy default
stream, which orders after torch's default-stream producers without an
explicit sync. The output image is a torch uint8 (H, W, 4) tensor on the
tensor's device (premultiplied RGBA8, same encode as the GL pass); the
caller moves it to wherever it's displayed.
"""

import math

import numpy as np

# Per-device compiled kernels and retained primary contexts, parked on sys so
# they are PROCESS-lifetime: globals().get would survive a hotswap re-exec
# but not the in-place restart (studio_server purges src.*), and the old
# module dict then became cyclic garbage that the next session's boot
# gc collector freed with no CUDA context current - PyCUDA's "Resources in
# out-of-thread context could not be cleaned up" warning, printed by the
# studio.py warning hook as a full stack trace. Same dedupe pattern as the
# other sys._lsd_* roots (see lifecycle.py); also spares the recompile.
import sys as _sys
_KERNELS = _sys.__dict__.setdefault("_lsd_cuda_march_kernels", {})
_CONTEXTS = _sys.__dict__.setdefault("_lsd_cuda_march_contexts", {})
_last_logged = globals().get("_last_logged")


def _log_once(msg):
    global _last_logged
    if msg != _last_logged:
        print(f"[cuda_march] {msg}")
        _last_logged = msg


# torch.dtype → load switch case in the kernel
DTYPE_CODES = {
    "torch.float32": 0, "torch.float16": 1, "torch.bfloat16": 2,
    "torch.float64": 3, "torch.int8": 4, "torch.uint8": 5, "torch.bool": 5,
    "torch.int16": 6, "torch.int32": 7, "torch.int64": 8,
}

KERNEL = r"""
#include <cuda_fp16.h>

__device__ __forceinline__ float load_at(const unsigned char* __restrict__ d,
                                         int dtype, long long e) {
    switch (dtype) {
        case 0: return ((const float*)d)[e];
        case 1: return __half2float(((const __half*)d)[e]);
        case 2: { unsigned short u = ((const unsigned short*)d)[e];
                  return __uint_as_float(((unsigned)u) << 16); }
        case 3: return (float)((const double*)d)[e];
        case 4: return (float)((const signed char*)d)[e];
        case 5: return (float)d[e];
        case 6: return (float)((const short*)d)[e];
        case 7: return (float)((const int*)d)[e];
        case 8: return (float)((const long long*)d)[e];
    }
    return 0.0f;
}

struct Vol {
    const unsigned char* data; int dtype;
    int nz, ny, nx;          // DISPLAYED extents (after neural flow)
    int snz, sny, snx;       // source view extents
    long long sz, sy, sx;    // source view element strides
    int nf_chop, nf_along, nf_chunk;   // -1 = off; axes 0=z 1=y 2=x
    float nlo, nhi; int norm_mode;     // 0 off, 1 (v-lo)/(hi-lo), 2 v/maxabs
};

// Shading parameters, one float array (see shade_params() in cuda_march.py)
struct Shade {
    bool draw_plane, draw_shading, self_shading;
    float shadow_opacity, shadow_softness, plane_side;
    float3 shadow_tint, light_pos, light_tint;
    float light_brightness, ambient_light, shading_strength;
};

// ── float3 helpers ──
__device__ __forceinline__ float3 f3(float x, float y, float z) { return make_float3(x, y, z); }
__device__ __forceinline__ float3 add3(float3 a, float3 b) { return f3(a.x+b.x, a.y+b.y, a.z+b.z); }
__device__ __forceinline__ float3 sub3(float3 a, float3 b) { return f3(a.x-b.x, a.y-b.y, a.z-b.z); }
__device__ __forceinline__ float3 mul3(float3 a, float s) { return f3(a.x*s, a.y*s, a.z*s); }
__device__ __forceinline__ float3 div3(float3 a, float3 b) { return f3(a.x/b.x, a.y/b.y, a.z/b.z); }
__device__ __forceinline__ float dot3(float3 a, float3 b) { return a.x*b.x + a.y*b.y + a.z*b.z; }
__device__ __forceinline__ float len3(float3 a) { return sqrtf(dot3(a, a)); }
__device__ __forceinline__ float3 norm3(float3 a) { float l = len3(a); return l > 0.f ? mul3(a, 1.f/l) : a; }
__device__ __forceinline__ float clamp01(float x) { return fminf(fmaxf(x, 0.f), 1.f); }

// Slab test against the box [-b, +b]: (t_enter, t_exit).
__device__ __forceinline__ float2 rayBox(float3 ro, float3 rd, float3 b) {
    float3 inv = f3(1.0f / rd.x, 1.0f / rd.y, 1.0f / rd.z);
    float t0x = (-b.x - ro.x) * inv.x, t1x = (b.x - ro.x) * inv.x;
    float t0y = (-b.y - ro.y) * inv.y, t1y = (b.y - ro.y) * inv.y;
    float t0z = (-b.z - ro.z) * inv.z, t1z = (b.z - ro.z) * inv.z;
    return make_float2(fmaxf(fmaxf(fminf(t0x, t1x), fminf(t0y, t1y)), fminf(t0z, t1z)),
                       fminf(fminf(fmaxf(t0x, t1x), fmaxf(t0y, t1y)), fmaxf(t0z, t1z)));
}

// Raw value at DISPLAY voxel (iz, iy, ix) — neural-flow remap + strided load
// + normalize. Indices must already be in range.
__device__ __forceinline__ float sample_i(const Vol& v, int iz, int iy, int ix) {
    int d[3] = {iz, iy, ix};
    if (v.nf_chop >= 0) {
        // cat(split(chop, chunk), along): display along-index a' = j*A + i
        // (block j, original i); display chop-index c' -> source j*chunk + c'.
        int A = (v.nf_along == 0) ? v.snz : ((v.nf_along == 1) ? v.sny : v.snx);
        int j = d[v.nf_along] / A;
        d[v.nf_along] -= j * A;
        d[v.nf_chop] += j * v.nf_chunk;
    }
    if (d[0] >= v.snz || d[1] >= v.sny || d[2] >= v.snx) return 0.0f;  // pad
    long long e = (long long)d[0] * v.sz + (long long)d[1] * v.sy
                + (long long)d[2] * v.sx;
    float x = load_at(v.data, v.dtype, e);
    if (v.norm_mode == 1) x = (x - v.nlo) / (v.nhi - v.nlo + 1e-12f);
    else if (v.norm_mode == 2) x = x / (v.nhi + 1e-12f);
    return x;
}

// Nearest sample at texcoord p in [0,1]^3 (x <-> width, y <-> height, z <-> depth)
// — the GL path's texture(volume, p).r with GL_NEAREST.
__device__ __forceinline__ float sample(const Vol& v, float3 p) {
    int ix = min(max((int)floorf(p.x * v.nx), 0), v.nx - 1);
    int iy = min(max((int)floorf(p.y * v.ny), 0), v.ny - 1);
    int iz = min(max((int)floorf(p.z * v.nz), 0), v.nz - 1);
    return sample_i(v, iz, iy, ix);
}

// Trilinear sample (GL's linear sampler, texel centers at (i+0.5)/n, edge
// clamped) — used by the SHADING reads only: normals and the light marches
// want a smooth field regardless of the colour march's nearest toggle.
// Reads the shading MIP when present (the fast path), else the source.

// The shared transfer function: raw sample -> (v: LUT coordinate, m: opacity
// drive). Contrast about mid-grey then brightness; `centered` maps signed data
// so raw 0 sits at the LUT middle and opacity keys on magnitude.
__device__ __forceinline__ float2 remapValue(float val, float brightness, float contrast, bool centered) {
    if (centered) val = val * 0.5f + 0.5f;
    val = (val - 0.5f) * contrast + 0.5f;
    float m;
    if (centered) {
        val = clamp01(0.5f + (val - 0.5f) * brightness);
        m = fabsf(val - 0.5f) * 2.0f;
    } else {
        val = clamp01(val * brightness);
        m = val;
    }
    return make_float2(val, m);
}

// The opacity ramp: at/above the gate fully opaque (hard isosurface), below it
// (m/gate)^4 scaled by density and the volume-NORMALIZED segment length.
__device__ __forceinline__ float alphaFor(float m, float seg_n, float gate, float density) {
    if (m >= gate) return 1.0f;
    float r = m / gate; r *= r; r *= r;
    return clamp01(r * density * seg_n * 50.0f);
}

struct Ctx {
    Vol v; float3 vs; float gate, density, brightness, contrast; bool centered;
    // Shading OPACITY mip: a small DENSE f16 x2 grid over the display
    // volume, each cell (f, k) of its block under the CURRENT transfer:
    // f = fraction of voxels at/above the opacity gate (hard occluders),
    // k = mean sub-gate opacity density (r^4 * density * 50, per unit
    // seg_n). The light march reconstructs a step of L physical voxels as
    // T = ((1-f)(1-k*s_vox))^L — exact for uniform haze, solid blocks,
    // and sparse spikes along ANY axis — so a coarse cell transmits the
    // SAME light as the high-res voxels it replaces, where value-space
    // estimators (avg/RMS/max) made blocky cells shade darker than the
    // data they approximate. Null = read the source directly.
    const __half* mip; int mz, my, mx;
};

__device__ __forceinline__ float2 mip_at(const Ctx& c, int iz, int iy, int ix) {
    long long e = (((long long)iz * c.my + iy) * c.mx + ix) * 2;
    return make_float2(__half2float(c.mip[e]), __half2float(c.mip[e + 1]));
}

// Trilinear over the (f, k) opacity mip — both channels in one pass.
__device__ __forceinline__ float2 sample_mip(const Ctx& c, float3 p) {
    float fx = p.x * c.mx - 0.5f, fy = p.y * c.my - 0.5f, fz = p.z * c.mz - 0.5f;
    int x0 = (int)floorf(fx), y0 = (int)floorf(fy), z0 = (int)floorf(fz);
    float tx = fx - x0, ty = fy - y0, tz = fz - z0;
    int x1 = min(max(x0 + 1, 0), c.mx - 1), y1 = min(max(y0 + 1, 0), c.my - 1), z1 = min(max(z0 + 1, 0), c.mz - 1);
    x0 = min(max(x0, 0), c.mx - 1); y0 = min(max(y0, 0), c.my - 1); z0 = min(max(z0, 0), c.mz - 1);
    float2 acc = make_float2(0.f, 0.f);
    float wz[2] = {1 - tz, tz}; float wy[2] = {1 - ty, ty}; float wx[2] = {1 - tx, tx};
    int zs[2] = {z0, z1}; int ys[2] = {y0, y1}; int xs[2] = {x0, x1};
    for (int a = 0; a < 2; a++)
        for (int b = 0; b < 2; b++)
            for (int d = 0; d < 2; d++) {
                float w = wz[a] * wy[b] * wx[d];
                float2 v = mip_at(c, zs[a], ys[b], xs[d]);
                acc.x += w * v.x; acc.y += w * v.y;
            }
    return acc;
}

// Full-res software trilinear on the SOURCE (the no-mip fallback).
__device__ __forceinline__ float sample_src_lin(const Ctx& c, float3 p) {
    int nx = c.v.nx, ny = c.v.ny, nz = c.v.nz;
    float fx = p.x * nx - 0.5f, fy = p.y * ny - 0.5f, fz = p.z * nz - 0.5f;
    int x0 = (int)floorf(fx), y0 = (int)floorf(fy), z0 = (int)floorf(fz);
    float tx = fx - x0, ty = fy - y0, tz = fz - z0;
    int x1 = min(max(x0 + 1, 0), nx - 1), y1 = min(max(y0 + 1, 0), ny - 1), z1 = min(max(z0 + 1, 0), nz - 1);
    x0 = min(max(x0, 0), nx - 1); y0 = min(max(y0, 0), ny - 1); z0 = min(max(z0, 0), nz - 1);
    float c00 = sample_i(c.v, z0, y0, x0) * (1 - tx) + sample_i(c.v, z0, y0, x1) * tx;
    float c10 = sample_i(c.v, z0, y1, x0) * (1 - tx) + sample_i(c.v, z0, y1, x1) * tx;
    float c01 = sample_i(c.v, z1, y0, x0) * (1 - tx) + sample_i(c.v, z1, y0, x1) * tx;
    float c11 = sample_i(c.v, z1, y1, x0) * (1 - tx) + sample_i(c.v, z1, y1, x1) * tx;
    float c0 = c00 * (1 - ty) + c10 * ty, c1 = c01 * (1 - ty) + c11 * ty;
    return c0 * (1 - tz) + c1 * tz;
}


// Transmittance from world point p toward light lp: a COARSE fixed-count
// march of the same transfer function (linear reads), multiplying out
// per-step opacity. Returns (T, t_occ): t_occ = distance along the light ray
// where T first dropped below 0.5 (the occluder height driving the ground
// penumbra); non-occluding rays report their mid-chord, box misses 0.
__device__ float2 lightVisibilityInfo(const Ctx& c, float3 p, float3 lp, int steps, float max_dist) {
    float3 ld = norm3(sub3(lp, p));
    float2 span = rayBox(p, ld, c.vs);
    float t0 = fmaxf(span.x, 0.0f);
    float t1 = fminf(fminf(span.y, len3(sub3(lp, p))), max_dist);
    if (t0 >= t1) return make_float2(1.0f, 0.0f);
    float ss = (t1 - t0) / (float)steps;
    float seg_n = ss * len3(div3(ld, c.vs));
    float T = 1.0f, t_occ = 0.0f, t = t0 + ss * 0.5f;
    // With the (f, k) opacity mip, per PHYSICAL VOXEL crossed (voxels are
    // world-space cubes, so voxels-per-step is direction-independent):
    // hard occluders hit with probability f, sub-gate haze contributes
    // k * s_vox where s_vox = the step's seg_n split over its L voxels —
    // keeping the fraction-of-volume opacity semantics direction-aware
    // (crossing a 4-voxel axis is 4 chances, not 512). T over the step is
    // ((1-f)(1-k*s_vox))^L: exact for uniform haze, solid blocks, and
    // sparse spikes. Without a mip: the full-res value path.
    float L = fmaxf(ss * 0.5f * (float)max(c.v.nx, max(c.v.ny, c.v.nz)), 1e-6f);
    float s_vox = seg_n / L;
    for (int i = 0; i < steps; i++) {
        float3 q = add3(mul3(div3(add3(p, mul3(ld, t)), c.vs), 0.5f), f3(0.5f, 0.5f, 0.5f));
        float a;
        if (c.mip) {
            float2 fk = sample_mip(c, q);
            float f = fminf(fmaxf(fk.x, 0.0f), 1.0f);
            float a_h = fminf(fmaxf(fk.y, 0.0f) * s_vox, 1.0f);
            float t_vox = (1.0f - f) * (1.0f - a_h);
            a = 1.0f - powf(fmaxf(t_vox, 0.0f), L);
        } else {
            float m = remapValue(sample_src_lin(c, q), c.brightness, c.contrast, c.centered).y;
            a = alphaFor(m, seg_n, c.gate, c.density);
        }
        T *= 1.0f - a;
        if (t_occ == 0.0f && T < 0.5f) t_occ = t;
        if (T < 0.02f) break;
        t += ss;
    }
    if (t_occ == 0.0f) t_occ = 0.5f * (t0 + t1);
    return make_float2(T, t_occ);
}

// Baked floor-shadow map lookup: (vis, t_occ) at floor point (x, y),
// bilinear, edge texels clamped; outside the map's [-R, R] square = fully
// lit. The map is baked at FULL RES (no mip) once per volume version.
__device__ __forceinline__ float2 floor_lookup(const __half* __restrict__ m,
                                               int fw, int fh, float Rx, float Ry,
                                               float x, float y) {
    if (x < -Rx || x > Rx || y < -Ry || y > Ry) return make_float2(1.0f, 0.0f);
    float u = (x + Rx) / (2.0f * Rx) * fw - 0.5f;
    float v = (y + Ry) / (2.0f * Ry) * fh - 0.5f;
    int x0 = (int)floorf(u), y0 = (int)floorf(v);
    float tx = u - x0, ty = v - y0;
    int x1 = min(max(x0 + 1, 0), fw - 1), y1 = min(max(y0 + 1, 0), fh - 1);
    x0 = min(max(x0, 0), fw - 1); y0 = min(max(y0, 0), fh - 1);
    #define FM(yy, xx, ch) __half2float(m[((long long)(yy) * fw + (xx)) * 2 + (ch)])
    float v00 = FM(y0, x0, 0), v10 = FM(y0, x1, 0), v01 = FM(y1, x0, 0), v11 = FM(y1, x1, 0);
    float t00 = FM(y0, x0, 1), t10 = FM(y0, x1, 1), t01 = FM(y1, x0, 1), t11 = FM(y1, x1, 1);
    #undef FM
    float a = (v00 * (1 - tx) + v10 * tx) * (1 - ty) + (v01 * (1 - tx) + v11 * tx) * ty;
    float b = (t00 * (1 - tx) + t10 * tx) * (1 - ty) + (t01 * (1 - tx) + t11 * tx) * ty;
    return make_float2(a, b);
}

// ── fine DDA (Amanatides-Woo) over display voxels in [t0, t1): each voxel
// along the ray is visited EXACTLY ONCE with its exact segment length —
// there is no step size. Zoomed out, no voxel is skipped (fixed steps
// aliased past sub-step spikes); zoomed in, a screen-filling voxel costs
// one sample instead of step_size's fifty, and nothing far-clips at
// max_steps * step_size. `budget` (shared across cells) is the watchdog.
// Returns false when the walk should stop (opacity saturated / budget out).
__device__ bool dda_fine(const Ctx& c, const Shade& S,
                         const float* __restrict__ lut, int lut_n,
                         float3 ro, float3 rd, float t0, float t1,
                         float dirn, float view_cos,
                         float4& acc, int& budget, int K)
{
    if (t1 <= t0) return true;
    int nx = c.v.nx, ny = c.v.ny, nz = c.v.nz;
    // index-space ray: u_a(t) = ((ro_a/vs_a)*0.5 + 0.5)*n_a + rd_a*0.5*n_a/vs_a * t
    float ox = ((ro.x / c.vs.x) * 0.5f + 0.5f) * nx, dx = rd.x * 0.5f * nx / c.vs.x;
    float oy = ((ro.y / c.vs.y) * 0.5f + 0.5f) * ny, dy = rd.y * 0.5f * ny / c.vs.y;
    float oz = ((ro.z / c.vs.z) * 0.5f + 0.5f) * nz, dz = rd.z * 0.5f * nz / c.vs.z;
    float tn = t0 + (t1 - t0) * 1e-6f;          // nudge off the entry face
    int ix = min(max((int)floorf(ox + dx * tn), 0), nx - 1);
    int iy = min(max((int)floorf(oy + dy * tn), 0), ny - 1);
    int iz = min(max((int)floorf(oz + dz * tn), 0), nz - 1);
    int sx = dx > 0.f ? 1 : -1, sy = dy > 0.f ? 1 : -1, sz2 = dz > 0.f ? 1 : -1;
    float BIG = 1e30f;
    float tDx = dx != 0.f ? fabsf(1.0f / dx) : BIG;
    float tDy = dy != 0.f ? fabsf(1.0f / dy) : BIG;
    float tDz = dz != 0.f ? fabsf(1.0f / dz) : BIG;
    float tMx = dx != 0.f ? ((float)(ix + (sx > 0 ? 1 : 0)) - ox) / dx : BIG;
    float tMy = dy != 0.f ? ((float)(iy + (sy > 0 ? 1 : 0)) - oy) / dy : BIG;
    float tMz = dz != 0.f ? ((float)(iz + (sz2 > 0 ? 1 : 0)) - oz) / dz : BIG;
    float tm = t0;
    while (true) {
        // K > 1 = the quality dial: coalesce up to K voxel crossings into
        // one sample (taken at the segment midpoint's voxel). K == 1 is
        // the exact walk and uses the tracked indices (bit-stable at
        // boundaries, which the coarse-skip equivalence relies on).
        float tExit = fminf(fminf(tMx, tMy), fminf(tMz, t1));
        for (int k2 = 1; k2 < K && tExit < t1; k2++) {
            if (tMx <= tMy && tMx <= tMz) {
                ix += sx; tMx += tDx;
                if (ix < 0 || ix >= nx) break;
            } else if (tMy <= tMz) {
                iy += sy; tMy += tDy;
                if (iy < 0 || iy >= ny) break;
            } else {
                iz += sz2; tMz += tDz;
                if (iz < 0 || iz >= nz) break;
            }
            tExit = fminf(fminf(tMx, tMy), fminf(tMz, t1));
        }
        float seg = tExit - tm;
        if (seg > 0.0f) {
            if (--budget < 0) return false;
            int jx = ix, jy = iy, jz = iz;
            if (K > 1) {
                float tc = tm + seg * 0.5f;
                jx = min(max((int)floorf(ox + dx * tc), 0), nx - 1);
                jy = min(max((int)floorf(oy + dy * tc), 0), ny - 1);
                jz = min(max((int)floorf(oz + dz * tc), 0), nz - 1);
            }
            float2 vm = remapValue(sample_i(c.v, jz, jy, jx),
                                   c.brightness, c.contrast, c.centered);
            float seg_n = seg * dirn * view_cos;
            float a = alphaFor(vm.y, seg_n, c.gate, c.density);
            if (a > 0.0f) {
                float f = fminf(fmaxf(vm.x * (float)lut_n - 0.5f, 0.0f), (float)(lut_n - 1));
                int i0 = (int)floorf(f); int i1 = min(i0 + 1, lut_n - 1); float wl = f - (float)i0;
                float3 col = f3(powf(lut[3*i0]   * (1.f - wl) + lut[3*i1]   * wl, 2.2f),
                                powf(lut[3*i0+1] * (1.f - wl) + lut[3*i1+1] * wl, 2.2f),
                                powf(lut[3*i0+2] * (1.f - wl) + lut[3*i1+2] * wl, 2.2f));
                // Voxels keep their LUT colours — no normal-based Lambert;
                // self_shading is a pure transmittance darkening.
                if (S.draw_shading && S.self_shading && a > 0.01f) {
                    float3 wp = add3(ro, mul3(rd, tm + seg * 0.5f));
                    float vis = lightVisibilityInfo(c, wp, S.light_pos, 6, 0.7f).x;
                    float lit = S.ambient_light + (1.0f - S.ambient_light) * vis;
                    float shade = 1.0f + (lit - 1.0f) * S.shading_strength;
                    col = mul3(col, shade);
                }
                float kk = (1.0f - acc.w) * a;
                acc.x += kk * col.x; acc.y += kk * col.y; acc.z += kk * col.z;
                acc.w += (1.0f - acc.w) * a;
                if (acc.w > 0.98f) return false;
            }
        }
        if (tExit >= t1) return true;
        if (tMx <= tMy && tMx <= tMz) {
            ix += sx; tm = tMx; tMx += tDx;
            if (ix < 0 || ix >= nx) return true;
        } else if (tMy <= tMz) {
            iy += sy; tm = tMy; tMy += tDy;
            if (iy < 0 || iy >= ny) return true;
        } else {
            iz += sz2; tm = tMz; tMz += tDz;
            if (iz < 0 || iz >= nz) return true;
        }
    }
}

extern "C" __global__ void march(
    const unsigned char* __restrict__ data, int dtype,
    int nz, int ny, int nx, int snz, int sny, int snx,
    long long sz, long long sy, long long sx,
    int nf_chop, int nf_along, int nf_chunk,
    float nlo, float nhi, int norm_mode,
    const float* __restrict__ lut, int lut_n,
    unsigned char* __restrict__ out, int W, int H,
    float tilt, float spin, float roll, float zoom, float pan_x, float pan_y, float pan_z,
    int ortho, float aspect, float vsx, float vsy, float vsz,
    float step_size, int max_steps, float density, float threshold,
    float brightness, float contrast, float gamma, int centered,
    const float* __restrict__ sh,
    const __half* __restrict__ mipd, int mz, int my, int mx,
    const __half* __restrict__ fmap, int fw, int fh, float fRx, float fRy)
{
    int px = blockIdx.x * blockDim.x + threadIdx.x;
    int py = blockIdx.y * blockDim.y + threadIdx.y;
    if (px >= W || py >= H) return;
    Ctx c;
    c.v = Vol{data, dtype, nz, ny, nx, snz, sny, snx, sz, sy, sx,
              nf_chop, nf_along, nf_chunk, nlo, nhi, norm_mode};
    c.vs = f3(vsx, vsy, vsz);
    c.gate = 1.0f - fminf(fmaxf(threshold, 0.0f), 0.999f);
    c.density = density; c.brightness = brightness; c.contrast = contrast;
    c.centered = centered != 0;
    c.mip = mipd; c.mz = mz; c.my = my; c.mx = mx;
    Shade S;
    S.draw_plane = sh[0] > 0.5f; S.shadow_opacity = sh[1]; S.shadow_softness = sh[2];
    S.shadow_tint = f3(sh[3], sh[4], sh[5]); S.plane_side = sh[6];
    S.draw_shading = sh[7] > 0.5f; S.self_shading = sh[8] > 0.5f;
    S.light_pos = f3(sh[9], sh[10], sh[11]); S.light_tint = f3(sh[12], sh[13], sh[14]);
    S.light_brightness = sh[15]; S.ambient_light = sh[16]; S.shading_strength = sh[17];

    // ── the GL pass's z-up orbit camera, verbatim ──
    float u = ((float)px + 0.5f) / (float)W, w = ((float)py + 0.5f) / (float)H;
    float ct = cosf(tilt), st = sinf(tilt), cs = cosf(spin), ss = sinf(spin);
    float3 fwd = f3(-cs * ct, -ss * ct, -st);
    float3 right0 = f3(-ss, cs, 0.0f);
    float3 up0 = f3(right0.y * fwd.z - right0.z * fwd.y,
                    right0.z * fwd.x - right0.x * fwd.z,
                    right0.x * fwd.y - right0.y * fwd.x);
    // roll turns right toward up about the view axis (voxel_camera.basis)
    float cr = cosf(roll), sr = sinf(roll);
    float3 right = f3(right0.x * cr + up0.x * sr, right0.y * cr + up0.y * sr,
                      right0.z * cr + up0.z * sr);
    float3 up = f3(right.y * fwd.z - right.z * fwd.y,
                   right.z * fwd.x - right.x * fwd.z,
                   right.x * fwd.y - right.y * fwd.x);
    float3 eye = sub3(f3(pan_x, pan_y, pan_z), mul3(fwd, zoom));
    float ndx = (u * 2.0f - 1.0f) * aspect, ndy = w * 2.0f - 1.0f;
    float3 ro, rd;
    if (ortho) {
        ro = add3(eye, mul3(add3(mul3(right, ndx), mul3(up, ndy)), zoom / 1.7f));
        rd = fwd;
    } else {
        ro = eye;
        rd = norm3(add3(mul3(fwd, 1.7f), add3(mul3(right, ndx), mul3(up, ndy))));
    }
    float view_cos = dot3(rd, fwd);

    // ── shadow catcher: the plane the box rests on (z = -vs.z, mirrored by
    // plane_side) is INVISIBLE — only the volume's cast shadow composites,
    // as a darkening with alpha = blocked light. One-sided. ──
    float plane_t = -1.0f, plane_a = 0.0f;
    float3 plane_c = S.shadow_tint;
    if (S.draw_plane && S.draw_shading && rd.z * S.plane_side < -1e-6f
            && ro.z * S.plane_side > -c.vs.z) {
        plane_t = (-c.vs.z * S.plane_side - ro.z) / rd.z;
        float3 pw = add3(ro, mul3(rd, plane_t));
        float ext = fmaxf(c.vs.x, c.vs.y);
        float r = fmaxf(sqrtf(pw.x * pw.x + pw.y * pw.y) - ext * 1.1f, 0.0f);
        float3 pl_light = f3(S.light_pos.x, S.light_pos.y, S.light_pos.z * S.plane_side);
        // Baked map when present (full-res, computed once per volume
        // version): (vis, t_occ) per floor texel, blur = 4 more map reads.
        float2 vi = fmap ? floor_lookup(fmap, fw, fh, fRx, fRy, pw.x, pw.y)
                         : lightVisibilityInfo(c, pw, pl_light, 24, 1e8f);
        float vis = vi.x;
        float blur_r = S.shadow_softness * vi.y;
        if (blur_r > 1e-4f) {
            float acc_v = vis;
            for (int k = 0; k < 4; k++) {
                float ang = (float)k * 1.5707963f + 0.7853982f;
                float3 op = add3(pw, mul3(f3(cosf(ang), sinf(ang), 0.0f), blur_r));
                acc_v += fmap ? floor_lookup(fmap, fw, fh, fRx, fRy, op.x, op.y).x
                              : lightVisibilityInfo(c, op, pl_light, 10, 1e8f).x;
            }
            vis = acc_v / 5.0f;
        }
        float shadow = (1.0f - S.ambient_light) * (1.0f - vis);
        plane_a = clamp01(shadow * S.shadow_opacity) * expf(-1.5f * r / ext);
    }

    float2 hit = rayBox(ro, rd, c.vs);
    bool box_hit = !(hit.x > hit.y || hit.y < 0.0f);
    float4 acc = make_float4(0.f, 0.f, 0.f, 0.f);
    if (box_hit || plane_t > 0.0f) {
        // Plane in FRONT of the volume: composite it first.
        if (plane_t > 0.0f && box_hit && plane_t <= fmaxf(hit.x, 0.0f)) {
            acc = make_float4(plane_c.x * plane_a, plane_c.y * plane_a, plane_c.z * plane_a, plane_a);
            plane_t = -1.0f;
        }
        if (box_hit) {
            // Two-level DDA: outer over the (f, k) mip cells (empty cells
            // skipped in O(1)), inner exact voxel traversal. No step size —
            // `step_size` is a no-op for the CUDA colour march; max_steps
            // is the voxel-visit watchdog. Tiny volumes (or no mip) go
            // straight to fine DDA.
            float t0v = fmaxf(hit.x, 0.0f);
            float t1v = hit.y;
            float dirn = len3(div3(rd, c.vs));
            int budget = max_steps;
            // step_size's CUDA meaning: the sampling stride, floored at one
            // voxel — K = how many voxel crossings coalesce per sample.
            // The default (0.0005) keeps K = 1 (exact) for anything up to
            // ~4000 voxels wide; raising it trades exactness for speed.
            float vox_w = 2.0f / (float)max(c.v.nx, max(c.v.ny, c.v.nz));
            int K = max(1, (int)(step_size / vox_w + 0.5f));
            bool coarse = c.mip && ((long long)c.v.nx * c.v.ny * c.v.nz > 262144);
            if (!coarse) {
                dda_fine(c, S, lut, lut_n, ro, rd, t0v, t1v, dirn, view_cos, acc, budget, K);
            } else {
                int mx = c.mx, my = c.my, mz = c.mz;
                float ox = ((ro.x / c.vs.x) * 0.5f + 0.5f) * mx, dx = rd.x * 0.5f * mx / c.vs.x;
                float oy = ((ro.y / c.vs.y) * 0.5f + 0.5f) * my, dy = rd.y * 0.5f * my / c.vs.y;
                float oz = ((ro.z / c.vs.z) * 0.5f + 0.5f) * mz, dz = rd.z * 0.5f * mz / c.vs.z;
                float tn = t0v + (t1v - t0v) * 1e-6f;
                int ix = min(max((int)floorf(ox + dx * tn), 0), mx - 1);
                int iy = min(max((int)floorf(oy + dy * tn), 0), my - 1);
                int iz = min(max((int)floorf(oz + dz * tn), 0), mz - 1);
                int sx = dx > 0.f ? 1 : -1, sy = dy > 0.f ? 1 : -1, sz2 = dz > 0.f ? 1 : -1;
                float BIG = 1e30f;
                float tDx = dx != 0.f ? fabsf(1.0f / dx) : BIG;
                float tDy = dy != 0.f ? fabsf(1.0f / dy) : BIG;
                float tDz = dz != 0.f ? fabsf(1.0f / dz) : BIG;
                float tMx = dx != 0.f ? ((float)(ix + (sx > 0 ? 1 : 0)) - ox) / dx : BIG;
                float tMy = dy != 0.f ? ((float)(iy + (sy > 0 ? 1 : 0)) - oy) / dy : BIG;
                float tMz = dz != 0.f ? ((float)(iz + (sz2 > 0 ? 1 : 0)) - oz) / dz : BIG;
                float tm = t0v;
                while (true) {
                    float tExit = fminf(fminf(tMx, tMy), fminf(tMz, t1v));
                    if (tExit > tm) {
                        float2 fk = mip_at(c, iz, iy, ix);
                        if (fk.x > 0.0f || fk.y > 1e-3f) {
                            if (!dda_fine(c, S, lut, lut_n, ro, rd, tm, tExit,
                                          dirn, view_cos, acc, budget, K))
                                break;
                        }
                    }
                    if (tExit >= t1v) break;
                    if (tMx <= tMy && tMx <= tMz) {
                        ix += sx; tm = tMx; tMx += tDx;
                        if (ix < 0 || ix >= mx) break;
                    } else if (tMy <= tMz) {
                        iy += sy; tm = tMy; tMy += tDy;
                        if (iy < 0 || iy >= my) break;
                    } else {
                        iz += sz2; tm = tMz; tMz += tDz;
                        if (iz < 0 || iz >= mz) break;
                    }
                }
            }
        }
        // Plane BEHIND the volume (the usual case): composite it under.
        if (plane_t > 0.0f) {
            float kk = (1.0f - acc.w) * plane_a;
            acc.x += kk * plane_c.x; acc.y += kk * plane_c.y; acc.z += kk * plane_c.z;
            acc.w += (1.0f - acc.w) * plane_a;
        }
    }
    float g = gamma / 2.2f;
    float dither = (fmodf(52.9829189f * fmodf(
        ((float)px + 0.5f) * 0.06711056f + ((float)py + 0.5f) * 0.00583715f, 1.0f), 1.0f)
        - 0.5f) / 255.0f;
    unsigned char* o = out + ((long long)py * W + px) * 4;
    o[0] = (unsigned char)(clamp01(powf(acc.x, g) + dither) * 255.0f + 0.5f);
    o[1] = (unsigned char)(clamp01(powf(acc.y, g) + dither) * 255.0f + 0.5f);
    o[2] = (unsigned char)(clamp01(powf(acc.z, g) + dither) * 255.0f + 0.5f);
    o[3] = (unsigned char)(clamp01(acc.w + dither) * 255.0f + 0.5f);
}

// Box-filter the DISPLAY volume (nf remap + normalize included, via
// sample_i) into a small dense f16 mip: one thread per mip cell, averaging
// every display voxel that maps into it — one full read of the tensor,
// once per volume version.
extern "C" __global__ void bake_mip(
    const unsigned char* __restrict__ data, int dtype,
    int nz, int ny, int nx, int snz, int sny, int snx,
    long long sz, long long sy, long long sx,
    int nf_chop, int nf_along, int nf_chunk,
    float nlo, float nhi, int norm_mode,
    float threshold, float density, float brightness, float contrast, int centered,
    __half* __restrict__ mip, int mz, int my, int mx)
{
    long long cell = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long total = (long long)mz * my * mx;
    if (cell >= total) return;
    Vol v = {data, dtype, nz, ny, nx, snz, sny, snx, sz, sy, sx,
             nf_chop, nf_along, nf_chunk, nlo, nhi, norm_mode};
    int ix = (int)(cell % mx), iy = (int)((cell / mx) % my), iz = (int)(cell / ((long long)mx * my));
    int x0 = (int)((long long)ix * nx / mx), x1 = (int)((long long)(ix + 1) * nx / mx);
    int y0 = (int)((long long)iy * ny / my), y1 = (int)((long long)(iy + 1) * ny / my);
    int z0 = (int)((long long)iz * nz / mz), z1 = (int)((long long)(iz + 1) * nz / mz);
    x1 = max(x1, x0 + 1); y1 = max(y1, y0 + 1); z1 = max(z1, z0 + 1);
    float gate = 1.0f - fminf(fmaxf(threshold, 0.0f), 0.999f);
    float f_acc = 0.0f, k_acc = 0.0f;
    for (int z = z0; z < z1; z++)
        for (int y = y0; y < y1; y++)
            for (int x = x0; x < x1; x++) {
                float m = remapValue(sample_i(v, z, y, x), brightness, contrast,
                                     centered != 0).y;
                if (m >= gate) { f_acc += 1.0f; }
                else { float r = m / gate; r *= r; r *= r;
                       k_acc += r * density * 50.0f; }
            }
    float n = (float)((z1 - z0) * (y1 - y0) * (x1 - x0));
    mip[cell * 2] = __float2half(f_acc / n);
    mip[cell * 2 + 1] = __float2half(k_acc / n);
}

// Bake the floor-shadow map: one thread per texel of a (fh, fw) grid over
// the floor square [-R, R]^2 at z = -vs.z * plane_side; each runs the SAME
// transmittance march the live path would — but at FULL RESOLUTION (no
// mip: software-trilinear source taps, the GL reference's field) and with
// a deeper step count than the per-frame budget ever allowed. Stores
// (vis, t_occ) as f16 pairs. Re-run only when the volume / light /
// transfer changes — never per frame.
extern "C" __global__ void bake_floor(
    const unsigned char* __restrict__ data, int dtype,
    int nz, int ny, int nx, int snz, int sny, int snx,
    long long sz, long long sy, long long sx,
    int nf_chop, int nf_along, int nf_chunk,
    float nlo, float nhi, int norm_mode,
    float vsx, float vsy, float vsz,
    float threshold, float density, float brightness, float contrast, int centered,
    float lx, float ly, float lz, float plane_side,
    __half* __restrict__ fmap, int fw, int fh, float Rx, float Ry, int steps)
{
    int ix = blockIdx.x * blockDim.x + threadIdx.x;
    int iy = blockIdx.y * blockDim.y + threadIdx.y;
    if (ix >= fw || iy >= fh) return;
    Ctx c;
    c.v = Vol{data, dtype, nz, ny, nx, snz, sny, snx, sz, sy, sx,
              nf_chop, nf_along, nf_chunk, nlo, nhi, norm_mode};
    c.vs = f3(vsx, vsy, vsz);
    c.gate = 1.0f - fminf(fmaxf(threshold, 0.0f), 0.999f);
    c.density = density; c.brightness = brightness; c.contrast = contrast;
    c.centered = centered != 0;
    c.mip = 0; c.mz = c.my = c.mx = 0;          // full-res taps
    float x = ((float)ix + 0.5f) / (float)fw * 2.0f * Rx - Rx;
    float y = ((float)iy + 0.5f) / (float)fh * 2.0f * Ry - Ry;
    float3 pw = f3(x, y, -vsz * plane_side);
    float3 pl = f3(lx, ly, lz * plane_side);
    float2 vi = lightVisibilityInfo(c, pw, pl, steps, 1e8f);
    fmap[((long long)iy * fw + ix) * 2] = __float2half(vi.x);
    fmap[((long long)iy * fw + ix) * 2 + 1] = __float2half(vi.y);
}
"""


def available():
    try:
        import pycuda.driver  # noqa: F401
        return True
    except Exception:
        return False


def _context_for(dev_index):
    """The retained PRIMARY context of CUDA device `dev_index` — the same
    context torch uses there, so torch pointers are valid in it."""
    ctx = _CONTEXTS.get(dev_index)
    if ctx is None:
        import pycuda.driver as cuda
        cuda.init()
        ctx = cuda.Device(int(dev_index)).retain_primary_context()
        _CONTEXTS[dev_index] = ctx
    return ctx


class _Pushed:
    """Push a device's primary context for the duration (no-op when it is
    already the current context — the render thread's display device)."""

    def __init__(self, dev_index):
        self.dev_index = dev_index
        self.pushed = False

    def __enter__(self):
        import pycuda.driver as cuda
        cuda.init()                      # idempotent; get_current needs it
        cur = cuda.Context.get_current()
        ctx = _context_for(self.dev_index)
        if cur is None or cur.handle != ctx.handle:
            ctx.push()
            self.pushed = True
        return self

    def __exit__(self, *exc):
        if self.pushed:
            import pycuda.driver as cuda
            cuda.Context.pop()
        return False


def _host_compiler_flags():
    """nvcc 12.1 refuses gcc > 12 as host compiler; point it at the newest
    supported gcc on the box when the default is too new."""
    import shutil
    # Needs the C++ front end too (gcc-12 without g++-12 has no cc1plus).
    for g in ("12", "11", "10"):
        if shutil.which("gcc-" + g) and shutil.which("g++-" + g):
            return ["-ccbin", "g++-" + g]
    return ["-allow-unsupported-compiler"]


def _kernel_for(dev_index, name="march"):
    """Compile (once per device, nvcc-cached on disk by pycuda) and return
    the named kernel. Must be called with that device's context pushed."""
    fns = _KERNELS.get(dev_index)
    if not isinstance(fns, dict) or fns.get("__source__") != hash(KERNEL):
        import pycuda.driver as cuda
        from pycuda.compiler import SourceModule
        cc = cuda.Device(int(dev_index)).compute_capability()
        mod = SourceModule(KERNEL, no_extern_c=True, arch="sm_%d%d" % cc,
                           options=["-O3"] + _host_compiler_flags())
        fns = {n: mod.get_function(n) for n in ("march", "bake_mip", "bake_floor")}
        fns["__source__"] = hash(KERNEL)
        _KERNELS[dev_index] = fns
    return fns[name]


def dtype_code(t):
    code = DTYPE_CODES.get(str(t.dtype))
    if code is None:
        raise ValueError(f"cuda_march: unsupported dtype {t.dtype}")
    return code


SHADE_N = 18


def shade_params(draw_plane=False, shadow_opacity=1.0, shadow_softness=0.15,
                 shadow_tint=(0.0, 0.02, 0.05), plane_side=1.0, draw_shading=False,
                 self_shading=False, light_pos=(50.0, -50.0, 200.0),
                 light_tint=(1.0, 1.0, 1.0), light_brightness=1.622,
                 ambient_light=0.3, shading_strength=0.7):
    """The kernel's shading parameters as one float list (the `sh` array;
    order = the Shade struct unpack in KERNEL). Defaults = shading OFF, so a
    bare march() renders the plain colour march."""
    return [1.0 if draw_plane else 0.0, float(shadow_opacity), float(shadow_softness),
            float(shadow_tint[0]), float(shadow_tint[1]), float(shadow_tint[2]),
            float(plane_side), 1.0 if draw_shading else 0.0, 1.0 if self_shading else 0.0,
            float(light_pos[0]), float(light_pos[1]), float(light_pos[2]),
            float(light_tint[0]), float(light_tint[1]), float(light_tint[2]),
            float(light_brightness), float(ambient_light), float(shading_strength)]


def march(view, out, lut, *, display_shape, nf=(-1, -1, 0), norm=(0.0, 1.0, 0),
          tilt=0.0, spin=0.0, roll=0.0, zoom=3.4, pan=(0.0, 0.0, 0.0), ortho=False,
          aspect=1.0, volume_scale=(1.0, 1.0, 1.0), step_size=0.005,
          max_steps=512, density=0.7, threshold=0.3, brightness=1.0,
          contrast=1.0, gamma=1.6, centered=False, shade=None, mip=None,
          floor_map=None, floor_extent=(0.0, 0.0)):
    """Raymarch `view` (a 3-D torch view (z, y, x) on a CUDA device, ANY
    strides, ANY supported dtype) into `out` (torch uint8 (H, W, 4) on the
    same device; premultiplied RGBA8). `lut` is a float32 (n, 3) torch
    tensor on that device. `display_shape` = (nz, ny, nx) after neural
    flow; `nf` = (chop_axis, along_axis, chunk) with axes 0=z 1=y 2=x (chop
    -1 = off); `norm` = (lo, hi, mode); `shade` = a float32 torch tensor of
    SHADE_N on the same device (shade_params(...)) or None = shading off.
    `mip` = the shading mip from build_mip() (all shading reads sample it —
    pass it whenever shade enables the plane or shading; without it the
    shading taps fall back to the strided source, correct but ~100× the
    loads on big tensors)."""
    import torch
    assert view.dim() == 3 and out.dim() == 3 and out.shape[2] == 4
    dev = view.device.index or 0
    if (out.device.index or 0) != dev or (lut.device.index or 0) != dev:
        raise ValueError("cuda_march: view/out/lut must share a device")
    if shade is None:
        shade = torch.tensor(shade_params(), dtype=torch.float32, device=view.device)
    elif (shade.device.index or 0) != dev or shade.numel() < SHADE_N:
        raise ValueError("cuda_march: shade must be a float32[SHADE_N] tensor on the view's device")
    H, W = int(out.shape[0]), int(out.shape[1])
    snz, sny, snx = (int(s) for s in view.shape)
    sz, sy, sx = (int(s) for s in view.stride())
    nz, ny, nx = (int(s) for s in display_shape)
    chop, along, chunk = nf
    f32, i32, i64 = np.float32, np.int32, np.int64
    vsx, vsy, vsz = volume_scale
    with _Pushed(dev):
        fn = _kernel_for(dev)
        block = (16, 16, 1)
        grid = ((W + 15) // 16, (H + 15) // 16, 1)
        fn(np.uintp(view.data_ptr()), i32(dtype_code(view)),
           i32(nz), i32(ny), i32(nx), i32(snz), i32(sny), i32(snx),
           i64(sz), i64(sy), i64(sx),
           i32(chop), i32(along), i32(chunk),
           f32(norm[0]), f32(norm[1]), i32(norm[2]),
           np.uintp(lut.data_ptr()), i32(lut.shape[0]),
           np.uintp(out.data_ptr()), i32(W), i32(H),
           f32(tilt), f32(spin), f32(roll), f32(zoom), f32(pan[0]), f32(pan[1]), f32(pan[2]),
           i32(1 if ortho else 0), f32(aspect), f32(vsx), f32(vsy), f32(vsz),
           f32(step_size), i32(max_steps), f32(density), f32(threshold),
           f32(brightness), f32(contrast), f32(gamma), i32(1 if centered else 0),
           np.uintp(shade.data_ptr()),
           np.uintp(mip.data_ptr() if mip is not None else 0),
           i32(mip.shape[0] if mip is not None else 0),
           i32(mip.shape[1] if mip is not None else 0),
           i32(mip.shape[2] if mip is not None else 0),
           np.uintp(floor_map.data_ptr() if floor_map is not None else 0),
           i32(floor_map.shape[1] if floor_map is not None else 0),
           i32(floor_map.shape[0] if floor_map is not None else 0),
           f32(floor_extent[0]), f32(floor_extent[1]),
           block=block, grid=grid)
    return out


FLOOR_MARGIN = 1.5          # map half-extent = margin around the box footprint
FLOOR_TEXELS_PER_VOXEL = 2.0
FLOOR_MIN_SIZE = 256
FLOOR_MAX_SIZE = 3072


def floor_map_extent(volume_scale, margin=FLOOR_MARGIN):
    """The baked floor map's half-extents (Rx, Ry): the box footprint with
    margin for the light's slant and the penumbra taps. Deliberately NOT the
    catcher's whole 3x fade region — the default light is near-vertical, so
    the shadow barely leaves the footprint, and hugging it is what buys
    texel density. (A user dragging light_pos to a glancing angle can clip
    at the map edge; raise FLOOR_MARGIN if that ever matters.)"""
    return (margin * float(volume_scale[0]), margin * float(volume_scale[1]))


def floor_map_size(display_shape, margin=FLOOR_MARGIN):
    """(fw, fh) matched to the DATA: ~FLOOR_TEXELS_PER_VOXEL map texels per
    voxel of the floor-facing axes (x = width, y = height), margin included,
    clamped to [FLOOR_MIN_SIZE, FLOOR_MAX_SIZE]. Small footprints get
    oversampled (crisp), huge ones ride the cap (>= ~1 texel/voxel up to
    ~2048-wide axes)."""
    nz, ny, nx = (int(x) for x in display_shape)
    def _sz(n):
        want = int(round(n * margin * FLOOR_TEXELS_PER_VOXEL))
        return max(FLOOR_MIN_SIZE, min(FLOOR_MAX_SIZE, (want + 15) // 16 * 16))
    return _sz(nx), _sz(ny)


def build_floor_map(view, *, display_shape, volume_scale, nf=(-1, -1, 0),
                    norm=(0.0, 1.0, 0), threshold=0.3, density=0.7,
                    brightness=1.0, contrast=1.0, centered=False,
                    light_pos=(50.0, -50.0, 200.0), plane_side=1.0,
                    size=None, steps=64):
    """Bake the floor-shadow map: (fh, fw, 2) f16 of (vis, t_occ) over the
    floor rect [-Rx, Rx] x [-Ry, Ry] (floor_map_extent), each texel a
    FULL-RES transmittance march toward the light — precise where the mip
    path smeared, and paid once per (volume, light, transfer) version
    instead of per frame. Resolution matches the data (floor_map_size)
    unless `size` (an (fw, fh) tuple or one int for both) overrides it.
    The extents ride back on the tensor as `fmap.extent`. Same device as
    `view`."""
    import torch
    nz, ny, nx = (int(x) for x in display_shape)
    dev = view.device.index or 0
    if size is None:
        fw, fh = floor_map_size(display_shape)
    elif isinstance(size, int):
        fw = fh = size
    else:
        fw, fh = (int(v) for v in size)
    fmap = torch.empty(fh, fw, 2, dtype=torch.float16, device=view.device)
    snz, sny, snx = (int(x) for x in view.shape)
    sz, sy, sx = (int(x) for x in view.stride())
    chop, along, chunk = nf
    Rx, Ry = floor_map_extent(volume_scale)
    f32, i32, i64 = np.float32, np.int32, np.int64
    with _Pushed(dev):
        fn = _kernel_for(dev, "bake_floor")
        fn(np.uintp(view.data_ptr()), i32(dtype_code(view)),
           i32(nz), i32(ny), i32(nx), i32(snz), i32(sny), i32(snx),
           i64(sz), i64(sy), i64(sx),
           i32(chop), i32(along), i32(chunk),
           f32(norm[0]), f32(norm[1]), i32(norm[2]),
           f32(volume_scale[0]), f32(volume_scale[1]), f32(volume_scale[2]),
           f32(threshold), f32(density), f32(brightness), f32(contrast),
           i32(1 if centered else 0),
           f32(light_pos[0]), f32(light_pos[1]), f32(light_pos[2]), f32(plane_side),
           np.uintp(fmap.data_ptr()), i32(fw), i32(fh), f32(Rx), f32(Ry), i32(steps),
           block=(16, 16, 1), grid=((fw + 15) // 16, (fh + 15) // 16, 1))
    fmap.extent = (Rx, Ry)
    return fmap


def build_mip(view, *, display_shape, nf=(-1, -1, 0), norm=(0.0, 1.0, 0),
              threshold=0.3, density=0.7, brightness=1.0, contrast=1.0,
              centered=False, cap=128):
    """Bake the shading OPACITY mip: a dense f16 (mz, my, mx) grid, each
    cell (f = hard-occluder fraction, k = mean sub-gate opacity density)
    of its block under the given transfer (nf remap + normalize applied) — the
    march reconstructs a step over L physical voxels as
    T = ((1-f)(1-k*s_vox))^L, so a coarse cell transmits the same light as
    the voxels it replaces (value-space bakes made blocky cells shade
    darker than the data). Bakes THROUGH the transfer, so cache it keyed
    on those params too. One full tensor read; a few MB, L2-resident.
    Same device as `view`."""
    import torch
    nz, ny, nx = (int(x) for x in display_shape)
    mz, my, mx = min(cap, nz), min(cap, ny), min(cap, nx)
    dev = view.device.index or 0
    mip = torch.empty(mz, my, mx, 2, dtype=torch.float16, device=view.device)
    snz, sny, snx = (int(x) for x in view.shape)
    sz, sy, sx = (int(x) for x in view.stride())
    chop, along, chunk = nf
    f32, i32, i64 = np.float32, np.int32, np.int64
    total = mz * my * mx
    with _Pushed(dev):
        fn = _kernel_for(dev, "bake_mip")
        fn(np.uintp(view.data_ptr()), i32(dtype_code(view)),
           i32(nz), i32(ny), i32(nx), i32(snz), i32(sny), i32(snx),
           i64(sz), i64(sy), i64(sx),
           i32(chop), i32(along), i32(chunk),
           f32(norm[0]), f32(norm[1]), i32(norm[2]),
           f32(threshold), f32(density), f32(brightness), f32(contrast),
           i32(1 if centered else 0),
           np.uintp(mip.data_ptr()), i32(mz), i32(my), i32(mx),
           block=(256, 1, 1), grid=((total + 255) // 256, 1, 1))
    return mip


def nf_display_shape(src_shape, chop_axis, along_axis, chunk, pad=True):
    """Displayed (z, y, x) extents after neural flow of a (z, y, x) source
    view — the cat(split) layout: chop axis → chunk, along axis →
    along * ceil(size/chunk) (the ragged last block zero-padded in the
    sampler). Axes are 0=z 1=y 2=x; chop -1 = off. Mirrors
    neural_flow_volume: a chunk that doesn't divide the axis is a no-op
    unless `pad`."""
    shape = list(int(s) for s in src_shape)
    if chop_axis < 0 or chop_axis == along_axis or chunk <= 0 or shape[chop_axis] <= chunk:
        return tuple(shape), (-1, -1, 0)
    if shape[chop_axis] % chunk != 0 and not pad:
        return tuple(shape), (-1, -1, 0)
    blocks = -(-shape[chop_axis] // chunk)
    shape[along_axis] *= blocks
    shape[chop_axis] = chunk
    return tuple(shape), (chop_axis, along_axis, chunk)
