"""Row edges — the y-axis twin of the column edge system (columns.py):
``{"y": …}`` edges registered on the window's ``_row_views``, the window's
top/bottom FRAME edges as draggable objects, one flat collision solve per
axis in ``window_edge_pass`` (``_frame_pass(window, "y")``).

  - bottom frame edge dragged UP past the compressed row pile (a
    cursor-driven right-drag): the window slides up and keeps min_height —
    no bounce on the following pass
  - top frame edge dragged DOWN (top-left mode): symmetric, the window
    slides down and keeps min_height
  - a foreign (non-cursor) height write below the pile span is walled
  - the two axes never touch each other's edges
  - row_edge_under_cursor: nearest edge below / above, banded on x
  - a real RowLayout adopts the window frame, seeds over the VISIBLE span
    (the lead above the first row raises its floor), and packs into a
    shrunk frame at MIN_ROW_HEIGHT spacing

Run: .venv/bin/python -m pytest tests/test_row_edge_solve.py -q
"""
import os
import sys

import conftest  # noqa: F401,E402

from meltygui.core.layout import column_core as C  # noqa: E402
from test_column_edge_solve import FakeWindow, run_pass  # noqa: E402
from meltygui.core.melty import Melty  # noqa: E402


def make_rows_window(**kw):
    """A fake window with three ROWS: dividers at 150 / 250 inside a
    400-tall frame (min_height 200 by default)."""
    Melty.frame_count = getattr(Melty, "frame_count", 0) or 0
    kw.setdefault("min_height", 200)
    w = FakeWindow(**kw)
    C.window_edge_pass(w)            # seeds both frame pairs
    top, bottom = w._frame_rows
    dividers = [{"y": 150.0}, {"y": 250.0}]
    w._row_views[("rows", "r")] = (w, [top, dividers[0], dividers[1], bottom])
    return w, top, bottom, dividers


def _ys(edges):
    return [round(e["y"]) for e in edges]


def test_bottom_edge_dragged_up_past_pile_slides_window_without_bounce():
    w, top, bottom, (d0, d1) = make_rows_window()
    w._pending_row_drags.append((bottom, 150.0, True))   # cursor-driven, 250 px up
    run_pass(w)
    # Pile compressed (40 px min rows), frame pair held at min_height 200:
    # the TOP edge was pushed to 150-200 = -50 and the window slid by it.
    assert (w.window_pos[1], w.height) == (0.0, 200)
    assert (top["y"], bottom["y"]) == (0.0, 200.0)
    assert (d0["y"], d1["y"]) == (120.0, 160.0)
    # x untouched.
    assert (w.window_pos[0], w.width) == (100.0, 300)
    # Next frame, no cursor input: nothing moves.
    before = (w.window_pos, w.height, top["y"], bottom["y"], d0["y"], d1["y"])
    run_pass(w)
    assert before == (w.window_pos, w.height, top["y"], bottom["y"], d0["y"], d1["y"])
    # Keep dragging up: the window keeps sliding 1:1, height pinned.
    w._pending_row_drags.append((bottom, bottom["y"] - 30.0, True))
    run_pass(w)
    assert (w.window_pos[1], w.height) == (-30.0, 200)


def test_top_edge_dragged_down_past_pile_slides_window_symmetrically():
    w, top, bottom, (d0, d1) = make_rows_window()
    w._pending_row_drags.append((top, 300.0, True))      # top-left mode: top edge → down
    run_pass(w)
    # 300 + 3×40 = 420 > 400: the bottom frame edge is pushed to 420, then
    # min_height 200 pulls it to 500; the window slides down by the top's
    # 300 and re-bases.
    assert (w.window_pos[1], w.height) == (350.0, 200)
    assert (top["y"], bottom["y"]) == (0.0, 200.0)
    assert (d0["y"], d1["y"]) == (40.0, 80.0)
    before = (w.window_pos, w.height)
    run_pass(w)
    assert before == (w.window_pos, w.height)


def test_foreign_height_below_pile_span_is_walled():
    w, top, bottom, (d0, d1) = make_rows_window(min_height=100)
    w.height = 100                                       # an outside writer shrinks the window
    run_pass(w)
    # The invariant drag of the bottom edge stops at the fully-compressed
    # span (3 × 40) instead of shoving the top frame edge: height grows back.
    assert (top["y"], bottom["y"]) == (0.0, 120.0)
    assert (d0["y"], d1["y"]) == (40.0, 80.0)
    assert (w.window_pos[1], w.height) == (50.0, 120)
    # And the window's min_height was floored at the pile.
    assert w.min_height == 120


