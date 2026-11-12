"""The value views and `draw_collection` driven through a hidden window (reserved desktop)."""
import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.core.rendering import gui_any
from meltygui.core.rendering.gui_any import draw_any, is_default_for
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view import dict_view
from meltygui.view.dict_view import ROW, Tree

ENTER, ESCAPE = (glfw.KEY_ENTER, 0), (glfw.KEY_ESCAPE, 0)


@pytest.fixture
def show():
    """`show(value, **arguments)`: a host drawing `draw_any(value)` in its corner; closed after the test."""
    hosts = []

    def make(value, width=220., height=22., **arguments):
        hosts.append(Host(draw_any, input_value=value, name='value', left=0., top=0., width=width, height=height,
                          **arguments))
        return hosts[-1]
    yield make
    for host in hosts:
        host.close()


@pytest.fixture
def registry():
    kept = [dict(table) for table in (gui_any.NAMES, gui_any.SHAPES, gui_any.TYPES)]
    yield
    for table, was in zip((gui_any.NAMES, gui_any.SHAPES, gui_any.TYPES), kept):
        table.clear()
        table.update(was)
    gui_any.ROUTES.clear()


def value(host):
    return host.arguments['input_value']


def drag(host, start, stop, y=10.):
    host.frame((start, y), True)
    host.frame(((start + stop) / 2, y), True)
    host.frame((stop, y), True)
    host.frame((stop, y), False)


def test_bool_press_flips_and_read_only_does_not(show):
    host = show(True)
    host.click(12, 10)
    assert value(host) is False
    host.click(12, 10)
    assert value(host) is True
    host.click(200, 10)                                        # beside the chip
    assert value(host) is True
    still = show(True, editable=False)
    still.click(12, 10)
    assert value(still) is True


def test_float_drag_steps_by_hundredths_of_its_start(show):
    host = show(.25)
    drag(host, 50, 90)                                         # ten notches of 0.01 (the value is below 1)
    assert value(host) == pytest.approx(.35)
    large = show(200.)
    drag(large, 50, 90)                                        # ten notches of 2
    assert value(large) == pytest.approx(220.)


def test_int_drag_and_typed_number(show):
    host = show(3)
    drag(host, 50, 70)
    assert value(host) == 8 and type(value(host)) is int
    host.click(60, 10)
    host.frame(text='42')
    assert value(host) == 8                                    # not taken before Enter
    host.frame(keys=[ENTER])
    assert value(host) == 42


def test_typed_number_dropped_by_escape_and_by_bad_text(show):
    host = show(1.5)
    host.click(60, 10)
    host.frame(text='9')
    host.frame(keys=[ESCAPE])
    assert value(host) == 1.5
    host.click(60, 10)
    host.frame(text='nine')
    host.frame(keys=[ENTER])
    assert value(host) == 1.5
    host.click(60, 10)
    host.frame(text='2e3')
    host.frame(keys=[ENTER])
    assert value(host) == 2000.


def test_number_keeps_its_own_type(show):
    class Level(int):
        pass

    host = show(Level(3))
    drag(host, 50, 70)
    assert value(host) == 8 and type(value(host)) is Level


def test_text_is_returned_as_it_is_typed_and_escape_restores(show):
    host = show('melty')
    host.click(200, 10)                                        # past the text: the caret goes to its end
    host.frame(text=' gui')
    assert value(host) == 'melty gui'
    host.frame(keys=[(glfw.KEY_BACKSPACE, 0)])
    assert value(host) == 'melty gu'
    host.frame(keys=[ESCAPE])
    assert value(host) == 'melty'


def test_text_of_several_lines_is_read_only(show):
    host = show('one\ntwo')
    host.click(20, 10)
    host.frame(text='x')
    assert value(host) == 'one\ntwo'


def test_text_follows_a_caller_that_keeps_its_value(show):
    host = show('fixed')
    host.adopt = False
    host.click(200, 10)
    host.frame(text='!')
    assert host.result == (True, 'fixed!')
    for _ in range(8):
        host.frame()
    assert host.result == (False, 'fixed')


def collection(show, data, **arguments):
    tree = Tree()
    host = show(data, width=400., height=260., tree=tree, **arguments)
    host.tree = tree
    return host


def field(row):
    """A point in the value field of row `row` of a 400 wide collection."""
    return 200., row * ROW + ROW * .5


def test_collection_writes_an_edit_into_the_dict_it_came_from(show):
    inner = {'flag': False, 'count': 3}
    data = {'name': 'melty', 'inner': inner}
    host = collection(show, data, expand=2)
    edited = host.frame(field(2), True)                        # rows: name, inner, flag, count
    host.frame(field(2), False)
    assert edited[0] and edited[1] is data                     # reported in the frame of the press
    assert inner == {'flag': True, 'count': 3} and data['inner'] is inner
    x, y = field(3)
    drag(host, x, x + 20, y)
    assert inner['count'] == 8


