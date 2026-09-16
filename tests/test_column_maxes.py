"""ColumnLayout ``column_maxes``: per-column caps stamped as "max" on each
column's right edge (the mirror of column_mins' "min").

  - a column at its cap doesn't open further: dragging its edge PULLS its
    far edge along (the min side PUSHES the edge ahead)
  - consecutive capped columns travel as one, like a min-packed pile
  - the chain stops at the first column with slack; an uncapped column
    never pulls
  - a wall (foreign-width frame drag) clamps the dragged edge at the
    summed caps instead of moving the wall
  - _clamp_interior packs a persisted over-cap column back down, and is
    idempotent when the frame is wider than every cap together
  - resolve_column_widths caps a flex share and re-splits the remainder
  - a real ColumnLayout with the merge window's config: widening the
    window through the frame edge drags the last divider with it

Run: .venv/bin/python -m pytest tests/test_column_maxes.py -q
"""
import os
import sys

import conftest  # noqa: F401,E402

from meltygui.views import columns as C  # noqa: E402
from test_column_edge_solve import make_window, run_pass  # noqa: E402
from test_column_frame_fit import _Cell, _window  # noqa: E402


def _xs(edges):
    return [round(e["x"]) for e in edges]


def _spans(edges):
    xs = _xs(edges)
    return [b - a for a, b in zip(xs, xs[1:])]


def _edges(xs, maxes=(), mins=()):
    """Edge list from x positions; maxes/mins indexed like column_maxes
    (column i → stamped on edge i+1)."""
    edges = [{"x": float(x)} for x in xs]
    for i, cap in enumerate(maxes):
        if cap:
            edges[i + 1]["max"] = float(cap)
    for i, floor in enumerate(mins):
        if floor:
            edges[i + 1]["min"] = float(floor)
    return edges


# ---------------------------------------------------------------- _drag_edge

def test_edge_at_cap_pulls_its_far_edge_along():
    # col 0 capped at 100 and already there: dragging edge 1 right pulls
    # edge 0 along 1:1; col 1 (uncapped) just shrinks.
    edges = _edges([0, 100, 400, 700], maxes=[100, None, None])
    C._drag_edge(edges, 1, 150.0)
    assert _xs(edges) == [50, 150, 400, 700]


def test_slack_below_cap_is_used_before_pulling():
    edges = _edges([0, 80, 400, 700], maxes=[100, None, None])
    C._drag_edge(edges, 1, 150.0)
    # col 0 opens from 80 to its cap 100, THEN edge 0 is carried the rest.
    assert _xs(edges) == [50, 150, 400, 700]


def test_uncapped_column_never_pulls():
    edges = _edges([0, 100, 400, 700])
    C._drag_edge(edges, 1, 150.0)
    assert _xs(edges) == [0, 150, 400, 700]


def test_consecutive_capped_columns_cascade_like_a_pile():
    # cols 0,1,2 all capped and at cap: dragging edge 3 right carries the
    # whole run; col 3 (uncapped, wide) absorbs.
    edges = _edges([0, 100, 200, 300, 900], maxes=[100, 100, 100, None])
    C._drag_edge(edges, 3, 340.0)
    assert _xs(edges) == [40, 140, 240, 340, 900]
    assert _spans(edges) == [100, 100, 100, 560]


def test_pull_chain_stops_at_first_column_with_slack():
    # col 1 sits 20 under its cap. Edge 3 dragged right 50: col 2 is at
    # cap → edge 2 carried 50 → col 1 opens 80→100 (its 20 of slack) → edge
    # 1 carried the remaining 30 → col 0 at cap → edge 0 carried 30.
    edges = _edges([0, 100, 180, 280, 900], maxes=[100, 100, 100, None])
    C._drag_edge(edges, 3, 330.0)
    assert _xs(edges) == [30, 130, 230, 330, 900]
    assert _spans(edges) == [100, 100, 100, 570]


def test_leftward_drag_pulls_symmetrically():
    edges = _edges([0, 600, 800, 900, 1000], maxes=[None, None, 100, 100])
    C._drag_edge(edges, 2, 750.0)
    # col 2 at cap → edge 3 carried left → col 3 at cap → edge 4 carried.
    assert _xs(edges) == [0, 600, 750, 850, 950]


def test_push_and_pull_run_on_both_sides_of_one_drag():
    # Ahead: col 2 at min (60). Behind: col 0 at cap. Dragging edge 1
    # right pushes edge 2 ahead AND pulls edge 0 behind.
    edges = _edges([0, 100, 160, 400], maxes=[100, None, None])
    C._drag_edge(edges, 1, 120.0)
    assert _xs(edges) == [20, 120, 180, 400]


def test_wall_clamps_pull_at_summed_caps():
    # Foreign widening of the right frame edge: every column capped and at
    # cap, left frame edge walled → the drag stops at Σcaps; nothing moves.
    edges = _edges([0, 100, 200, 300], maxes=[100, 100, 100])
    C._drag_edge(edges, 3, 500.0, walls=frozenset({id(edges[0])}))
    assert _xs(edges) == [0, 100, 200, 300]
    # With slack in the middle the clamp lands at the summed caps.
    edges = _edges([0, 100, 150, 250], maxes=[100, 100, 100])
    C._drag_edge(edges, 3, 500.0, walls=frozenset({id(edges[0])}))
    assert _xs(edges) == [0, 100, 200, 300]


def test_wall_out_of_reach_behind_an_uncapped_column_does_not_clamp():
    edges = _edges([0, 100, 200, 300], maxes=[None, 100, 100])
    C._drag_edge(edges, 3, 500.0, walls=frozenset({id(edges[0])}))
    assert _xs(edges) == [0, 300, 400, 500]


