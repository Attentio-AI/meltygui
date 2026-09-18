"""The edge motion guard: while a button is held, no edge outruns the pointer.

Run: .venv/bin/pytest tests/test_edge_motion_guard.py -q
"""
import pytest

from meltygui.core.diagnostics import edge_motion_guard as guard
from meltygui.core.layout import edge_constraints
from meltygui.core.melty import Melty
from meltygui.core.windowing import os_frame
from test_column_edge_solve import FakeWindow


class Rig:
    """A window with one column layout, a pointer, and the guard's reports."""

    def __init__(self, monkeypatch):
        self.window = FakeWindow(width=600, x=100.0)
        self.window.name = "win"
        self.left, self.right = {"x": 0.0}, {"x": 600.0}
        self.divider = {"x": 300.0}
        self.window._frame_edges = [self.left, self.right]
        self.window._edge_views[("row", "r")] = (self.window, [self.left, self.divider, self.right])
        self.pointer = [400.0, 200.0]
        self.down = True
        self.reports = []
        monkeypatch.setattr(os_frame, "_all_windows", lambda: [self.window])
        monkeypatch.setattr(os_frame, "_root_windows", lambda: [self.window])
        monkeypatch.setattr(os_frame, "_enabled", lambda: False)
        monkeypatch.setattr(guard, "_enabled", lambda: True)
        monkeypatch.setattr(guard, "_button_down", lambda: self.down)
        monkeypatch.setattr(guard, "_pointer", lambda origin: tuple(self.pointer))
        monkeypatch.setattr(guard, "_emit", self.reports.append)
        guard._STATE.update(gesture=None, watch=set(), watch_until=-1, writes=[], watching=False)
        Melty.frame_count = 100
        self.frame()                                    # the press frame: baselines

    def frame(self, dx=0.0, edge=None, move=0.0):
        Melty.frame_count += 1
        self.pointer[0] += dx
        if edge is not None:
            edge["x"] += move
        guard.check_frame()

    def kinds(self):
        return [v["kind"] for report in self.reports for v in report["violations"]]


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def test_edge_following_the_pointer_is_silent(rig):
    for _ in range(10):
        rig.frame(dx=30.0, edge=rig.divider, move=30.0)
    assert rig.reports == []


def test_contact_stop_and_opposite_edge_flip_are_silent(rig):
    rig.frame(dx=30.0, edge=rig.divider, move=12.0)         # contact frame: part of the way
    rig.frame(dx=30.0, edge=rig.divider, move=0.0)          # blocked: standing still
    rig.frame(dx=30.0, edge=rig.left, move=-30.0)           # blocked far edge: the near edge gives instead
    assert rig.reports == []


def test_edge_faster_than_the_pointer_is_reported_with_state(rig):
    rig.frame(dx=30.0, edge=rig.divider, move=30.0)
    rig.frame(dx=30.0, edge=rig.divider, move=200.0)
    assert rig.kinds() == ["faster than the pointer"]
    report = rig.reports[0]
    violation = report["violations"][0]
    assert violation["edge"] == "win ('row', 'r')[1]" and violation["step"] == 200.0
    assert report["windows"][0]["name"] == "win" and report["windows"][0]["width"] == 600
    assert report["native"]["mode"] in ("walls", "feed", "x11")
    assert any("check_frame" in line for line in report["stack"])


def test_edge_creeping_farther_than_the_pointer_is_reported(rig):
    for _ in range(30):
        rig.frame(dx=10.0, edge=rig.divider, move=13.0)     # each step within the 3-frame allowance
    assert "farther than the pointer" in rig.kinds()


def test_edge_jitter_under_a_still_pointer_is_reported(rig):
    rig.frame(dx=30.0, edge=rig.divider, move=30.0)
    for _ in range(guard.RECENT_FRAMES):
        rig.frame(dx=0.0)
    rig.frame(dx=0.0, edge=rig.right, move=20.0)
    rig.frame(dx=0.0, edge=rig.right, move=-20.0)
    assert rig.kinds()[:1] == ["faster than the pointer"]


def test_new_edges_and_release_reset_baselines(rig):
    late = {"x": 500.0}
    rig.window._edge_views[("row", "late")] = (rig.window, [rig.left, late, rig.right])
    rig.frame(dx=5.0)                                        # first seen: no report
    rig.down = False
    rig.frame(dx=0.0, edge=rig.divider, move=250.0)          # released: not a gesture
    rig.down = True
    rig.frame(dx=0.0)                                        # new press: fresh baselines
    rig.frame(dx=5.0, edge=rig.divider, move=5.0)
    assert rig.reports == []


def test_reports_are_capped_per_gesture(rig):
    for _ in range(guard.MAX_REPORTS_PER_GESTURE + 5):
        rig.frame(dx=1.0, edge=rig.divider, move=50.0)
    assert len(rig.reports) == guard.MAX_REPORTS_PER_GESTURE


def test_solver_stack_is_captured_for_watched_edges(rig):
    rig.frame(dx=30.0, edge=rig.divider, move=200.0)        # first report arms the watch
    assert guard.watching()
    graph = edge_constraints.EdgeGraph([(rig.left, rig.divider, 60.0, None),
                                        (rig.divider, rig.right, 60.0, None)])
    Melty.frame_count += 1
    rig.pointer[0] += 30.0
    assert edge_constraints.solve_edge(graph, rig.divider, rig.divider["x"] + 150.0)
    guard.check_frame()
    writes = rig.reports[-1]["solver_writes"]
    assert writes and any("solve_edge" in line for line in writes[0]["stack"])
    Melty.frame_count += guard.WATCH_FRAMES + 1
    rig.down = False
    guard.check_frame()
    assert not guard.watching()
