"""An executing legacy PTY reader survives the transition to output subscribers."""
import ast
import os
from pathlib import Path
import sys
import time

from meltygui.code.file_converters import _hotswap_class
from meltygui.core.services import terminal_runtime
from meltygui.model import terminal_model
from meltygui.state.terminal_state import TerminalScreenState


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, 'PTY reader did not publish within five seconds'
        time.sleep(0.01)


def compile_terminal(namespace, legacy=False):
    tree = ast.parse(Path(terminal_model.__file__).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    cls.decorator_list = []  # Test definitions must not register as application values.
    if legacy:
        fixture = ast.parse((Path(__file__).parent / 'fixtures/terminal_before_runtime.py').read_text())
        methods = next(node for node in fixture.body if isinstance(node, ast.ClassDef)).body
        replacements = {node.name: node for node in methods}
        cls.body = [replacements.get(node.name, node) if isinstance(node, ast.FunctionDef) else node
                    for node in cls.body if not isinstance(node, ast.FunctionDef)
                    or node.name not in ('subscribe', 'unsubscribe', '_notify_changed', 'snapshot')]
    exec(compile(ast.fix_missing_locations(tree), '<terminal-live-migration>', 'exec'), namespace)
    return namespace['Terminal']


class Owner:
    closable = False

    def __init__(self):
        self.invalidations = 0

    def invalidate(self):
        self.invalidations += 1

    def invalidate_up(self, force=False):
        assert force
        self.invalidations += 1


def test_running_legacy_frame_and_held_callbacks_follow_hotswap(monkeypatch):
    # Compile the real current class with the exact old initializer/reader. This
    # preserves the old field layout and an ACTUALLY executing old code object,
    # while avoiding edits to production definitions during the test suite.
    namespace = {'__name__': '_terminal_live_migration'}
    legacy_class = compile_terminal(namespace, legacy=True)
    value = legacy_class(launch_cmd=['/bin/cat'])
    old_reader_code = legacy_class._read_loop.__code__
    old_owner = Owner()
    value._ds = old_owner
    wakes = []
    monkeypatch.setattr('meltygui.core.windowing.glfw_utils.request_render', lambda: wakes.append(True))
    runtime_namespace = {'__name__': '_terminal_runtime_live_migration'}
    runtime_source = Path(terminal_runtime.__file__).read_text()
    exec(compile(runtime_source, '<terminal-runtime-live-migration>', 'exec'), runtime_namespace)
    runtime_namespace['request_render'] = lambda: wakes.append(True)
    runtime_class = runtime_namespace['TerminalRuntime']
    fd = None
    try:
        value.start(40, 6)
        wait_for(lambda: value.screen is not None and old_owner.invalidations > 0)
        value.write(b'before\n')
        wait_for(lambda: 'before' in ''.join(value.screen.display))
        fd, pid, screen, lock = value.master_fd, value.pid, value.screen, value.lock
        reader_frames = []
        for frame in sys._current_frames().values():
            while frame is not None:
                if frame.f_code is old_reader_code:
                    reader_frames.append(frame)
                frame = frame.f_back
        assert reader_frames, 'The test must migrate an executing legacy read loop'
        assert '_listeners' not in vars(value)

        _hotswap_class(legacy_class, compile_terminal(namespace))
        assert type(value) is legacy_class
        first_owner, second_owner = Owner(), Owner()
        first, second = runtime_class(), runtime_class()
        first._owner_ds, second._owner_ds = first_owner, second_owner
        state = TerminalScreenState()
        state.scroll, state.sel_anchor = 3, (1, 2)
        first.prepare(value, 40, 6)
        second.prepare(value, 40, 6)
        assert value._ds is not old_owner
        assert value._ds._terminal() is value
        assert (value.master_fd, value.pid, value.screen, value.lock) == (fd, pid, screen, lock)
        assert len(value._listeners) == 2
        old_count = old_owner.invalidations
        value.write(b'after\n')
        wait_for(lambda: first_owner.invalidations > 0 and second_owner.invalidations > 0)
        assert old_owner.invalidations == old_count
        assert any(frame.f_code is old_reader_code for frame in reader_frames)

        # The model holds WeakMethods captured before this edit. Hotswap must
        # preserve their functions and make their next call run the new source.
        callbacks = [listener() for listener in value._listeners]
        edited = runtime_source.replace('        self._owner_ds.invalidate_up(force=True)',
                                        '        self._test_revision = 2\n'
                                        '        self._owner_ds.invalidate_up(force=True)')
        exec(compile(edited, '<terminal-runtime-live-migration-v2>', 'exec'), runtime_namespace)
        runtime_namespace['request_render'] = lambda: wakes.append(True)
        _hotswap_class(runtime_class, runtime_namespace['TerminalRuntime'])
        value.write(b'edited\n')
        wait_for(lambda: vars(first).get('_test_revision') == 2 and vars(second).get('_test_revision') == 2)
        assert [listener() for listener in value._listeners] == callbacks
        assert first._owner_ds is first_owner and second._owner_ds is second_owner
        assert (state.scroll, state.sel_anchor) == (3, (1, 2))
        assert (value.master_fd, value.pid, value.screen, value.lock) == (fd, pid, screen, lock)
        assert wakes
        assert '_ds' not in vars(terminal_model.Terminal(launch_cmd=['/bin/cat']))
    finally:
        if value.master_fd is not None:
            # End cat with EOF; this lets the original frame execute its original
            # finally/reap path, rather than simulating completion in a new frame.
            value.write(b'\x04')
            wait_for(lambda: not value._reader_alive)
        if fd is not None:
            os.close(fd)  # The legacy reader did not close its master descriptor.
