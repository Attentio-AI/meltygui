"""Native display pacing receives worker completions without a GLFW window."""
import sys
import threading
from types import SimpleNamespace

from meltygui.core.melty import Melty
from meltygui.core.windowing import glfw_utils


def test_worker_completion_wakes_native_host_and_applies_on_render_thread(monkeypatch):
    wakes, applied = [], []
    monkeypatch.setitem(sys.modules, '_melty_ios', SimpleNamespace(
        request_frame=lambda: wakes.append(threading.get_ident())))
    monkeypatch.setattr(Melty, 'glfw_window', None)
    monkeypatch.setattr(Melty, '_render_tasks', [])
    monkeypatch.setattr(glfw_utils, '_needs_render', threading.Event())
    monkeypatch.setattr(glfw_utils, '_all_surfaces_generation', 0)

    def desktop_wake():
        raise AssertionError('Native host tried to wake GLFW')

    monkeypatch.setitem(glfw_utils.glfw.__dict__, 'post_empty_event', desktop_wake)
    worker = threading.Thread(target=lambda: Melty.post_to_render(
        lambda: applied.append(threading.get_ident())))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert wakes == [worker.ident]
    assert applied == []
    assert glfw_utils._needs_render.is_set()
    Melty._drain_render_tasks()
    assert applied == [threading.get_ident()]
    requested, generation = glfw_utils.take_surface_request(object(), 0)
    assert requested and generation == 1
