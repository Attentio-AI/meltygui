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
    costs = lambda *seconds: [(clock(backend), cost) for cost in seconds]
    window.costs = costs(*[.002] * 8)
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
    # Guard and drawing together longer than a period: the refresh after next, started before the next.
    window.costs = costs(*[.008] * 8)
    window.shown = clock(backend)
    waits.clear()
    backend.pace()
    assert backend.late and abs(backend.aim - (window.shown + .020)) < 1e-6
    assert abs(sum(waits) - (.020 - native_wayland.GUARD - .008)) < .002
    # Slower to draw than a period: drawn at once, as without a late start.
    window.costs = costs(*[.011] * 8)
    window.shown = clock(backend)
    backend.pace()
    assert not backend.late and backend.aim is None
    # Asked for a frame just before a refresh (the compositor draws for it now): the one after it.
    window.costs = costs(*[.002] * 8)
    window.shown = clock(backend) - .008
    waits.clear()
    backend.pace()
    assert backend.late and abs(backend.aim - (window.shown + .020)) < 1e-6
    assert abs(sum(waits) - (.012 - native_wayland.GUARD - .002)) < .002
    # One slow frame among many is left to the guard.
    window.costs = costs(*[.002] * 23 + [.006])
    window.shown = clock(backend)
    waits.clear()
    backend.pace()
    assert abs(sum(waits) - (.010 - native_wayland.GUARD - .002)) < .002


def paced():
    """A window on a 100 Hz output and `present(late, judged, gap)`: the next frame aims `gap`
    refreshes after the one before it and is on screen `late` refreshes after that."""
    backend, window = backend_with_window()
    backend.outputs[5] = SimpleNamespace(refresh=100000)       # a refresh every 10 ms
    window.outputs.add(5)
    at = [100.]

    def present(late=0, judged=True, gap=1):
        aim = at[0] + gap * .010
        at[0] = aim + late * .010
        whole = int(at[0])
        backend.feedbacks[1] = window, at[0] - .005, aim if judged else None
        backend.presented(1, 0, whole, round((at[0] - whole) * 1e9), 0, 0, 0, 0)

    def run(seconds):
        for _ in range(round(seconds * 100)):
            present()

    def burst():
        for _ in range(native_wayland.MISSES):
            present(late=1)
    for _ in range(native_wayland.MISSES + 2):
        present(late=1)                     # a window opening: its first frames are not counted
    run(native_wayland.CALM)
    return backend, window, present, run, burst, at


def test_misses_lift_the_guard_and_the_lift_is_gone_half_a_second_later():
    backend, window, present, run, burst, at = paced()
    GUARD, MISS = native_wayland.GUARD, native_wayland.MISS
    assert backend.guard == GUARD and abs(window.latency_ms - 5.) < 1e-3
    for _ in range(native_wayland.MISSES - 1):
        present(late=1)                     # fewer than a burst: the compositor's own skips
    assert backend.guard == GUARD
    present(late=1)
    assert abs(backend.guard - (GUARD + MISS)) < 1e-9 and backend.base == GUARD
    run(native_wayland.FADE / 2)
    assert GUARD < backend.guard < GUARD + MISS
    run(native_wayland.FADE / 2 + .02)      # frames land again: back where it was
    assert backend.guard == GUARD and backend.base == GUARD
    # Many disturbances far apart never add up: each is answered and forgotten.
    for _ in range(20):
        burst()
        run(native_wayland.AGAIN + .1)
    assert backend.guard == GUARD and backend.base == GUARD


def test_only_frames_handed_over_in_time_for_the_next_refresh_are_judged():
    backend, window, present, run, burst, at = paced()
    GUARD = native_wayland.GUARD
    for _ in range(4 * native_wayland.MISSES):
        present(late=1, judged=False)       # took longer to draw than expected: no guard helps
        present(gap=2)                      # aimed past the next refresh and made it: not a skipped one
        present(late=1, gap=30)             # the first frame after a pause
        present(late=4)                     # the compositor paused
        present(late=.3)                    # a compositor a little late is no miss
    assert backend.guard == GUARD and not backend.missed


def test_the_base_learns_from_misses_that_keep_coming_and_eases_back_to_where_it_started():
    backend, window, present, run, burst, at = paced()
    GUARD, MISS, STEP = native_wayland.GUARD, native_wayland.MISS, native_wayland.STEP
    for _ in range(12):                     # lift after lift for four seconds: no disturbance
        burst()
        run(.3)
    learned = backend.base
    assert GUARD + MISS - 1e-9 <= learned <= GUARD + 2 * MISS + 1e-9
    run(native_wayland.REST - 3)            # resting: it stays, and the lift is gone
    assert backend.base == learned and backend.guard == learned
    run(3 + native_wayland.CALM * 3)        # rested: back towards the start, a step at a time
    assert learned - 8 * STEP < backend.base < learned - STEP
    run(120)                                # nothing misses: to the start, and no further
    assert backend.base == GUARD and backend.guard == GUARD
    # Another refresh rate starts over, at most half a period.
    assert backend.tune(.004, at[0]) == .002


def test_easing_that_misses_goes_back_and_rests_longer_the_next_time():
    backend, window, present, run, burst, at = paced()
    GUARD, MISS, REST = native_wayland.GUARD, native_wayland.MISS, native_wayland.REST
    needed = backend.base = GUARD + 4 * MISS        # what this display turned out to need
    backend.settled = at[0]
    run(native_wayland.CALM * 3)
    assert backend.base < needed
    burst()                                 # shorter misses: back, no higher than before
    assert abs(backend.base - needed) < 1e-9 and backend.rest == REST
    run(REST - 1)
    assert abs(backend.base - needed) < 1e-9
    run(1 + native_wayland.CALM * 3)
    burst()                                 # the same again: it tries less often
    assert abs(backend.base - needed) < 1e-9 and backend.rest == 2 * REST


def test_slow_frames_are_forgotten_after_half_a_second():
    backend, window = backend_with_window()
    now = clock(backend)
    window.costs = [(now - 1., .007)] * 6 + [(now - .1, .002)] * 5
    assert backend.cost(window, now) == .002
    # Nothing that fresh (frames far apart): the newest one.
    window.costs = [(now - 3., .007), (now - 2., .003)]
    assert backend.cost(window, now) == .003
