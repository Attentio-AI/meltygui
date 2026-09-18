from meltygui.core.diagnostics import fps_counter
from meltygui.core.diagnostics.fps_counter import FpsCounter


def test_frame_ms_is_the_last_frames_render_time(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(fps_counter.time, 'perf_counter', lambda: clock[0])
    counter = FpsCounter()
    assert counter.frame_ms == 0.0
    started = counter.frame_started()
    clock[0] += 0.004
    counter.frame_finished(started)
    assert round(counter.frame_ms, 6) == 4.0
    clock[0] += 5.0   # idle time between frames is not frame time
    started = counter.frame_started()
    clock[0] += 0.002
    counter.frame_finished(started)
    assert round(counter.frame_ms, 6) == 2.0
