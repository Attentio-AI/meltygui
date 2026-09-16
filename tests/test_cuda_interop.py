"""CUDA→GL interop: device-to-device tensor upload through a registered PBO,
verified by reading the texture back and comparing against the tensor. Skips
cleanly on machines without CUDA or a GL-enabled pycuda."""

import numpy as np
import OpenGL.GL as gl
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip('meltygui_pycuda.gl')

from meltygui.core.graphics.cuda_interop_core import ensure_context
from meltygui.model.cuda_texture_model import tensor_to_texture
from meltygui.core.graphics.gl_state import GLState

needs_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="no CUDA device")


@pytest.fixture
def cuda_state(gl_context):
    if not ensure_context():
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
    tex = tensor_to_texture(cuda_state, "v", t, version=1)
    assert tex is not None and tex.shape == (4, 5, 6)
    assert np.array_equal(_readback(tex), t.cpu().numpy())


@needs_cuda
def test_version_gates_reupload(cuda_state):
    t = torch.zeros(2, 3, 4, dtype=torch.float32).cuda()
    tex = tensor_to_texture(cuda_state, "v", t, version=1)
    t.fill_(5.0)
    # Same version → stale texture by design (no copy issued).
    tex = tensor_to_texture(cuda_state, "v", t, version=1)
    assert _readback(tex).max() == 0.0
    # Bumped version → fresh device-to-device copy.
    tex = tensor_to_texture(cuda_state, "v", t, version=2)
    assert _readback(tex).min() == 5.0


@needs_cuda
def test_half_precision(cuda_state):
    t = torch.linspace(0, 1, 2 * 3 * 4, dtype=torch.float16).reshape(2, 3, 4).cuda()
    tex = tensor_to_texture(cuda_state, "v16", t, version=1)
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
    tex16 = tensor_to_texture(cuda_state, "fmt16", t16, version=1)
    assert fmt_of(tex16) == int(gl.GL_R16F) and tex16.internal_format == int(gl.GL_R16F)
    t32 = torch.zeros(2, 2, 2, dtype=torch.float32).cuda()
    tex32 = tensor_to_texture(cuda_state, "fmt32", t32, version=1)
    assert fmt_of(tex32) == int(gl.GL_R32F) and tex32.internal_format == int(gl.GL_R32F)


@needs_cuda
def test_shape_change_recreates_composite(cuda_state):
    a = torch.ones(2, 2, 2, dtype=torch.float32).cuda()
    tex_a = tensor_to_texture(cuda_state, "v", a, version=1)
    b = torch.ones(3, 3, 3, dtype=torch.float32).cuda()
    tex_b = tensor_to_texture(cuda_state, "v", b, version=2)
    assert tex_b.shape == (3, 3, 3) and tex_b.texture_id != tex_a.texture_id
    # Old composite queued; its deleter (unregister → delete buffer/texture)
    # must run clean in the flush.
    assert GLState.flush_deletes() >= 1


@needs_cuda
@pytest.mark.parametrize('source_device', range(torch.cuda.device_count()))
def test_tensor_on_each_device_gets_moved(cuda_state, source_device):
    """A tensor living on a different GPU than the interop (GL) device is
    moved by torch first — never a raw cross-device memcpy_dtod (no peer
    access between 4090s; the raw copy is a driver fault)."""
    t = torch.full((2, 2, 2), 3.0, dtype=torch.float32, device=f"cuda:{source_device}")
    tex = tensor_to_texture(cuda_state, "vx", t, version=1)
    assert tex is not None
    assert _readback(tex).min() == 3.0


@needs_cuda
def test_fallback_gates(cuda_state):
    cpu = torch.zeros(2, 2, 2)
    assert tensor_to_texture(cuda_state, "v", cpu, version=1) is None
    ints = torch.zeros(2, 2, 2, dtype=torch.int32).cuda()
    assert tensor_to_texture(cuda_state, "v", ints, version=1) is None
    # Non-contiguous is fine - made contiguous (a device-local copy) first.
    noncontig = torch.zeros(4, 4, 4).cuda().permute(2, 1, 0)
    assert tensor_to_texture(cuda_state, "vnc", noncontig, version=1) is not None


