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


def test_fps_averages_render_durations_and_excludes_idle(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(fps_counter.time, 'perf_counter', lambda: clock[0])
    counter = FpsCounter()
    assert counter.fps == 0.0
    for duration in (0.004, 0.012):
        clock[0] += 30.0
        started = counter.frame_started()
        clock[0] += duration
        counter.frame_finished(started)
    # Reciprocal of mean duration, not mean of instantaneous rates.
    assert round(counter.fps, 6) == 125.0
    assert round(counter.frame_ms, 6) == 12.0
    for _ in range(fps_counter.FPS_SAMPLE_COUNT):
        started = counter.frame_started()
        clock[0] += 0.010
        counter.frame_finished(started)
    assert round(counter.fps, 6) == 100.0
    assert len(counter.render_times) == fps_counter.FPS_SAMPLE_COUNT


def test_zero_duration_does_not_divide_by_zero(monkeypatch):
    monkeypatch.setattr(fps_counter.time, 'perf_counter', lambda: 1.0)
    counter = FpsCounter()
    counter.frame_finished(counter.frame_started())
    assert counter.fps == 0.0
