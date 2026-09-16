"""The cell-graph collision solve (columns._solve_graph): edges collide
only THROUGH a cell that spans between them, never by mere proximity
along the axis.

  - the column dividers of two different rows (both rows adopt the frame
    edges, share no cell) pass each other freely
  - a divider pushed into the frame still moves the frame, and the frame
    still packs BOTH rows' last columns (the frame edge is a shared cell
    boundary)
  - the window's min_width floor is the LONGEST cell chain, not the sum
    over every edge
  - a nested layout's shared far edge carries every cell it bounds: the
    wall clamp takes the binding (longest) chain
  - a shared edge pulled by a capped cell also pushes the other cells it
    bounds
  - the rows of two different columns are independent the same way

Run: .venv/bin/python -m pytest tests/test_edge_graph_solve.py -q
"""
import os
import sys

import conftest  # noqa: F401,E402

from meltygui.views import columns as C  # noqa: E402
from test_column_edge_solve import FakeWindow, run_pass  # noqa: E402
from meltygui.melty import Melty  # noqa: E402


def _window(**kw):
    Melty.frame_count = getattr(Melty, "frame_count", 0) or 0
    w = FakeWindow(**kw)
    C.window_edge_pass(w)
    return w


def _xs(edges):
    return [round(e["x"]) for e in edges]


def test_dividers_of_different_rows_pass_each_other():
    w = _window(width=600, min_width=50)
    left, right = w._frame_edges
    a1, b1 = {"x": 300.0}, {"x": 310.0}
    w._edge_views[("row", "a")] = (w, [left, a1, right])   # row A's columns
    w._edge_views[("row", "b")] = (w, [left, b1, right])   # row B's columns
    w._pending_drags.append((a1, 350.0))                   # a1 crosses b1
    run_pass(w)
    assert (a1["x"], b1["x"]) == (350.0, 310.0)            # b1 untouched
    w._pending_drags.append((a1, 100.0))                   # and back over it
    run_pass(w)
    assert (a1["x"], b1["x"]) == (100.0, 310.0)
    assert (w.window_pos[0], w.width) == (100.0, 600)      # frame untouched


def test_frame_edge_still_packs_every_rows_columns():
    w = _window(width=600, min_width=50)
    left, right = w._frame_edges
    a1, b1 = {"x": 300.0}, {"x": 500.0}
    w._edge_views[("row", "a")] = (w, [left, a1, right])
    w._edge_views[("row", "b")] = (w, [left, b1, right])
    # a1 dragged past b1 and into the right frame edge: b1 is passed (no
    # shared cell), the frame is pushed (cell a1→right) and the window
    # grows — b1's cell just gets wider.
    w._pending_drags.append((a1, 580.0))
    run_pass(w)
    assert a1["x"] == 580.0 and b1["x"] == 500.0
    assert w.width == 640 and right["x"] == 640.0
    # A foreign shrink (walled left edge) packs BOTH rows against the
    # frame through their own last cells.
    w.width = 200
    run_pass(w)
    assert right["x"] == 200.0
    assert (a1["x"], b1["x"]) == (140.0, 140.0)
    assert left["x"] == 0.0 and w.window_pos[0] == 100.0


def test_min_width_floor_is_the_longest_chain_not_the_sum():
    w = _window(width=600, min_width=50)
    left, right = w._frame_edges
    a1, a2 = {"x": 200.0}, {"x": 400.0}
    b1 = {"x": 300.0}
    w._edge_views[("row", "a")] = (w, [left, a1, a2, right])   # 3 cells → 180
    w._edge_views[("row", "b")] = (w, [left, b1, right])       # 2 cells → 120
    run_pass(w)
    assert w.min_width == 180          # was 60 × 5 edges = 300 under the sum
    # And the foreign shrink stops exactly there.
    w.width = 100
    run_pass(w)
    assert w.width == 180 and _xs([left, a1, a2, right]) == [0, 60, 120, 180]
    assert b1["x"] == 120.0


def test_nested_shared_edge_wall_clamp_takes_the_binding_chain():
    # A = [L, a1, R]; B nested in A's first cell = [L, b1, a1] (L and a1
    # shared by reference). Dragging a1 left with L walled: the chain
    # L→b1→a1 (two floors, 120) binds before the direct cell L→a1 (60).
    L, a1, R = {"x": 0.0}, {"x": 300.0}, {"x": 600.0}
    b1 = {"x": 150.0}
    graph = C._EdgeGraph(C._cells_from_lists([[L, a1, R], [L, b1, a1]]))
    C._solve_graph(graph, a1, 20.0, walls=frozenset({id(L), id(R)}))
    assert _xs([L, b1, a1, R]) == [0, 60, 120, 600]
    # Unwalled, the same drag pushes b1 into L and carries L along.
    L, a1, R = {"x": 0.0}, {"x": 300.0}, {"x": 600.0}
    b1 = {"x": 150.0}
    graph = C._EdgeGraph(C._cells_from_lists([[L, a1, R], [L, b1, a1]]))
    C._solve_graph(graph, a1, 20.0)
    assert _xs([L, b1, a1, R]) == [-100, -40, 20, 600]


def test_pulled_shared_edge_pushes_the_other_cells_it_bounds():
    # A = [L, a1, R] with A's first cell capped at 100; B = [a1, b1, R]
    # shares a1. Dragging a1 right past the cap pulls L along (A's cell),
    # and b1 ahead of it is pushed by B's cell a1→b1 as usual.
    L, a1, R = {"x": 0.0}, {"x": 100.0, "max": 100}, {"x": 600.0}
    b1 = {"x": 150.0}
    graph = C._EdgeGraph(C._cells_from_lists([[L, a1, R], [a1, b1, R]]))
    C._solve_graph(graph, a1, 200.0)
    assert _xs([L, a1, b1, R]) == [100, 200, 260, 600]


def test_rows_of_different_columns_are_independent_too():
    w = _window(height=600, min_height=50)
    top, bottom = w._frame_rows
    a1, b1 = {"y": 300.0}, {"y": 310.0}
    w._row_views[("rows", "a")] = (w, [top, a1, bottom])
    w._row_views[("rows", "b")] = (w, [top, b1, bottom])
    w._pending_row_drags.append((a1, 400.0))
    run_pass(w)
    assert (a1["y"], b1["y"]) == (400.0, 310.0)
    assert w.min_height == 80          # longest chain: 2 × MIN_ROW_HEIGHT


def test_single_list_drag_edge_is_unchanged():
    # The list API (_clamp_interior's cap repair, the older tests): one
    # list's consecutive edges are its cells.
    edges = [{"x": 0.0}, {"x": 100.0}, {"x": 200.0}, {"x": 300.0}]
    C._drag_edge(edges, 1, 180.0)
    assert _xs(edges) == [0, 180, 240, 300]
    C._drag_edge(edges, 2, 50.0, walls=frozenset({id(edges[0])}))
    assert _xs(edges) == [0, 60, 120, 300]
