"""Focused reproduction: transparent ordinary views in a Melty parent chain."""
from types import SimpleNamespace

import pytest

from test_os_frame import studio, hand, root, nested, abs_of, os_frame, C


@pytest.fixture(autouse=True)
def placement_layout(monkeypatch):
    """The fake-window harness needs the real wrapper's post-layout hook.

    Its recursive abs_of is the placement oracle; real DrawState exposes
    this origin through _anchor_base, which other os_frame tests cover.
    """
    def origin(window):
        x, y = abs_of(window)
        return (os_frame.applied_origin('x') + x - window.window_pos[0],
                os_frame.applied_origin('y') + y - window.window_pos[1])

    original_pass = C.window_edge_pass

    def after_layout(window):
        os_frame.rebase_pin(window)
        window.abs_left, window.abs_top = abs_of(window)
        return original_pass(window)

    monkeypatch.setattr(os_frame, '_anchor_base', lambda window: tuple(
        value - offset for value, offset in zip(abs_of(window), window.window_pos)))
    monkeypatch.setattr(os_frame, 'pin_origin', origin)
    monkeypatch.setattr(C, 'window_edge_pass', after_layout)


@pytest.mark.parametrize('height_delta', [0., -10.])
@pytest.mark.parametrize('ordinary_depth', [0, 1, 2])
@pytest.mark.parametrize('reflow_fraction', [0., 0.5, 1.])
def test_native_shrink_through_ordinary_parents(studio, ordinary_depth, reflow_fraction, height_delta):
    parent = root(studio, x=100., width=400, min_width=200, name='parent')
    child = nested(studio, parent, x=200., width=260, min_width=200, name='child')
    class ReflowView(SimpleNamespace):
        @property
        def window_pos(self):
            return (reflow_fraction * (parent.width - 400) / ordinary_depth, 0.)

    node = parent
    for _ in range(ordinary_depth):
        node = ReflowView(parent_window=node, closable=False, width=400, height=400)
    child.parent_window = node
    studio.frame(parent, child)
    for available, expected_parent, expected_child in [
            (500., (100., 340), (200., 200)),
            (400., (100., 240), (100., 200)),
            (300., (60., 200), (60., 200))]:
        studio.size[0] = available
        studio.size[1] += height_delta
        studio.frame(parent, child)
        observed = (parent.window_pos[0], parent.width, abs_of(child)[0], child.width)
        expected = (*expected_parent, expected_parent[0] + expected_child[0], expected_child[1])
        assert observed == expected, (ordinary_depth, available, observed, expected, abs_of(child))
        before = (observed, child.window_pos, len(studio.requests))
        for _ in range(3):
            studio.frame(parent, child)
            observed = (parent.window_pos[0], parent.width, abs_of(child)[0], child.width)
            assert (observed, child.window_pos, len(studio.requests)) == before


@pytest.mark.parametrize('ordinary_depth', [0, 2])
@pytest.mark.parametrize('operation', ['child_move', 'parent_move', 'parent_resize'])
def test_hand_operations_through_ordinary_parents(studio, monkeypatch, ordinary_depth, operation):
    import test_os_frame as existing

    class OrdinaryView:
        closable = False
        window_pos = (0., 0.)

        def __init__(self, parent):
            self.parent_window = parent

        @property
        def width(self):
            return self.parent_window.width

        @property
        def height(self):
            return self.parent_window.height

    def wrapped_nested(st, parent, *args, **kwargs):
        child = nested(st, parent, *args, **kwargs)
        node = parent
        for _ in range(ordinary_depth):
            node = OrdinaryView(node)
        child.parent_window = node
        return child

    monkeypatch.setattr(existing, 'nested', wrapped_nested)
    tests = {
        'child_move': existing.test_a_nested_hand_move_pushes_the_os_edge,
        'parent_move': existing.test_dragging_a_parent_makes_its_nested_windows_push_the_os_edge,
        'parent_resize': existing.test_resizing_a_parent_makes_its_nested_windows_push_the_os_edge,
    }
    tests[operation](studio)


