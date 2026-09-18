"""Windows drawn with ``unmanaged=True`` stay outside the edge solver.

A RenderHost parks its 40 px envelope left of the display. Solving it floored
the width at the axis minimum and native containment pushed it back inside
the surface every frame: a per-frame feedback loop and a sliver flickering
down the left edge during native resizes.

Run: .venv/bin/pytest tests/test_unmanaged_window_frames.py -q
"""
from meltygui.core.layout import column_core as C
from meltygui.core.windowing import os_frame
from test_column_edge_solve import FakeWindow


def _stub(unmanaged):
    w = FakeWindow(width=40, min_width=0, x=-60.0, height=40, min_height=0, y=100.0)
    w.closable = True
    w._kwargs = {"unmanaged": True} if unmanaged else {}
    return w


def _run(w, frames=3):
    for _ in range(frames):
        from meltygui.core.melty import Melty
        Melty.frame_count = (getattr(Melty, "frame_count", 0) or 0) + 1
        C.window_edge_pass(w)
        w.abs_left, w.abs_top = w.window_pos


def test_unmanaged_stub_keeps_its_caller_geometry():
    w = _stub(unmanaged=True)
    _run(w)
    assert (w.window_pos, w.width, w.height) == ((-60.0, 100.0), 40, 40)
    assert w._frame_edges is None and w._frame_rows is None
    assert w.invalidations == 0


def test_unmanaged_window_is_not_a_native_containment_participant(monkeypatch):
    stub, managed = _stub(unmanaged=True), _stub(unmanaged=False)
    monkeypatch.setattr(os_frame, "_movable_roots", lambda: [stub, managed])
    monkeypatch.setattr(os_frame, "_all_windows", lambda: [stub, managed])
    assert os_frame._open(managed)
    assert not os_frame._open(stub)
    assert os_frame._colliding_windows() == [managed]
