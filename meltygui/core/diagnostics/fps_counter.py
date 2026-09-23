"""Per-surface render cost, excluding all idle time between requested frames."""
from collections import deque
import time


# Average this many completed renders; idle time never advances the window.
FPS_SAMPLE_COUNT = 60


class FpsCounter:
    def __init__(self):
        self.frame_ms = 0.0
        self.render_times = deque(maxlen=FPS_SAMPLE_COUNT)

    @property
    def fps(self):
        total_ms = sum(self.render_times)
        return 1000.0 * len(self.render_times) / total_ms if total_ms > 0 else 0.0

    def frame_started(self):
        return time.perf_counter()

    def frame_finished(self, started):
        self.frame_ms = (time.perf_counter() - started) * 1000.0
        self.render_times.append(self.frame_ms)
