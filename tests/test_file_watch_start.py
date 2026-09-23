"""Models may request the shared observer concurrently from background readers."""
import threading
import time
from types import SimpleNamespace

from meltygui.core.melty import FileWatch


def test_start_is_atomic_for_concurrent_subscribers(monkeypatch):
    class Observer:
        started = False
        count = 0

        def is_alive(self):
            return self.started

        def start(self):
            self.count += 1
            time.sleep(0.02)
            self.started = True

    observer = Observer()
    monkeypatch.setattr(FileWatch, "observer", observer)
    monkeypatch.setattr(FileWatch, "handler", SimpleNamespace())
    start = threading.Barrier(6)

    def subscribe():
        start.wait()
        FileWatch.start()

    threads = [threading.Thread(target=subscribe) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
    assert observer.count == 1
