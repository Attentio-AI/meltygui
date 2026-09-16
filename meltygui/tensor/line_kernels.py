"""Line rasterization directly from strided CUDA tensor storage.

Only per-line range metadata and the final RGBA image are allocated. Source
samples are read in place, in their original dtype; no packed series texture.
"""
import numpy as np
from meltygui.tensor import cuda_march as kernels

_SOURCE = kernels.KERNEL.split('struct Vol {', 1)[0] + r'''
extern "C" __global__ void line_ranges(const unsigned char *data, int dtype,
    int nl, int ns, long long sl, long long ss, float *ranges) {
    int l = blockIdx.x * blockDim.x + threadIdx.x;
    if (l >= nl) return;
    float lo = INFINITY, hi = -INFINITY;
    for (int s = 0; s < ns; ++s) {
        float v = load_at(data, dtype, l * sl + s * ss);
        if (isfinite(v)) { lo = fminf(lo, v); hi = fmaxf(hi, v); }
    }
    ranges[l*2] = lo; ranges[l*2+1] = hi;
}
extern "C" __global__ void lines_image(const unsigned char *data, int dtype,
    int nl, int ns, long long sl, long long ss, const float *ranges, int norm,
    __half *out, int w, int h, float zx, float zy, float px, float py,
    float ymin, float ymax, float margin, float ux, float uy, float lw, float opacity,
    const float *lut, int lutn, float red, float green, float blue) {
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= w || y >= h) return;
    float r=0, g=0, b=0, a=0;
    float dx = ns > 1 ? 2.f * margin * zx * ux / (ns-1) : 1.f;
    float xzero = w*.5f + (-margin-px)*zx*ux;
    float pad = lw*.5f + 1.f;
    int first = max(0, (int)floorf((x+.5f-pad-xzero)/dx)-1);
    int last = min(ns-2, (int)ceilf((x+.5f+pad-xzero)/dx));
    for (int l=0; l<nl; ++l) {
        float dist = INFINITY;
        for (int s=first; s<=last; ++s) {
            float v0=load_at(data,dtype,l*sl+s*ss);
            float v1=load_at(data,dtype,l*sl+(s+1)*ss);
            if (!isfinite(v0) || !isfinite(v1)) continue;
            if (norm) {
                float lo=ranges[l*2], scale=fmaxf(ranges[l*2+1]-lo, 1.e-12f);
                v0=(v0-lo)/scale; v1=(v1-lo)/scale;
            }
            float ax=xzero+s*dx, bx=ax+dx;
            float ay=h*.5f+(((v0-ymin)/(ymax-ymin)*2.f-1.f)*margin-py)*zy*uy;
            float by=h*.5f+(((v1-ymin)/(ymax-ymin)*2.f-1.f)*margin-py)*zy*uy;
            float vx=bx-ax, vy=by-ay, qx=x+.5f-ax, qy=y+.5f-ay;
            float t=fminf(1.f,fmaxf(0.f,(qx*vx+qy*vy)/fmaxf(vx*vx+vy*vy,1.e-12f)));
            dist=fminf(dist,hypotf(qx-t*vx,qy-t*vy));
        }
        float alpha=fminf(1.f,fmaxf(0.f,lw*.5f+.5f-dist))*opacity;
        if (alpha > 0) {
            int ci=min(lutn-1, (int)((l+.5f)/nl*lutn));
            float cr=nl>1?lut[ci*3]:red, cg=nl>1?lut[ci*3+1]:green, cb=nl>1?lut[ci*3+2]:blue;
            r=powf(fmaxf(cr,0.f),2.2f)*alpha+r*(1-alpha);
            g=powf(fmaxf(cg,0.f),2.2f)*alpha+g*(1-alpha);
            b=powf(fmaxf(cb,0.f),2.2f)*alpha+b*(1-alpha);
            a=alpha+a*(1-alpha);
        }
    }
    int o=(y*w+x)*4;
    out[o]=__float2half(r); out[o+1]=__float2half(g);
    out[o+2]=__float2half(b); out[o+3]=__float2half(a);
}
'''
# Keep compiled CUDA modules alive across hotswap, like the volume kernels.
import sys
_CACHE = sys.__dict__.setdefault('_melty_cuda_line_kernels', {})


def _functions(device):
    entry = _CACHE.get(device)
    if entry is None or entry[0] != _SOURCE:
        import meltygui_pycuda.driver as cuda
        from meltygui_pycuda.compiler import SourceModule
        cc = cuda.Device(device).compute_capability()
        module = SourceModule(_SOURCE, no_extern_c=True, arch='sm_%d%d' % cc,
                              options=['-O3'] + kernels._host_compiler_flags())
        entry = (_SOURCE, module, module.get_function('line_ranges'), module.get_function('lines_image'))
        _CACHE[device] = entry
    return entry[2:]


def ranges(lines):
    """Small range metadata, calculated without masking/copying input values."""
    import torch
    result = torch.empty((lines.shape[0], 2), dtype=torch.float32, device=lines.device)
    with kernels._Pushed(lines.device.index or 0):
        function, _ = _functions(lines.device.index or 0)
        function(np.uintp(lines.data_ptr()), np.int32(kernels.dtype_code(lines)),
                 np.int32(lines.shape[0]), np.int32(lines.shape[1]),
                 np.int64(lines.stride(0)), np.int64(lines.stride(1)),
                 np.uintp(result.data_ptr()), block=(128, 1, 1),
                 grid=((lines.shape[0]+127)//128, 1, 1))
    return result


def render(lines, stats, out, lut, *, normalize=False, zoom_x=1., zoom_y=1.,
           pan_x=0., pan_y=0., y_range=(0., 1.), margin=.92,
           unit=(80., 60.), line_width=1.5, line_opacity=1., single_color=(.35, .75, 1.)):
    """Read original CUDA samples directly; write only the output image."""
    h, w = out.shape[:2]
    i, f = np.int32, np.float32
    with kernels._Pushed(lines.device.index or 0):
        _, function = _functions(lines.device.index or 0)
        function(np.uintp(lines.data_ptr()), i(kernels.dtype_code(lines)),
                 i(lines.shape[0]), i(lines.shape[1]),
                 np.int64(lines.stride(0)), np.int64(lines.stride(1)),
                 np.uintp(stats.data_ptr()), i(normalize), np.uintp(out.data_ptr()), i(w), i(h),
                 *map(f, (zoom_x, zoom_y, pan_x, pan_y, *y_range, margin,
                          *unit, line_width, line_opacity)),
                 np.uintp(lut.data_ptr()), i(lut.shape[0]), *map(f, single_color),
                 block=(16, 16, 1), grid=((w+15)//16, (h+15)//16, 1))
    return out
