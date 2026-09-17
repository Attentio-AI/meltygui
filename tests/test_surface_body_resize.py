"""Surface roots below native chrome retain the inset through edge contacts."""
from meltygui.core.layout import edge_constraints

import pytest

from test_os_frame import studio, hand, app_root, abs_of, os_frame, C, Melty, tb


def body_frame(st, root, top):
    Melty.frame_count += 1
    st.land()
    os_frame.apply_rebase()
    Melty.display_size = tuple(st.size)
    os_frame.begin_frame()
    tb.poll_os_window_drag()
    root.abs_left, root.abs_top = abs_of(root)
    os_frame.solve()
    width, height = os_frame.content_size(tuple(st.size))
    root.window_pos = (0., top)
    root.width, root.height = width, height - top
    root.abs_left, root.abs_top = abs_of(root)
    C.window_edge_pass(root)
    root.abs_left, root.abs_top = abs_of(root)
    return os_frame.flush()


@pytest.mark.parametrize('top', [0., 30.])
@pytest.mark.parametrize('index', [0, 1])
def test_surface_body_frame_contacts_preserve_chrome(studio, hand, top, index):
    studio.size = [720., 720.]
    root = app_root(studio)
    root.min_height = 320
    body_frame(studio, root, top)
    near, far = root._frame_rows
    divider = {'y': 190.}
    root._row_views[('col', 'body')] = (root, [near, divider, far])
    root._row_cells[('col', 'body')] = ([120., 200.], [None, None])
    body_frame(studio, root, top)
    origin = studio.pos[1]
    expected = [{'y': origin + top}, {'y': origin + top + 190.},
                {'y': origin + 720.}]
    graph = edge_constraints.EdgeGraph(C._cells_from_lists([expected], 'y',
                           specs=[([120., 200.], [None, None])]))
    direction = 1 if index == 0 else -1
    for step in range(1, 13):
        target = origin + (top if index == 0 else 720.) + direction * 40 * step
        edge_constraints.solve_edge(graph, expected[index * 2], target, axis='y')
        edge = (near, far)[index]
        root._pending_row_drags.append((edge, edge['y'] + direction * 40, True))
        body_frame(studio, root, top)
        native_near, native_far = os_frame.edges('y')
        assert (native_near['y'], native_far['y']) == pytest.approx(
            (expected[0]['y'] - top, expected[2]['y']))
        # Native bodies stay at their content inset even before a move is acknowledged.
        assert root.window_pos[1] == pytest.approx(top)
        assert root.height == pytest.approx(expected[2]['y'] - expected[0]['y'])
        assert [e['y'] for e in (near, divider, far)] == pytest.approx(
            [e['y'] - expected[0]['y'] for e in expected])
    # Reverse the same gesture through the compressed pile. Interior
    # dividers retain their contact positions as the frame opens again.
    for _ in range(12):
        edge = (near, far)[index]
        root._pending_row_drags.append((edge, edge['y'] - direction * 40, True))
        body_frame(studio, root, top)
        native_near, native_far = os_frame.edges('y')
        assert root.height == pytest.approx(native_far['y'] - native_near['y'] - top)
        # Native bodies stay at their content inset even before a move is acknowledged.
        assert root.window_pos[1] == pytest.approx(top)
        assert divider['y'] >= 120.
        assert far['y'] - divider['y'] >= 200.
    assert root.height == 720. - top


@pytest.mark.parametrize('index', [0, 1])
def test_compositor_resize_keeps_body_inset_and_settles(studio, index):
    studio.size = [720., 720.]
    root = app_root(studio)
    body_frame(studio, root, 30.)
    near, far = root._frame_rows
    divider = {'y': 190.}
    root._row_views[('col', 'body')] = (root, [near, divider, far])
    root._row_cells[('col', 'body')] = ([120., 200.], [None, None])
    body_frame(studio, root, 30.)
    for delta in (80., 80., -80., -80.):
        if index == 0:
            studio.pos[1] += delta
        studio.size[1] -= delta
        body_frame(studio, root, 30.)
        assert root.height == studio.size[1] - 30.
        assert root.window_pos == (0., 30.)
        assert near['y'] == 0.
        assert far['y'] == root.height
        assert divider['y'] >= 120.
        assert far['y'] - divider['y'] >= 200.
        before = (root.height, divider['y'], len(studio.requests))
        for _ in range(3):
            body_frame(studio, root, 30.)
            assert (root.height, divider['y'], len(studio.requests)) == before


@pytest.mark.parametrize('top', [0., 30.])
def test_interior_divider_pushes_surface_through_fixed_chrome_gap(studio, hand, top):
    studio.size = [720., 720.]
    root = app_root(studio)
    root.min_height = 320
    body_frame(studio, root, top)
    near, far = root._frame_rows
    divider = {'y': 190.}
    root._row_views[('col', 'body')] = (root, [near, divider, far])
    root._row_cells[('col', 'body')] = ([120., 200.], [None, None])
    body_frame(studio, root, top)
    origin = studio.pos[1]
    previous = 0.
    for travel in (-100., -200., -100., 0., 100., 400., 0.):
        expected = [{'y': origin + top}, {'y': origin + top + 190.},
                    {'y': origin + 720.}]
        graph = edge_constraints.EdgeGraph(C._cells_from_lists([expected], 'y',
                               specs=[([120., 200.], [None, None])]))
        edge_constraints.solve_edge(graph, expected[1], expected[1]['y'] + travel, axis='y')
        root._pending_row_drags.append((divider, divider['y'] + travel - previous, True))
        previous = travel
        body_frame(studio, root, top)
        native_near, native_far = os_frame.edges('y')
        assert (native_near['y'], native_far['y']) == pytest.approx(
            (expected[0]['y'] - top, expected[2]['y']))
        assert root.height == pytest.approx(expected[2]['y'] - expected[0]['y'])
        assert [e['y'] for e in (near, divider, far)] == pytest.approx(
            [e['y'] - expected[0]['y'] for e in expected])
