"""Thread-scoped Python standard streams for the editor's function console.

Other application threads keep their original streams. OS file descriptors and
subprocess output are deliberately untouched. Captures are bounded per run.
"""
import contextlib
import io
import sys
import threading


_local = threading.local()
_stream_lock = threading.RLock()
_stream_users = 0
_proxies = {}


class _StreamProxy:
    def __init__(self, name, fallback):
        self.name = name
        self.fallback = fallback

    def __getattr__(self, name):
        streams = getattr(_local, 'streams', {})
        return getattr(streams.get(self.name, self.fallback), name)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line


@contextlib.contextmanager
def capture(console):
    global _stream_users
    with _stream_lock:
        if not _stream_users:
            for name in ('stdin', 'stdout', 'stderr'):
                proxy = _StreamProxy(name, getattr(sys, name))
                _proxies[name] = proxy
                setattr(sys, name, proxy)
        _stream_users += 1
    previous = getattr(_local, 'streams', {})
    _local.streams = dict(stdin=console.stdin, stdout=console.stdout, stderr=console.stderr)
    try:
        yield
    finally:
        _local.streams = previous
        with _stream_lock:
            _stream_users -= 1
            if not _stream_users:
                for name, proxy in _proxies.items():
                    if getattr(sys, name) is proxy:
                        setattr(sys, name, proxy.fallback)
                _proxies.clear()


class _Output(io.TextIOBase):
    encoding = 'utf-8'

    def __init__(self, console):
        self.console = console

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError('write() requires str')
        self.console.append(text)
        return len(text)

    def flush(self):
        pass


class _Input(io.TextIOBase):
    encoding = 'utf-8'

    def __init__(self, console):
        self.console = console

    def readable(self):
        return True

    def readline(self, size=-1):
        return self.console.read(size, line=True)

    def read(self, size=-1):
        return self.console.read(size, line=False)


class FunctionConsole:
    LIMIT = 200_000

    def __init__(self, wake=lambda: None):
        self.wake = wake
        self.condition = threading.Condition()
        self.text = ''
        self.input_text = ''
        self.eof = False
        self.waiting = False
        self.running = False
        self.draft = ''
        self.thread = None
        self.stdin = _Input(self)
        self.stdout = _Output(self)
        self.stderr = _Output(self)

    def append(self, text):
        with self.condition:
            self.text = (self.text + text)[-self.LIMIT:]
        self.wake()

    def snapshot(self):
        with self.condition:
            return self.text, self.running, self.waiting

    def send(self, text):
        with self.condition:
            if self.eof or not self.running:
                return
            self.text = (self.text + text + '\n')[-self.LIMIT:]
            self.input_text += text + '\n'
            self.condition.notify_all()
        self.wake()

    def close_input(self):
        with self.condition:
            self.eof = True
            self.condition.notify_all()
        self.wake()

    def read(self, size, line):
        if size == 0:
            return ''
        with self.condition:
            while True:
                newline = self.input_text.find('\n')
                enough = size >= 0 and len(self.input_text) >= size
                if self.eof or enough or (line and newline >= 0):
                    count = len(self.input_text) if size < 0 else min(size, len(self.input_text))
                    if line and newline >= 0:
                        count = min(count, newline + 1)
                    result, self.input_text = self.input_text[:count], self.input_text[count:]
                    self.waiting = False
                    return result
                self.waiting = True
                self.wake()
                self.condition.wait()

    def start(self, run, done):
        """Start one run; done(result) runs on the worker and must marshal UI work."""
        with self.condition:
            if self.running:
                return False
            self.running = True
            self.waiting = self.eof = False
            self.text = self.input_text = self.draft = ''

        def worker():
            try:
                with capture(self):
                    try:
                        result = run()
                    except BaseException as error:
                        # SystemExit/KeyboardInterrupt must finish this run,
                        # never terminate the editor or strand its busy thread.
                        result = (False, f'{type(error).__name__}: {error}')
                        self.append(result[1] + '\n')
            finally:
                with self.condition:
                    self.running = self.waiting = False
                self.wake()
            done(result)

        self.thread = threading.Thread(target=worker, name='meltygui-function-console', daemon=True)
        self.thread.start()
        self.wake()
        return True
