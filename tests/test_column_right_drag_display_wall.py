"""A right-drag inside a column, pushed through to the display's left wall.

Lukas, 2026-09-18: "Right click drag resize inside a column. The right edge
of the column pushes the left edge of the parent window left" and, once that
left edge meets the display, "the right edge is flying out".

The rules (docs/WINDOW_COLLISION_COLUMNS.md): compression to a minimum pushes
the opposite edge, the cascade reaches the Melty frame, the native frame and
finally the display. At that immovable boundary only the ACTIVELY DRAGGED
view may switch to its opposite edge; "other edges move through contact or
their existing layout constraints, not because they inherit the active view's
edge switch", and "repeated frames at a stationary cursor must not continue
growing or moving geometry". The window's right edge is neither the column's
opposite edge nor in contact with anything: it stays where it is.

Run: .venv/bin/pytest tests/test_column_right_drag_display_wall.py -q
"""
import pytest

from meltygui.core.diagnostics import edge_motion_guard as guard
from meltygui.core.melty import Melty
from meltygui.core.windowing import os_frame
from test_os_frame import app_frame
from test_os_frame import app_root
from test_os_frame import os_x
from test_os_frame import root
from test_os_frame import studio  # noqa: F401  (fixture)

MIN_COLUMN = 60.0          # column_core.MIN_COLUMN_WIDTH, the floor of an unconfigured column


def _editor(st):
    """A window on the OS window's left edge with three columns, like the
    code editor's shortcuts | commits | editor."""
    window = root(st, x=0.0, width=900, min_width=200, name="editor")
    left, right = window._frame_edges
    first, second = {"x": 300.0}, {"x": 600.0}
    window._edge_views[("row", "columns")] = (window, [left, first, second, right])
    return window, first, second


def _screen_edges(window):
    """The window's left and right edges on the screen."""
    near = os_frame.applied_origin("x") + window.window_pos[0]
    return near, near + window.width


@pytest.fixture
def held(studio, monkeypatch):
    """The right button is held for the whole test: one sticky gesture."""
    monkeypatch.setattr(os_frame, "_any_button_down", lambda: True)
    return studio


def _right_drag(st, window, edge, travel, steps):
    """What core_render queues for a right-drag each frame: the latched edge,
    targeted at where it is plus the pointer's step this frame, cursor-driven.
    Yields the window's screen edges after each frame."""
    window._resize_target_edge, window._resize_from_top_left = edge, False
    for _ in range(steps):
        window._pending_drags.append((edge, edge["x"] + travel / steps, True))
        st.frame(window)
        yield _screen_edges(window)


def test_column_right_drag_into_the_display_wall_never_moves_the_windows_right_edge(held):
    studio = held
    window, first, second = _editor(studio)
    _left, right_at_press = _screen_edges(window)
    assert os_x()[0] == 400.0                  # 400 px of room before the display's left wall
    # The second column's right edge, dragged left by far more than the
    # columns (2 x 240 of slack) and the room to the display (400) can give.
    lefts, rights = zip(*_right_drag(studio, window, second, travel=-1600.0, steps=40))
    assert min(lefts) == 0.0                   # the cascade did reach the display's left wall
    assert os_x()[0] == 0.0
    assert max(rights) <= right_at_press       # ... and the window's right edge never moved out
    assert rights[-1] == right_at_press


def test_in_an_app_root_the_os_windows_right_edge_never_moves(held):
    """The code editor: its root IS the OS window (frame-pinned), so the edge
    that flew out was the native far edge."""
    studio = held
    app = app_root(studio)
    app_frame(studio, app)
    app_frame(studio, app)
    left, right = app._frame_edges
    first, second = {"x": 1000.0}, {"x": 2000.0}
    app._edge_views[("row", "columns")] = (app, [left, first, second, right])
    app._resize_target_edge, app._resize_from_top_left = second, False
    near_at_press, far_at_press = os_x()
    fars = []
    for _ in range(60):
        app._pending_drags.append((second, second["x"] - 60.0, True))   # 3600 px of hand, leftwards
        app_frame(studio, app)
        fars.append(os_x()[1])
    assert os_x()[0] == 0.0 < near_at_press    # the columns pushed the OS window's left edge to the display
    assert max(fars) <= far_at_press           # its right edge never moved out
    settled = (os_x(), [e["x"] for e in app._edge_views[("row", "columns")][1]])
    for _ in range(10):                        # the hand rests
        app._pending_drags.append((second, second["x"], True))
        app_frame(studio, app)
        assert (os_x(), [e["x"] for e in app._edge_views[("row", "columns")][1]]) == settled


def test_holding_the_pointer_still_past_the_wall_moves_nothing(held):
    studio = held
    window, first, second = _editor(studio)
    list(_right_drag(studio, window, second, travel=-1600.0, steps=40))
    settled = (_screen_edges(window), os_x(), [e["x"] for e in window._edge_views[("row", "columns")][1]])
    for _ in range(10):                        # the hand rests: the same target, frame after frame
        window._pending_drags.append((second, second["x"], True))
        studio.frame(window)
        assert (_screen_edges(window), os_x(),
                [e["x"] for e in window._edge_views[("row", "columns")][1]]) == settled


def test_reversing_the_same_drag_restores_the_columns_and_both_frames(held):
    studio = held
    window, first, second = _editor(studio)
    before = (_screen_edges(window), [e["x"] for e in window._edge_views[("row", "columns")][1]])
    list(_right_drag(studio, window, second, travel=-1600.0, steps=40))
    list(_right_drag(studio, window, second, travel=1600.0, steps=40))
    assert (_screen_edges(window), [e["x"] for e in window._edge_views[("row", "columns")][1]]) == before


def test_the_edge_motion_guard_finds_nothing_to_report(held, monkeypatch):
    """The detector itself, unchanged: every edge follows the pointer or
    stands still for the whole gesture."""
    studio = held
    window, first, second = _editor(studio)
    window._resize_target_edge, window._resize_from_top_left = second, False
    reports = []
    pointer = [os_frame.applied_origin("x") + 600.0, 500.0]
    monkeypatch.setattr(guard, "_enabled", lambda: True)
    monkeypatch.setattr(guard, "_button_down", lambda: True)
    monkeypatch.setattr(guard, "_right_down", lambda: True)
    monkeypatch.setattr(guard, "_native_resize_live", lambda: False)
    monkeypatch.setattr(guard, "_pointer", lambda origin: tuple(pointer))
    monkeypatch.setattr(guard, "_surface", lambda: None)
    monkeypatch.setattr(guard, "_emit", reports.append)
    monkeypatch.setattr(Melty, "draw_state_registry", {}, raising=False)
    guard._STATE.update(gestures={}, watch=set(), watch_until=-1, writes=[], watching=False)
    guard.check_frame()                        # the press frame: baselines
    for step in range(1, 81):
        travel = -1600.0 * step / 80
        pointer[0] = 1000.0 + travel           # the hand, in screen coordinates
        window._pending_drags.append((second, second["x"] - 20.0, True))
        studio.frame(window)
        guard.check_frame()
    assert [v["kind"] + ": " + v["edge"] for report in reports for v in report["violations"]] == []
