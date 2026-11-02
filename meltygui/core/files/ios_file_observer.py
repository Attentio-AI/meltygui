"""Keep polling-watch startup and teardown off the iOS render thread."""
import queue
import threading
from types import SimpleNamespace

from watchdog.events import DirModifiedEvent
from watchdog.observers.polling import PollingObserver


class IOSFileObserver(threading.Thread):
    """FileWatch's lifecycle owner; only this worker touches the polling observer.

    Watchdog takes an initial directory snapshot in schedule(), before starting
    the emitter, and joins it in unschedule(). Both can perform slow SSH I/O.
    Tokens let views relinquish a watch even while that initial scan is running.
    """
    def __init__(self, observer=None):
        super().__init__(name='ios-file-watch-registration', daemon=True)
        self._observer = observer if observer is not None else PollingObserver()
        self._jobs = queue.Queue()
        self._stopping = False

    def schedule(self, handler, path, *, recursive=False):
        token = SimpleNamespace(handler=handler, path=path, recursive=recursive,
                                live=not self._stopping, watch=None)
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

    def run(self):
        observer = self._observer
        try:
            observer.start()
            while (job := self._jobs.get()) is not None:
                operation, token = job
                if self._stopping:
                    continue
                try:
                    if operation == 'add' and token.live:
                        token.watch = observer.schedule(token.handler, token.path,
                                                        recursive=token.recursive)
                        if token.live and not self._stopping:
                            # Changes during the initial scan have no previous
                            # snapshot to diff against. Refresh the view once.
                            observer.event_queue.put((DirModifiedEvent(token.path), token.watch))
                        else:
                            observer.unschedule(token.watch)
                            token.watch = None
                    elif operation == 'remove' and token.watch is not None:
                        observer.unschedule(token.watch)
                        token.watch = None
                except OSError as error:
                    print(f'FileWatch: cannot {operation} watch {token.path}: {error}', flush=True)
        finally:
            observer.stop()
            if observer.is_alive():
                observer.join()
