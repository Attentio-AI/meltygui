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

# Per-device compiled kernels + retained primary contexts. globals().get
# avoids hotswap re-exec (a re-exec must not recompile or re-retain).
_KERNELS = globals().get("_KERNELS") or {}
_CONTEXTS = globals().get("_CONTEXTS") or {}
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

// Nearest sample at texcoord p in [0,1]^3 (x ↔ width, y ↔ height, z ↔ depth)
// — the GL path's texture(vol, p).r with GL_NEAREST.
__device__ __forceinline__ float sample(const Vol& v, float3 p) {
    int d[3];
    d[2] = min(max((int)floorf(p.x * v.nx), 0), v.nx - 1);
    d[1] = min(max((int)floorf(p.y * v.ny), 0), v.ny - 1);
    d[0] = min(max((int)floorf(p.z * v.nz), 0), v.nz - 1);
    if (v.nf_chop >= 0) {
        // cat(split(chop, chunk), along): display along-index a' = j*A + i
        // (block j, original i); display chop-index c' → source j*chunk + c'.
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

// texture(lut, t) on a LINEAR-filtered 1-D texture: texel centers at
// (i + 0.5)/n, edge-clamped — matched so the two paths color identically.
__device__ __forceinline__ float3 lut_at(const float* __restrict__ lut, int n, float t) {
    float f = fminf(fmaxf(t * (float)n - 0.5f, 0.0f), (float)(n - 1));
    int i = (int)floorf(f); int k = min(i + 1, n - 1); float w = f - (float)i;
    return make_float3(lut[3*i]   * (1.f - w) + lut[3*k]   * w,
                       lut[3*i+1] * (1.f - w) + lut[3*k+1] * w,
                       lut[3*i+2] * (1.f - w) + lut[3*k+2] * w);
}

extern "C" __global__ void march(
    const unsigned char* __restrict__ data, int dtype,
    int nz, int ny, int nx, int snz, int sny, int snx,
    long long sz, long long sy, long long sx,
    int nf_chop, int nf_along, int nf_chunk,
    float nlo, float nhi, int norm_mode,
    const float* __restrict__ lut, int lut_n,
    unsigned char* __restrict__ out, int W, int H,
    float tilt, float spin, float zoom, float pan_x, float pan_y, float pan_z,
    int ortho, float aspect, float vsx, float vsy, float vsz,
    float step_size, int max_steps, float density, float threshold,
    float brightness, float contrast, float gamma, int centered)
{
    int px = blockIdx.x * blockDim.x + threadIdx.x;
    int py = blockIdx.y * blockDim.y + threadIdx.y;
    if (px >= W || py >= H) return;
    Vol v = {data, dtype, nz, ny, nx, snz, sny, snx, sz, sy, sx,
             nf_chop, nf_along, nf_chunk, nlo, nhi, norm_mode};

    // ── the GL pass's z-up orbit camera, verbatim ──
    float u = ((float)px + 0.5f) / (float)W, w = ((float)py + 0.5f) / (float)H;
    float ct = cosf(tilt), st = sinf(tilt), cs = cosf(spin), ss = sinf(spin);
    float3 fwd = make_float3(-cs * ct, -ss * ct, -st);
    float3 right = make_float3(-ss, cs, 0.0f);
    float3 up = make_float3(right.y * fwd.z - right.z * fwd.y,
                            right.z * fwd.x - right.x * fwd.z,
                            right.x * fwd.y - right.y * fwd.x);
    float3 eye = make_float3(pan_x - fwd.x * zoom, pan_y - fwd.y * zoom, pan_z - fwd.z * zoom);
    float ndx = (u * 2.0f - 1.0f) * aspect, ndy = w * 2.0f - 1.0f;
    float3 ro, rd;
    if (ortho) {
        float k = zoom / 1.7f;
        ro = make_float3(eye.x + (right.x * ndx + up.x * ndy) * k,
                         eye.y + (right.y * ndx + up.y * ndy) * k,
                         eye.z + (right.z * ndx + up.z * ndy) * k);
        rd = fwd;
    } else {
        ro = eye;
        rd = make_float3(fwd.x * 1.7f + right.x * ndx + up.x * ndy,
                         fwd.y * 1.7f + right.y * ndx + up.y * ndy,
                         fwd.z * 1.7f + right.z * ndx + up.z * ndy);
        float il = rsqrtf(rd.x * rd.x + rd.y * rd.y + rd.z * rd.z);
        rd.x *= il; rd.y *= il; rd.z *= il;
    }
    float view_cos = rd.x * fwd.x + rd.y * fwd.y + rd.z * fwd.z;

    // ── slab test against the box [-vs, +vs] ──
    float3 inv = make_float3(1.0f / rd.x, 1.0f / rd.y, 1.0f / rd.z);
    float t0x = (-vsx - ro.x) * inv.x, t1x = (vsx - ro.x) * inv.x;
    float t0y = (-vsy - ro.y) * inv.y, t1y = (vsy - ro.y) * inv.y;
    float t0z = (-vsz - ro.z) * inv.z, t1z = (vsz - ro.z) * inv.z;
    float tin = fmaxf(fmaxf(fminf(t0x, t1x), fminf(t0y, t1y)), fminf(t0z, t1z));
    float tout = fminf(fminf(fmaxf(t0x, t1x), fmaxf(t0y, t1y)), fmaxf(t0z, t1z));
    float4 acc = make_float4(0.f, 0.f, 0.f, 0.f);
    if (!(tin > tout || tout < 0.0f)) {
        float t = fmaxf(tin, 0.0f);
        float gate = 1.0f - fminf(fmaxf(threshold, 0.0f), 0.999f);
        // seg → fraction of the volume traversed (the GL path's seg_n)
        float rdn = sqrtf((rd.x / vsx) * (rd.x / vsx) + (rd.y / vsy) * (rd.y / vsy)
                          + (rd.z / vsz) * (rd.z / vsz));
        for (int i = 0; i < max_steps; i++) {
            if (t >= tout || acc.w > 0.98f) break;
            float seg = fminf(step_size, tout - t);
            float tm = t + seg * 0.5f;
            float3 p = make_float3((ro.x + rd.x * tm) / vsx * 0.5f + 0.5f,
                                   (ro.y + rd.y * tm) / vsy * 0.5f + 0.5f,
                                   (ro.z + rd.z * tm) / vsz * 0.5f + 0.5f);
            // remapValue: contrast about mid-grey, then brightness
            float val = sample(v, p);
            if (centered) val = val * 0.5f + 0.5f;
            val = (val - 0.5f) * contrast + 0.5f;
            float m;
            if (centered) {
                val = fminf(fmaxf(0.5f + (val - 0.5f) * brightness, 0.f), 1.f);
                m = fabsf(val - 0.5f) * 2.0f;
            } else {
                val = fminf(fmaxf(val * brightness, 0.f), 1.f);
                m = val;
            }
            float seg_n = seg * rdn * view_cos;
            float a;
            if (m >= gate) a = 1.0f;
            else { float r = m / gate; r *= r; r *= r;
                   a = fminf(fmaxf(r * density * seg_n * 50.0f, 0.f), 1.f); }
            if (a > 0.0f) {
                float3 c = lut_at(lut, lut_n, val);
                c.x = powf(c.x, 2.2f); c.y = powf(c.y, 2.2f); c.z = powf(c.z, 2.2f);
                float k = (1.0f - acc.w) * a;
                acc.x += k * c.x; acc.y += k * c.y; acc.z += k * c.z;
                acc.w += (1.0f - acc.w) * a;
            }
            t += step_size;
        }
    }
    float g = gamma / 2.2f;
    float dither = (fmodf(52.9829189f * fmodf(
        ((float)px + 0.5f) * 0.06711056f + ((float)py + 0.5f) * 0.00583715f, 1.0f), 1.0f)
        - 0.5f) / 255.0f;
    unsigned char* o = out + ((long long)py * W + px) * 4;
    o[0] = (unsigned char)(fminf(fmaxf(powf(acc.x, g) + dither, 0.f), 1.f) * 255.0f + 0.5f);
    o[1] = (unsigned char)(fminf(fmaxf(powf(acc.y, g) + dither, 0.f), 1.f) * 255.0f + 0.5f);
    o[2] = (unsigned char)(fminf(fmaxf(powf(acc.z, g) + dither, 0.f), 1.f) * 255.0f + 0.5f);
    o[3] = (unsigned char)(fminf(fmaxf(acc.w + dither, 0.f), 1.f) * 255.0f + 0.5f);
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


def _kernel_for(dev_index):
    """Compile (once per device, nvcc-cached on disk by pycuda) and return
    the march function. Must be called with that device's context pushed."""
    fn = _KERNELS.get(dev_index)
    if fn is None:
        import pycuda.driver as cuda
        from pycuda.compiler import SourceModule
        cc = cuda.Device(int(dev_index)).compute_capability()
        mod = SourceModule(KERNEL, no_extern_c=True, arch="sm_%d%d" % cc,
                           options=["-O3"] + _host_compiler_flags())
        fn = mod.get_function("march")
        _KERNELS[dev_index] = fn
    return fn


def dtype_code(t):
    code = DTYPE_CODES.get(str(t.dtype))
    if code is None:
        raise ValueError(f"cuda_march: unsupported dtype {t.dtype}")
    return code


def march(view, out, lut, *, display_shape, nf=(-1, -1, 0), norm=(0.0, 1.0, 0),
          tilt=0.0, spin=0.0, zoom=3.4, pan=(0.0, 0.0, 0.0), ortho=False,
          aspect=1.0, volume_scale=(1.0, 1.0, 1.0), step_size=0.005,
          max_steps=512, density=0.7, threshold=0.3, brightness=1.0,
          contrast=1.0, gamma=1.6, centered=False):
    """Raymarch `view` (a 3-D torch view (z, y, x) on a CUDA device, ANY
    strides, ANY supported dtype) into `out` (torch uint8 (H, W, 4) on the
    same device; premultiplied RGBA8). `lut` is a float32 (n, 3) torch
    tensor on that device. `display_shape` = (nz, ny, nx) after neural
    flow; `nf` = (chop_axis, along_axis, chunk) with axes 0=z 1=y 2=x (chop
    -1 = off); `norm` = (lo, hi, mode)."""
    import torch
    assert view.dim() == 3 and out.dim() == 3 and out.shape[2] == 4
    dev = view.device.index or 0
    if (out.device.index or 0) != dev or (lut.device.index or 0) != dev:
        raise ValueError("cuda_march: view/out/lut must share a device")
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
           f32(tilt), f32(spin), f32(zoom), f32(pan[0]), f32(pan[1]), f32(pan[2]),
           i32(1 if ortho else 0), f32(aspect), f32(vsx), f32(vsy), f32(vsz),
           f32(step_size), i32(max_steps), f32(density), f32(threshold),
           f32(brightness), f32(contrast), f32(gamma), i32(1 if centered else 0),
           block=block, grid=grid)
    return out


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
