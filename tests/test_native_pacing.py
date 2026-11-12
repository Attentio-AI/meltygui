"""Frame pacing of the native Wayland backend (Backend.pace), without a compositor."""
import time
from types import SimpleNamespace

from meltygui.core.windowing.backends import native_wayland
from meltygui.core.windowing.backends.native_wayland import Backend, NativeWindow


def backend_with_window():
    backend = Backend()
    backend.native = SimpleNamespace(destroy=lambda proxy: None)
    window = NativeWindow(backend, 800, 600, 'paced', {})
    backend.windows.append(window)
    return backend, window


def test_input_is_read_only_after_the_compositor_asked_for_the_frame():
    backend, window = backend_with_window()
    window.frame_due, window.frame_asked = 7, time.monotonic()
    backend.callbacks_due[7] = window
    waits = []

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 3:
            backend.frame_done(7, 0)        # the frame callback arrives on the third wake
    backend.wait_events_timeout = wait
    backend.pace()
    assert len(waits) == 3 and window.frame_due is None and window.frame_at > 0
    assert backend.sampled > 0 and not backend.late
    # Nothing asked for: no wait at all.
    backend.pace()
    assert len(waits) == 3


def test_a_window_that_is_never_asked_does_not_hold_the_loop():
    backend, window = backend_with_window()
    window.frame_due, window.frame_asked = 7, time.monotonic() - native_wayland.WAIT - .01
    backend.wait_events_timeout = lambda timeout: (_ for _ in ()).throw(AssertionError('waited'))
    backend.pace()
    assert window.frame_due == 7


def test_the_period_is_the_outputs_never_measured_from_the_frames():
    backend, window = backend_with_window()
    assert backend.period(window) == 1 / 120
    backend.outputs[5] = SimpleNamespace(refresh=100000)       # mHz, from the output's mode
    window.outputs.add(5)
    assert backend.period(window) == .010
    window.refresh = .004                                      # what a presented frame reported
    assert backend.period(window) == .004


def clock(backend):
    return time.clock_gettime(backend.clock)


def test_starting_late_hands_the_frame_over_just_before_the_next_refresh(monkeypatch):
    monkeypatch.setattr(native_wayland, 'LATE', True)
    backend, window = backend_with_window()
    backend.outputs[5] = SimpleNamespace(refresh=100000)
    window.outputs.add(5)
    window.costs = [.002] * 8
    window.shown = clock(backend)            # a frame was on screen just now: refreshes follow every 10 ms
    waits = []
    backend.wait_events_timeout = lambda timeout: (waits.append(timeout), time.sleep(timeout))
    backend.pace()
    # Until the next refresh, less the guard and the drawing time.
    assert backend.late and abs(backend.aim - (window.shown + .010)) < 1e-6
    assert abs(sum(waits) - (.010 - native_wayland.GUARD - .002)) < .002
    # Not wanted (nothing asks for a frame): no late start.
    window.shown = clock(backend)
    backend.pace(lambda: False)
    assert not backend.late and backend.aim is None
    # No frame on screen lately: the compositor draws the next one at once, nothing to aim at.
    window.shown = clock(backend) - 1.
    backend.pace()
    assert not backend.late
    # Too slow for the next refresh: drawn at once, as without a late start.
    window.costs = [.009] * 8
    window.shown = clock(backend)
    backend.pace()
    assert not backend.late and backend.aim is None
    # One slow frame among many is left to the guard.
    window.costs = [.002] * 23 + [.006]
    window.shown = clock(backend)
    waits.clear()
    backend.pace()
    assert abs(sum(waits) - (.010 - native_wayland.GUARD - .002)) < .002


def test_only_frequent_misses_of_late_frames_lengthen_the_guard():
    backend, window = backend_with_window()
    backend.outputs[5] = SimpleNamespace(refresh=100000)       # a refresh every 10 ms
    window.outputs.add(5)
    at = [100.]

    def present(late, milliseconds):
        """The next frame is on screen `milliseconds` after the one before it."""
        at[0] += milliseconds / 1000
        whole = int(at[0])
        backend.feedbacks[1] = window, at[0] - .005, at[0] if late else None
        backend.presented(1, 0, whole, round((at[0] - whole) * 1e9), 0, 0, 0, 0)
    present(True, 10)
    present(True, 20)                       # a skipped refresh on its own: nothing
    assert backend.slack == 0 and abs(window.latency_ms - 5.) < 1e-3
    present(True, 13)                       # a compositor a little late is no miss
    present(True, 300)                      # nor is a pause in drawing
    for _ in range(native_wayland.MISSES - 2):
        present(True, 20)                   # a few more: still the compositor's own skips
    assert backend.slack == 0
    present(True, 20)                       # this many so close together: the hand-over is too late
    assert backend.slack == native_wayland.MISS
    present(True, 10)
    assert native_wayland.MISS * .99 < backend.slack < native_wayland.MISS
    held = backend.slack
    present(False, 20)                      # a frame that did not start late is not judged
    assert backend.slack == held
    for _ in range(native_wayland.MISSES - 1):
        for _ in range(native_wayland.RECENT // 2):
            present(True, 10)
        present(True, 20)                   # skips spread far apart never add up
    assert backend.slack < held
