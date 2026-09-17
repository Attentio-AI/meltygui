"""CUDA compilation ownership, hot replacement and feature independence."""
from pathlib import Path
import subprocess
import sys
import threading

import numpy as np
import pytest

from meltygui.core.graphics import cuda_context_core, cuda_kernel_core


@pytest.fixture
def cuda():
    driver = pytest.importorskip('meltygui_pycuda.driver')
    driver.init()
    if not driver.Device.count():
        pytest.skip('CUDA device required')
    return driver


def test_import_does_not_initialize_optional_cuda():
    result = subprocess.run([sys.executable, '-c', '''
import sys
from meltygui.core.melty import Melty
from meltygui.core.graphics import cuda_kernel_core
from meltygui.model import cuda_tensor_model
from meltygui.view import voxel_cuda_view, graph_cuda_view
assert Melty.cuda_interop is None
assert 'torch' not in sys.modules
assert 'meltygui_pycuda.driver' not in sys.modules
'''], capture_output=True, text=True, close_fds=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_cannot_compile_or_mutate_cache(cuda):
    cuda_context_core.primary_context_for(0)
    errors = []
    def compile_on_worker():
        try:
            cuda_kernel_core.kernel_functions('worker', 0, '', ())
        except RuntimeError as error:
            errors.append(str(error))
    thread = threading.Thread(target=compile_on_worker)
    thread.start()
    thread.join()
    assert errors == ['rendering CUDA contexts belong to the render thread']


def test_recompile_changes_output_and_failure_preserves_last_module(cuda):
    import torch
    source = 'extern "C" __global__ void value(int *out) { out[0] = 7; }'
    out = torch.zeros(1, dtype=torch.int32, device='cuda:0')
    torch.cuda.synchronize(0)
    caller = cuda_context_core._native_context()
    first = cuda_kernel_core.kernel_functions('test-output', 0, source, ('value',))
    assert cuda_kernel_core.kernel_functions('test-output', 0, source, ('value',)) is first
    with cuda_context_core.using_device(0):
        first['value'](np.uintp(out.data_ptr()), block=(1, 1, 1), grid=(1, 1, 1))
    assert cuda_context_core._native_context() == caller
    assert out.item() == 7
    replacement = source.replace('= 7', '= 11')
    second = cuda_kernel_core.kernel_functions('test-output', 0, replacement, ('value',))
    assert second is not first
    with cuda_context_core.using_device(0):
        second['value'](np.uintp(out.data_ptr()), block=(1, 1, 1), grid=(1, 1, 1))
    assert out.item() == 11
    with pytest.raises(Exception):
        cuda_kernel_core.kernel_functions('test-output', 0, 'invalid CUDA syntax', ('value',))
    assert cuda_context_core._native_context() == caller
    assert cuda_kernel_core.kernel_functions('test-output', 0, replacement, ('value',)) is second


def test_hotswap_preserves_compiler_runtime_and_features(cuda):
    from test_render_func_integration import _init_melty
    from meltygui.code.file_converters import _recompile_module, stamp_module_baseline
    from meltygui.view import voxel_cuda_view, graph_cuda_view
    _init_melty()
    state = cuda_context_core.runtime()
    voxel = voxel_cuda_view._kernel_for(0)
    lines = graph_cuda_view._functions(0)
    function = cuda_kernel_core.kernel_functions
    kernel_class = cuda_kernel_core.CudaKernel
    for module in (cuda_kernel_core, voxel_cuda_view, graph_cuda_view):
        source = Path(module.__file__).read_text()
        stamp_module_baseline(module, source)
        assert _recompile_module(module, source, module.__file__) is None
    assert cuda_context_core.runtime() is state
    assert cuda_kernel_core.kernel_functions is function
    assert cuda_kernel_core.CudaKernel is kernel_class
    assert voxel_cuda_view._kernel_for(0) is voxel
    assert graph_cuda_view._functions(0) == lines


def test_legacy_compiled_functions_transfer_without_recompilation(cuda, monkeypatch):
    from meltygui.view import voxel_cuda_view, graph_cuda_view
    voxel = voxel_cuda_view._kernel_for(0)
    lines = graph_cuda_view._functions(0)
    state = cuda_context_core.runtime()
    voxel_entry = state.kernels[('voxels', 0, ('march', 'bake_mip', 'bake_floor'))]
    line_entry = state.kernels[('lines', 0, ('line_ranges', 'lines_image'))]
    monkeypatch.setattr(state, 'kernels', {})
    monkeypatch.delattr(state, 'legacy_kernels')
    monkeypatch.setitem(sys.__dict__, '_lsd_cuda_march_kernels', {
        0: dict(voxel_entry.functions, __source__=hash(voxel_cuda_view.KERNEL))})
    monkeypatch.setitem(sys.__dict__, '_melty_cuda_line_kernels', {
        0: (graph_cuda_view._SOURCE, line_entry.module, *lines)})
    import meltygui_pycuda.compiler
    def forbidden(*args, **kwargs):
        raise AssertionError('existing CUDA functions recompiled')
    monkeypatch.setattr(meltygui_pycuda.compiler, 'SourceModule', forbidden)
    assert voxel_cuda_view._kernel_for(0) is voxel
    assert graph_cuda_view._functions(0) == lines
    assert '_lsd_cuda_march_kernels' not in sys.__dict__
    assert '_melty_cuda_line_kernels' not in sys.__dict__
    assert state.legacy_kernels == {'voxels': {}, 'lines': {}}
