"""GPU frame-segment timing via GL_TIMESTAMP query objects.

CPU timers around post_frame's segments show where the RENDER THREAD blocks,
but a present stall is GPU backpressure: the CPU submits everything quickly
and swap_buffers waits for the queue to drain — the CPU split blames "swap"
while the actual cost sits in whichever segment's draw calls flooded the GPU
(UI draw data, the tile-capture pass, the filter/shadow passes, ...).
Timestamp queries mark when the GPU *executes* each boundary, so a completed
frame yields per-segment GPU milliseconds.

Non-blocking by design: results are polled a few frames late (ring of DEPTH
slots) and a slot whose result isn't ready yet is simply dropped when its
turn to be reused comes. Inert unless the perf-trace toggle is on, and it
permanently disables itself on the first GL error (headless contexts, old
drivers, missing GL 3.3 timer support)."""

import time


class GpuFrameTimer:
    DEPTH = 4          # frames in flight before a result is read
    MAX_STAMPS = 8     # boundaries per frame

    def __init__(self):
        self._gl = None
        self._rings = None      # DEPTH x MAX_STAMPS query IDs
        self._labels = None     # DEPTH x MAX_STAMPS stamp labels
        self._counts = [0] * self.DEPTH
        self._frames = [None] * self.DEPTH   # app frame number per slot
        self._slot = 0
        self._dead = False
        self._active = False    # stamping enabled for the current frame

    def begin(self, enabled, frame_no):
        """Arm the current slot for this frame. GL context must be current."""
        if self._dead or not enabled:
            self._active = False
            return
        try:
            if self._rings is None:
                from OpenGL import GL as gl
                self._gl = gl
                self._rings = [[int(q) for q in gl.glGenQueries(self.MAX_STAMPS)]
                               for _ in range(self.DEPTH)]
                self._labels = [[None] * self.MAX_STAMPS
                                for _ in range(self.DEPTH)]
            self._counts[self._slot] = 0
            self._frames[self._slot] = frame_no
            self._active = True
        except Exception:
            self._dead = True
            self._active = False

    def stamp(self, label):
        if not self._active:
            return
        try:
            i = self._counts[self._slot]
            if i >= self.MAX_STAMPS:
                return
            self._gl.glQueryCounter(self._rings[self._slot][i],
                                    self._gl.GL_TIMESTAMP)
            self._labels[self._slot][i] = label
            self._counts[self._slot] = i + 1
        except Exception:
            self._dead = True
            self._active = False

    def end(self):
        """Advance the ring; poll the OLDEST slot. Returns (frame_no,
        {label: gpu_ms}) for a completed frame, else None. Never blocks: an
        unavailable result stays queued until its slot is about to be reused,
        then drops."""
        if not self._active:
            return None
        out = None
        try:
            gl = self._gl
            nxt = (self._slot + 1) % self.DEPTH
            n = self._counts[nxt]
            if n >= 2 and self._frames[nxt] is not None:
                avail = gl.glGetQueryObjectiv(self._rings[nxt][n - 1],
                                              gl.GL_QUERY_RESULT_AVAILABLE)
                if avail:
                    ts = [int(gl.glGetQueryObjectui64v(self._rings[nxt][i],
                                                       gl.GL_QUERY_RESULT))
                          for i in range(n)]
                    segs = {}
                    for i in range(1, n):
                        segs[self._labels[nxt][i]] = (ts[i] - ts[i - 1]) / 1e6
                    out = (self._frames[nxt], segs)
                    self._frames[nxt] = None
                    self._counts[nxt] = 0
            self._slot = nxt
        except Exception:
            self._dead = True
            self._active = False
            return None
        return out


# Module singleton; survives hotswap (re-exec reuses existing context).
GPU_TIMER = globals().get("GPU_TIMER") or GpuFrameTimer()