def test_axes_never_touch_each_other():
    w, top, bottom, (r0, r1) = make_rows_window()
    left, right = w._frame_edges
    c0, c1 = {"x": 100.0}, {"x": 200.0}
    w._edge_views[("row", "c")] = (w, [left, c0, c1, right])
    w._pending_drags.append((c1, 150.0))                 # a column drag …
    run_pass(w)
    assert (c0["x"], c1["x"]) == (90.0, 150.0)
    assert (r0["y"], r1["y"]) == (150.0, 250.0)          # … moves no row edge
    w._pending_row_drags.append((r0, 230.0))             # a row drag …
    run_pass(w)
    assert (r0["y"], r1["y"]) == (230.0, 270.0)
    assert (c0["x"], c1["x"]) == (90.0, 150.0)           # … moves no column edge


class _Band:
    """A rows host whose edges span an x band (its clip)."""
    def __init__(self, clip):
        self.abs_clip_rect = clip
        self.abs_left, self.abs_top = clip[0], clip[1]
        self.closed, self.size_change = False, False


def test_row_edge_under_cursor_bands_on_x_and_falls_back_to_the_frame():
    w, top, bottom, (r0, r1) = make_rows_window()
    # The registered rows view spans x 100..400 (absolute).
    host = _Band((100.0, 50.0, 400.0, 450.0))
    w._row_views[("rows", "r")] = (host, [top, r0, r1, bottom])
    # Cursor at window y 200 inside the band: nearest edge below is r1
    # (250), above is r0 (150).
    assert C.row_edge_under_cursor(w, 200.0, 250.0) is r1
    assert C.row_edge_under_cursor(w, 200.0, 250.0, above=True) is r0
    # Outside the band on x: only the window's own frame entry qualifies
    # (its clip is None → abs_left + measured width) — the frame edges.
    assert C.row_edge_under_cursor(w, 200.0, 900.0) is bottom
    assert C.row_edge_under_cursor(w, 200.0, 900.0, above=True) is top
    # Below the last interior edge → the bottom frame edge.
    assert C.row_edge_under_cursor(w, 300.0, 250.0) is bottom


class _RowsCell:
    """Minimal draw_state stand-in for RowLayout."""
    def __init__(self, window):
        self.parent_window = window
        self._parent = window
        self.misc = {}
        self.id = "rows"
        self.row_edges = None
        self.size_change = False
        self.freeze_resize = False
        self.abs_clip_rect = None
        self.abs_left, self.abs_top = window.abs_left, window.abs_top
        self.height = None
        self.content_width = window.width
        self._row_container = False

    def hover_eligible(self, rect=None):
        return False

    def get_action(self, *a, **k):
        return None

    def on_action(self, *a, **k):
        return None

    def invalidate(self, note=None):
        pass


def _rows_window(height):
    Melty.frame_count = getattr(Melty, "frame_count", 0) or 0
    w = FakeWindow(height=height, min_height=100)
    C.window_edge_pass(w)
    w._parent = None
    return w


def _layout(window, cell, lead=30.0):
    """Build a RowLayout with the flow cursor `lead` px below the window
    top (a header line above the rows), as a body would."""
    import meltygui_imgui as imgui
    imgui.new_frame()
    imgui.begin("Host")
    try:
        imgui.set_cursor_screen_pos((window.abs_left + 10, window.abs_top + lead))
        rows = C.RowLayout(cell, 3, row_edges=cell.row_edges,
                           row_heights=[100, None, None],
                           resizable=False, border_color=None)
        cell.row_edges = rows.edges
        return rows
    finally:
        imgui.end()
        imgui.end_frame()


