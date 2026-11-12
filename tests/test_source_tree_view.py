"""`draw_editor(source)` beside `draw_any(source.tree)`, driven through a hidden window (reserved desktop):
an edit in either shows in the other."""
import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.core.rendering import gui_any
from meltygui.core.rendering.gui_any import draw_any, is_default_for
from meltygui.core.rendering.gui_prototype import gui
from meltygui.editor import syntax
from meltygui.model.source_model import Source
from meltygui.state.editor_state import EditorState
from meltygui.view.dict_view import ROW, Tree
from meltygui.view.editor_view import draw_editor

TEXT = '''GAIN = 0.5
ON = True
NAME = 'melty'
COUNT = 3

class Knobs:
    level = 7
    label = 'knob'
'''
SPLIT = 400.           # the editor's width; the tree is to its right
ENTER = (glfw.KEY_ENTER, 0)


def both(source=None, state=None, rows=None, editor=True):
    """A caller that draws the text and its tree side by side, as an app does."""
    if editor:
        draw_editor(source, name='source', state=state, left=0., top=0., width=SPLIT, height=560., fold_defaults=False)
    return draw_any(source.tree, name='tree', tree=rows, expand=2, left=SPLIT, top=0., width=400., height=560.)


@pytest.fixture
def show():
    syntax.tools()
    hosts = []

    def make(text=TEXT, editor=True):
        source, state, rows = Source(text), EditorState(), Tree()
        host = Host(both, source=source, state=state, rows=rows, editor=editor)
        host.source, host.state, host.rows = source, state, rows
        hosts.append(host)
        return host
    yield make
    for host in hosts:
        host.close()
        host.source.close()


@pytest.fixture
def drawn():
    """The values a view registered for the names 'GAIN' and 'level' was run with, in order."""
    kept = [dict(table) for table in (gui_any.NAMES, gui_any.SHAPES, gui_any.TYPES)]
    found = []

    @is_default_for('GAIN', 'level', 'EXTRA')
    @gui(listen=())
    def record(input_value, editable=True):
        found.append(input_value)
    yield found
    for table, was in zip((gui_any.NAMES, gui_any.SHAPES, gui_any.TYPES), kept):
        table.clear()
        table.update(was)
    gui_any.ROUTES.clear()


def field(row):
    """A point in the value field of row `row` of the tree."""
    return SPLIT + 200., row * ROW + ROW * .5


def landed(host, frames=3):
    """Everything asked for is read, and the frames after it ran."""
    host.source.settle()
    for _ in range(frames):
        host.frame()


def keys(host):
    rows = host.rows.rows
    return [node.keys[index] for _, node, index in rows.window(0, 99)]


def test_the_tree_shows_the_code(show):
    host = show()
    assert keys(host) == ['GAIN', 'ON', 'NAME', 'COUNT', 'Knobs', 'level', 'label']


def test_an_edit_in_the_tree_is_in_the_text_and_the_editor_follows(show):
    host = show()
    session = next(iter(host.source.sessions))
    host.click(*field(1))                                       # the chip of ON
    assert host.source.text == TEXT.replace('ON = True', 'ON = False')
    host.click(*field(3))                                       # COUNT: type a number
    host.frame(text='42')
    host.frame(keys=[ENTER])
    host.click(*field(6))                                       # Knobs.label: every keystroke is written
    host.frame(text='s')
    assert host.source.text == TEXT.replace('ON = True', 'ON = False').replace('COUNT = 3', 'COUNT = 42').replace("'knob'", "'knobs'")
    landed(host)
    assert session.seen == host.source.head and session.doc.line(3) == 'COUNT = 42'      # the editor's session is on that text
    assert host.source.tree['COUNT'] == 42 and not host.source.grown.pending


def test_a_drag_in_the_tree_does_not_snap_back(show, drawn):
    host = show()
    del drawn[:]
    x, y = field(5)                                             # Knobs.level is drawn by the recording view; drag COUNT instead
    x, y = field(3)
    host.frame((x, y), True)
    seen = []
    for step in range(1, 30):
        host.frame((x + 4. * step, y), True)                    # a notch a frame: each is a splice, read on the source's thread
        seen.append(host.source.tree['COUNT'])
    host.frame((x + 116., y), False)
    assert seen == sorted(seen) and seen[-1] == 32, seen
    assert host.source.text.splitlines()[3] == 'COUNT = 32'
    landed(host)
    assert host.source.tree['COUNT'] == 32


def test_typing_in_the_editor_shows_in_the_tree(show, drawn):
    host = show()
    assert drawn == [0.5, 7]
    host.click(60., 8.)                                         # the editor takes the keyboard; the caret goes to line 0
    host.frame(keys=[(glfw.KEY_END, 0)])
    host.frame(text='2')
    host.frame(text='5')
    assert host.source.text.startswith('GAIN = 0.525\n')
    landed(host)
    assert drawn == [0.5, 7, 0.525] and host.source.tree['GAIN'] == 0.525


def test_a_key_typed_in_the_editor_is_a_row_of_the_tree(show, drawn):
    host = show()
    tree = host.source.tree
    host.click(60., 8.)
    host.frame(keys=[(glfw.KEY_HOME, 0)])
    for char in 'EXTRA = 1':
        host.frame(text=char)
    host.frame(keys=[ENTER])
    landed(host)
    assert host.source.tree is not tree                         # other keys: another object for the view
    assert keys(host) == ['EXTRA', 'GAIN', 'ON', 'NAME', 'COUNT', 'Knobs', 'level', 'label']
    assert drawn[-2:] == [1, 0.5] or 1 in drawn
    host.click(*field(2))                                       # the rows moved down by one: ON is row 2 now
    assert host.source.text.splitlines()[2] == 'ON = False'


def test_a_tree_alone_follows_a_splice_from_outside(show, drawn):
    host = show(editor=False)
    assert drawn == [0.5, 7] and not host.source.sessions
    host.source.splice(TEXT.index('0.5'), TEXT.index('0.5') + 3, '0.875')
    host.frame()
    assert drawn == [0.5, 7]                                    # not read yet: it rests first
    landed(host)
    assert drawn == [0.5, 7, 0.875]
    host.click(*field(1))
    assert host.source.text.splitlines()[:2] == ['GAIN = 0.875', 'ON = False']


def test_idle_frames_run_no_view(show):
    host = show()
    landed(host, 6)
    runs = host.cache.executor.runs
    for _ in range(5):
        host.frame()
    assert host.cache.executor.runs == runs                    # nothing polls the source
    host.frame(text='x')                                        # (no view has the keyboard: nothing to run either)
    host.source.splice(0, 0, '# a comment\n')                  # an edit that changes no key and no value
    landed(host, 6)
    runs = host.cache.executor.runs
    for _ in range(5):
        host.frame()
    assert host.cache.executor.runs == runs


def test_one_reading_runs_the_tree_once(show, drawn):
    host = show()
    landed(host, 6)
    runs = host.cache.executor.runs
    host.source.splice(TEXT.index('0.5'), TEXT.index('0.5') + 3, '0.25')
    landed(host, 6)
    # The editor's pane and block for the splice, and for the reading: the collection (twice: it lands the
    # reading it was woken for, which wakes the views that read the tree) and the one value that changed.
    assert drawn[-1] == 0.25 and host.cache.executor.runs - runs <= 8, host.cache.executor.runs - runs