def test_repeated_parent_right_resize_preserves_child_relative_position(studio):
    """Native requests between hand frames must not cancel the next hand step."""
    parent = root(studio, x=1000., width=800, name='parent')
    child = nested(studio, parent, x=200., width=400, name='child')

    class PlacementView:
        closable = False
        window_pos = (0., 0.)

        def __init__(self, parent):
            self.parent_window = parent

        @property
        def width(self):
            return self.parent_window.width

        @property
        def height(self):
            return self.parent_window.height

    child.parent_window = PlacementView(PlacementView(parent))
    child.parent_anchor_pos = 'top_right'
    studio.frame(parent, child)
    for step in range(1, 21):
        edge = C._frame(parent, 'x')[1]
        C._pending(parent, 'x').append((edge, edge['x'] + 40., True))
        studio.frame(parent, child)
        assert parent.width == 800 + 40 * step
        assert child.window_pos[0] == 200.
        assert abs_of(child)[0] == parent.window_pos[0] + parent.width + 200.


@pytest.mark.parametrize('depth', [1, 2])
@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('operation', ['move', 'resize'])
def test_inspector_push_through_layout_sized_workspace(studio, hand, monkeypatch, depth, axis, operation):
    """MCE's explicit-size workspace follows the native root on the next draw."""
    from test_os_frame import app_root, app_frame, FakeWindow
    studio.size = [800., 600.]
    app = app_root(studio)
    workspaces = []
    parent = app
    for index in range(depth):
        workspace = FakeWindow(width=parent.width-10, min_width=60, x=5)
        workspace.height = parent.height-30
        workspace.window_pos = (5., 20.)
        workspace.parent_window = parent
        workspace.id = workspace.name = f'workspace-{index}'
        workspace.closable = True
        workspace._kwargs = dict(window_pos=(5., 20.), width=workspace.width, height=workspace.height)
        workspaces.append(workspace)
        studio.nested.append(workspace)
        parent = workspace
    child = FakeWindow(width=200, min_width=100, x=-200)
    child.height = 150
    child.window_pos = (-200., -150.)
    child.id = child.name = 'inspector'
    child.parent_window = parent
    child.parent_anchor_pos = 'bottom_right'
    child.closable = True
    studio.nested.append(child)
    previous_pass = C.window_edge_pass

    def layout_then_pass(window):
        if window in workspaces:
            window.width = window.parent_window.width-10
            window.height = window.parent_window.height-30
            window.window_pos = (5., 20.)
            window._kwargs.update(width=window.width, height=window.height)
        return previous_pass(window)

    monkeypatch.setattr(C, 'window_edge_pass', layout_then_pass)
    def frame():
        return app_frame(studio, app, *workspaces, child)
    frame()
    index = 0 if axis == 'x' else 1
    start = studio.pos[index] + abs_of(child)[index]
    initial_far = os_frame.edges(axis)[1][axis]
    size = child.width if axis == 'x' else child.height
    previous_step = 0
    for step in [*range(1, 16), *range(14, -1, -1)]:
        increment = (step - previous_step) * 10.
        previous_step = step
        if operation == 'move':
            child._pending_move = (increment, 0.) if axis == 'x' else (0., increment)
        else:
            edge = (child._frame_edges if axis == 'x' else child._frame_rows)[1]
            pending = child._pending_drags if axis == 'x' else child._pending_row_drags
            pending.append((edge, edge[axis]+increment, True))
        frame()
        # Let the native request and explicitly passed workspace sizes land.
        frame()
        expected_near = start + (step*10 if operation == 'move' else 0)
        expected_far = start + size + step*10
        assert studio.pos[index] + abs_of(child)[index] == pytest.approx(expected_near)
        assert os_frame.edges(axis)[1][axis] == pytest.approx(max(initial_far, expected_far))
    settled = (child.window_pos, studio.size[:], len(studio.requests))
    for _ in range(3):
        frame()
        assert (child.window_pos, studio.size[:], len(studio.requests)) == settled


