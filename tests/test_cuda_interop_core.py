"""Interop plumbing rejects unsafe calls and balances mapped-buffer failures."""
from contextlib import nullcontext
from types import SimpleNamespace
import subprocess
import sys
import threading

import pytest

from meltygui.core.graphics import cuda_interop_core, cuda_context_core
from meltygui.core.graphics.gl_state import is_gl_thread, ResourceDeletionDeferred


def test_import_does_not_initialize_cuda_or_require_torch():
    result = subprocess.run([sys.executable, '-c', '''
import sys
from meltygui.model.cuda_texture_model import tensor_to_texture
from meltygui.core.melty import Melty
assert Melty.cuda_interop is None
assert 'torch' not in sys.modules
assert 'meltygui_pycuda.driver' not in sys.modules
'''], close_fds=False, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_worker_thread_cannot_initialize_interop(monkeypatch):
    assert is_gl_thread()
    calls = []
    monkeypatch.setattr(cuda_interop_core, 'gl_devices', lambda: calls.append(True))
    results = []
    worker = threading.Thread(target=lambda: results.append(cuda_interop_core.ensure_context()))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert results == [False] and calls == []


@pytest.mark.parametrize('failure', ['capacity', 'copy'])
def test_mapping_is_unmapped_on_failure(monkeypatch, failure):
    cuda = pytest.importorskip('meltygui_pycuda.driver')
    events = []
    mapping = SimpleNamespace(device_ptr_and_size=lambda: (10, 2 if failure == 'capacity' else 100),
                              unmap=lambda: events.append('unmap'))
    registered = SimpleNamespace(map=lambda: mapping)
    monkeypatch.setattr(cuda_context_core, 'using_context', lambda context: nullcontext())

    def copy(*args):
        events.append('copy')
        raise RuntimeError('copy failed')

    monkeypatch.setattr(cuda, 'memcpy_dtod', copy)
    with pytest.raises((ValueError, RuntimeError)):
        cuda_interop_core.copy_to_buffer(registered, object(), 20, 16)
    assert events == (['unmap'] if failure == 'capacity' else ['copy', 'unmap'])


def test_unregister_failure_requests_deferred_cleanup(monkeypatch):
    monkeypatch.setattr(cuda_context_core, 'using_context', lambda context: nullcontext())
    monkeypatch.setattr(cuda_interop_core, 'log_once', lambda message: None)

    def fail():
        raise RuntimeError('CUDA still owns the buffer')

    with pytest.raises(ResourceDeletionDeferred):
        cuda_interop_core.unregister_buffer(SimpleNamespace(unregister=fail), object())