@needs_cuda
def test_voxel_view_preserves_cuda_source_and_reuses_volume(gl_context):
    from test_render_func_integration import _init_melty, _tick_frame
    from conftest import begin_frame, end_frame
    from meltygui.view.voxel_view import draw_voxels
    from meltygui.core.graphics.tensor_core import _voxels_cleanup

    runtime = _init_melty()
    source = torch.rand(4, 5, 6).cuda()
    states, resources = [], []

    def frame():
        _tick_frame(runtime)
        begin_frame()
        try:
            changed, value, draw_state = draw_voxels(
                source, name="CUDA volume", width=200, height=160,
                x_dim=2, y_dim=1, z_dim=0, cuda_march=True,
                name_size=0, num_size=0, draw_plane=False, draw_shading=False,
                show_bg=False, with_header=None, show_header=False, shadow=False,
                use_cache=False, return_extras=True)
            assert not changed
            assert value is source
            states.append(draw_state.misc["gl_state"])
            resources.append(states[-1].peek("cuda_view"))
            assert draw_state.misc["voxel_state"].cuda_error is None
            return draw_state
        finally:
            end_frame()

    frame()
    draw_state = frame()
    assert states[0] is states[1]
    assert resources[0] is resources[1]
    assert resources[0].view.data_ptr() == source.data_ptr()
    assert states[0].peek("cuda_image") is not None

    # An edit in the new feature module preserves camera edits, view identity,
    # injected state and GPU allocations; changed source defaults still apply.
    from pathlib import Path
    from meltygui.view import voxel_view
    from meltygui.code.file_converters import _recompile_module, stamp_module_baseline
    path = Path(voxel_view.__file__)
    original = path.read_text()
    stamp_module_baseline(voxel_view, original)
    draw_state.spin = 1.123
    local_state = draw_state.misc["voxel_state"]
    output = states[0].peek("cuda_out")
    image = states[0].peek("cuda_image")
    try:
        assert _recompile_module(voxel_view, original.replace('tilt=0.283', 'tilt=0.383'),
                                 str(path)) is None
        assert voxel_view.draw_voxels is draw_voxels
        assert frame() is draw_state
        assert draw_state.spin == 1.123
        assert draw_state.tilt == 0.383
        assert draw_state.misc["voxel_state"] is local_state
        assert states[-1] is states[0]
        # Hotswap's live-value capture may republish the tensor; its generation
        # correctly refreshes the lightweight adapter without copying storage.
        assert resources[-1].view.data_ptr() == source.data_ptr()
        assert states[0].peek("cuda_out") is output
        assert states[0].peek("cuda_image") is image
    finally:
        assert _recompile_module(voxel_view, original, str(path)) is None
    _voxels_cleanup(draw_state)
    assert not states[0]._resources
    GLState.flush_deletes()


@needs_cuda
def test_cache_hit_skips_tensor_sync_and_copy(cuda_state, monkeypatch):
    from meltygui.model import cuda_texture_model
    tensor = torch.ones(2, 3, 4, device='cuda')
    texture = tensor_to_texture(cuda_state, 'cached', tensor, version=1)

    def unexpected(*args, **kwargs):
        raise AssertionError('cache hit performed a device transfer or synchronization')

    monkeypatch.setattr(torch.cuda, 'synchronize', unexpected)
    monkeypatch.setattr(cuda_texture_model, 'copy_to_buffer', unexpected)
    assert tensor_to_texture(cuda_state, 'cached', tensor, version=1) is texture


@needs_cuda
def test_nondefault_stream_finishes_before_driver_copy(cuda_state, monkeypatch):
    from meltygui.model import cuda_texture_model
    from meltygui.core.graphics.cuda_interop_core import current_device_index
    device = current_device_index()
    tensor = torch.zeros(16, 16, 16, device=f'cuda:{device}')
    stream = torch.cuda.Stream(device=device)
    finished = torch.cuda.Event()
    with torch.cuda.stream(stream):
        torch.cuda._sleep(20_000_000)
        tensor.fill_(7.0)
        finished.record()
    original = cuda_texture_model.copy_to_buffer
    ready_at_copy = []

    def copy(*args):
        ready_at_copy.append(finished.query())
        return original(*args)

    monkeypatch.setattr(cuda_texture_model, 'copy_to_buffer', copy)
    texture = tensor_to_texture(cuda_state, 'stream', tensor, version=1)
    assert ready_at_copy == [True]
    assert np.all(_readback(texture) == 7.0)


