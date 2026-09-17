"""Terminal output observers and injected runtime keep independent views alive."""
import gc
import weakref

from meltygui.model.terminal_model import Terminal
from meltygui.state.terminal_state import TerminalScreenState
from meltygui.core.services.terminal_runtime import TerminalRuntime
from meltygui.core.melty import Melty


class Owner:
    def __init__(self, closable=False):
        self.closable = closable
        self.invalidations = 0

    def invalidate_up(self, force=False):
        self.invalidate()

    def invalidate(self):
        self.invalidations += 1


def terminal():
    return Terminal(launch_cmd=['/bin/bash', '-i'])


def attach(value, owner):
    runtime = TerminalRuntime()
    runtime._owner_ds = owner
    runtime.prepare(value, 80, 24)
    return runtime


def test_output_reaches_each_view_and_does_not_retain_closed_view(monkeypatch):
    value = terminal()
    monkeypatch.setattr(value, 'start', lambda *args: None)
    monkeypatch.setattr('meltygui.core.services.terminal_runtime.request_render', lambda: None)
    first, second = Owner(), Owner()
    a, b = attach(value, first), attach(value, second)
    value._notify_changed()
    assert (first.invalidations, second.invalidations) == (1, 1)
    ref = weakref.ref(a)
    del a
    gc.collect()
    assert ref() is None
    value._notify_changed()
    assert (first.invalidations, second.invalidations) == (1, 2)
    assert len(value._listeners) == 1


def test_rebinding_unsubscribes_previous_terminal(monkeypatch):
    first, second = terminal(), terminal()
    for value in (first, second):
        monkeypatch.setattr(value, 'start', lambda *args: None)
    monkeypatch.setattr('meltygui.core.services.terminal_runtime.request_render', lambda: None)
    owner = Owner()
    runtime = attach(first, owner)
    runtime.prepare(second, 80, 24)
    first._notify_changed()
    assert owner.invalidations == 0
    second._notify_changed()
    assert owner.invalidations == 1


def test_runtime_owns_first_focus_resize_and_input_routing(monkeypatch):
    from meltygui.core.services import terminal_core
    calls = []
    value = terminal()
    value.screen = object()
    monkeypatch.setattr(value, 'start', lambda *args: calls.append(('start', args)))
    monkeypatch.setattr(value, 'resize', lambda *args: calls.append(('resize', args)))
    monkeypatch.setattr(Melty, 'text_focused_ds', None)
    owner = Owner(closable=True)
    runtime = attach(value, owner)
    assert Melty.text_focused_ds is owner
    assert calls == [('start', (80, 24)), ('resize', (80, 24))]
    monkeypatch.setattr(terminal_core, '_forward_keys', lambda *args: calls.append('keys'))
    state = TerminalScreenState()
    runtime.forward_keys(value, state)
    assert calls[-1] == 'keys'
    Melty.text_focused_ds = None
    runtime.prepare(value, 90, 30)
    assert Melty.text_focused_ds is None
    runtime.forward_keys(value, state)
    assert calls[-1] == ('resize', (90, 30))


def test_local_state_and_wrapped_links():
    from meltygui.core.conversion.dict_conversion import DictConversion
    from meltygui.view.terminal_view import _find_links
    first, second = TerminalScreenState(), TerminalScreenState()
    first.scroll = 20
    first.sel_anchor = (1, 2)
    assert isinstance(first, DictConversion)
    assert (second.scroll, second.sel_anchor) == (0, None)
    text = '/tmp/example.py:42'
    grid = [[(c, None, None, False, False) for c in row.ljust(10)]
            for row in (text[:10], text[10:])]
    assert _find_links(grid, 10) == [('/tmp/example.py', 42, [(0, 0, 10), (1, 0, 8)])]


def test_start_failure_notifies_observers(monkeypatch):
    from meltygui.core.services import terminal_core
    value = terminal()
    owner = Owner()
    value.subscribe(owner.invalidate)
    monkeypatch.setattr(terminal_core, '_spawn_in_pty',
                        lambda *args: (_ for _ in ()).throw(OSError('test spawn failure')))
    value._start_and_read(80, 24)
    assert 'test spawn failure' in value.error
    assert owner.invalidations == 1


def test_snapshot_keeps_blank_cells_and_isolated_rows():
    import pyte
    value = terminal()
    from meltygui.core.services.terminal_core import _make_history_screen
    value.screen = _make_history_screen(pyte, 10, 3)
    pyte.ByteStream(value.screen).feed(b'hello')
    rows, cols, cursor, modes, history, buffer = value.snapshot()
    assert (rows, cols) == (3, 10)
    assert ''.join(cell.data for cell in buffer[0]) == 'hello     '
    assert ''.join(cell.data for cell in buffer[2]) == '          '
    pyte.ByteStream(value.screen).feed(b'!')
    assert buffer[0][5].data == ' '
