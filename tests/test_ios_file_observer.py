"""Real polling watches with intentionally stalled initial filesystem I/O."""
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingEmitter, PollingObserver
from meltygui.core.files.ios_file_observer import IOSFileObserver


def test_initial_scan_and_cancel_do_not_block_caller(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    threads = []
    original = PollingEmitter.on_thread_start
    def blocked(emitter):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        original(emitter)
    monkeypatch.setattr(PollingEmitter, 'on_thread_start', blocked)
    delegate = PollingObserver(timeout=.02)
    observer = IOSFileObserver(delegate)
    observer.start()
    try:
        start = time.monotonic()
        watch = observer.schedule(FileSystemEventHandler(), str(tmp_path))
        assert time.monotonic() - start < .2
        assert entered.wait(1)
        start = time.monotonic()
        observer.unschedule(watch)
        observer.stop()
        assert time.monotonic() - start < .2
    finally:
        release.set()
        observer.stop()
        observer.join(3)
    assert threads == [observer.ident]
    assert not observer.is_alive() and not delegate.is_alive()
    assert not delegate.emitters


def test_ready_watch_reports_refresh_and_external_changes(tmp_path):
    ready, modified = threading.Event(), threading.Event()
    file = tmp_path / 'file.txt'
    file.write_text('before')
    class Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if event.is_directory:
                ready.set()
            elif event.src_path == str(file):
                modified.set()
    observer = IOSFileObserver(PollingObserver(timeout=.02))
    observer.start()
    try:
        observer.schedule(Handler(), str(tmp_path))
        assert ready.wait(2)
        file.write_text('after with different size')
        assert modified.wait(2)
    finally:
        observer.stop()
        observer.join(3)
    assert not observer.is_alive()
