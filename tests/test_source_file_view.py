"""`draw_any(Source(module).tree)` through a hidden window (reserved desktop): the view shows the
file, an edit in it is on disk, and a change another program makes shows without a caller's help."""
import importlib.util
import sys
import time

import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.core.rendering.gui_any import draw_any
from meltygui.model.source_file_model import flush, framing
from meltygui.model.source_model import Source
from meltygui.view.common.dict_view import ROW, Tree

TEXT = '''GAIN = 0.5
ON = True
NAME = 'melty'
COUNT = 3

class Knobs:
    level = 7
    label = 'knob'
'''


ENTER = (glfw.KEY_ENTER, 0)


def caller(scope=None, rows=None, seen=None):
    """A caller that draws the code of a module, as an app does: it asks for the tree in a frame."""
    seen.append(framing())
    return draw_any(scope.tree, name='tree', tree=rows, expand=2, left=0., top=0., width=400., height=560.)


@pytest.fixture
def show(tmp_path, request):
    hosts = []

    def make(text=TEXT, of=lambda module: module):
        path = tmp_path / 'knobs.py'
        path.write_text(text)
        name = f'view_knobs_{request.node.name}'
        spec = importlib.util.spec_from_file_location(name, path)
        module = sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        host = Host(caller, scope=Source(of(module)), rows=Tree(), seen=[])
        host.path, host.scope, host.rows, host.name = path, host.arguments['scope'], host.arguments['rows'], name
        hosts.append(host)
        return host
    yield make
    for host in hosts:
        host.close()
        del sys.modules[host.name]


def field(row):
    """A point in the value field of row `row` of the tree."""
    return 200., row * ROW + ROW * .5


def keys(host):
    return [node.keys[index] for _, node, index in host.rows.rows.window(0, 99)]


def shows(host, check, seconds=5.):
    """Run frames until `check()` holds: what arrives from another thread is taken by the frame loop."""
    end = time.monotonic() + seconds
    while not check():
        if time.monotonic() > end:
            return False
        time.sleep(.002)
        host.frame()
    return True


def test_the_view_shows_the_file_without_waiting_for_it(show):
    host = show()                                               # the first frame ran right after the source was made
    assert host.arguments['seen'] == [True]
    assert shows(host, lambda: keys(host) == ['GAIN', 'ON', 'NAME', 'COUNT', 'Knobs', 'level', 'label'])
    assert host.scope.file.bell is not None                     # the io thread wakes the frame loop from the first frame on


def test_an_edit_in_the_view_is_in_the_file(show):
    host = show()
    assert shows(host, lambda: len(keys(host)) == 7)
    host.click(*field(1))                                       # the chip of ON
    host.click(*field(3))                                       # COUNT: type a number
    host.frame(text='42')
    host.frame(keys=[ENTER])
    flush()
    assert host.path.read_text() == host.scope.text == TEXT.replace('ON = True', 'ON = False').replace('COUNT = 3', 'COUNT = 42')
    x, y = field(0)                                             # GAIN: a drag is a write a frame
    host.frame((x, y), True)
    for step in range(1, 30):
        host.frame((x + 4. * step, y), True)
    host.frame((x + 116., y), False)
    dragged = host.scope.tree['GAIN']
    flush()
    assert dragged != 0.5 and f'GAIN = {dragged!r}\n' in host.path.read_text() and host.path.read_text() == host.scope.text


def test_a_change_by_another_program_shows_in_the_view(show):
    host = show()
    assert shows(host, lambda: len(keys(host)) == 7)
    host.path.write_text(TEXT.replace('COUNT = 3', 'COUNT = 30\nADDED = [1, 2]'))
    assert shows(host, lambda: keys(host)[:6] == ['GAIN', 'ON', 'NAME', 'COUNT', 'ADDED', 0]), keys(host)
    assert host.scope.tree['COUNT'] == 30 and host.scope.tree['ADDED'].plain() == [1, 2]


def test_a_class_and_its_module_are_one_text_in_two_views(show):
    host = show(of=lambda module: module.Knobs)
    assert shows(host, lambda: keys(host) == ['level', 'label'])
    module = Source(sys.modules[host.name])
    module.tree['Knobs']['level'] = 8                           # written through the module's scope
    assert shows(host, lambda: host.scope.tree['level'] == 8)
    host.click(*field(0))
    host.frame(text='81')                                       # typed in the class's view
    host.frame(keys=[ENTER])
    assert module.tree['Knobs']['level'] == 81
    flush()
    assert host.path.read_text() == TEXT.replace('level = 7', 'level = 81')
