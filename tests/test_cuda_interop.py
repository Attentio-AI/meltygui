"""CUDA→GL interop: device-to-device tensor upload through a registered PBO,
verified by reading the texture back and comparing against the tensor. Skips
cleanly on machines without CUDA or a GL-enabled pycuda."""

import numpy as np
import OpenGL.GL as gl
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip('meltygui_pycuda.gl')

import meltygui.tensor.cuda_interop as cuda_interop
from meltygui.core.graphics.gl_state import GLState

needs_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="no CUDA device")


@pytest.fixture
def cuda_state(gl_context):
    if not cuda_interop.ensure_context():
        pytest.skip("no CUDA context available alongside the GL context")
    st = GLState()
    yield st
    st.release()
    GLState.flush_deletes()


def _readback(tex):
    gl.glBindTexture(gl.GL_TEXTURE_3D, tex.texture_id)
    raw = gl.glGetTexImage(gl.GL_TEXTURE_3D, 0, gl.GL_RED, gl.GL_FLOAT)
    gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
    arr = np.frombuffer(raw, np.float32) if isinstance(raw, bytes) else np.asarray(raw, np.float32)
    return arr.reshape(tex.shape)


@needs_cuda
def test_roundtrip_exact(cuda_state):
    t = (torch.arange(4 * 5 * 6, dtype=torch.float32).reshape(4, 5, 6) / 120.0).cuda()
    tex = cuda_interop.tensor_to_texture(cuda_state, "v", t, version=1)
    assert tex is not None and tex.shape == (4, 5, 6)
    assert np.array_equal(_readback(tex), t.cpu().numpy())


@needs_cuda
def test_version_gates_reupload(cuda_state):
    t = torch.zeros(2, 3, 4, dtype=torch.float32).cuda()
    tex = cuda_interop.tensor_to_texture(cuda_state, "v", t, version=1)
    t.fill_(5.0)
    # Same version → stale texture by design (no copy issued).
    tex = cuda_interop.tensor_to_texture(cuda_state, "v", t, version=1)
    assert _readback(tex).max() == 0.0
    # Bumped version → fresh device-to-device copy.
    tex = cuda_interop.tensor_to_texture(cuda_state, "v", t, version=2)
    assert _readback(tex).min() == 5.0


@needs_cuda
def test_half_precision(cuda_state):
    t = torch.linspace(0, 1, 2 * 3 * 4, dtype=torch.float16).reshape(2, 3, 4).cuda()
    tex = cuda_interop.tensor_to_texture(cuda_state, "v16", t, version=1)
    assert tex is not None
    assert np.allclose(_readback(tex), t.float().cpu().numpy(), atol=2e-3)


@needs_cuda
def test_internal_format_pairs_with_dtype(cuda_state):
    """R16F for half, R32F for float — the pairing the PBO path depends on."""
    def fmt_of(tex):
        gl.glBindTexture(gl.GL_TEXTURE_3D, tex.texture_id)
        fmt = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_3D, 0, gl.GL_TEXTURE_INTERNAL_FORMAT)
        gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
        return int(fmt[0]) if hasattr(fmt, "__len__") else int(fmt)

    t16 = torch.zeros(2, 2, 2, dtype=torch.float16).cuda()
    tex16 = cuda_interop.tensor_to_texture(cuda_state, "fmt16", t16, version=1)
    assert fmt_of(tex16) == int(gl.GL_R16F) and tex16.internal_format == int(gl.GL_R16F)
    t32 = torch.zeros(2, 2, 2, dtype=torch.float32).cuda()
    tex32 = cuda_interop.tensor_to_texture(cuda_state, "fmt32", t32, version=1)
    assert fmt_of(tex32) == int(gl.GL_R32F) and tex32.internal_format == int(gl.GL_R32F)


@needs_cuda
def test_shape_change_recreates_composite(cuda_state):
    a = torch.ones(2, 2, 2, dtype=torch.float32).cuda()
    tex_a = cuda_interop.tensor_to_texture(cuda_state, "v", a, version=1)
    b = torch.ones(3, 3, 3, dtype=torch.float32).cuda()
    tex_b = cuda_interop.tensor_to_texture(cuda_state, "v", b, version=2)
    assert tex_b.shape == (3, 3, 3) and tex_b.texture_id != tex_a.texture_id
    # Old composite queued; its deleter (unregister → delete buffer/texture)
    # must run clean in the flush.
    assert GLState.flush_deletes() >= 1


@needs_cuda
def test_tensor_on_other_device_gets_moved(cuda_state):
    """A tensor living on a different GPU than the interop (GL) device is
    moved by torch first — never a raw cross-device memcpy_dtod (no peer
    access between 4090s; the raw copy is a driver fault)."""
    if torch.cuda.device_count() < 2:
        pytest.skip("needs a second CUDA device")
    t = torch.full((2, 2, 2), 3.0, dtype=torch.float32, device="cuda:1")
    tex = cuda_interop.tensor_to_texture(cuda_state, "vx", t, version=1)
    assert tex is not None
    assert _readback(tex).min() == 3.0


@needs_cuda
def test_fallback_gates(cuda_state):
    cpu = torch.zeros(2, 2, 2)
    assert cuda_interop.tensor_to_texture(cuda_state, "v", cpu, version=1) is None
    ints = torch.zeros(2, 2, 2, dtype=torch.int32).cuda()
    assert cuda_interop.tensor_to_texture(cuda_state, "v", ints, version=1) is None
    # Non-contiguous is fine - made contiguous (a device-local copy) first.
    noncontig = torch.zeros(4, 4, 4).cuda().permute(2, 1, 0)
    assert cuda_interop.tensor_to_texture(cuda_state, "vnc", noncontig, version=1) is not None


@needs_cuda
def test_voxel_io_preserves_source_for_renderer(gl_context):
    """End to end through the host io: a CUDA tensor reaches view_func as a
    GLTexture via the interop resource (volume_cuda), not the cpu one."""
    if not cuda_interop.ensure_context():
        pytest.skip("no CUDA context available alongside the GL context")
    from test_render_func_integration import _init_melty, _tick_frame
    from conftest import begin_frame, end_frame
    from meltygui.tensor.voxel_playground import voxel_io

    meltygui = _init_melty()
    got = {}

    def view_stub(input_value=None, **kw):
        got["tex"] = input_value
        return False, input_value

    t = torch.rand(4, 5, 6).cuda()
    for _ in range(2):
        _tick_frame(meltygui)
        begin_frame()
        voxel_io(input_value=t, view_func=view_stub, name="VoxIOCuda")
        end_frame()

    assert got["tex"] is t
    # Conversion was deliberately deferred: no upload resources on the IO host.
    for draw_state in meltygui.vis.root.draw_state_registry.values():
        state = draw_state.misc.get("gl_state")
        assert state is None or "volume_cuda" not in state._resources
