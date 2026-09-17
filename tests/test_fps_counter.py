from meltygui.core.diagnostics import fps_counter
from meltygui.core.diagnostics.fps_counter import FpsCounter


def test_rate_covers_the_current_burst_only(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(fps_counter.time, 'perf_counter', lambda: clock[0])
    counter = FpsCounter()
    assert counter.label().startswith('-- fps')
    for _ in range(10):
        started = counter.frame_started()
        clock[0] += 0.004
        counter.frame_finished(started)
        clock[0] += 0.006
    assert counter.label() == '100 fps  4.0 ms'
    clock[0] += 5.0   # idle: the next frame starts a new burst
    counter.frame_started()
    assert counter.label().startswith('100 fps')
    clock[0] += 1 / 120
    counter.frame_started()
    assert counter.label().startswith('120 fps')


def test_window_is_bounded():
    counter = FpsCounter()
    for _ in range(fps_counter.WINDOW_FRAMES * 3):
        counter.frame_started()
    assert len(counter.frame_starts) == fps_counter.WINDOW_FRAMES
