"""Run large background compiler checks without holding the UI interpreter's GIL."""
import atexit
import queue
import pickle
import sys
import threading
from pathlib import Path


class _CompilerWorker:
    def __init__(self):
        self.lock = threading.Lock()
        self.interpreter = None
        self.channel = None
        self.unavailable = False
        self.requests = queue.Queue()
        self.thread = None

    def check(self, text, prefixes):
        reply = queue.Queue(maxsize=1)
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name='syntax-compiler', daemon=True)
                self.thread.start()
            self.requests.put((text, prefixes, reply))
        result = reply.get()
        if isinstance(result, BaseException) and not isinstance(result, SyntaxError):
            raise result
        return result

    def run(self):
        try:
            while True:
                request = self.requests.get()
                if request is None:
                    break
                text, prefixes, reply = request
                try:
                    reply.put(self.execute(text, prefixes))
                except BaseException as error:
                    reply.put(error)
        finally:
            # CPython interpreter destruction must run on its owning thread.
            if self.interpreter is not None:
                import _xxsubinterpreters as interpreters
                import _xxinterpchannels as channels
                interpreters.destroy(self.interpreter)
                channels.destroy(self.channel)
                self.interpreter = self.channel = None

    def execute(self, text, prefixes):
        from meltygui.code.syntax_check import check_syntax
        if self.unavailable:
            return check_syntax(text, prefixes)
        with self.lock:
            try:
                import _xxsubinterpreters as interpreters
                import _xxinterpchannels as channels
                if self.interpreter is None:
                    self.interpreter = interpreters.create()
                    self.channel = channels.create()
                # Load this small, pure helper module so source hotswaps apply.
                # Never import the GUI package into the worker interpreter.
                interpreters.run_string(self.interpreter, '''
import pickle, runpy, _xxinterpchannels as channels
check = runpy.run_path(HELPER)['check_syntax']
error = check(TEXT, pickle.loads(PREFIXES))
payload = None if error is None else (type(error), error.args, {
    name: getattr(error, name, None) for name in
    ('msg', 'filename', 'lineno', 'offset', 'text', 'end_lineno', 'end_offset', 'print_file_and_line')})
channels.send(CHANNEL, pickle.dumps(payload))
del TEXT, PREFIXES, error
''', shared={'HELPER': str(Path(__file__).with_name('syntax_check.py')),
             'TEXT': text, 'PREFIXES': pickle.dumps(prefixes), 'CHANNEL': int(self.channel)})
                payload = pickle.loads(channels.recv(self.channel))
                if payload is None:
                    return None
                kind, args, fields = payload
                error = kind(*args)
                for name, value in fields.items():
                    setattr(error, name, value)
                return error
            except Exception as error:
                self.unavailable = True
                print(f'Isolated syntax checks unavailable ({error!r}); using local compiler')
        return check_syntax(text, prefixes)

    def close(self):
        with self.lock:
            thread = self.thread
            if thread is not None:
                self.requests.put(None)
        if thread is not None:
            thread.join()
            with self.lock:
                self.thread = None


def check_isolated(text, prefixes):
    current = threading.current_thread()
    gl_state = sys.modules.get('meltygui.gl_state')
    if current is threading.main_thread() or current is getattr(gl_state, '_gl_thread', None):
        from meltygui.code.syntax_check import check_syntax
        return check_syntax(text, prefixes)
    return _worker.check(text, prefixes)


# Import serialization makes first creation safe even with concurrent imports.
_worker = getattr(sys, '_melty_syntax_check_worker', None)
if _worker is None:
    _worker = sys._melty_syntax_check_worker = _CompilerWorker()
    atexit.register(_worker.close)
