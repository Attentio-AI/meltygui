"""How late can a frame be handed to this display's compositor and still make its refresh?

    .venv/bin/python tools/pace_calibrate.py [seconds per step]

Opens a window that redraws continuously and steps `native_wayland.GUARD` (the time before a
refresh by which a frame is handed over) from long to short, a few seconds each. For every step
it prints how many frames missed their refresh and the input-read-to-screen time. Pick the
shortest guard whose misses are no more than the first row's (those are the compositor's own):
that is where the self-tuning guard (`Backend.tune`) should settle, and a good `GUARD` (its start) in
meltygui/core/windowing/backends/native_wayland.py. Move the pointer
over the window during a second run: the compositor is busier while it handles input, and frames
that are slower to draw than this one need the difference on top. `off grid` is how far the
compositor's presented times stray from its own period: the late start aims at them, so a large
value means the aim itself moves. Native Wayland only.
"""
import sys
import time

from meltygui import glfw_window, imgui
from meltygui.core.windowing import glfw_utils
from meltygui.core.windowing.backends import native_wayland

# Lever: the guards tried, in milliseconds, and the seconds each runs.
GUARDS = [6., 5., 4.5, 4., 3.5, 3., 2.5, 2., 1.5, 1., .5]
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 4.

native_wayland.LATE, native_wayland.ADAPT = True, False    # fixed guards: nothing tunes itself while measuring
presented = native_wayland.Backend.presented
rows = {}                # guard -> [late frames, missed, sum of latency ms, off-grid ms of each frame, worst latency ms]
state = {'step': -1, 'until': 0.}


def judge(self, proxy, high, low, nanoseconds, refresh, *rest):
    window, sampled, aim = self.feedbacks.get(proxy, (None, 0., None))
    before = window.shown if window is not None else 0.
    result = presented(self, proxy, high, low, nanoseconds, refresh, *rest)
    if aim is not None and state['step'] >= 0:
        # As the backend judges it: a refresh skipped between two frames in a row.
        period = self.period(window)
        row = rows.setdefault(GUARDS[state['step']], [0, 0, 0., [], 0.])
        gap = window.shown - before
        row[0] += 1
        row[1] += 1.5 * period < gap < 2.5 * period
        row[2] += window.latency_ms
        # How far the compositor's presented time is from a whole number of periods after the last.
        row[3].append(abs(gap - round(gap / period) * period) * 1000)
        row[4] = max(row[4], window.latency_ms)
    return result


native_wayland.Backend.presented = judge


@glfw_window(name='Pace calibration', app_id='meltygui-pace-calibrate', width=900, height=420)
def app():
    now = time.monotonic()
    if now >= state['until']:
        state['step'] += 1
        if state['step'] >= len(GUARDS):
            print(f'{"guard":>8} {"frames":>7} {"missed":>7} {"":>6} {"input read to screen":>22} {"worst":>9} {"off grid (95 %)":>17}')
            for guard, (frames, missed, latency, off, worst) in rows.items():
                print(f'{guard:6.1f} ms {frames:7d} {missed:7d} {missed / max(frames, 1):6.1%} {latency / max(frames, 1):19.1f} ms '
                      f'{worst:6.1f} ms {sorted(off)[int(len(off) * .95)] if off else 0.:14.2f} ms', flush=True)
            raise SystemExit
        # (The first second of a step still shows frames of the one before: they are few.)
        native_wayland.GUARD = GUARDS[state['step']] / 1000
        state['until'] = now + SECONDS
    imgui.set_cursor_pos((30, 60))
    imgui.text(f'Measuring: guard {GUARDS[state["step"]]:.1f} ms  (step {state["step"] + 1} of {len(GUARDS)})')
    # Something that moves, so every frame is a new one.
    x = 30 + (now * 400) % 800
    imgui.get_window_draw_list().add_rect_filled(x, 120, x + 40, 160, imgui.get_color_u32_rgba(.9, .5, .2, 1.))
    glfw_utils.request_render()
