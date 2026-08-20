"""cuda_march: the in-place CUDA raymarcher behind draw_voxels(cuda_march=True).
Kernel equivalence (strided view vs materialized volume, neural flow,
normalize, any dtype) and a real-GL check that the CUDA image blitted into
the FBO matches the GL voxel_pass on the same volume."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
import pytest
import torch

pytestmark = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs CUDA")

from src.lsd.gl_gui import cuda_march as cm
from src.lsd.gl_gui.view.playground.voxel_playground import (
    TensorDim, slice_volume, slice_volume_view)

DEV = "cuda:0"


def _march(vol_or_cv, lut, W=160, H=120, **kw):
    out = torch.zeros(H, W, 4, dtype=torch.uint8, device=DEV)
    if hasattr(vol_or_cv, "nf"):
        cv = vol_or_cv
        cm.march(cv.view, out, lut, display_shape=cv.shape, nf=cv.nf, norm=cv.norm, **kw)
    else:
        v = vol_or_cv.float().contiguous()
        cm.march(v, out, lut, display_shape=tuple(v.shape), **kw)
    return out.cpu().numpy()


@pytest.fixture(scope="module")
def lut():
    return torch.tensor([[0, 0, 1], [0, 1, 0], [1, 0, 0]], dtype=torch.float32, device=DEV)


@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16, torch.float16,
                                   torch.int32, torch.uint8, torch.bool, torch.float64])
def test_view_matches_materialized_any_dtype(lut, dtype):
    torch.manual_seed(0)
    t = torch.rand(4, 30, 24, 50, device=DEV) * 2 - 1
    if dtype in (torch.int32, torch.uint8):
        t = (t.abs() * 3).to(dtype)
    else:
        t = (t > 0) if dtype == torch.bool else t.to(dtype)
    kw = dict(x_dim=3, y_dim=2, z_dim=1, slices=(2,))
    vol, m1, s1 = slice_volume(t, (), **kw)
    cv = slice_volume_view(t, (), **kw)
    assert (m1, s1, tuple(vol.shape)) == (cv.mapping, cv.source_shape, cv.shape)
    # a VIEW over the source storage - no copy
    assert cv.view.untyped_storage().data_ptr() == t.untyped_storage().data_ptr()
    assert cv.view.dtype == t.dtype                             # any dtype pass
    cam = dict(tilt=0.4, spin=0.9, zoom=3.4, volume_scale=(1.0, 0.48, 0.6),
               step_size=0.004, max_steps=2000, threshold=0.3, density=0.7,
               brightness=1.2, centered=True)
    a, b = _march(vol, lut, **cam), _march(cv, lut, **cam)
    assert a[..., 3].any()
    assert np.array_equal(a, b)


@pytest.mark.parametrize("case", [
    dict(x_dim=3, y_dim=4, z_dim=2, slices=(2, 3), normalize=True),
    dict(x_dim=4, y_dim=2, z_dim=3, slices=(0, 1), nf_on=True, nf_chop=TensorDim(4),
         nf_along=TensorDim(2), nf_chunk=16, nf_pad=True),
    dict(x_dim=4, y_dim=2, z_dim=3, slices=(0, 1), nf_on=True, nf_chop=TensorDim(4),
         nf_along=TensorDim(2), nf_chunk=16, nf_pad=False),   # 70 % 16 → no-op
    dict(x_dim=4, y_dim=2, z_dim=3, mean_dims=(1,), normalize=True),
    dict(x_dim=2, y_dim=3, z_dim=4, sort_dim=2, slices=(1, 2)),
])
def test_view_matches_materialized_features(lut, case):
    torch.manual_seed(1)
    t = torch.randn(3, 5, 40, 33, 70, device=DEV).to(torch.bfloat16)
    vol, m1, s1 = slice_volume(t, (), **case)
    cv = slice_volume_view(t, (), **case)
    assert (m1, s1, tuple(vol.shape)) == (cv.mapping, cv.source_shape, cv.shape)
    longest = max(cv.shape)
    vs = (cv.shape[2] / longest, cv.shape[1] / longest, cv.shape[0] / longest)
    cam = dict(tilt=0.4, spin=0.9, zoom=3.4, volume_scale=vs, step_size=0.004,
               max_steps=2000, threshold=0.3, density=0.7, brightness=1.2)
    a, b = _march(vol, lut, **cam), _march(cv, lut, **cam)
    assert a[..., 3].any()
    assert np.array_equal(a, b)


def test_nf_display_shape():
    assert cm.nf_display_shape((8, 16, 64), 2, 0, 16) == ((32, 16, 16), (2, 0, 16))
    assert cm.nf_display_shape((8, 16, 70), 2, 0, 16) == ((40, 16, 16), (2, 0, 16))  # pad
    assert cm.nf_display_shape((8, 16, 70), 2, 0, 16, pad=False) == ((8, 16, 70), (-1, -1, 0))
    assert cm.nf_display_shape((8, 16, 12), 2, 0, 16) == ((8, 16, 12), (-1, -1, 0))  # fits
    assert cm.nf_display_shape((8, 16, 64), -1, 0, 16) == ((8, 16, 64), (-1, -1, 0))


# ── real GL: the CUDA image through _cuda_render/image_blit_pass matches the
# GL voxel_pass on the same volume (nearest, shading/plane off) ─────────────

@pytest.fixture
def st(gl_context):
    from src.lsd.gl_gui.gl_state import GLState
    state = GLState()
    yield state
    state.release()
    GLState.flush_deletes()


def test_cuda_image_matches_gl_voxel_pass(st):
    import OpenGL.GL as gl
    from src.lsd.gl_gui.view.playground.voxel_playground import (
        CudaVolumeView, LUTS, _cuda_render, image_blit_pass, voxel_pass)
    torch.manual_seed(2)
    vol = (torch.rand(24, 32, 40, device=DEV) > 0.93).float() * 0.9
    W, H = 128, 96
    cam = dict(tilt=0.35, spin=0.8, zoom=3.4, pan=(0.0, 0.0, 0.0), ortho=False,
               volume_scale=(1.0, 0.8, 0.6), step_size=0.01, max_steps=600,
               density=0.7, threshold=0.3, brightness=1.0, contrast=1.0,
               gamma=1.6, centered=False)

    def read():
        raw = gl.glReadPixels(0, 0, W, H, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        return np.frombuffer(raw, np.uint8).reshape(H, W, 4).copy()

    fb = st.fbo("target", W, H)
    # GL path
    tex = st.texture3d("volume", vol.cpu().numpy(), version=1)
    lut_tex = st.texture1d("lut", LUTS["jet"], version=("jet",))
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST); gl.glDisable(gl.GL_BLEND)
        gl.glClearColor(0, 0, 0, 0); gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        voxel_pass(st, volume=tex, volume_lin=tex, lut=lut_tex, aspect=W / H,
                   pan_x=0.0, pan_y=0.0, pan_z=0.0,
                   draw_plane=False, draw_shading=False, self_shading=False,
                   **{k: v for k, v in cam.items() if k != "pan"})
        a = read()
    assert voxel_pass.last_error is None, voxel_pass.last_error
    # CUDA path
    cv = CudaVolumeView(vol, vol.shape, (-1, -1, 0), (0.0, 1.0, 0), (0, 1, 2), vol.shape)
    img = _cuda_render(st, cv, W, H, lut="jet", **cam)
    assert img is not None
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST); gl.glDisable(gl.GL_BLEND)
        gl.glClearColor(0, 0, 0, 0); gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        image_blit_pass(st, image=img)
        b = read()
    assert image_blit_pass.last_error is None, image_blit_pass.last_error
    assert a[..., 3].any() and b[..., 3].any()
    lit = (a[..., 3] > 0) | (b[..., 3] > 0)
    diff = np.abs(a.astype(int) - b.astype(int)).max(-1)
    # Same camera, volume, transfer function and encode; the two paths only
    # differ in float rounding (GL texture coords vs floorf(p*n) at voxel
    # seams), so all but a sliver of boundary pixels must be identical.
    assert (diff[lit] > 8).mean() < 0.02, (diff[lit] > 8).mean()
    assert np.median(diff[lit]) <= 1


def test_shading_touches_only_the_floor_unless_self():
    """The new shading model: draw_shading + self OFF leaves every volume
    pixel identical to shading-off (only the floor shadow differs);
    self ON darkens volume pixels via transmittance, still no normals."""
    torch.manual_seed(3)
    z, y, x = torch.meshgrid(torch.linspace(-1, 1, 24), torch.linspace(-1, 1, 32),
                             torch.linspace(-1, 1, 40), indexing="ij")
    vol = torch.exp(-(x * x + y * y * 1.5 + z * z * 2.0) * 3.0).to(DEV) * 0.9
    lut = torch.tensor([[0, 0, 1], [0, 1, 0], [1, 0, 0]], dtype=torch.float32, device=DEV)
    vs = (1.0, 0.8, 0.6)
    kw = dict(display_shape=tuple(vol.shape), tilt=0.35, spin=0.8, zoom=3.4,
              volume_scale=vs, step_size=0.01, max_steps=600,
              threshold=0.3, density=0.7)
    fmap = cm.build_floor_map(vol, display_shape=tuple(vol.shape), volume_scale=vs,
                              threshold=0.3, density=0.7)
    mip = cm.build_mip(vol, display_shape=tuple(vol.shape))
    def render(sp, **extra):
        sh = torch.tensor(sp, dtype=torch.float32, device=DEV)
        out = torch.zeros(120, 160, 4, dtype=torch.uint8, device=DEV)
        cm.march(vol, out, lut, shade=sh, **kw, **extra)
        return out.cpu().numpy().astype(int)
    off = render(cm.shade_params())
    plane_only = render(cm.shade_params(draw_plane=True, draw_shading=True),
                        floor_map=fmap, floor_extent=fmap.extent)
    selfed = render(cm.shade_params(draw_plane=True, draw_shading=True, self_shading=True),
                    floor_map=fmap, floor_extent=fmap.extent, mip=mip)
    vol_px = off[..., 3] > 0
    d_plane = np.abs(plane_only - off).max(-1)
    # the floor shadow differs...
    assert (d_plane > 10).any()
    # ...but pixels the volume fully covers (opaque, no floor behind) are
    # untouched: where the volume is opaque the plane can't shine.
    opaque = off[..., 3] >= 250
    assert d_plane[opaque].max() <= 1, d_plane[opaque].max()
    # self-shading darkens volume pixels (transmittance), never brightens
    d_self = selfed[..., :3] - plane_only[..., :3]
    assert (d_self.min(-1) < -10).sum() > 50  # visibly darkened somewhere
    assert d_self[vol_px].max() <= 1          # and only ever darkens


def test_build_mip_fk_semantics():
    """(f, k) opacity mip: f = fraction of the block at/above the gate,
    k = mean sub-gate opacity density."""
    gate = 1.0 - 0.3
    # identity: an opaque voxel -> f=1 k=0; a sub-gate voxel -> f=0, k=r^4*density*50
    t = torch.zeros(2, 2, 2, device=DEV); t[0, 0, 0] = 1.0; t[0, 0, 1] = 0.5
    mip = cm.build_mip(t, display_shape=(2, 2, 2))
    assert mip.shape == (2, 2, 2, 2)
    assert abs(mip[0, 0, 0, 0].item() - 1.0) < 1e-3 and mip[0, 0, 0, 1].item() == 0.0
    k_half = (0.5 / gate) ** 4 * 0.7 * 50.0
    assert mip[0, 0, 1, 0].item() == 0.0
    assert abs(mip[0, 0, 1, 1].item() - k_half) < 0.02 * k_half
    # downsampled: a lone opaque spike in a 2^3 block -> f = 1/8 (coverage)
    t2 = torch.zeros(4, 4, 4, device=DEV); t2[0, 0, 0] = 1.0
    mip2 = cm.build_mip(t2, display_shape=(4, 4, 4), cap=2)
    assert abs(mip2[0, 0, 0, 0].item() - 1.0 / 8.0) < 1e-3
    assert mip2[1, 1, 1, 0].item() == 0.0 and mip2[1, 1, 1, 1].item() == 0.0
    # a SOLID opaque block stays f=1
    t3 = torch.ones(4, 4, 4, device=DEV)
    mip3 = cm.build_mip(t3, display_shape=(4, 4, 4), cap=2)
    assert abs(mip3[0, 0, 0, 0].item() - 1.0) < 1e-3


def test_mip_sparse_occluders_still_shadow():
    """A flat sparse volume (the flat-tensor-on-the-floor case): heavy
    downsampling must not erase the floor shadow. Compare the mip-shaded
    floor against the no-mip (full-res taps) reference."""
    torch.manual_seed(5)
    vol = torch.zeros(4, 512, 512, device=DEV)          # flat slab, z thin
    m = torch.rand(4, 512, 512, device=DEV) > 0.995     # sparse bright voxels
    vol[m] = 1.0
    lut = torch.tensor([[1, 1, 1]] * 2, dtype=torch.float32, device=DEV)
    kw = dict(display_shape=tuple(vol.shape), tilt=0.9, spin=0.6, zoom=3.0,
              volume_scale=(1.0, 1.0, 4 / 512), step_size=0.01, max_steps=400,
              threshold=0.3, density=0.7)
    def render(mip, draw_plane):
        sh = torch.tensor(cm.shade_params(draw_plane=draw_plane, draw_shading=True),
                          dtype=torch.float32, device=DEV)
        out = torch.zeros(160, 160, 4, dtype=torch.uint8, device=DEV)
        cm.march(vol, out, lut, mip=mip, shade=sh, **kw)
        return out.cpu().numpy().astype(int)
    def shadow_px(mip):
        # the shadow = what the plane adds over the plane-off render
        diff = np.abs(render(mip, True) - render(mip, False)).max(-1)
        return int((diff > 10).sum())
    ref = shadow_px(None)
    got = shadow_px(cm.build_mip(vol, display_shape=tuple(vol.shape)))
    assert ref > 100, ref                       # the scene does cast shadow
    # RMS calibration: the mip's shadow footprint tracks the full-res
    # shadow (not vanished like avg, not 9x smeared like max)
    assert ref * 0.5 < got < ref * 2.5, (got, ref)


def test_baked_floor_map_matches_live_march():
    """The baked floor map must reproduce the no-mip live march (same math,
    full res) — and beat the mip path's approximation on sparse data."""
    torch.manual_seed(7)
    vol = torch.zeros(4, 256, 256, device=DEV)
    vol[torch.rand(4, 256, 256, device=DEV) > 0.995] = 1.0
    lut = torch.tensor([[1, 1, 1]] * 2, dtype=torch.float32, device=DEV)
    vs = (1.0, 1.0, 4 / 256)
    kw = dict(display_shape=tuple(vol.shape), tilt=0.9, spin=0.6, zoom=3.0,
              volume_scale=vs, step_size=0.01, max_steps=400,
              threshold=0.3, density=0.7)
    sh = torch.tensor(cm.shade_params(draw_plane=True, draw_shading=True),
                      dtype=torch.float32, device=DEV)
    def render(**extra):
        out = torch.zeros(200, 200, 4, dtype=torch.uint8, device=DEV)
        cm.march(vol, out, lut, shade=sh, **kw, **extra)
        return out.cpu().numpy().astype(int)
    ref = render()                                        # live full-res march
    # steps=24 matches the live path's budget exactly (the production
    # default of 64 is deliberately MORE precise, which would differ)
    fmap = cm.build_floor_map(vol, display_shape=tuple(vol.shape),
                              volume_scale=vs, threshold=0.3, density=0.7,
                              steps=24)
    got = render(floor_map=fmap, floor_extent=fmap.extent)
    diff = np.abs(ref - got).max(-1)
    # Pixels the plane doesn't touch must be untouched by the map path.
    sh_np = torch.tensor(cm.shade_params(draw_plane=False, draw_shading=True),
                         dtype=torch.float32, device=DEV)
    out_np = torch.zeros(200, 200, 4, dtype=torch.uint8, device=DEV)
    cm.march(vol, out_np, lut, shade=sh_np, **kw)
    floorish = np.abs(ref - out_np.cpu().numpy().astype(int)).max(-1) > 2
    assert floorish.any()
    assert np.median(diff[~floorish]) == 0
    # Floor pixels match in bulk. Exact equality is impossible BY DESIGN:
    # the live path's blur taps march at 10 steps while its center ray uses
    # 24 - the map has uniform 24-step values for both, so penumbra
    # pixels differ (the baked result is the more consistent one).
    assert np.mean(diff[floorish]) < 25, np.mean(diff[floorish])
    # and it IS the precise shadow: closer to ref than the mip approximation
    mip = cm.build_mip(vol, display_shape=tuple(vol.shape))
    approx = render(mip=mip)
    assert np.abs(ref - got).sum() < np.abs(ref - approx).sum()


def test_floor_map_size_tracks_data():
    # ~2 texels per voxel incl. the 1.5 margin, 16-aligned
    assert cm.floor_map_size((4, 100, 200)) == (608, 304)
    # small footprints keep the floor of 256 (oversampled = crisp)
    assert cm.floor_map_size((4, 32, 48)) == (256, 256)
    # huge axes ride the cap
    assert cm.floor_map_size((4, 4096, 4096)) == (3072, 3072)
    # rect maps: extents follow volume_scale per axis
    Rx, Ry = cm.floor_map_extent((1.0, 0.25, 0.1))
    assert abs(Rx - 1.5) < 1e-6 and abs(Ry - 0.375) < 1e-6