@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('side', [0, 1])
@pytest.mark.parametrize('target', ['frame', 'divider'])
def test_caller_sized_workspace_edges_resize_native_parent(studio, hand, monkeypatch, axis, side, target):
    from test_os_frame import app_root, app_frame, FakeWindow
    studio.size = studio.feed_size = [800., 600.]
    app = app_root(studio)
    workspace = FakeWindow(width=790, height=570, min_width=100, min_height=100, x=5, y=20)
    workspace.id = workspace.name = 'workspace'
    workspace.parent_window = app
    workspace.closable = True
    workspace._kwargs = dict(window_pos=(5., 20.), width=790, height=570, frame_pinned=True)
    studio.nested.append(workspace)
    previous_pass = C.window_edge_pass

    def layout(window):
        if window is workspace:
            window.width = app.width - 10
            window.height = app.height - 30
            window.window_pos = (5., 20.)
            window._kwargs.update(width=window.width, height=window.height)
        return previous_pass(window)

    monkeypatch.setattr(C, 'window_edge_pass', layout)
    def frame():
        app_frame(studio, app, workspace)
    frame()
    declared_minimum = (app.min_width, app.min_height)
    index = 0 if axis == 'x' else 1
    gap = 10 if axis == 'x' else 30
    edges = workspace._frame_edges if axis == 'x' else workspace._frame_rows
    size = workspace.width if axis == 'x' else workspace.height
    dividers = [{axis: size/3}, {axis: size*2/3}]
    registry = workspace._edge_views if axis == 'x' else workspace._row_views
    registry['cells'] = (workspace, [edges[0], *dividers, edges[1]])
    specs = C._specs(workspace, axis)
    specs['cells'] = ([100., 100., 100.], [None, None, None])
    frame()
    dragged = edges[side] if target == 'frame' else dividers[side]
    previous = 0
    for total in [*range(20, 641, 20), *range(620, -1, -20)]:
        inc = (total-previous) * (1 if side == 0 else -1)
        previous = total
        pending = workspace._pending_drags if axis == 'x' else workspace._pending_row_drags
        pending.append((dragged, dragged[axis]+inc, True))
        frame()
        native = os_frame.edges(axis)
        native_span = native[1][axis] - native[0][axis]
        actual = workspace.width if axis == 'x' else workspace.height
        assert actual == pytest.approx(native_span-gap, abs=.5)
        assert workspace.window_pos == (5., 20.)
        assert edges[0][axis] == 0
        assert edges[1][axis] == pytest.approx(actual)
        # Caller layout and compositor acknowledgement cannot undo the resize.
        frame()
        assert (workspace.width if axis == 'x' else workspace.height) == pytest.approx(actual)
    assert studio.size[index] == (800 if axis == 'x' else 600)
    # A child's constraints must not overwrite the ancestor's declared policy.
    assert (app.min_width, app.min_height) == declared_minimum


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_native_near_resize_compensates_deferred_anchor_once_per_frame(studio, hand, axis):
    """A queued native move and sticky replay must not both carry the inspector."""
    from test_os_frame import app_root, app_frame, FakeWindow
    studio.size = [800., 600.]
    app = app_root(studio)
    placement = SimpleNamespace(parent_window=app, closable=False,
                                window_pos=(0., 0.), width=800, height=600)
    child = FakeWindow(width=200, min_width=100, x=200)
    child.height = 150
    child.window_pos = (200., 100.)
    child.parent_window = placement
    child.id = child.name = 'inspector'
    child.closable = True
    studio.nested.append(child)
    app_frame(studio, app, child)
    i = 0 if axis == 'x' else 1
    initial = os_frame.applied_origin(axis) + abs_of(child)[i]
    # Consecutive movement, pauses while an acknowledgement lands, and reversal.
    for delta in (-8., -7., 0., -8., -7., 0., 7., 8., 7., 8., 0., 0.):
        if delta:
            os_frame.queue_drag(axis, 0, delta)
        app_frame(studio, app, child)
        actual = os_frame.applied_origin(axis) + abs_of(child)[i]
        assert actual == pytest.approx(initial), (axis, delta, actual, initial)
