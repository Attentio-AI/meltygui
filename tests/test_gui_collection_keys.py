"""`draw_collection` rows keep to themselves: a dict may use any keys at any depth, the same dict may
be drawn by several collections, and an edit runs each view at most once a frame (hidden window)."""
import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.core.rendering.gui_any import draw_any
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view.common.dict_view import ROW

ENTER = (glfw.KEY_ENTER, 0)
WIDE, TALL = 400., 200.          # one collection
X = 300.                         # inside a row's value field


@pytest.fixture
def host():
    hosts = []

    def make(view, **arguments):
        made = Host(view, name='host', left=0., top=0., width=WIDE, **arguments)
        made.cache.executor.end_frame()
        hosts.append(made)
        return made
    yield make
    for made in hosts:
        made.close()


def frames(host, *steps):
    """Run `steps` (the arguments of `Host.frame`) and return the views that ran twice in one of them."""
    twice = []
    for step in steps:
        host.frame(**step)
        twice += [host.cache.records[node][2].__name__ for node in host.cache.executor.end_frame() or ()]
    return twice


def drag(row, top=0.):
    """The frames of a sideways drag by two notches over the value of row `row`."""
    y = top + (row + .5) * ROW
    return [{'position': (X + moved, y), 'down': down} for moved, down in ((0, True), (4, True), (8, True), (8, False))]


def typed(row, text, top=0.):
    """The frames of a click on the value of row `row`, `text` typed and Enter."""
    y = top + (row + .5) * ROW
    return [{'position': (X, y), 'down': True}, {'position': (X, y), 'down': False}, {'text': text}, {'keys': [ENTER]}, {}]


def shown(host, collection):
    """The values the views of the collection named `collection` were last passed, top down."""
    cache = host.cache
    owner = next(node for node, (_, arguments) in cache.invocations.items() if arguments.get('name') == collection)
    rows = [(arguments['top'], value) for node, (value, arguments) in cache.invocations.items()
            if cache.graph.parent(node) == owner]
    return [value for _, value in sorted(rows, key=lambda row: row[0])]


def test_one_dict_in_two_collections_runs_an_edited_row_once(host):
    data = {'item 1': 1, 'item 2': 5, 'nested': {'nested_2': {'item 1': 1, 'item 2': 5}, 'a_string': 'Testing'}}

    @gui
    def pair(width=0.):
        draw_any(data, name='first', expand=3, left=0., top=0., width=width, height=TALL)
        draw_any(data, name='second', expand=3, left=0., top=TALL, width=width, height=TALL)

    made = host(pair, height=2 * TALL)                         # rows: item 1, item 2, nested, nested_2, item 1, item 2, a_string
    assert frames(made, *drag(5)) == []
    assert data['nested']['nested_2'] == {'item 1': 1, 'item 2': 7} and data['item 2'] == 5
    assert frames(made, *typed(5, '451')) == []
    assert data['nested']['nested_2']['item 2'] == 451
    assert shown(made, 'second')[:6] == [1, 5, True, True, 1, 451]   # the other collection follows
    assert frames(made, *drag(1, TALL), *typed(6, ' again', TALL)) == []    # and from the second collection
    assert data == {'item 1': 1, 'item 2': 7, 'nested': {'nested_2': {'item 1': 1, 'item 2': 451}, 'a_string': 'Testing again'}}


def test_an_edit_runs_no_other_view_of_an_equal_value(host):
    ran = []

    @gui
    def number(input_value: int, event=None):
        ran.append(input_value)
        pressed = event.left_mouse_down()
        return pressed, input_value + pressed

    values = [5, 5]

    @gui
    def pair(input_value: list, width=0.):
        for at, value in enumerate(input_value):
            edited, input_value[at] = number(value, key=at, left=0., top=at * ROW, width=width, height=ROW)
        return False, input_value

    made = host(pair, input_value=values, height=TALL)
    del ran[:]
    assert frames(made, {'position': (X, ROW * .5), 'down': True}, {'down': False}, {}) == []
    assert values == [6, 5] and ran.count(5) == 1              # the press ran its own view; the other 5 never ran


def test_keys_of_any_kind_at_any_depth_are_rows_of_their_own(host):
    class Named:
        """Two keys with one repr."""
        __repr__ = lambda self: 'Named'

    one, two = Named(), Named()
    inner = {one: 1, two: 2, 1.5: 3, '1.5': 4, 1: 5, '1': 6}
    data = {'same': {'same': inner}, "same\x1f'same'": 7, 1: 8, '1': 9}
    made = host(draw_any, input_value=data, height=2 * TALL, expand=3)    # rows: same, same, the six of inner, then three
    twice = []
    for row in range(2, 11):
        twice += frames(made, *drag(row))
    assert twice == []
    assert inner == {one: 3, two: 4, 1.5: 5., '1.5': 6, 1: 7, '1': 8}
    assert data == {'same': {'same': inner}, "same\x1f'same'": 9, 1: 10, '1': 11}
