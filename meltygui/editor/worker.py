"""The editor session's background thread: whole-file work off the render thread.

    worker.ask('folds', outline, text)      # the newest request of a kind wins
    for kind, result in worker.take(): ...  # on the render thread, next run

The thread starts with the first request and ends after `IDLE` seconds
without one. Requests made while the user types wait until the text rests:
a running job shares the interpreter with the render thread, so frames drawn
meanwhile are slower. Ask only when the answer can have changed.
"""
import threading
import time
from collections import deque

# Levers: seconds requests must stop coming before they run; seconds without
# work before the thread ends.
QUIET = 0.15
IDLE = 20.


class Worker:
    def __init__(self):
        self.jobs = {}                 # kind -> (function, arguments), waiting
        self.done = deque()            # (kind, result), for the render thread
        self.wake = None               # called from the thread when results wait
        self.lock = threading.Lock()   # guards `jobs` and `thread`
        self.running = threading.Lock()
        self.event = threading.Event()
        self.thread = None
        self.due = 0.                  # when the waiting requests may run

    def ask(self, kind, function, *arguments, rest=False, now=False):
        """Run `function(*arguments)`; its result arrives through `take`.
        `rest`: wait for the text to rest first. `now`: run it right here
        (small inputs, where a frame without the result would show)."""
        if now:
            self.jobs.pop(kind, None)
            self.done.append((kind, function(*arguments)))
            return
        with self.lock:
            self.jobs[kind] = (function, arguments)
            # Typing only moves `due`: the thread is not woken per keystroke.
            self.due = time.monotonic() + (QUIET if rest else 0.)
            if self.thread is None:
                self.thread = threading.Thread(target=self.work, name='editor-worker', daemon=True)
                self.thread.start()
        self.event.set()

    def take(self):
        """The results that arrived, oldest first."""
        found = []
        while self.done:
            found.append(self.done.popleft())
        return found

    def run(self):
        with self.lock:
            jobs, self.jobs = self.jobs, {}
        for kind, (function, arguments) in jobs.items():
            try:
                self.done.append((kind, function(*arguments)))
            except Exception as error:
                print(f'editor worker: {kind} failed: {error!r}')
        return bool(jobs)

    def settle(self):
        """Finish everything asked for, on the calling thread (tests, exit)."""
        with self.running:
            if self.run() and self.wake is not None:
                self.wake()

    def work(self):
        while True:
            if not self.event.wait(IDLE):
                with self.lock:
                    if not self.jobs:
                        self.thread = None
                        return
            self.event.clear()
            while self.due > time.monotonic():
                time.sleep(max(self.due - time.monotonic(), 0.))
            with self.running:
                ran = self.run()
            if ran and self.wake is not None:
                self.wake()
