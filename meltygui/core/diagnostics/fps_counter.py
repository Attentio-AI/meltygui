"""Per-OS-window render-thread frame time for the Toggles.show_fps titlebar
readout (titlebar.paint_fps). The rate beside it is imgui's own
io.framerate; this measures what one Surface.frame cost.
"""
import time


class FpsCounter:
    def __init__(self):
        self.frame_ms = 0.0

    def frame_started(self):
        return time.perf_counter()

    def frame_finished(self, started):
        self.frame_ms = (time.perf_counter() - started) * 1000.0
