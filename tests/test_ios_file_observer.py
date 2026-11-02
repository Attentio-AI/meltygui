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


class RuntimeBridge:
    def __init__(self):
        self.callback = None

    def watch_paths(self, callback):
        import weakref
        self.callback = weakref.ref(callback)

    def remote_path(self, path):
        return '/home/project' + path[len('/phone/project'):] if path.startswith('/phone/project') else None

    def emit(self, *changes):
        self.callback()(changes)


def test_remote_watch_never_traverses_or_polls_and_maps_events(monkeypatch):
    bridge = RuntimeBridge()
    ready, modified, deleted = threading.Event(), threading.Event(), threading.Event()
    def forbidden(*args, **kwargs):
        raise AssertionError('SSH watch attempted filesystem traversal/polling')
    monkeypatch.setattr('meltygui.core.files.ios_file_observer.PollingObserver', forbidden)
    monkeypatch.setattr('os.scandir', forbidden)
    events = []
    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            events.append((event.event_type, event.src_path))
            if event.is_directory:
                ready.set()
            if event.src_path == '/phone/project/nested/file.py':
                (deleted if event.event_type == 'deleted' else modified).set()
    observer = IOSFileObserver(bridge=bridge)
    observer.start()
    try:
        token = observer.schedule(Handler(), '/phone/project', recursive=True)
        assert ready.wait(2)
        bridge.emit(('/home/project/nested/file.py', 'modified', False))
        assert modified.wait(2)
        bridge.emit(('/home/project/nested/file.py', 'deleted', False))
        assert deleted.wait(2)
        observer.unschedule(token)
        assert observer._observer is None
    finally:
        observer.stop()
        observer.join(3)
    assert not observer.is_alive()
    assert bridge.callback() is None


def test_nonrecursive_watch_and_cancellation():
    bridge = RuntimeBridge()
    ready, modified = threading.Event(), threading.Event()
    seen = []
    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            seen.append(event.src_path)
            if event.src_path == '/phone/project':
                ready.set()
            if event.src_path == '/phone/project/direct.py':
                modified.set()
    observer = IOSFileObserver(bridge=bridge)
    observer.start()
    try:
        token = observer.schedule(Handler(), '/phone/project')
        assert ready.wait(2)
        bridge.emit(('/home/project/nested/deep.py', 'modified', False),
                    ('/home/project/direct.py', 'created', False))
        assert modified.wait(2)
        assert '/phone/project/nested/deep.py' not in seen
        observer.unschedule(token)
        assert not token.live
    finally:
        observer.stop()
        observer.join(3)


def test_slow_consumer_has_bounded_queue_and_overflow_without_scan(monkeypatch):
    observer = IOSFileObserver(bridge=RuntimeBridge())
    for i in range(12000):
        observer._receive([(f'/home/project/{i}.py', 'modified', False)])
    assert len(observer._changes) <= 4096
    assert observer._jobs.qsize() == 1
    assert observer._overflow
    calls = []
    handler = FileSystemEventHandler()
    handler.on_overflow = lambda *args: calls.append(args)
    token = observer.schedule(handler, '/phone/project', recursive=True)
    token.remote = '/home/project'
    observer._tokens.append(token)
    observer._dispatch('/', 'overflow', True)
    assert calls == [('/phone/project', True)]
    token.live = False
    observer._dispatch('/', 'overflow', True)
    assert len(calls) == 1


def test_overflow_refreshes_only_known_files_without_walking(monkeypatch):
    from meltygui.core.melty import FileWatch, Melty
    seen = []
    monkeypatch.setattr(Melty, 'code_cache', {'/project/cached.py': 'old', '/elsewhere/keep.py': 'old'})
    monkeypatch.setattr(FileWatch, 'path_to_draw_states', {'/project/nested/open.py': set()})
    monkeypatch.setattr(FileWatch, 'project_tracked', {'/project/tracked.py'})
    monkeypatch.setattr(FileWatch, '_on_event', lambda event: seen.append(event.src_path))
    FileWatch._on_overflow('/project', True)
    assert set(seen) == {'/project/cached.py', '/project/nested/open.py', '/project/tracked.py'}
    seen.clear()
    FileWatch._on_overflow('/project', False)
    assert set(seen) == {'/project/cached.py', '/project/tracked.py'}
