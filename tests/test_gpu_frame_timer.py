import ctypes
from types import SimpleNamespace

from meltygui.core.diagnostics.gpu_frame_timer import GpuFrameTimer


def test_reads_64_bit_queries_into_explicit_output_without_truncation():
    timestamps = {10: 2**55, 11: 2**55 + 1_250_000}
    calls = []
    def read(query, parameter, output):
        calls.append(query)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))[0] = timestamps[query]
    timer = GpuFrameTimer()
    timer._active = True
    timer._gl = SimpleNamespace(GL_QUERY_RESULT_AVAILABLE=1, GL_QUERY_RESULT=2,
                                glGetQueryObjectiv=lambda *args: True,
                                glGetQueryObjectui64v=read)
    timer._rings = [[], [10, 11], [], []]
    timer._labels = [[], ['start', 'ui'], [], []]
    timer._counts[1] = 2
    timer._frames[1] = 42
    assert timer.end() == (42, {'ui': 1.25})
    assert calls == [10, 11]
    assert not timer._dead


def test_unavailable_query_never_reads_or_blocks():
    timer = GpuFrameTimer()
    timer._active = True
    timer._gl = SimpleNamespace(GL_QUERY_RESULT_AVAILABLE=1,
                                glGetQueryObjectiv=lambda *args: False)
    timer._rings = [[], [10, 11], [], []]
    timer._counts[1] = 2
    timer._frames[1] = 42
    assert timer.end() is None
    assert not timer._dead


def test_gpu_timer_with_real_gl():
    import os
    import pytest
    if not os.environ.get('MELTY_GPU_TIMER_TEST'):
        pytest.skip('opt-in real GL check; run on a reserved desktop')
    from conftest import _ensure_gl_context
    from OpenGL import GL
    _ensure_gl_context()
    timer = GpuFrameTimer()
    completed = []
    for frame in range(8):
        timer.begin(True, frame)
        timer.stamp('start')
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        timer.stamp('ui')
        GL.glFinish()  # Only this test waits: make completed-query assertions deterministic.
        value = timer.end()
        if value is not None:
            completed.append(value)
    assert not timer._dead
    assert completed and all(result['ui'] >= 0 for _, result in completed)
