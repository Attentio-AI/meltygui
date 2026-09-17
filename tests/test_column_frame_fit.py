"""ColumnLayout vs its frame: interior dividers never sit outside the far
edges (_clamp_interior — a state no drag could repair: an edge past the
frame sorts after it in the flat solve and its handle is off-window).

Pure edge-list checks for the clamp; the frame-shrink case runs a real
ColumnLayout against the test_column_edge_solve fake window.
"""
import os
import sys

import conftest  # noqa: F401,E402

from meltygui.core.layout import column_core as C  # noqa: E402
from test_column_edge_solve import make_window, run_pass  # noqa: E402


def _xs(edges):
    return [round(e["x"]) for e in edges]


def test_clamp_packs_out_of_frame_edges_at_min_spacing():
    edges = [{"x": 0.0}, {"x": 240.0}, {"x": 1160.0}, {"x": 2080.0},
             {"x": 1200.0}]
    C._clamp_interior(edges)
    assert _xs(edges) == [0, 240, 1080, 1140, 1200]
    assert all(b["x"] - a["x"] >= C.MIN_COLUMN_WIDTH
               for a, b in zip(edges, edges[1:]))


def test_clamp_leaves_in_frame_edges_alone():
    edges = [{"x": 0.0}, {"x": 240.0}, {"x": 760.0}, {"x": 1280.0},
             {"x": 1800.0}]
    C._clamp_interior(edges)
    assert _xs(edges) == [0, 240, 760, 1280, 1800]


def test_clamp_left_side_too():
    edges = [{"x": 100.0}, {"x": 20.0}, {"x": 50.0}, {"x": 600.0}]
    C._clamp_interior(edges)
    assert _xs(edges) == [100, 160, 220, 600]


class _Cell:
    """Minimal draw_state stand-in for ColumnLayout: the fake window's
    fields plus what the row touches (misc, id, size_change, …)."""
    def __init__(self, window):
        self.parent_window = window
        self._parent = window
        self.misc = {}
        self.id = "row"
        self.column_edges = None
        self.size_change = False
        self.freeze_resize = False
        self.abs_clip_rect = None
        self.height = 400
        self.content_width = window.width
        self._column_container = False

    def hover_eligible(self, rect=None):
        return False

    def get_action(self, *a, **k):
        return None

    def on_action(self, *a, **k):
        return None

    def invalidate(self, note=None):
        pass


def _window(width):
    """A fresh fake window at `width` with NO pre-registered row (the
    edge-solve test's helper seeds one at 100/200 for its own cases)."""
    window, _left, _right, _dividers = make_window(width=width, min_width=200)
    window._edge_views.pop(("row", "r"), None)
    window._parent = None
    return window


def _layout(window, cell):
    import meltygui_imgui as imgui
    imgui.new_frame()
    imgui.begin("Host")
    try:
        row = C.ColumnLayout(cell, 4, column_edges=cell.column_edges,
                             column_widths=[240, None, None, None],
                             resizable=False, border_color=None)
        cell.column_edges = row.edges
        return _xs(row.edges)
    finally:
        imgui.end()
        imgui.end_frame()


def test_pixel_row_keeps_widths_but_stays_in_frame():
    window = _window(1800)
    cell = _Cell(window)
    assert _layout(window, cell) == [0, 240, 760, 1280, 1800]
    window.width = 1000
    run_pass(window)
    # Default physics: the first columns keep their pixels, the tail packs
    # against the frame at MIN spacing — but nothing is left outside it.
    xs = _layout(window, cell)
    assert xs[0] == 0 and xs[-1] == 1000
    assert xs[1] == 240
    assert all(b - a >= C.MIN_COLUMN_WIDTH for a, b in zip(xs, xs[1:]))


def test_per_column_mins_ride_the_edges_and_bound_the_solve():
    """column_mins stamp a "min" on each column's right edge; the contact
    cascade, the wall clamp and the frame-fit clamp all space edges by the
    per-edge floor instead of the flat MIN_COLUMN_WIDTH (the merge window's
    250px file columns / 150px code panes)."""
    window = _window(1800)
    cell = _Cell(window)
    import meltygui_imgui as imgui
    imgui.new_frame()
    imgui.begin("Host")
    try:
        row = C.ColumnLayout(cell, 5,
                             column_widths=[250, None, None, None, 250],
                             column_mins=[250, 150, 150, 150, 250],
                             resizable=False, border_color=None)
        edges = row.edges
        assert [e.get("min") for e in edges] == [None, 250, 150, 150, 150, 250]
    finally:
        imgui.end()
        imgui.end_frame()
    # Contact physics: dragging the right frame edge onto the pile (the
    # left frame edge walled, as the foreign-width solve does) packs at
    # the PER-COLUMN minimums (250+150+150+150+250 = 950), not 5×60.
    C._drag_edge(edges, 5, 300.0, walls=frozenset({id(edges[0])}))
    xs = [round(e["x"]) for e in edges]
    assert xs[0] == 0
    spans = [b - a for a, b in zip(xs, xs[1:])]
    assert spans == [250, 150, 150, 150, 250], spans
    # And the frame-fit clamp packs out-of-frame edges at the same floors.
    wild = [{"x": 0.0}, {"x": 240.0, "min": 250}, {"x": 2000.0, "min": 150},
            {"x": 2100.0, "min": 150}, {"x": 2200.0, "min": 150},
            {"x": 1000.0, "min": 250}]
    C._clamp_interior(wild)
    xs = [round(e["x"]) for e in wild]
    assert xs == [0, 250, 450, 600, 750, 1000], xs
    spans = [b - a for a, b in zip(xs, xs[1:])]
    assert all(span >= floor for span, floor
               in zip(spans, [250, 150, 150, 150, 250])), spans