@needs_cuda
@pytest.mark.parametrize('stage', ['buffer', 'registration', 'texture'])
def test_partial_allocation_failure_releases_resources_and_keeps_last_good(cuda_state, monkeypatch, stage):
    from meltygui.model import cuda_texture_model
    tensor = torch.ones(2, 2, 2, device='cuda')
    previous = tensor_to_texture(cuda_state, 'partial', tensor, version=1)
    previous_owner = cuda_state.peek('partial')
    created_buffers, created_textures = [], []
    generate_buffers, generate_textures = gl.glGenBuffers, gl.glGenTextures

    def buffer(*args):
        value = generate_buffers(*args)
        created_buffers.append(int(value))
        return value

    def texture(*args):
        value = generate_textures(*args)
        created_textures.append(int(value))
        return value

    def fail(*args, **kwargs):
        raise RuntimeError(f'deliberate {stage} allocation failure')

    with monkeypatch.context() as patch:
        patch.setattr(gl, 'glGenBuffers', buffer)
        patch.setattr(gl, 'glGenTextures', texture)
        target, name = {'buffer': (gl, 'glBufferData'),
                        'registration': (cuda_texture_model, 'register_buffer'),
                        'texture': (gl, 'glTexImage3D')}[stage]
        patch.setattr(target, name, fail)
        assert tensor_to_texture(cuda_state, 'partial', torch.zeros(3, 3, 3, device='cuda'), 2) is None
    assert cuda_state.peek('partial') is previous_owner
    assert np.all(_readback(previous) == 1.0)
    GLState.flush_deletes()
    assert created_buffers and all(not gl.glIsBuffer(value) for value in created_buffers)
    assert all(not gl.glIsTexture(value) for value in created_textures)
    assert gl.glIsTexture(previous.texture_id)
    replacement = tensor_to_texture(cuda_state, 'partial', torch.zeros(3, 3, 3, device='cuda'), 2)
    assert replacement is not None and np.all(_readback(replacement) == 0.0)


@needs_cuda
def test_failed_upload_restores_bindings_and_retries_version(cuda_state, monkeypatch):
    from meltygui.model import cuda_texture_model
    tensor = torch.ones(2, 2, 2, device='cuda')
    texture = tensor_to_texture(cuda_state, 'upload', tensor, 1)
    sentinel_buffer = gl.glGenBuffers(1)
    sentinel_texture = gl.glGenTextures(1)
    gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, sentinel_buffer)
    gl.glBindTexture(gl.GL_TEXTURE_3D, sentinel_texture)
    try:
        def fail(*args, **kwargs):
            raise RuntimeError('deliberate GL upload failure')

        with monkeypatch.context() as patch:
            patch.setattr(gl, 'glTexSubImage3D', fail)
            tensor.fill_(8.0)
            assert tensor_to_texture(cuda_state, 'upload', tensor, 2) is None
        assert int(gl.glGetIntegerv(gl.GL_PIXEL_UNPACK_BUFFER_BINDING)) == sentinel_buffer
        assert int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_3D)) == sentinel_texture
        assert cuda_state.peek('upload').last_version == 1
        assert tensor_to_texture(cuda_state, 'upload', tensor, 2) is texture
        assert int(gl.glGetIntegerv(gl.GL_PIXEL_UNPACK_BUFFER_BINDING)) == sentinel_buffer
        assert int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_3D)) == sentinel_texture
        # Also allocate under nonzero caller bindings: NULL must not read this PBO.
        assert tensor_to_texture(cuda_state, 'new upload', tensor, 1) is not None
        assert int(gl.glGetIntegerv(gl.GL_PIXEL_UNPACK_BUFFER_BINDING)) == sentinel_buffer
    finally:
        gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, 0)
        gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
        gl.glDeleteBuffers(1, [sentinel_buffer])
        gl.glDeleteTextures([sentinel_texture])
    assert np.all(_readback(texture) == 8.0)


@needs_cuda
def test_failed_unregister_keeps_buffer_alive_until_retry(cuda_state, monkeypatch):
    from meltygui.core.graphics.gl_state import ResourceDeletionDeferred
    from meltygui.model import cuda_texture_model
    tensor_to_texture(cuda_state, 'delete', torch.ones(2, 2, 2, device='cuda'), 1)
    owner = cuda_state.peek('delete')
    buffer, texture = owner.buffer, owner.texture.texture_id
    original = cuda_texture_model.unregister_buffer
    calls = []

    def unregister(*args):
        calls.append('unregister')
        if len(calls) == 1:
            raise ResourceDeletionDeferred()
        return original(*args)

    monkeypatch.setattr(cuda_texture_model, 'unregister_buffer', unregister)
    cuda_state.drop('delete')
    assert GLState.flush_deletes() == 0
    assert calls == ['unregister']
    assert gl.glIsBuffer(buffer) and gl.glIsTexture(texture)
    assert GLState.flush_deletes() == 1
    assert calls == ['unregister', 'unregister']
    assert not gl.glIsBuffer(buffer) and not gl.glIsTexture(texture)


