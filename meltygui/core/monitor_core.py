import subprocess
import threading
import time

from meltygui.core.mcp_server import _LOG_RESULT_CAP
from meltygui.core.render_funcs import RenderFuncs
from meltygui.core.window_decoration import window

_SENTINEL = object()


class _MonitorMeta(type):
    _brightness = None
    _pending = None
    _event = threading.Event()
    _thread = None
    _lock = threading.Lock()

    def __init__(cls, *args):
        super().__init__(*args)

        def _worker():
            while True:
                cls._event.wait()
                cls._event.clear()
                with cls._lock:
                    value = cls._pending
                if value is _SENTINEL:
                    break
                try:
                    value = min(max(1, value), 100)
                    subprocess.run(["ddcutil", "setvcp", "10", str(value)])
                except Exception as e:
                    print(f"Monitor error: {e}")

        cls._thread = threading.Thread(target=_worker, daemon=True)
        cls._thread.start()

        def _fetch():
            # result = subprocess.run(["ddcutil", "getvcp", "10"], capture_output=True, text=True)
            # for part in result.stdout.split(","):
            #     if "current value" in part:
            #         cls._brightness = int(part.split("=")[1].strip())
            #         return
            cls._brightness = 30

        threading.Thread(target=_fetch, daemon=True).start()

    @property
    def brightness(cls):
        return cls._brightness

    @brightness.setter
    def brightness(cls, value):
        cls._brightness = value
        with cls._lock:
            cls._pending = value
        cls._event.set()

    def shutdown(cls):
        with cls._lock:
            cls._pending = _SENTINEL
        cls._event.set()
        cls._thread.join()


class Monitor(metaclass=_MonitorMeta):
    pass


@window(live=True, view_func=RenderFuncs.draw_type)
class MCPServerLog:
    """Append-only, in-memory record of MCP tool interactions for this launcher.

    Every tool call (and its outcome) is appended to the ``logs`` class
    attribute by the logging wrapper in ``start_launcher_mcp``. It's a class
    attribute rather than an instance field so it's reachable from anywhere —
    including ``eval_python`` — as just ``MCPServerLog.logs`` without threading
    an instance around.

    Each entry is a dict::

        {"tool": str, "args": dict, "result": str|None,
         "error": str|None, "timestamp": float}
    """

    logs = []
    def __init__(self):
        pass

    @classmethod
    def record(cls, tool, args, result=None, error=None):
        def _cap(v):
            if v is None:
                return None
            s = v if isinstance(v, str) else repr(v)
            return s if len(s) <= _LOG_RESULT_CAP else s[:_LOG_RESULT_CAP] + "…"

        cls.logs.append({
            "tool": tool,
            "args": dict(args),
            "result": _cap(result),
            "error": _cap(error),
            "timestamp": time.time(),
        })
