import subprocess
import threading

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
            result = subprocess.run(["ddcutil", "getvcp", "10"], capture_output=True, text=True)
            for part in result.stdout.split(","):
                if "current value" in part:
                    cls._brightness = int(part.split("=")[1].strip())
                    return
            cls._brightness = 1

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