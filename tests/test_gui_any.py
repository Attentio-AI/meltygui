"""`draw_any` routing and the row model of `draw_collection`: no window needed."""
import collections
import dataclasses
import random
import types

import pytest

from meltygui.core.rendering import gui_any, keyed
from meltygui.core.rendering.gui_any import default_for, is_default_for
from meltygui.core.rendering.shaped import Shaped
from meltygui.view import dict_view, value_view
from meltygui.view.dict_view import Rows


@pytest.fixture
def registry():
    """Registrations made in a test are gone after it."""
    kept = [dict(table) for table in (gui_any.NAMES, gui_any.SHAPES, gui_any.TYPES)]
    yield
    for table, was in zip((gui_any.NAMES, gui_any.SHAPES, gui_any.TYPES), kept):
        table.clear()
        table.update(was)
    gui_any.ROUTES.clear()


def view(label):
    def drawn(input_value=None):
        return False, input_value
    drawn.__name__ = drawn.__qualname__ = label
    return drawn


def test_built_in_views_by_type():
    assert default_for(True) is value_view.draw_bool          # (bool before int: the nearest class wins)
    assert default_for(3) is value_view.draw_int
    assert default_for(1.5) is value_view.draw_float
    assert default_for('a') is value_view.draw_str
    assert default_for(None) is value_view.draw_none
    assert default_for({}) is dict_view.draw_collection
    assert default_for([1, 2]) is dict_view.draw_collection   # (read by keys: core/rendering/keyed.py)
    assert default_for(object()) is value_view.draw_value


def test_subclass_goes_to_its_nearest_registered_base(registry):
    class Level(int):
        pass

    class Deep(Level):
        pass

    assert default_for(Level(3)) is value_view.draw_int
    level = is_default_for(Level)(view('level'))
    assert default_for(Deep(3)) is level and default_for(3) is value_view.draw_int


def test_name_beats_shape_beats_type(registry):
    by_type = is_default_for(tuple)(view('by_type'))
    by_shape = is_default_for(Shaped(tuple, (3,), float))(view('by_shape'))
    by_name = is_default_for('tint')(view('by_name'))
    assert default_for((1, 2)) is by_type
    assert default_for((.1, .2, .3)) is by_shape
    assert default_for((1, 2, 3)) is by_type                  # three ints are no colour
    assert default_for((.1, .2, .3), 'tint') is by_name
    assert default_for(5, 'tint') is by_name
    assert default_for((.1, .2, .3), 'other') is by_shape


def test_more_constrained_shape_wins(registry):
    loose = is_default_for(Shaped(tuple, (None,)))(view('loose'))
    exact = is_default_for(Shaped(tuple, (3,), float))(view('exact'))
    assert default_for((.1, .2, .3)) is exact
    assert default_for((.1, .2)) is loose


def test_type_by_name_without_importing_it(registry):
    class Tensor:
        pass

    class Parameter(Tensor):
        pass

    named = is_default_for('Tensor')(view('named'))
    assert default_for(Tensor()) is named and default_for(Parameter()) is named


def test_a_later_registration_replaces_and_routes_are_cleared(registry):
    assert default_for(2.5) is value_view.draw_float
    mine = is_default_for(float)(view('mine'))
    assert default_for(2.5) is mine


def test_registration_makes_a_gui_view_and_rejects_other_keys(registry):
    mine = is_default_for(complex)(view('mine'))
    assert mine.__gui__ and default_for(1j) is mine
    with pytest.raises(TypeError):
        is_default_for(3)


def naive(value, depth, path, rows, expand, flipped):
    for key, item in value.items():
        rows.append((depth, key))
        if isinstance(item, dict) and ((depth + 1 < expand) != (path + (key,) in flipped)):
            naive(item, depth + 1, path + (key,), rows, expand, flipped)


def tree(depth):
    return {f'k{at}': (tree(depth - 1) if depth and random.random() < .35 else at) for at in range(random.randint(0, 6))}


