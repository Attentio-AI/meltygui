"""Timeline logging for diagnosing cross-thread slowness (symbol index / code
host load). NOT a profiler: many interacting threads plus GIL-bound parsing make
sampling misleading, so instead every meaningful unit of work writes one line
with wall-clock time, frame count, and thread label — the log reads as a single
interleaved timeline. A gap in frame numbers while a worker line is open = the
GIL was held; overlapping spans show which threads stacked up.

    12:34:56.789 f001234 [render         ] ensure_index: spawn recompute file=toggles.py

Usage:
    from meltygui.core.diagnostics.perf_trace import trace, trace_rl, span, once

    trace("warmer build start", files=150)
    with span("cold compute", file=name):      # logs "... took 812.4ms" on exit
        ...
    with span("probe", min_ms=2.0): ...        # silent unless >= 2ms
    trace_rl("slow-probe", "probe slow", ...)  # at most 1 line/sec per key
    if once(("host", name)): trace(...)        # once per key per session

Gate: Toggles.symbol_perf_log (missing Toggles => enabled). Sink is LOG_PATH,
append mode with a session header — append (not truncate) so a jedi-pool child
process importing this module can't wipe the parent's log mid-run. Never raises:
a logging failure must not take down the render loop.

Everything here is stdlib-only; project state is read via sys.modules (no
imports) so this module is importable from anywhere without cycles.
"""
import os
import sys
import threading
import time
from meltygui.core.runtime.paths import debug_log_path

LOG_PATH = debug_log_path("lsd_symbol_perf.log")
_MAX_CARRYOVER_BYTES = 5 * 1024 * 1024   # start fresh when the file grows past this

_lock = threading.Lock()
_rl_last: dict = {}       # rate-limit key -> last emit monotonic
_once_keys: set = set()   # keys already emitted via once()


def _enabled() -> bool:
    try:
        from meltygui.core.runtime.toggles import Toggles
        return bool(Toggles.symbol_perf_log)
    except Exception:
        return True


def enabled() -> bool:
    """Public gate for callers that do per-frame work BEYOND logging (e.g.
    the GPU frame timer's query objects) — same toggle as trace()."""
    return _enabled()


def _open_log():
    """One line-buffered append handle per process, adopted across hotswap /
    dual-import via sys (the established sharing pattern for process singletons)."""
    fh = getattr(sys, "_symbol_perf_fh", None)
    if fh is not None:
        return fh
    try:
        mode = "a"
        try:
            if os.path.getsize(LOG_PATH) > _MAX_CARRYOVER_BYTES:
                mode = "w"
        except OSError:
            pass
        fh = open(LOG_PATH, mode, buffering=1)
        fh.write(f"\n=== session start pid={os.getpid()} "
                 f"{time.strftime('%Y-%m-%d %X')} ===\n")
    except Exception:
        fh = False   # sentinel: don't retry every call
    sys._symbol_perf_fh = fh
    return fh


def _frame() -> int:
    mel = (sys.modules.get("meltygui.core.melty")
           or sys.modules.get("lsd.gl_gui.melty"))
    try:
        return mel.Melty.frame_count if mel is not None else -1
    except Exception:
        return -1


def _thread_label() -> str:
    t = threading.current_thread()
    try:
        gs = (sys.modules.get("meltygui.core.graphics.gl_state")
              or sys.modules.get("lsd.gl_gui.gl_state"))
        if gs is not None and getattr(gs, "_gl_thread", None) is t:
            return "render"
    except Exception:
        pass
    name = t.name
    if name == "MainThread":
        return "main"
    # ThreadPoolExecutor-0_3 -> pool_3 (Background pool workers)
    if name.startswith("ThreadPoolExecutor"):
        return "pool_" + name.rsplit("_", 1)[-1]
    return name


def _fmt_fields(fields: dict) -> str:
    if not fields:
        return ""
    parts = []
    for k, v in fields.items():
        if isinstance(v, float):
            v = f"{v:.1f}"
        parts.append(f"{k}={v}")
    return "  " + " ".join(parts)


def trace(msg: str, **fields):
    """One timeline line. Swallows every failure."""
    if not _enabled():
        return
    try:
        fh = _open_log()
        if not fh:
            return
        now = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
        line = (f"{ts} f{_frame():06d} [{_thread_label():<15}] "
                f"{msg}{_fmt_fields(fields)}\n")
        with _lock:
            fh.write(line)
    except Exception:
        pass


def trace_rl(key, msg: str, min_interval: float = 1.0, **fields):
    """Rate-limited trace: at most one line per `min_interval` seconds per key.
    For per-frame paths that are only interesting when they stay slow."""
    if not _enabled():
        return
    try:
        now = time.monotonic()
        last = _rl_last.get(key)
        if last is not None and now - last < min_interval:
            return
        _rl_last[key] = now
    except Exception:
        return
    trace(msg, **fields)