# ---------------------------------------------------------- _clamp_interior

def test_clamp_packs_an_over_cap_first_column_rightward():
    edges = _edges([0, 500, 800, 1200], maxes=[350, None, None])
    C._clamp_interior(edges)
    assert _xs(edges) == [0, 350, 800, 1200]


def test_clamp_rolls_overflow_through_capped_neighbours():
    edges = _edges([0, 500, 700, 1200], maxes=[350, 200, None])
    C._clamp_interior(edges)
    assert _xs(edges) == [0, 350, 550, 1200]


def test_clamp_over_cap_last_column_pulls_its_left_edge_out():
    # The merge window shape: last column capped, frame fixed → its left
    # edge moves right and the uncapped column before it absorbs.
    edges = _edges([0, 350, 700, 1200], maxes=[350, None, 350])
    C._clamp_interior(edges)
    assert _xs(edges) == [0, 350, 850, 1200]


def test_clamp_is_idempotent_when_frame_exceeds_summed_caps():
    edges = _edges([0, 200, 300, 700], maxes=[100, 100, 100])
    C._clamp_interior(edges)
    once = _xs(edges)
    # Every column but the last sits at its cap; the last overflows (the
    # frame is the authority) and a second pass changes nothing.
    assert once == [0, 100, 200, 700]
    C._clamp_interior(edges)
    assert _xs(edges) == once


def test_cap_never_undercuts_min_in_clamp_or_stamp():
    # A cap below the min reads as the min when stamped by ColumnLayout.
    assert C._column_cap([40], 0, 60) == 60.0
    assert C._column_cap([None], 0, 60) is None
    assert C._column_cap([], 0, 60) is None


# ---------------------------------------------------- resolve_column_widths

def test_flex_share_is_capped_and_remainder_resplit():
    widths = C.resolve_column_widths([None, None, None], 3, 900,
                                     column_maxes=[100, None, None])
    assert widths == [100, 400, 400]
    widths = C.resolve_column_widths([250, None, None, None, 250], 5, 1800,
                                     column_mins=[250, 150, 150, 150, 250],
                                     column_maxes=[350, None, None, None, 350])
    assert widths == [250, 1300 / 3, 1300 / 3, 1300 / 3, 250]


def test_fixed_width_is_clamped_to_its_cap():
    widths = C.resolve_column_widths([500, None], 2, 900,
                                     column_maxes=[350, None])
    assert widths == [350, 550]


# ------------------------------------------------------------ ColumnLayout

def _merge_layout(window, cell, widths=(250, None, None, None, 250)):
    import meltygui_imgui as imgui
    imgui.new_frame()
    imgui.begin("Host")
    try:
        row = C.ColumnLayout(cell, 5, column_edges=cell.column_edges,
                             column_widths=list(widths),
                             column_mins=[250, 150, 350, 150, 250],
                             column_maxes=[350, None, None, None, 350],
                             resizable=False, border_color=None)
        cell.column_edges = row.edges
        return row.edges
    finally:
        imgui.end()
        imgui.end_frame()


def test_column_maxes_ride_the_edges_and_widening_drags_the_divider():
    window = _window(1800)
    cell = _Cell(window)
    edges = _merge_layout(window, cell)
    assert [e.get("max") for e in edges] == [None, 350, None, None, None, 350]
    assert [e.get("min") for e in edges] == [None, 250, 150, 350, 150, 250]
    # Open the outer columns to their caps by drag.
    C._drag_edge(edges, 1, 350.0)
    C._drag_edge(edges, 4, 1450.0)
    assert _spans(edges)[0] == 350 and _spans(edges)[4] == 350
    # A foreign widening (window.width written) drags the right frame
    # edge through the solve: the capped last column carries edge 4 along
    # and the uncapped EXTERNAL pane before it grows instead.
    window.width = 2000
    run_pass(window)
    edges = _merge_layout(window, cell)
    xs = _xs(edges)
    assert xs[-1] == 2000 and xs[4] == 1650
    assert _spans(edges)[4] == 350 and _spans(edges)[0] == 350
    # And a persisted over-cap first column is packed back down when the
    # layout is rebuilt (the cap applied after the fact).
    edges[1]["x"] = 600.0
    edges = _merge_layout(window, cell)
    assert _spans(edges)[0] == 350


def test_seed_respects_caps():
    window = _window(1800)
    cell = _Cell(window)
    edges = _merge_layout(window, cell, widths=(None,) * 5)
    spans = _spans(edges)
    assert spans[0] == 350 and spans[4] == 350
    assert abs(sum(spans) - 1800) <= 1


def test_cell_rect_is_the_box_cell_clips_to():
    window = _window(1800)
    cell = _Cell(window)
    import meltygui_imgui as imgui
    imgui.new_frame()
    imgui.begin("Host")
    try:
        row = C.ColumnLayout(cell, 3, column_widths=[300, None, None],
                             padding=10.0, padding_y=4.0, resizable=False,
                             border_color=None)
        x, y, w, h = row.cell_rect(1, height=400)
        assert (x, w) == (round(row.win_x + row.edges[1]["x"] + 10),
                          round(row.inner_width(1)))
        assert y == round(row.top + 4) and h == 400 - 8
        # And a cell's clip is exactly that box.
        from meltygui.core.core_decoration import Core
        with row.cell(1, height=400):
            clip = Core.melty.get_clip_rect()
        assert tuple(round(v) for v in clip) == (x, y, x + w, y + h)
    finally:
        imgui.end()
        imgui.end_frame()
