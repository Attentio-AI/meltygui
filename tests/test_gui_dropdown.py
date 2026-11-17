"""`draw_dropdown` (view/choice_view.py): a sequence of choices, a dict of {label: choice}, and a
dict in that dict as a submenu, opened beside its row by the pointer or the keyboard.

The app is one dropdown at (10, 10), 160 wide. The tests drive the real router through the GL test
host, frame by frame.
"""
import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.view.choice_view import GAP, draw_dropdown, entries, extent, options, shown, submenu
from meltygui.view.value_view import ROW

FLAT = ('Small', 'Medium', 'Large')
DEEP = {'Tiny': 0}
SIZES = {'Small': 1, 'More': {'Huge': 9, 'Deeper': DEEP}, 'Large': 3}
BOX = (20.0, 20.0)
LEFT, TOP, WIDTH = 10.0, 10.0, 160.0


def app(input_value=None, choices=SIZES):
    return draw_dropdown(input_value, name='size', choices=choices, left=LEFT, top=TOP, width=WIDTH)


@pytest.fixture
def host():
    host = Host(app, input_value=1, choices=SIZES)
    host.frame()
    yield host
    host.close()


def lists(host):
    """The open lists' nodes, outermost first."""
    body = options.__gui_definition__[0]
    found = [node for node, record in host.cache.records.items() if record[2] is body]
    return sorted(found, key=lambda node: host.cache.position(node)[0])


def row(host, node, at):
    """The surface position of row `at` of the list `node`."""
    x, y = host.cache.position(node)
    return x + 10.0, y + at * ROW + ROW * 0.5


def test_the_rows_of_a_sequence_and_of_a_dict():
    assert entries(FLAT) == [('Small', 'Small'), ('Medium', 'Medium'), ('Large', 'Large')]
    assert entries(SIZES)[0] == ('Small', 1)
    assert submenu(SIZES, SIZES['More']) and not submenu(SIZES, 1)
    assert not submenu((DEEP,), DEEP)           # a dict in a sequence is a choice
    assert not submenu({'Empty': {}}, {})       # and so is an empty one
    assert shown(SIZES, 9) == 'Huge' and shown(SIZES, 0) == 'Tiny' and shown(SIZES, 5) == '5'
    assert shown(FLAT, 'Large') == 'Large'


def test_a_sequence_picks_as_before(host):
    host.frame(input_value='Small', choices=FLAT)
    host.click(*BOX)
    host.frame()
    (found,) = lists(host)
    host.frame(row(host, found, 2))
    host.click(*row(host, found, 2))
    host.frame()
    assert host.arguments['input_value'] == 'Large' and lists(host) == []


def test_a_dict_picks_the_value_of_the_label(host):
    host.click(*BOX)
    host.frame()
    (found,) = lists(host)
    assert host.cache.position(found) == (LEFT, TOP + ROW + GAP)
    assert host.cache.states[found].index == 0              # the current choice is selected
    host.frame(row(host, found, 2))
    host.click(*row(host, found, 2))
    host.frame()
    assert host.arguments['input_value'] == 3 and lists(host) == []


def test_the_pointer_opens_a_submenu_beside_its_row_and_a_pick_there_closes_all(host):
    host.click(*BOX)
    host.frame()
    (found,) = lists(host)
    host.frame(row(host, found, 1))
    host.frame()
    first, second = lists(host)
    x, y = host.cache.position(first)
    assert host.cache.position(second) == (x + WIDTH, y + ROW)
    assert host.cache.graph.rect(second)[2:] == extent(SIZES['More'])
    host.frame(row(host, second, 0))
    host.click(*row(host, second, 0))
    host.frame()
    host.frame()
    assert host.arguments['input_value'] == 9 and lists(host) == []


def test_a_submenu_goes_when_the_selection_moves_on(host):
    host.click(*BOX)
    host.frame()
    (found,) = lists(host)
    host.frame(row(host, found, 1))
    host.frame()
    assert len(lists(host)) == 2
    host.frame(row(host, found, 2))
    host.frame()
    assert lists(host) == [found]
    host.frame(keys=[(glfw.KEY_UP, 0)])         # the list has the keyboard again
    host.frame()
    assert host.cache.states[found].index == 1


def test_submenus_nest(host):
    host.click(*BOX)
    host.frame()
    (found,) = lists(host)
    host.frame(row(host, found, 1))
    host.frame()
    second = lists(host)[1]
    host.frame(row(host, second, 1))
    host.frame()
    third = lists(host)[2]
    host.frame(row(host, third, 0))
    host.click(*row(host, third, 0))
    host.frame()
    host.frame()
    host.frame()
    assert host.arguments['input_value'] == 0 and lists(host) == []


def test_the_keyboard_opens_leaves_and_picks(host):
    host.click(*BOX)
    host.frame()
    host.frame(keys=[(glfw.KEY_DOWN, 0)])
    host.frame(keys=[(glfw.KEY_RIGHT, 0)])
    host.frame()
    assert len(lists(host)) == 2
    host.frame(keys=[(glfw.KEY_LEFT, 0)])       # back: the submenu closes, the list stays
    host.frame()
    assert len(lists(host)) == 1
    host.frame(keys=[(glfw.KEY_ENTER, 0)])      # Enter opens it too
    host.frame()
    assert len(lists(host)) == 2
    host.frame(keys=[(glfw.KEY_DOWN, 0)])
    host.frame(keys=[(glfw.KEY_ENTER, 0)])
    host.frame()
    host.frame()
    assert host.arguments['input_value'] == 9 and lists(host) == []


def test_escape_closes_the_innermost_list(host):
    host.click(*BOX)
    host.frame()
    (found,) = lists(host)
    host.frame(row(host, found, 1))
    host.frame()
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    host.frame()
    assert lists(host) == [found]
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    host.frame()
    assert lists(host) == [] and host.arguments['input_value'] == 1