def once(key) -> bool:
    """True the first time `key` is seen this session — for once-only lines."""
    try:
        if key in _once_keys:
            return False
        _once_keys.add(key)
        return True
    except Exception:
        return False


class span:
    """Context manager logging '<label> took Xms' on exit (only when >= min_ms).
    Extra context can be attached mid-span via .add(k=v); an exception inside
    the span is noted on the line and re-raised."""

    __slots__ = ("label", "min_ms", "fields", "t0", "c0")

    def __init__(self, label: str, min_ms: float = 0.0, **fields):
        self.label = label
        self.min_ms = min_ms
        self.fields = fields
        self.t0 = 0.0
        self.c0 = 0.0

    def add(self, **fields):
        self.fields.update(fields)

    def __enter__(self):
        self.t0 = time.monotonic()
        self.c0 = time.thread_time()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            dt_ms = (time.monotonic() - self.t0) * 1000.0
            if dt_ms >= self.min_ms:
                # cpu ≪ wall on a slow span = this thread was GIL-starved, not
                # doing the work - the span label is then the victim, not the
                # culprit (see the 2026-07-31 stall hunts).
                cpu_ms = (time.thread_time() - self.c0) * 1000.0
                suffix = " EXC=" + exc_type.__name__ if exc_type is not None else ""
                trace(f"{self.label} took {dt_ms:.1f}ms (cpu {cpu_ms:.1f}ms){suffix}",
                      **self.fields)
        except Exception:
            pass
        return False


# ── Stall watchdog: what is the render thread blocked on? ──────────────────────
# Wall≫cpu slow frames (the post-boot 1000ms+ bursts) mean the render thread
# is WAITING - lock, GIL, GL/present backpressure - and the per-phase spans
# can't pin on what. This daemon samples Melty's clock; when it sits still
# past `threshold_s` while the render thread is mid-frame (NOT parked in
# glfw wait_events - an idle studio is not a stall), it dumps every thread's
# current call stack to the log. One dump per stall, re-armed when the frame
# counter moves; a second dump is forced if the stall passes 3x threshold.
# Cost when healthy: one attribute read per poll (20Hz). Same toggle as trace.

def _render_thread():
    gs = (sys.modules.get("meltygui.core.graphics.gl_state")
          or sys.modules.get("lsd.gl_gui.gl_state"))
    return getattr(gs, "_gl_thread", None) if gs is not None else None


def _dump_all_stacks(reason: str):
    import traceback
    frames = sys._current_frames()
    render_t = _render_thread()
    for t in threading.enumerate():
        frame = frames.get(t.ident)
        if frame is None:
            continue
        stack = traceback.extract_stack(frame)[-10:]
        chain = " <- ".join(
            f"{fs.filename.rsplit('/', 1)[-1]}:{fs.lineno} {fs.name}"
            for fs in reversed(stack))
        label = "render" if t is render_t else t.name
        trace(f"STALL {reason} [{label}] {chain}")


def _stall_watchdog(threshold_s: float, poll_s: float):
    last_count = -1
    still_since = time.monotonic()
    dumped = 0
    while True:
        time.sleep(poll_s)
        try:
            if not _enabled():
                continue
            count = _frame()
            now = time.monotonic()
            if count != last_count:
                last_count = count
                still_since = now
                dumped = 0
                continue
            stalled_s = now - still_since
            want = 1 if stalled_s >= threshold_s else 0
            if want and stalled_s >= threshold_s * 3:
                want = 2
            if dumped >= want:
                continue
            render_t = _render_thread()
            frame = sys._current_frames().get(render_t.ident) if render_t else None
            if frame is None:
                continue
            # Parked between frames = idle, not a stall. wait_events blocks
            # there; poll_events/sleep cover some launcher-style loops.
            names = set()
            f = frame
            while f is not None and len(names) < 12:
                names.add(f.f_code.co_name)
                f = f.f_back
            if {"wait_events", "poll_events"} & names:
                still_since = now
                continue
            dumped = want
            _dump_all_stacks(f"{stalled_s:.2f}s frame={count}")
        except Exception:
            pass  # the watchdog must never hurt the app


def ensure_stall_watchdog(threshold_s: float = 0.35, poll_s: float = 0.05):
    """Idempotent, process-lifetime (sys-guarded like the log handle — an
    in-process studio restart adopts the running one instead of stacking)."""
    if getattr(sys, "_lsd_stall_watchdog", None) is not None:
        return
    t = threading.Thread(target=_stall_watchdog, args=(threshold_s, poll_s),
                         name="stall-watchdog", daemon=True)
    sys._lsd_stall_watchdog = t
    t.start()
