"""The tree edits and the pointer tests of draw_tiles (meltygui/view/tiles_view.py)."""
from meltygui.view.tiles_view import GAP, aim, find, join, merge, split, tidy, under


def tree():
    right = {'id': 3, 'axis': 'y', 'cells': [{'id': 4}, {'id': 5}], 'sizes': [300.0, 500.0]}
    return {'id': 0, 'axis': 'x', 'cells': [{'id': 1}, {'id': 2}, right], 'sizes': [200.0, 400.0, 600.0]}


def ids(node):
    return [ids(cell) if 'cells' in cell else cell['id'] for cell in node['cells']]


def test_split_along_the_parent_adds_a_sibling():
    root = tree()
    holder, edge = split(root, root, 1, 0, 150.0, 400.0, True)
    assert holder is root and edge == 2
    assert ids(root) == [1, 6, 2, [4, 5]]
    assert root['sizes'] == [200.0, 150.0, 250.0, 600.0]


def test_split_across_the_parent_nests_and_copies_the_tile():
    root = tree()
    root['cells'][0]['scene'] = 'a'
    holder, edge = split(root, root, 0, 1, 100.0, 800.0, False)
    assert holder is root['cells'][0] and edge == 1
    assert holder['axis'] == 'y' and holder['sizes'] == [100.0, 700.0]
    assert holder['cells'] == [{'id': 1, 'scene': 'a'}, {'id': 6, 'scene': 'a'}]
    assert holder['id'] == 7


def test_join_gives_the_room_to_the_kept_tile_and_collapses_the_split():
    root = tree()
    right = root['cells'][2]
    join(root, right, 0, 1)
    assert ids(root) == [1, 2, 5]
    assert root['sizes'] == [200.0, 400.0, 600.0]
    join(root, root, 1, 0)
    assert ids(root) == [1, 5] and root['sizes'] == [600.0, 600.0]


def test_tidy_splices_a_split_of_the_same_axis_and_keeps_the_root():
    inner = {'id': 3, 'axis': 'x', 'cells': [{'id': 4}, {'id': 5}], 'sizes': [10.0, 30.0]}
    root = {'id': 0, 'axis': 'x', 'cells': [{'id': 1}, inner], 'sizes': [100.0, 200.0]}
    tidy(root)
    assert ids(root) == [1, 4, 5] and root['sizes'] == [100.0, 50.0, 150.0]
    only = {'id': 0, 'axis': 'x', 'cells': [{'id': 9, 'axis': 'y', 'cells': [{'id': 1}, {'id': 2}], 'sizes': [1.0, 2.0]}],
            'sizes': [5.0]}
    tidy(only)
    assert only['id'] == 0 and only['axis'] == 'y' and ids(only) == [1, 2]


def hand(x, y):
    """A drag that began in tile 2, whose cell starts at (200, 0): the pointer at (x, y) in the window."""
    return {'tile': 2, 'x': x, 'y': y, 'ox': 200.0 + GAP, 'oy': GAP}


SPANS = {1: (200.0, 800.0), 2: (400.0, 800.0), 4: (600.0, 300.0), 5: (600.0, 500.0)}


def test_under_finds_the_source_a_leaf_neighbour_and_nothing_else():
    root = tree()
    assert under(root, 1, hand(300.0, 50.0), SPANS) == 1
    assert under(root, 1, hand(100.0, 50.0), SPANS) == 0
    assert under(root, 1, hand(700.0, 50.0), SPANS) is None      # a split, not a tile
    assert under(root, 1, hand(100.0, 900.0), SPANS) is None     # below the shared edge
    assert under(root, 1, hand(-10.0, 50.0), SPANS) is None      # past the neighbour


def test_join_turns_round_over_the_source_and_cancels_elsewhere():
    root = tree()
    drag = hand(100.0, 50.0)
    aim(root, 1, drag, SPANS)
    assert drag['gone'] == 1 and drag['other'] == 1
    drag.update(x=300.0)
    aim(root, 1, drag, SPANS)
    assert drag['gone'] == 2                                     # back over the source: it goes instead
    drag.update(y=900.0)
    aim(root, 1, drag, SPANS)
    assert drag['gone'] is None
    drag.update(x=300.0, y=50.0)
    aim(root, 1, drag, SPANS)
    assert merge(root, root, 1, drag)
    assert ids(root) == [1, [4, 5]] and root['sizes'] == [600.0, 600.0]
    assert find(root, 2) is None
