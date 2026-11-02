"""Per-surface render cost and completed display cadence, excluding idle time."""
from collections import deque
import time


# Average this many completed renders; idle time never advances the window.
FPS_SAMPLE_COUNT = 60
PRESENTATION_SAMPLE_COUNT = 12  # About 100 ms at 120 Hz, updated every frame.


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


class PresentationFpsCounter(FpsCounter):
    """Displayed cadence from native (run, frame count, presentation time).

    Render cost remains available separately as frame_ms. A new display-link
    run discards the idle interval and retains the last measured rate until
    another pair of frames is presented.
    """
    def __init__(self):
        super().__init__()
        self._presentation = None
        self._intervals = deque(maxlen=PRESENTATION_SAMPLE_COUNT)
        self._fps = 0.0

    @property
    def fps(self):
        return self._fps

    def reset_presentation(self):
        self._presentation = None
        self._intervals.clear()

    def record_presentation(self, sample):
        run, count, timestamp = sample
        if count <= 0 or timestamp <= 0:
            return
        previous = self._presentation
        if previous is not None:
            old_run, old_count, old_time = previous
            if count <= old_count or timestamp <= old_time:
                return
            if run == old_run:
                self._intervals.append((count - old_count, timestamp - old_time))
                self._fps = (sum(frames for frames, _ in self._intervals)
                             / sum(duration for _, duration in self._intervals))
            else:
                self._intervals.clear()
        self._presentation = (run, count, timestamp)
