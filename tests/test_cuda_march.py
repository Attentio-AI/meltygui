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
