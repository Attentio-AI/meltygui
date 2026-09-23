"""Window frame-edge solve (columns.window_edge_pass / _solve_collisions):
a right-drag corner resize queues the latched edge as a CURSOR-DRIVEN drag.

  - right frame edge dragged LEFT past the compressed pile: the window
    slides left and keeps min_width — no bounce on the following pass
    (before: the wrapper's min_width re-stamp and the foreign-width
    invariant yanked the right edge back every frame)
  - left frame edge dragged RIGHT (top-left mode): symmetric, the window
    slides right and keeps min_width
  - a foreign (non-cursor) width write below the pile span is still walled

Run: .venv/bin/python -m pytest tests/test_column_edge_solve.py -q
"""
import os
import sys


from meltygui.core.layout import column_core as C
from meltygui.core.melty import Melty


class FakeWindow:
    """Just what window_edge_pass touches."""
    def __init__(self, width=300, min_width=200, x=100.0, height=400,
                 min_height=100, y=50.0):
        self.id, self.name = "win", "win"
        self.width, self.height, self.min_width = width, height, min_width
        self.min_height = min_height
        self.window_pos = (x, y)
        self.abs_left, self.abs_top = x, y
        self.expanded, self.closed, self.size_change = True, False, False
        self._edge_views, self._pending_drags, self._frame_edges = {}, [], None
        self._row_views, self._pending_row_drags, self._frame_rows = {}, [], None
        self._edges_frame = None
        self.invalidations = 0

    def get_action(self, *a, **k):
        return None

    def on_action(self, *a, **k):
        return None

    def invalidate(self, note=None):
        self.invalidations += 1


def make_window(**kw):
    Melty.frame_count = getattr(Melty, "frame_count", 0) or 0
    w = FakeWindow(**kw)
    C.window_edge_pass(w)            # seeds the frame edges at [0, width]
    left, right = w._frame_edges
    dividers = [{"x": 100.0}, {"x": 200.0}]
    w._edge_views[("row", "r")] = (w, [left, dividers[0], dividers[1], right])
    return w, left, right, dividers


def run_pass(w):
    Melty.frame_count += 1
    C.window_edge_pass(w)
    # What the wrapper does after the pass, every frame.
    w.width = max(w.width, w.min_width)
    w.height = max(w.height, w.min_height)
    w.abs_left, w.abs_top = w.window_pos


def test_right_edge_dragged_left_past_pile_slides_window_without_bounce():
    w, left, right, (d0, d1) = make_window()
    w._pending_drags.append((right, 150.0, True))      # cursor-driven, 150 px left
    run_pass(w)
    # Pile compressed (60 px min columns), frame pair held at min_width 200:
    # the LEFT edge was pushed to 150-200 = -50 and the window slid by it.
    assert (w.window_pos[0], w.width) == (50.0, 200)
    assert (left["x"], right["x"]) == (0.0, 200.0)
    assert (d0["x"], d1["x"]) == (80.0, 140.0)
    # Next frame, no cursor input: nothing moves — width already ≥ min_width,
    # so the wrapper re-stamp queues no foreign drag against the cursor edge.
    before = (w.window_pos, w.width, left["x"], right["x"], d0["x"], d1["x"])
    run_pass(w)
    assert before == (w.window_pos, w.width, left["x"], right["x"], d0["x"], d1["x"])
    # Keep dragging left: the window keeps sliding 1:1, width pinned.
    w._pending_drags.append((right, right["x"] - 30.0, True))
    run_pass(w)
    assert (w.window_pos[0], w.width) == (20.0, 200)


def test_left_edge_dragged_right_past_pile_slides_window_symmetrically():
    w, left, right, (d0, d1) = make_window()
    w._pending_drags.append((left, 150.0, True))       # top-left mode: left edge → right
    run_pass(w)
    assert (w.window_pos[0], w.width) == (250.0, 200)
    assert (left["x"], right["x"]) == (0.0, 200.0)
    assert (d0["x"], d1["x"]) == (60.0, 120.0)
    before = (w.window_pos, w.width)
    run_pass(w)
    assert before == (w.window_pos, w.width)


def test_foreign_width_below_pile_span_is_walled():
    w, left, right, (d0, d1) = make_window(min_width=100)
    w.width = 100                                       # an outside writer shrinks the window
    run_pass(w)
    # The invariant drag of the right edge stops at the fully-compressed span
    # (3 × 60) instead of shoving the left frame edge: width grows back.
    assert (left["x"], right["x"]) == (0.0, 180.0)
    assert (d0["x"], d1["x"]) == (60.0, 120.0)
    assert (w.window_pos[0], w.width) == (100.0, 180)


def test_frame_hit_regions_use_both_solved_dimensions(monkeypatch):
    window = FakeWindow(width=300, height=400)
    C.window_edge_pass(window)
    rectangles = {}
    def register(events, **kwargs):
        for event in (events,) if isinstance(events, str) else events:
            rectangles[event, kwargs['view_id']] = kwargs['rect']
    monkeypatch.setattr(window, 'on_action', register)
    window._pending_drags.append((window._frame_edges[1], 360., True))
    window._pending_row_drags.append((window._frame_rows[1], 450., True))
    run_pass(window)
    assert (window.width, window.height) == (360, 450)
    half = C.EDGE_GRAB_WIDTH / 2
    x, y = window.window_pos
    assert rectangles['left_mouse_drag', 'win_edge_x_1'] == (x+360-half, y, x+360+half, y+450)
    assert rectangles['left_mouse_drag', 'win_edge_y_1'] == (x, y+450-half, x+360, y+450+half)



def test_window_minimum_follows_the_pile_back_down():
    """The frame pass floors min_width at the compressed pile of the
    window's layouts. That floor must come back DOWN when a layout loses a
    cell: raise-only, a tile column added and joined away kept the window
    at five columns' floor for good (the tiles demo, 09-21)."""
    w, left, right, (d0, d1) = make_window(min_width=100)   # 3 columns: pile 180
    run_pass(w)
    assert w.min_width == 180
    d2 = {"x": 250.0}                                        # a fourth column
    w._edge_views[("row", "r")] = (w, [left, d0, d1, d2, right])
    run_pass(w)
    assert w.min_width == 240
    w._edge_views[("row", "r")] = (w, [left, d0, d1, right])  # joined away again
    run_pass(w)
    assert w.min_width == 180
    # A minimum the wrapper re-stamps from its kwarg every frame is the
    # declared base: the floor is the larger of it and the pile.
    for edges, expected in (([left, d0, d1, right], 200),
                            ([left, d0, d1, d2, right], 240),
                            ([left, d0, d1, right], 200)):
        w._edge_views[("row", "r")] = (w, edges)
        w.min_width = 200
        run_pass(w)
        assert w.min_width == expected
