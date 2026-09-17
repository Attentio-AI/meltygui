"""Per-OS-window frame rate for the Toggles.show_fps titlebar readout.

Frames render on request, so an idle window presents nothing: the rate is
measured over the recent burst of consecutive frames only. A gap longer than
IDLE_GAP_S starts a new burst instead of averaging the idle time in; the
previous burst's rate stays on display until the new one has two frames.
"""
import time

# Frames the rate is averaged over. Raise for a steadier, slower readout.
WINDOW_FRAMES = 60
# A pause this long between frames counts as idle, not as a slow frame.
IDLE_GAP_S = 0.25


class FpsCounter:
    def __init__(self):
        self.frame_starts = []
        self.rate = None
        self.frame_ms = 0.0

    def frame_started(self):
        now = time.perf_counter()
        if self.frame_starts and now - self.frame_starts[-1] > IDLE_GAP_S:
            self.frame_starts.clear()
        self.frame_starts.append(now)
        del self.frame_starts[:-WINDOW_FRAMES]
        if len(self.frame_starts) >= 2:
            self.rate = (len(self.frame_starts) - 1) / (now - self.frame_starts[0])
        return now

    def frame_finished(self, started):
        self.frame_ms = (time.perf_counter() - started) * 1000.0

    def label(self):
        """'118 fps  7.9 ms': the latest burst's rate and the previous frame's
        render-thread time. A lone frame after idle keeps the last burst's rate."""
        rate = "--" if self.rate is None else f"{self.rate:.0f}"
        return f"{rate} fps  {self.frame_ms:.1f} ms"
