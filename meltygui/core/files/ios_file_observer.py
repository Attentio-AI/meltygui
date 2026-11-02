"""Runtime notifications for SSH paths; background polling for local iOS files."""
import os
import queue
import sys
import threading
from types import SimpleNamespace

from watchdog.events import (DirCreatedEvent, DirDeletedEvent, DirModifiedEvent,
                             FileCreatedEvent, FileDeletedEvent, FileModifiedEvent)
from watchdog.observers.polling import PollingObserver


class IOSFileObserver(threading.Thread):
    """Own subscriptions off the render thread, without enumerating SSH trees."""
    def __init__(self, observer=None, bridge=None):
        super().__init__(name='ios-file-watch-registration', daemon=True)
        self._observer = observer
        self._bridge = bridge
        self._jobs = queue.Queue()
        self._stopping = False
        self._tokens = []
        self._changes_lock = threading.Lock()
        self._changes = {}
        self._overflow = False
        self._changes_queued = False
        self._callback = None

    def schedule(self, handler, path, *, recursive=False):
        token = SimpleNamespace(handler=handler, path=os.path.normpath(str(path)),
                                recursive=recursive, live=not self._stopping,
                                watch=None, remote=None)
        if token.live:
            self._jobs.put(('add', token))
        return token

    def unschedule(self, token):
        token.live = False
        if not self._stopping:
            self._jobs.put(('remove', token))

    def stop(self):
        self._stopping = True
        self._jobs.put(None)

    def _receive(self, changes):
        # Native delivery never waits for handlers, reads files or schedules
        # one Python job per file. Both sides of the bridge have a fixed cap.
        with self._changes_lock:
            if self._stopping:
                return
            for path, kind, directory in changes:
                if self._overflow:
                    break
                if len(self._changes) >= 4096 and path not in self._changes:
                    self._changes.clear()
                    self._overflow = True
                    break
                self._changes[path] = (path, kind, directory)
            if not self._changes_queued:
                self._changes_queued = True
                self._jobs.put(('changes', None))

    @staticmethod
    def _within(path, root):
        return path == root or path.startswith(root.rstrip('/') + '/')

    def _dispatch(self, path, kind, directory):
        events = {(False, 'created'): FileCreatedEvent, (True, 'created'): DirCreatedEvent,
                  (False, 'deleted'): FileDeletedEvent, (True, 'deleted'): DirDeletedEvent,
                  (False, 'modified'): FileModifiedEvent, (True, 'modified'): DirModifiedEvent}
        for token in tuple(self._tokens):
            if not token.live or token.remote is None:
                continue
            if kind == 'overflow':
                if not (self._within(token.remote, path) or self._within(path, token.remote)):
                    continue
                # Invalidate only state consumers already hold, never rescan.
                callback = getattr(token.handler, 'on_overflow', None)
                if callback:
                    callback(token.path, token.recursive)
                token.handler.dispatch(DirModifiedEvent(token.path))
                continue
            if not self._within(path, token.remote):
                continue
            if not token.recursive and path != token.remote and os.path.dirname(path) != token.remote:
                continue
            suffix = path[len(token.remote):].lstrip('/')
            local = os.path.join(token.path, suffix) if suffix else token.path
            token.handler.dispatch(events[(bool(directory), kind)](local))
            if kind in ('created', 'deleted') and suffix:
                token.handler.dispatch(DirModifiedEvent(os.path.dirname(local)))

    def run(self):
        try:
            if self._bridge is None and sys.platform == 'ios':
                import _hub_files
                self._bridge = _hub_files
            if self._bridge is not None:
                self._callback = self._receive  # native side deliberately retains only a weakref
                self._bridge.watch_paths(self._callback)
            while (job := self._jobs.get()) is not None:
                operation, token = job
                if self._stopping:
                    continue
                try:
                    if operation == 'changes':
                        with self._changes_lock:
                            changes = ([('/', 'overflow', True)] if self._overflow
                                       else list(self._changes.values()))
                            self._changes.clear()
                            self._overflow = self._changes_queued = False
                        for change in changes:
                            self._dispatch(*change)
                    elif operation == 'add' and token.live:
                        token.remote = self._bridge.remote_path(token.path) if self._bridge else None
                        if token.remote is not None:
                            self._tokens.append(token)
                            if token.live:
                                token.handler.dispatch(DirModifiedEvent(token.path))
                        else:
                            if self._observer is None:
                                self._observer = PollingObserver()
                            if not self._observer.is_alive():
                                self._observer.start()
                            token.watch = self._observer.schedule(token.handler, token.path,
                                                                 recursive=token.recursive)
                            if token.live and not self._stopping:
                                self._observer.event_queue.put((DirModifiedEvent(token.path), token.watch))
                            else:
                                self._observer.unschedule(token.watch)
                                token.watch = None
                    elif operation == 'remove':
                        self._tokens[:] = [item for item in self._tokens if item is not token]
                        if token.watch is not None:
                            self._observer.unschedule(token.watch)
                            token.watch = None
                except Exception as error:
                    print(f'FileWatch: cannot {operation}: {error}', flush=True)
        finally:
            self._callback = None
            self._tokens.clear()
            with self._changes_lock:
                self._changes.clear()
            if self._observer is not None:
                self._observer.stop()
                if self._observer.is_alive():
                    self._observer.join()