@needs_cuda
def test_wrong_cuda_context_rejects_upload_but_cleanup_restores_owner(cuda_state):
    import meltygui_pycuda.driver as cuda
    from meltygui.core.graphics.cuda_interop_core import (
        cuda_ready, current_device_index, detach_inactive_primary,
    )
    if torch.cuda.device_count() < 2:
        pytest.skip('needs another CUDA device')
    device = current_device_index()
    tensor = torch.ones(2, 2, 2, device=f'cuda:{device}')
    tensor_to_texture(cuda_state, 'context', tensor, 1)
    owner = cuda_state.peek('context')
    buffer, texture = owner.buffer, owner.texture.texture_id
    original = cuda.Context.get_current()
    other = cuda.Device((device + 1) % cuda.Device.count()).retain_primary_context()
    other.push()
    try:
        assert not cuda_ready()
        assert not ensure_context()
        assert tensor_to_texture(cuda_state, 'wrong context', tensor, 1) is None
        assert cuda.Context.get_current() == other
        cuda_state.drop('context')
        GLState.flush_deletes()
        assert cuda.Context.get_current() == other
        assert not gl.glIsBuffer(buffer) and not gl.glIsTexture(texture)
    finally:
        cuda.Context.pop()
        detach_inactive_primary(other)
    assert cuda.Context.get_current() == original
    assert current_device_index() == device
    assert ensure_context()


@needs_cuda
def test_driver_copy_failure_unmaps_buffer_for_retry(cuda_state, monkeypatch):
    import meltygui_pycuda.driver as cuda
    tensor = torch.ones(2, 2, 2, device='cuda')
    with monkeypatch.context() as patch:
        def fail(*args):
            raise RuntimeError('deliberate copy failure')
        patch.setattr(cuda, 'memcpy_dtod', fail)
        assert tensor_to_texture(cuda_state, 'copy failure', tensor, 1) is None
    assert cuda_state.peek('copy failure').last_version != 1
    texture = tensor_to_texture(cuda_state, 'copy failure', tensor, 1)
    assert texture is not None and np.all(_readback(texture) == 1.0)


@needs_cuda
def test_none_version_uploads_once(cuda_state):
    tensor = torch.full((2, 2, 2), 4.0, device='cuda')
    texture = tensor_to_texture(cuda_state, 'none version', tensor, None)
    assert texture is not None and np.all(_readback(texture) == 4.0)
    tensor.fill_(6.0)
    assert tensor_to_texture(cuda_state, 'none version', tensor, None) is texture
    assert np.all(_readback(texture) == 4.0)


@needs_cuda
def test_interop_hotswap_preserves_runtime_allocations_and_updates_queued_cleanup(cuda_state):
    from pathlib import Path
    from test_render_func_integration import _init_melty
    from meltygui.code.file_converters import _recompile_module, stamp_module_baseline
    from meltygui.core.graphics import cuda_interop_core
    from meltygui.model import cuda_texture_model

    _init_melty()
    tensor = torch.ones(2, 2, 2, device='cuda')
    texture = tensor_to_texture(cuda_state, 'hot interop', tensor, 1)
    owner = cuda_state.peek('hot interop')
    buffer, registration = owner.buffer, owner.registered
    state = cuda_interop_core.runtime()
    primary = state.primary_context
    state.last_logged = 'runtime diagnostic'
    original_function = cuda_texture_model.tensor_to_texture
    original_class = cuda_texture_model.CudaVolume
    modules = (cuda_interop_core, cuda_texture_model)
    sources = {module: Path(module.__file__).read_text() for module in modules}
    try:
        for module in modules:
            source = sources[module]
            stamp_module_baseline(module, source)
            if module is cuda_texture_model:
                source = source.replace('def _release_volume(volume, context):',
                    'def _release_volume(volume, context):\n    volume.last_version = "hot cleanup"')
            assert _recompile_module(module, source, module.__file__) is None
        assert cuda_interop_core.runtime() is state
        assert state.primary_context is primary
        assert state.last_logged == 'runtime diagnostic'
        assert cuda_texture_model.CudaVolume is original_class
        assert cuda_texture_model.tensor_to_texture is original_function
        assert tensor_to_texture(cuda_state, 'hot interop', tensor, 1) is texture
        assert cuda_state.peek('hot interop') is owner
        assert owner.buffer == buffer and owner.registered is registration
        # The callback was created before the edit and already lives in GLState.
        cuda_state.drop('hot interop')
        GLState.flush_deletes()
        assert owner.last_version == 'hot cleanup'
        assert not gl.glIsBuffer(buffer)
        assert not gl.glIsTexture(texture.texture_id)
    finally:
        for module in reversed(modules):
            assert _recompile_module(module, sources[module], module.__file__) is None


