"""The tree edits and the pointer tests of draw_tiles (meltygui/view/common/tiles_view.py)."""
from meltygui.view.common.tiles_view import GAP, REACH, aim, blank, chosen, find, join, mend, merge, split, tidy, title, track, under


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


def test_mend_replaces_an_id_a_view_edited_into_one_the_tree_has():
    root = tree()
    assert not mend(root) and ids(root) == [1, 2, [4, 5]]
    dragged = root['cells'][0]
    dragged['id'] = 2                                            # what a drag on draw_any's `id` row does
    root['cells'][2]['cells'][1]['id'] = 3                       # a tile with its split's id
    assert mend(root)
    found = [root['id'], *ids(root)[:2], root['cells'][2]['id'], *ids(root)[2]]
    assert len(set(found)) == 6 and root['id'] == 0 and max(found) == 6
    assert not mend(root)


def hand(x, y):
    """A drag that began in tile 2, the second cell of the root, whose cell starts at (200, 0): the
    pointer at (x, y) in the window. `leaves` are the root's cells: two tiles and a split."""
    return {'tile': 2, 'cell': 1, 'leaves': [1, 2, None], 'x': x, 'y': y, 'ox': 200.0 + GAP, 'oy': GAP}


SIZES = [200.0, 400.0, 600.0]           # the root's cells along x
ACROSS = 800.0                          # and their height


def test_under_finds_the_source_a_leaf_neighbour_and_nothing_else():
    assert under(hand(300.0, 50.0), 0, SIZES, ACROSS) == 1
    assert under(hand(100.0, 50.0), 0, SIZES, ACROSS) == 0
    assert under(hand(700.0, 50.0), 0, SIZES, ACROSS) is None    # a split, not a tile
    assert under(hand(100.0, 900.0), 0, SIZES, ACROSS) is None   # below the shared edge
    assert under(hand(-10.0, 50.0), 0, SIZES, ACROSS) is None    # past the neighbour


def test_join_turns_round_over_the_source_and_cancels_elsewhere():
    root = tree()
    drag = hand(100.0, 50.0)
    aim(drag, 0, SIZES, ACROSS)
    assert drag['gone'] == 1 and drag['other'] == 1
    drag.update(x=300.0)
    aim(drag, 0, SIZES, ACROSS)
    assert drag['gone'] == 2                                     # back over the source: it goes instead
    drag.update(y=900.0)
    aim(drag, 0, SIZES, ACROSS)
    assert drag['gone'] is None
    drag.update(x=300.0, y=50.0)
    aim(drag, 0, SIZES, ACROSS)
    assert merge(root, root, 1, drag)
    assert ids(root) == [1, [4, 5]] and root['sizes'] == [600.0, 600.0]
    assert find(root, 2) is None


def test_track_wakes_draw_tiles_only_for_what_needs_the_tree():
    drag = dict(hand(300.0, 50.0), sx=300.0, sy=50.0)
    assert not track(drag)                                       # not far enough to be decided
    drag.update(x=300.0 + REACH)
    assert track(drag)                                           # `begin` decides: draw_tiles runs
    drag['released'] = True
    assert track(drag)


def draw_big_scene(input_value):
    pass


def test_a_tile_shows_the_view_it_names_and_the_first_one_otherwise():
    views = (blank, draw_big_scene)
    assert chosen({'id': 1, 'view': 'draw_big_scene'}, views) is draw_big_scene
    assert chosen({'id': 1}, views) is blank
    assert chosen({'id': 1, 'view': 'renamed'}, views) is blank
    assert title(draw_big_scene) == 'Big scene' and title(blank) == 'Blank'


def test_mend_completes_a_blank_dict_and_a_nested_one():
    root = {}
    assert mend(root)
    assert root == {'id': 0, 'axis': 'x', 'cells': [{'id': 1}]}
    assert not mend(root)                                        # nothing left to correct
    kept = {'view': 'draw_notes', 'text': 'hi'}                  # a tile at the root keeps what it held
    assert mend(kept) and kept['cells'] == [{'view': 'draw_notes', 'text': 'hi', 'id': 1}]
    stack = {'cells': [{}, {}]}
    nested = {'cells': [{'note': 'a'}, stack, {'axis': 'x', 'cells': ()}, None]}
    assert mend(nested) and not mend(nested)
    assert nested['axis'] == 'x' and stack['axis'] == 'y'        # side by side, then the other axis
    assert nested['cells'][0]['note'] == 'a' and nested['cells'][1] is stack
    assert nested['cells'][2]['cells'] == [{'id': nested['cells'][2]['cells'][0]['id']}]
    found = [nested['id'], nested['cells'][2]['id'], stack['id'], *(leaf for cell in ids(nested) for leaf in (cell if isinstance(cell, list) else [cell]))]
    assert sorted(found) == list(range(8))


def test_mend_replaces_an_id_that_is_no_int_and_keeps_the_others():
    root = {'id': 'root', 'cells': [{'id': 7}, {'id': None}, {'id': [1]}]}
    assert mend(root)
    assert ids(root)[0] == 7 and sorted([root['id'], *ids(root)[1:]]) == [8, 9, 10]