def test_row_layout_adopts_the_frame_and_seeds_over_the_visible_span():
    window = _rows_window(400)
    cell = _RowsCell(window)
    rows = _layout(window, cell)
    top, bottom = window._frame_rows
    # Far edges ARE the window's frame objects (by reference).
    assert rows.edges[0] is top and rows.edges[-1] is bottom
    assert rows.owned == [False, True, True, False]
    # Seeded below the 30 px lead: 100 fixed, the rest split the 270 left.
    assert _ys(rows.edges) == [0, 130, 265, 400]
    # The lead raises the first row's floor so the VISIBLE row keeps
    # MIN_ROW_HEIGHT when the top frame edge comes down.
    assert rows.edges[1]["min"] == C.MIN_ROW_HEIGHT + 30
    # Registered on the window's y registry under the ("rows", id) key.
    assert window._row_views[("rows", "rows")][1] is rows.edges
    # The window's min_height floors at the pile: lead+40 + 40 + 40.
    run_pass(window)
    assert window.min_height >= 150


def test_row_layout_packs_into_a_shrunk_frame():
    window = _rows_window(400)
    cell = _RowsCell(window)
    _layout(window, cell)
    window.height = 200                                  # frame shrinks
    run_pass(window)
    # Bottom edge dragged to 200 (top walled): the pile packs upward at
    # 40 px and the first row, floored at 70 (lead 30 + 40), keeps its 120.
    assert _ys(cell.row_edges) == [0, 120, 160, 200]
    # Re-laying out with the persisted edges changes nothing.
    rows = _layout(window, cell)
    assert _ys(rows.edges) == [0, 120, 160, 200]
    assert all(b["y"] - a["y"] >= C.MIN_ROW_HEIGHT
               for a, b in zip(rows.edges[1:], rows.edges[2:]))


# --- detached outer edges: a layout hung on its own far-edge dicts still pushes the frame -----

def _detached_rows_window():
    """A window with a rows layout built on its OWN top / bottom dicts
    (like the chat sidebar's RowLayout(top_edge={...}, bottom_edge={...})):
    rows at 75..185..400 with floors 110 / 80, the window 500 tall."""
    from test_column_edge_solve import FakeWindow
    Melty.frame_count = (getattr(Melty, "frame_count", 0) or 0) + 1
    w = FakeWindow(width=300, height=500, min_height=100, x=0.0, y=0.0)
    C.window_edge_pass(w)
    top, mid, bottom = {"y": 75.0}, {"y": 185.0}, {"y": 400.0}
    w._row_views[("rows", "sidebar")] = (w, [top, mid, bottom])
    w._row_cells[("rows", "sidebar")] = ([110.0, 80.0], [None, None])
    return w, top, mid, bottom


def test_a_detached_layouts_push_stops_at_its_own_edge():
    """A layout hung on its own far-edge dicts is not linked to the frame:
    the interior edge dragged up past the first row's floor packs the
    layout (its own top edge takes the remainder, nothing behind it) and
    the WINDOW is untouched — the layout re-dictates that edge on its
    next build, so a frame push from here would be thrown away."""
    w, top, mid, bottom = _detached_rows_window()
    frame_top, frame_bottom = w._frame_rows
    w._pending_row_drags.append((mid, 185.0 - 100.0, True))
    Melty.frame_count += 1
    C.window_edge_pass(w)
    assert (w.window_pos[1], w.height) == (0.0, 500)
    assert frame_top["y"] == 0.0 and frame_bottom["y"] == 500.0
    assert mid["y"] == 85.0 and top["y"] == -25.0                  # the layout's own edge took the push


def test_the_right_drag_latch_skips_a_detached_outer_edge_for_the_frame():
    """A right-drag whose innermost cell ends at a layout's DETACHED outer
    edge takes the window's frame edge on that side (the hand sees the
    window end there); an interior edge, or an outer edge shared with
    another list, is latched as before."""
    w, top, mid, bottom = _detached_rows_window()
    w._row_bands[("rows", "sidebar")] = ({"x": 0.0}, {"x": 300.0})
    frame_top, frame_bottom = w._frame_rows
    assert C.row_edge_under_cursor(w, 300.0, 100.0) is frame_bottom     # below `mid`: the detached bottom → the frame
    assert C.row_edge_under_cursor(w, 300.0, 100.0, above=True) is mid  # above: the interior edge
    assert C.row_edge_under_cursor(w, 100.0, 100.0, above=True) is frame_top   # the detached top → the frame
    # shared with a second list: no longer detached
    w._row_views[("rows", "other")] = (w, [bottom, {"y": 450.0}])
    w._row_bands[("rows", "other")] = ({"x": 0.0}, {"x": 300.0})
    assert C.row_edge_under_cursor(w, 300.0, 100.0) is bottom

