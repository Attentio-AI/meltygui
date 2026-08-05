"""Timeline logging for diagnosing cross-thread slowness (symbol index / code
host load). NOT a profiler: many interacting threads plus GIL-bound parsing make
sampling misleading, so instead every meaningful unit of work writes one line
with wall-clock time, frame count, and thread label — the log reads as a single
interleaved timeline. A gap in frame numbers while a worker line is open = the
GIL was held; overlapping spans show which threads stacked up.

    12:34:56.789 f001234 [render         ] ensure_index: spawn recompute file=toggles.py

Usage:
    from src.lsd.gl_gui.perf_trace import trace, trace_rl, span, once

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

LOG_PATH = "/tmp/lsd_symbol_perf.log"
_MAX_CARRYOVER_BYTES = 5 * 1024 * 1024   # start fresh when the file grows past this

_lock = threading.Lock()
_rl_last: dict = {}       # rate-limit key -> last emit monotonic
_once_keys: set = set()   # keys already emitted via once()


def _enabled() -> bool:
    try:
        from src.lsd.gl_gui.toggles import Toggles
        return bool(getattr(Toggles, "symbol_perf_log", True))
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
    mel = (sys.modules.get("src.lsd.gl_gui.melty")
           or sys.modules.get("lsd.gl_gui.melty"))
    try:
        return mel.Melty.frame_count if mel is not None else -1
    except Exception:
        return -1


def _thread_label() -> str:
    t = threading.current_thread()
    try:
        gs = (sys.modules.get("src.lsd.gl_gui.gl_state")
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