@needs_cuda
def test_standalone_upload_initializes_display_context_and_restores_borrowed_context():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-c', '''
import glfw
import torch
import meltygui_pycuda.driver as cuda
from meltygui.core.graphics import cuda_interop_core
from meltygui.core.graphics.gl_state import GLState
from meltygui.model.cuda_texture_model import tensor_to_texture
assert glfw.init()
glfw.window_hint(glfw.VISIBLE, False)
window = glfw.create_window(160, 120, 'standalone interop', None, None)
assert window
glfw.make_context_current(window)
tensor = torch.ones(2, 2, 2, device='cuda:0')
assert cuda.Context.get_current() is None
borrowed = cuda_interop_core._native_context()
state = GLState()
try:
    assert tensor_to_texture(state, 'cold', tensor, 1) is not None
    assert cuda_interop_core.current_device_index() in cuda_interop_core.gl_devices()
    assert cuda_interop_core.runtime().primary_context is not None
finally:
    state.release()
    GLState.flush_deletes()
    cuda_interop_core._detach_primary()
    assert cuda.Context.get_current() is None
    assert cuda_interop_core._native_context() == borrowed
    glfw.destroy_window(window)
'''], close_fds=False, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_cuda
def test_native_context_switch_is_scoped_for_upload_and_cleanup(cuda_state):
    import meltygui_pycuda.driver as cuda
    from meltygui.core.graphics import cuda_interop_core
    if cuda.Device.count() < 2:
        pytest.skip('needs another CUDA device')
    device = cuda_interop_core.current_device_index()
    tensor = torch.ones(2, 2, 2, device=f'cuda:{device}')
    tensor_to_texture(cuda_state, 'native mismatch', tensor, 1)
    updated_tensor = tensor * 2
    previous = cuda.Context.get_current()
    other = cuda.Device((device + 1) % cuda.Device.count()).retain_primary_context()
    library = cuda_interop_core._driver_library()
    with cuda_interop_core.using_context(previous):
        assert library.cuCtxSetCurrent(other.handle) == 0
    assert cuda_interop_core._native_context() == previous.handle
    assert library.cuCtxSetCurrent(other.handle) == 0
    try:
        # Torch/native CUDA can change the real context without changing
        # PyCUDA's Python-side stack. Scope the upload without changing its caller.
        assert cuda.Context.get_current() == previous
        assert not cuda_interop_core.cuda_ready()
        texture = tensor_to_texture(cuda_state, 'native update', updated_tensor, 2)
        assert texture is not None and np.all(_readback(texture) == 2.0)
        assert cuda_interop_core._native_context() == other.handle
        cuda_state.drop('native mismatch')
        GLState.flush_deletes()
        assert cuda.Context.get_current() == previous
        assert cuda_interop_core._native_context() == other.handle
    finally:
        assert library.cuCtxSetCurrent(previous.handle) == 0
        cuda_interop_core.detach_inactive_primary(other)
    assert cuda_interop_core.cuda_ready()


@needs_cuda
def test_each_gl_context_owns_its_interop_allocation(cuda_state, gl_context):
    import glfw
    first_window, _ = gl_context
    first_tensor = torch.ones(2, 3, 4, device='cuda')
    first_texture = tensor_to_texture(cuda_state, 'context owner', first_tensor, 1)
    glfw.window_hint(glfw.VISIBLE, False)
    second_window = glfw.create_window(160, 120, 'interop context', None, first_window)
    assert second_window
    glfw.make_context_current(second_window)
    second_state = GLState()
    try:
        assert tensor_to_texture(cuda_state, 'wrong GL owner', first_tensor, 1) is None
        second_texture = tensor_to_texture(second_state, 'context owner', first_tensor * 3, 1)
        assert second_texture is not None
        assert second_texture.texture_id != first_texture.texture_id
        assert np.all(_readback(second_texture) == 3.0)
        second_state.release()
        GLState.flush_deletes()
        assert not gl.glIsTexture(second_texture.texture_id)
        assert gl.glIsTexture(first_texture.texture_id)
    finally:
        second_state.release()
        GLState.flush_deletes()
        glfw.make_context_current(first_window)
        glfw.destroy_window(second_window)
    assert np.all(_readback(first_texture) == 1.0)