def test_collection_press_opens_and_closes_a_branch(show):
    data = {'a': 1, 'inner': {'b': 2, 'c': 3}, 'z': 4}
    host = collection(show, data)
    rows = host.tree.rows
    assert rows.count == 3
    views = len(host.cache.records)
    host.frame((30., ROW * 1.5), True)                         # a press on the branch's key
    assert len(host.cache.records) == views + 2                # its two values are shown in the frame of the press
    host.frame((30., ROW * 1.5), False)
    assert host.tree.rows.count == 5 and host.tree.flipped == [('inner',)]
    host.click(*field(3))                                      # 'c' is now row 3
    host.frame(text='7')
    host.frame(keys=[ENTER])
    assert data['inner']['c'] == 7
    host.click(30, ROW * 1.5)
    assert host.tree.rows.count == 3 and host.tree.flipped == []


def test_collection_declares_the_rows_in_view_only(show):
    data = {f'item {at}': at for at in range(100_000)}
    host = collection(show, data)
    in_view = int(260 // ROW) + 2
    assert len(host.cache.records) <= in_view + 3               # the rows, the collection, its end mark
    host.frame((200., 100.), wheel=-50.)                        # a long way down
    host.frame()
    assert host.tree.rows.count == 100_000
    assert len(host.cache.records) <= in_view + 3
    first = int(host.cache.states[next(iter(host.cache.root_ids))].scroll_offset[1] // ROW)
    assert first > 20
    host.click(200., ROW * .5 + (ROW - host.cache.states[next(iter(host.cache.root_ids))].scroll_offset[1] % ROW))
    host.frame(text='-1')
    host.frame(keys=[ENTER])
    assert sorted(key for key, item in data.items() if item == -1) == [f'item {first + 1}']


def test_collection_routes_by_key_and_by_shape(show, registry):
    drawn = []

    @is_default_for('tint')
    @gui(listen=())
    def by_name(input_value):
        drawn.append(('name', input_value))

    from meltygui.core.rendering.shaped import Shaped

    @is_default_for(Shaped(tuple, (2,), int))
    @gui(listen=())
    def by_shape(input_value):
        drawn.append(('shape', input_value))

    collection(show, {'tint': 'red', 'size': (3, 4), 'other': (1., 2.)})
    assert drawn == [('name', 'red'), ('shape', (3, 4))]


def test_collection_follows_a_dict_changed_outside(show):
    data = {'a': 1, 'b': 2}
    host = collection(show, data)
    data['c'] = 3
    del data['a']
    host.cache.invalidate(data)
    host.frame()
    rows = host.tree.rows
    assert [node.keys[index] for _, node, index in rows.window(0, 9)] == ['b', 'c']


def test_collection_read_only(show):
    data = {'flag': True, 'count': 3}
    host = collection(show, data, editable=False)
    host.click(*field(0))
    x, y = field(1)
    drag(host, x, x + 40, y)
    assert data == {'flag': True, 'count': 3}


def test_draw_collection_is_the_view_of_a_dict():
    assert gui_any.default_for({}) is dict_view.draw_collection


def typing(host):
    """Whether a view of the host holds a typing session."""
    return any(getattr(state, 'field', None) is not None for state in host.cache.states.values())


def test_typing_ends_when_its_row_goes_out_of_sight(show):
    data = {f'item {at}': at for at in range(1000)}
    host = collection(show, data)
    host.click(*field(1))
    host.frame(text='77')
    assert typing(host)
    host.frame((200., 100.), wheel=-50.)                        # the row is far above the view now
    host.frame()
    host.frame((200., 100.), wheel=50.)                         # and back: the same row is a new view
    host.frame()
    assert not typing(host) and data['item 1'] == 1             # what was typed and not taken is gone


def taken(host, x, y):
    """Whether a view keeps a left drag that starts at (x, y) from the window the host would move."""
    from test_editor_pointer import kept
    host.frame((x, y))
    held = kept(host, (x, y))
    assert 'right_mouse_dragged' not in held, 'a right drag resizes the window over every view'
    assert host.cache.imgui_input.left == ('left_mouse_dragged' in held)
    return host.cache.imgui_input.left


def test_a_left_drag_is_the_windows_except_over_a_control(show):
    data = {'flag': True, 'count': 3, 'name': 'melty', 'none': None, 'inner': {'a': 1}}
    host = collection(show, data)
    assert not taken(host, 40., ROW * 1.5), "a leaf's key"
    assert not taken(host, 200., 240.), 'the empty space below the rows'
    assert not taken(host, 380., ROW * .5), 'beside the True chip, in its row'
    assert not taken(host, 200., ROW * 3.5), 'None, which is read only'
    assert not taken(host, 300., ROW * 4.5), "beside a branch's key"
    assert taken(host, 180., ROW * .5), 'the chip: a press flips it'
    assert taken(host, 200., ROW * 1.5), 'a number: the drag changes it'
    assert taken(host, 200., ROW * 2.5), 'a text: the drag selects in it'
    assert taken(host, 30., ROW * 4.5), "a branch's key: a press opens it"


def test_read_only_values_leave_the_left_drag_to_the_window(show):
    data = {'flag': True, 'count': 3, 'name': 'melty', 'inner': {'a': 1}}
    host = collection(show, data, editable=False)
    for row in range(3):
        assert not taken(host, 180., ROW * (row + .5))
    assert taken(host, 30., ROW * 3.5), "a branch's key still opens"