def test_rows_match_a_naive_flattening_through_random_flips():
    random.seed(3)
    for _ in range(150):
        data, expand = tree(4), random.randint(1, 5)
        rows = Rows(data, expand, ())
        for _ in range(6):
            want = []
            naive(data, 0, (), want, expand, rows.flipped)
            assert rows.count == len(want)
            for first in range(0, len(want) + 1, max(len(want) // 5, 1)):
                got = [(node.depth, node.keys[index]) for _, node, index in rows.window(first, 7)]
                assert got == want[first:first + 7]
            if want:
                rows.flip(random.randrange(len(want)))


def test_rows_restore_the_flipped_paths():
    data = {'a': {'x': 1, 'deep': {'y': 2}}, 'b': 3}
    rows = Rows(data, 1, ())
    assert rows.count == 2
    assert rows.flip(0) == {('a',)} and rows.count == 4
    assert rows.flip(2) == {('a',), ('a', 'deep')} and rows.count == 5
    again = Rows(data, 1, rows.flipped)
    assert [(node.depth, node.keys[index]) for _, node, index in again.window(0, 9)] == \
        [(0, 'a'), (1, 'x'), (1, 'deep'), (2, 'y'), (0, 'b')]
    assert rows.flip(4) is None                                # a leaf does not flip


def test_rows_follow_a_dict_changed_outside():
    data = {'a': {'x': 1}, 'b': 2}
    rows = Rows(data, 2, ())
    assert rows.count == 3
    data['c'] = 3                                              # a new key
    data['a']['y'] = 4                                         # a new key in an open branch
    rows.top = rows.check(rows.top)
    assert rows.count == 5
    data['a'] = {'only': 1}                                    # another dict under an open branch
    rows.top = rows.check(rows.top)
    assert [node.keys[index] for _, node, index in rows.window(0, 9)] == ['a', 'only', 'b', 'c']
    data['a'] = 7                                              # no dict any more
    rows.top = rows.check(rows.top)
    assert rows.count == 3


def test_a_dict_with_a_view_of_its_own_is_a_leaf(registry):
    is_default_for('transform')(view('transform'))
    rows = Rows({'transform': {'x': 1}, 'plain': {'y': 2}}, 3, ())
    assert [node.keys[index] for _, node, index in rows.window(0, 9)] == ['transform', 'plain', 'y']


def test_public_names_are_the_views_and_cost_nothing_to_import():
    import meltygui
    from test_startup_imports import imported_after, RENDER_LIBRARIES
    assert meltygui.rust.draw_any is gui_any.draw_any and meltygui.rust.is_default_for is is_default_for
    assert meltygui.rust.Shaped is Shaped and meltygui.rust.draw_collection is dict_view.draw_collection
    assert meltygui.rust.draw_float is value_view.draw_float
    with pytest.raises(AttributeError):
        meltygui.rust.draw_nothing
    base = imported_after('import meltygui')
    loaded = imported_after('import meltygui.rust', 'assert sorted(dir(meltygui.rust))[0] == "Keyed"')
    assert loaded - base == {'meltygui.rust'} and not loaded & set(RENDER_LIBRARIES)


@pytest.mark.parametrize('first', ['from meltygui.rust import draw_collection',
                                   'from meltygui.view.value_view import draw_float',
                                   'from meltygui.rust import draw_any',
                                   'import meltygui; meltygui.rust.draw_bool'])
def test_any_name_can_be_the_first_one_imported(first):
    from test_startup_imports import imported_after
    loaded = imported_after(first, 'from meltygui.rust import draw_any, draw_collection',
                            'from meltygui.core.rendering.gui_any import default_for',
                            'assert default_for({}) is draw_collection and default_for(1.5).__name__ == "draw_float"')
    assert 'meltygui.view.dict_view' in loaded


@pytest.mark.parametrize('order', ['app first', 'built in first'])
def test_an_apps_view_wins_whichever_registers_first(order):
    from test_startup_imports import imported_after
    imported_after(
        'from meltygui.core.rendering.gui_any import default_for, is_default_for',
        'import meltygui.view.value_view' if order == 'built in first' else '',
        'mine = is_default_for(float)(lambda input_value=None: None)',
        'import meltygui.view.value_view',
        'assert default_for(1.5) is mine and default_for(3).__name__ == "draw_int"',
        'assert default_for({}).__name__ == "draw_collection"')


def test_importing_the_router_loads_no_view():
    from test_startup_imports import imported_after
    base = imported_after('from meltygui import gui')
    loaded = imported_after('from meltygui import gui', 'from meltygui.core.rendering.gui_any import draw_any')
    assert loaded - base == {'meltygui.core.rendering.gui_any', 'meltygui.core.rendering.shaped',
                             'meltygui.core.rendering.keyed'}


# --- values that are read by keys (core/rendering/keyed.py) -------------------------------------
@dataclasses.dataclass
class Camera:
    name: str = 'main'
    fov: float = 60.


@dataclasses.dataclass(frozen=True, slots=True)
class Lens:
    focal: float = 35.


Point = collections.namedtuple('Point', 'x y')


class Table:
    """Looks like a dict and is none: no base class, no registration."""

    def __init__(self, **items):
        self.items = items

    def keys(self):
        return self.items.keys()

    def __getitem__(self, key):
        return self.items[key]

    def __setitem__(self, key, item):
        self.items[key] = item

    def __len__(self):
        return len(self.items)


class Plain:
    def __init__(self):
        self.a, self._hidden = 1, 2


def read(value):
    found = keyed.keyed(value.__class__)
    return list(found.keys(value)), [found.get(value, key) for key in found.keys(value)], bool(found.put), found.count(value)


@pytest.mark.parametrize('value, keys, items, writable', [
    ({'a': 1, 2: 'b'}, ['a', 2], [1, 'b'], True),
    (collections.OrderedDict(a=1), ['a'], [1], True),
    (collections.ChainMap({'a': 1}, {'b': 2}), ['b', 'a'], [2, 1], True),
    (collections.UserDict(a=1), ['a'], [1], True),
    (types.MappingProxyType({'a': 1}), ['a'], [1], False),
    (Table(a=1, b=2), ['a', 'b'], [1, 2], True),
    ([5, 6], [0, 1], [5, 6], True),
    (collections.deque([5, 6]), [0, 1], [5, 6], True),
    ((5, 6), [0, 1], [5, 6], False),
    (Point(1, 2), ['x', 'y'], [1, 2], False),
    (Camera(), ['name', 'fov'], ['main', 60.], True),
    (Lens(), ['focal'], [35.], False),
    (types.SimpleNamespace(a=1, _b=2), ['a'], [1], True),
])
def test_what_looks_like_a_dict_is_read_by_keys(value, keys, items, writable):
    assert read(value) == (keys, items, writable, len(keys))
    assert default_for(value) is dict_view.draw_collection


def test_a_dict_conversion_is_read_by_its_attributes_without_the_excluded_ones():
    tree = dict_view.Tree()
    assert default_for(tree) is dict_view.draw_collection
    assert read(tree)[0] == [name for name in vars(tree) if name[0] != '_' and name not in type(tree).__excluded_attrs__]
    assert 'flipped' in read(tree)[0]


@pytest.mark.parametrize('value', [3, 'text', b'bytes', {1, 2}, Plain(), len, object(), type, range(3)])
def test_what_does_not_look_like_a_dict_is_a_leaf(value):
    assert keyed.keyed(value.__class__) is None
    assert default_for(value) is not dict_view.draw_collection


def test_keyed_as_reads_a_type_by_name_and_a_view_of_its_own_still_wins(registry):
    was = dict(keyed.KEYED)
    try:
        found = keyed.keyed_as('Plain', keys=keyed.ATTRS.keys, get=getattr, put=setattr)
        assert keyed.keyed(Plain) is found and read(Plain()) == (['a'], [1], True, 1)
        assert default_for(Plain()) is dict_view.draw_collection
        own = is_default_for(Plain)(view('own'))
        assert default_for(Plain()) is own
        with pytest.raises(TypeError):
            keyed.keyed_as(3, keys=list)
    finally:
        keyed.KEYED.clear()
        keyed.KEYED.update(was)
        for cache in keyed.DERIVED:
            cache.clear()


def test_rows_nest_through_every_keyed_type():
    data = {'list': [1, {'deep': 2}], 'camera': Camera(), 'pair': (3, 4), 'leaf': 5}
    rows = Rows(data, 4, ())
    assert [(node.depth, node.keys[index]) for _, node, index in rows.window(0, 99)] == \
        [(0, 'list'), (1, 0), (1, 1), (2, 'deep'), (0, 'camera'), (1, 'name'), (1, 'fov'), (0, 'pair'), (1, 0), (1, 1),
         (0, 'leaf')]
    assert rows.flip(0) == {('list',)} and rows.count == 8
    data['list'].append(9)                                     # a list that grew outside, under a shut branch
    data['pair'] = (3, 4, 5)                                   # another tuple under an open one
    rows.top = rows.check(rows.top)
    assert rows.count == 9
    rows.flip(0)
    assert rows.count == 13
    data['list'].clear()                                       # an index in an open list is gone
    rows.top = rows.check(rows.top)
    assert rows.count == 9


def test_a_value_without_keys_is_refused_with_the_lever():
    with pytest.raises(TypeError, match='keyed_as'):
        Rows(3, 1, ())
