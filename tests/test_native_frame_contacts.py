"""A native frame's geometry must cross the same cell contacts exactly once."""
import pytest
from test_os_frame import studio, hand, app_root, app_frame, os_frame, C


@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('index', [0, 1])
def test_native_edge_pushes_divider_before_opposite_frame(studio, hand, axis, index):
    studio.size = [720., 720.]
    root = app_root(studio)
    # The whole pile and the window minimum agree; contact, rather than
    # an extra size constraint, decides when the opposite edge moves.
    root.min_width = root.min_height = 320
    app_frame(studio, root)
    pair = root._frame_edges if axis == 'x' else root._frame_rows
    near, far = pair
    divider = {axis: 190.}
    views = root._edge_views if axis == 'x' else root._row_views
    specs = root._edge_cells if axis == 'x' else root._row_cells
    key = ('row' if axis == 'x' else 'col', 'contact-test')
    views[key] = (root, [near, divider, far])
    specs[key] = ([120., 200.], [None, None])
    app_frame(studio, root)
    coordinate = 0 if axis == 'x' else 1
    origin = studio.pos[coordinate]
    # Independent collision graph is the reference for the native-frame
    # adapter, including a push through the divider into the opposite edge.
    expected = [{axis: origin}, {axis: origin + 190}, {axis: origin + 720}]
    graph = C._EdgeGraph(C._cells_from_lists([expected], axis,
                           specs=[([120., 200.], [None, None])]))
    direction = 1 if index == 0 else -1
    for step in range(1, 11):
        target = origin + (0 if index == 0 else 720) + direction * 50 * step
        C._solve_graph(graph, expected[0 if index == 0 else 2], target, axis=axis)
        root_pending = root._pending_drags if axis == 'x' else root._pending_row_drags
        edge = pair[index]
        root_pending.append((edge, edge[axis] + direction * 50, True))
        app_frame(studio, root)
        native_near, native_far = os_frame.edges(axis)
        assert (native_near[axis], native_far[axis]) == pytest.approx(
            (expected[0][axis], expected[2][axis]))
        size = root.width if axis == 'x' else root.height
        assert size == pytest.approx(expected[2][axis] - expected[0][axis])
        assert [edge[axis] for edge in (near, divider, far)] == pytest.approx(
            [edge[axis] - expected[0][axis] for edge in expected])
