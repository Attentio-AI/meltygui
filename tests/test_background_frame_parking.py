import threading
from types import SimpleNamespace

import pytest

from meltygui.code import libcst_conversion
from meltygui.core.melty import Melty
from meltygui.core.windowing import glfw_utils
from meltygui.core.windowing.surface import Surface


@pytest.mark.parametrize('fails', [False, True])
def test_surface_announces_entire_frame_and_clears_after_error(monkeypatch, fails):
    old_scope = object()
    monkeypatch.setattr(Melty, '_frame_draw_start', 0.0)
    monkeypatch.setattr(glfw_utils, 'render_scope', old_scope)
    seen = []
    def draw():
        seen.append(Melty._frame_draw_start)
        assert glfw_utils.render_scope is surface
        if fails:
            raise RuntimeError('frame failed')
    surface = SimpleNamespace(_frame=draw)
    if fails:
        with pytest.raises(RuntimeError, match='frame failed'):
            Surface.frame(surface)
    else:
        Surface.frame(surface)
    assert seen[0] > 0
    assert Melty._frame_draw_start == 0.0
    assert glfw_utils.render_scope is old_scope


def test_worker_yields_during_surface_frame_without_gl_owner(monkeypatch):
    from meltygui.core.graphics import gl_state
    monkeypatch.setattr(gl_state, '_gl_thread', None)
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    monkeypatch.setattr(Melty, '_frame_draw_start', 0.0)
    slept, results = [], []
    def sleep(seconds):
        slept.append(seconds)
        Melty._frame_draw_start = 0.0
    monkeypatch.setattr(libcst_conversion.time, 'sleep', sleep)
    def draw():
        assert libcst_conversion._park_while_frame() == 0  # never block the UI
        thread = threading.Thread(target=lambda: results.append(libcst_conversion._park_while_frame()))
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
    Surface.frame(SimpleNamespace(_frame=draw))
    assert slept == [libcst_conversion._FRAME_PARK_SLICE_S]
    assert len(results) == 1
    assert Melty._frame_draw_start == 0
