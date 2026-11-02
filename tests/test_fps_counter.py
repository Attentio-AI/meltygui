import pytest

from meltygui.core.diagnostics import fps_counter
from meltygui.core.diagnostics.fps_counter import FpsCounter
from meltygui.core.diagnostics.fps_counter import PresentationFpsCounter


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


def test_presented_fps_counts_completed_frames_including_batched_callbacks(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(fps_counter.time, 'perf_counter', lambda: clock[0])
    counter = PresentationFpsCounter()
    counter.record_presentation((1, 0, 0.0))
    counter.record_presentation((1, 1, 10.0))
    for count in (2, 4, 5, 8):
        started = counter.frame_started()
        clock[0] += .00625  # 160 CPU renders/s must not become displayed FPS.
        counter.frame_finished(started)
        counter.record_presentation((1, count, 10.0 + (count - 1) / 120))
        assert counter.fps == pytest.approx(120)
    assert counter.frame_ms == pytest.approx(6.25)


def test_presented_fps_retains_rate_across_idle_and_does_not_hide_missed_frames():
    counter = PresentationFpsCounter()
    counter.record_presentation((1, 1, 10.0))
    counter.record_presentation((1, 2, 10.0 + 1 / 120))
    for _ in range(20):
        counter.record_presentation((1, 2, 10.0 + 1 / 120))
    assert counter.fps == pytest.approx(120)
    # A new display-link run omits the 30-second idle gap.
    counter.record_presentation((2, 3, 40.0))
    assert counter.fps == pytest.approx(120)
    counter.record_presentation((2, 4, 40.0 + 1 / 120))
    assert counter.fps == pytest.approx(120)
    counter.record_presentation((2, 5, 40.0 + 3 / 120))
    assert counter.fps == pytest.approx(80)  # One presentation missed a refresh.


def test_presented_fps_toggle_resets_baseline_without_replacing_last_readout():
    counter = PresentationFpsCounter()
    counter.record_presentation((1, 1, 10.0))
    counter.record_presentation((1, 2, 10.0 + 1 / 120))
    counter.reset_presentation()
    counter.record_presentation((1, 3, 90.0))
    assert counter.fps == pytest.approx(120)
    counter.record_presentation((1, 4, 90.0 + 1 / 60))
    assert counter.fps == pytest.approx(60)
