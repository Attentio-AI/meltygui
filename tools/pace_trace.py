"""What is frame pacing doing on this display right now?

    .venv/bin/python tools/pace_trace.py [seconds] [--plain]

Opens a window that redraws continuously with pacing as the apps run it (the guard tunes itself)
and prints one line a second:

    latency   input read to on screen, mean and worst
    guard     what `Backend.tune` has the guard at
    missed    frames handed over in time that came on screen a refresh late
    asked     how long before a refresh the compositor asked for the next frame (its frame callback):
              on a compositor that draws at a deadline, that is the deadline
    handed    how long before a refresh the frame was handed over (the swap)

A latency of a period plus guard plus drawing time with `handed` shorter than `asked` is a frame
that arrives after the compositor has drawn for that refresh. `--plain` turns the late start off
(`LATE = False`): a client that swaps right after it is asked, to compare, or to run beside a
second trace as another window that draws all the time. Native Wayland only.
"""
import statistics
import sys
import time

from meltygui import glfw_window, imgui
from meltygui.core.windowing import glfw_utils
from meltygui.core.windowing.backends import native_wayland

# Levers: seconds to run, and whether frames start late.
SECONDS = float(next((arg for arg in sys.argv[1:] if not arg.startswith('-')), 20))
native_wayland.LATE = '--plain' not in sys.argv

Backend = native_wayland.Backend
presented, frame_done, swap_buffers = Backend.presented, Backend.frame_done, Backend.swap_buffers
asked, handed, frames = [], [], []      # clock times of frame callbacks and swaps; (latency ms, missed) per presented frame
state = {'until': 0., 'line': 0., 'backend': None}


def on_presented(self, proxy, high, low, nanoseconds, refresh, *rest):
    window, sampled, aim = self.feedbacks.get(proxy, (None, 0., None))
    result = presented(self, proxy, high, low, nanoseconds, refresh, *rest)
    if window is not None:
        frames.append((window.latency_ms, aim is not None and window.shown - aim > .5 * self.period(window)))
    return result


def on_frame_done(self, proxy, stamp):
    asked.append(time.clock_gettime(self.clock))
    return frame_done(self, proxy, stamp)


def on_swap(self, window):
    handed.append(time.clock_gettime(self.clock))
    state['backend'] = self
    return swap_buffers(self, window)


Backend.presented, Backend.frame_done, Backend.swap_buffers = on_presented, on_frame_done, on_swap


def lead(times, shown, period):
    """Median milliseconds before the refresh that follows them, on the grid of presented times."""
    return statistics.median((shown - at) % period for at in times) * 1000 if times and shown else 0.


@glfw_window(name='Pace trace', app_id='meltygui-pace-trace', width=900, height=420)
def app():
    now = time.monotonic()
    backend = state['backend']
    if not state['until']:
        state['until'], state['line'] = now + SECONDS, now + 1
        print(f'{"frames":>6} {"latency":>10} {"worst":>9} {"guard":>8} {"missed":>7} {"asked":>9} {"handed":>9}', flush=True)
    if now >= state['line']:
        window = max(backend.windows, key=lambda window: window.shown, default=None) if backend else None
        shown, period = (window.shown, backend.period(window)) if window else (0., 1 / 120)
        latencies = [latency for latency, missed in frames] or [0.]
        print(f'{len(frames):6d} {statistics.mean(latencies):7.1f} ms {max(latencies):6.1f} ms '
              f'{(backend.guard if backend else 0.) * 1000:5.1f} ms {sum(missed for latency, missed in frames):7d} '
              f'{lead(asked, shown, period):6.1f} ms {lead(handed, shown, period):6.1f} ms', flush=True)
        asked.clear(), handed.clear(), frames.clear()
        state['line'] = now + 1
    if now >= state['until']:
        raise SystemExit
    imgui.set_cursor_pos((30, 60))
    imgui.text('Tracing frame pacing' if native_wayland.LATE else 'Tracing frame pacing (plain: no late start)')
    # Something that moves, so every frame is a new one.
    x = 30 + (now * 400) % 800
    imgui.get_window_draw_list().add_rect_filled(x, 120, x + 40, 160, imgui.get_color_u32_rgba(.9, .5, .2, 1.))
    glfw_utils.request_render()
