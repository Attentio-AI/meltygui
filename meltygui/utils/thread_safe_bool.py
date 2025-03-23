## Utils
from asyncio import Lock


def clamp(value, min_value, max_value):
    return max(min(value, max_value), min_value)


class ThreadSafeBool:
    def __init__(self, initial_value=False):
        self._value = initial_value
        self._lock = Lock()

    def __bool__(self):
        with self._lock:
            return self._value

    def __eq__(self, other):
        with self._lock:
            return self._value == other

    def __set__(self, value):
        with self._lock:
            self._value = bool(value)
