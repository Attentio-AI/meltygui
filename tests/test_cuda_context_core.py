"""Shared device scopes preserve callers, compiled kernels and retained leases."""
import sys
from types import SimpleNamespace

import pytest

from meltygui.core.graphics import cuda_context_core
from meltygui.core.melty import Melty


def test_adopts_live_contexts_without_replacing_runtime(monkeypatch):
    context = object()
    previous = SimpleNamespace(primary_context=object(), primary_thread=None,
                               last_logged='keep', driver_library=object())
    monkeypatch.setattr(Melty, 'cuda_interop', previous)
    monkeypatch.setitem(sys.__dict__, '_lsd_cuda_march_contexts', {2: context})
    assert cuda_context_core.runtime() is previous
    assert previous.contexts == {2: context}
    assert previous.last_logged == 'keep'
    assert '_lsd_cuda_march_contexts' not in sys.__dict__
    assert cuda_context_core.runtime().contexts[2] is context


@pytest.fixture
def cuda():
    driver = pytest.importorskip('meltygui_pycuda.driver')
    torch = pytest.importorskip('torch')
    if not torch.cuda.is_available():
        pytest.skip('needs CUDA')
    driver.init()
    return driver


def test_nested_device_scopes_restore_native_context_on_exception(cuda):
    owner = cuda_context_core.primary_context_for(0)
    other_device = 1 if cuda.Device.count() > 1 else 0
    other = cuda_context_core.primary_context_for(other_device)
    with cuda_context_core.using_context(owner):
        # Model a native CUDA client changing context behind PyCUDA's back.
        library = cuda_context_core._driver_library()
        assert library.cuCtxSetCurrent(other.handle) == 0
        try:
            with pytest.raises(ValueError, match='scope failed'):
                with cuda_context_core.using_device(0):
                    assert cuda.Context.get_current().handle == owner.handle
                    assert cuda_context_core._native_context() == owner.handle
                    with cuda_context_core.using_device(other_device):
                        assert cuda_context_core._native_context() == other.handle
                    assert cuda_context_core._native_context() == owner.handle
                    raise ValueError('scope failed')
            assert cuda.Context.get_current().handle == owner.handle
            assert cuda_context_core._native_context() == other.handle
        finally:
            assert library.cuCtxSetCurrent(owner.handle) == 0


def test_kernel_context_survives_standalone_stack_release(cuda):
    context = cuda_context_core.primary_context_for(0)
    # A subprocess avoids borrowing the test session's own display stack entry.
    import subprocess
    result = subprocess.run([sys.executable, '-c', '''
from meltygui.core.graphics import cuda_context_core as core
import meltygui_pycuda.driver as cuda
context = core.primary_context_for(0)
assert core.ensure_primary_context(0)
assert core.runtime().primary_context.handle == context.handle
core._detach_primary()
assert core.runtime().contexts[0] is context
with core.using_device(0):
    cuda.Context.synchronize()
assert cuda.Context.get_current() is None
'''], close_fds=False, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert cuda_context_core.primary_context_for(0) is context


def test_hotswap_preserves_pool_and_compiled_kernels(cuda):
    from pathlib import Path
    from test_render_func_integration import _init_melty
    from meltygui.code.file_converters import _recompile_module, stamp_module_baseline
    from meltygui.view import graph_cuda_view as line_kernels

    _init_melty()
    state = cuda_context_core.runtime()
    context = cuda_context_core.primary_context_for(0)
    with cuda_context_core.using_device(0):
        functions = line_kernels._functions(0)
    function = cuda_context_core.using_device
    source = Path(cuda_context_core.__file__).read_text()
    stamp_module_baseline(cuda_context_core, source)
    assert _recompile_module(cuda_context_core, source, cuda_context_core.__file__) is None
    assert cuda_context_core.runtime() is state
    assert cuda_context_core.primary_context_for(0) is context
    assert cuda_context_core.using_device is function
    with cuda_context_core.using_device(0):
        assert line_kernels._functions(0) == functions


def test_voxels_and_lines_use_the_pool_on_every_device(cuda):
    import torch
    from meltygui.view import voxel_cuda_view as cuda_march, graph_cuda_view as line_kernels

    for device in range(cuda.Device.count()):
        context = cuda_context_core.primary_context_for(device)
        source = torch.arange(24, dtype=torch.float32, device=f'cuda:{device}').reshape(2, 12)
        volume = torch.ones(4, 4, 4, device=source.device)
        lut = torch.ones(2, 3, device=source.device)
        shade = torch.tensor(cuda_march.shade_params(), device=source.device)
        out = torch.zeros(32, 40, 4, dtype=torch.float16, device=source.device)
        torch.cuda.synchronize(device)
        stats = line_kernels.ranges(source)
        with cuda_context_core.using_device((device + 1) % cuda.Device.count()):
            caller = cuda_context_core._native_context()
            line_kernels.render(source, stats, out, lut, y_range=(0., 24.), unit=(20., 16.))
            assert cuda_context_core._native_context() == caller
            cuda_march.march(volume, out, lut, display_shape=volume.shape, shade=shade)
            assert cuda_context_core._native_context() == caller
        torch.cuda.synchronize(device)
        torch.testing.assert_close(stats[:, 0], source[:, 0])
        torch.testing.assert_close(stats[:, 1], source[:, -1])
        assert torch.isfinite(out).all() and (out[..., 3] > 0).any()
        assert cuda_context_core.runtime().contexts[device] is context
