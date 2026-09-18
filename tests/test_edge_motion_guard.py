"""The edge motion guard: while a button is held, no edge outruns or lags the pointer.

Run: .venv/bin/pytest tests/test_edge_motion_guard.py -q
"""
import pytest

from meltygui.core.diagnostics import edge_motion_guard as guard
from meltygui.core.layout import column_core
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
        self.native_resize = False
        monkeypatch.setattr(guard, "_native_resize_live", lambda: self.native_resize)
        self.right_button = False
        monkeypatch.setattr(guard, "_right_down", lambda: self.right_button)
        monkeypatch.setattr(guard, "_pointer", lambda origin: tuple(self.pointer))
        monkeypatch.setattr(guard, "_emit", self.reports.append)
        guard._STATE.update(gestures={}, watch=set(), watch_until=-1, writes=[], watching=False)
        monkeypatch.setattr(guard, "_surface", lambda: None)
        monkeypatch.setattr(guard, "_unapplied", lambda: (0.0, 0.0))
        monkeypatch.setattr(Melty, "draw_state_registry", {}, raising=False)
        self.clock = [1000.0]
        monkeypatch.setattr(guard, "_now", lambda: self.clock[0])
        Melty.frame_count = 100
        self.frame()                                    # the press frame: baselines

    def frame(self, dx=0.0, edge=None, move=0.0, dy=0.0, axis="x"):
        Melty.frame_count += 1
        self.pointer[0] += dx
        self.pointer[1] += dy
        if edge is not None:
            edge[axis] += move
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


def test_contact_start_then_full_speed_is_silent(rig):
    rig.frame(dx=30.0)                                      # a pushed edge not yet reached
    rig.frame(dx=30.0, edge=rig.right, move=18.0)           # contact: picks up part way
    for _ in range(4):
        rig.frame(dx=30.0, edge=rig.right, move=30.0)       # then at the pointer's speed
    assert rig.reports == []


def test_contact_end_then_still_is_silent(rig):
    for _ in range(4):
        rig.frame(dx=30.0, edge=rig.divider, move=30.0)
    rig.frame(dx=30.0, edge=rig.divider, move=9.0)          # meets a barrier part way
    for _ in range(4):
        rig.frame(dx=30.0)                                  # blocked: still
    assert rig.reports == []


def test_acknowledgement_lag_alternation_is_silent(rig):
    # A native move landing a frame before the pointer's local shift makes
    # the measured pointer alternate 2x / 0 while the edge moves x each frame.
    for k in range(12):
        rig.frame(dx=0.0 if k % 2 else 24.0, edge=rig.divider, move=12.0)
    assert rig.reports == []


def test_edge_lagging_a_fast_hand_is_reported(rig):
    for _ in range(4):
        rig.frame(dx=30.0, edge=rig.divider, move=22.0)     # 73% of the hand, chunk after chunk
    assert rig.kinds() == ["slower than the pointer"]
    violation = rig.reports[0]["violations"][0]
    assert [c["kind"] for c in violation["chunks"]] == ["partial", "partial", "partial"]


def test_edge_lagging_a_slow_hand_is_reported(rig):
    # Under the per-frame tolerance every frame looks still; the chunks add up.
    for _ in range(60):
        rig.frame(dx=1.5, edge=rig.divider, move=1.1)
    assert "slower than the pointer" in rig.kinds()


def test_part_way_between_two_moving_chunks_is_reported(rig):
    for move in (30.0, 12.0, 30.0, 30.0):
        rig.frame(dx=30.0, edge=rig.divider, move=move)
    assert rig.kinds() == ["slower than the pointer"]


def test_edge_trailing_the_hand_by_one_frame_is_silent(rig):
    steps = [26.0, 32.0, 26.0, 40.0, 30.0, 36.0, 28.0]
    previous = 26.0
    for dx in steps:
        rig.frame(dx=dx, edge=rig.divider, move=previous)   # this frame moves by last frame's pointer step
        previous = dx
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
    rig.frame(dx=0.0, edge=rig.divider, move=250.0)          # released: the gesture ends at once
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


def test_window_without_an_edge_pass_does_not_break_a_report(rig, monkeypatch):
    class Bare:
        name, id, width, height, window_pos = "bare", "bare", 200, 100, (0.0, 0.0)
        abs_left = abs_top = 0
        expanded, closed = True, False
    bare = Bare()
    monkeypatch.setattr(os_frame, "_all_windows", lambda: [rig.window, bare])
    rig.frame(dx=30.0, edge=rig.divider, move=30.0)
    rig.frame(dx=30.0, edge=rig.divider, move=200.0)
    assert rig.kinds() == ["faster than the pointer"]
    assert [w["name"] for w in rig.reports[0]["windows"]] == ["win", "bare"]


def test_row_edges_are_checked_on_their_axis(rig):
    top, bottom, row = {"y": 0.0}, {"y": 400.0}, {"y": 200.0}
    rig.window._frame_rows = [top, bottom]
    rig.window._row_views[("rows", "r")] = (rig.window, [top, row, bottom])
    rig.frame(dy=20.0)                                           # first seen: baseline
    rig.frame(dy=20.0, edge=row, move=20.0, axis="y")
    rig.frame(dy=20.0, edge=row, move=90.0, axis="y")
    assert [(v["axis"], v["kind"]) for v in rig.reports[0]["violations"]] == [("y", "faster than the pointer")]
    assert rig.reports[0]["violations"][0]["edge"] == "win ('rows', 'r')[1]"


def test_layout_registered_on_an_ordinary_view_is_checked(rig, monkeypatch):
    class View:
        name, id, width, height = "panel", "panel", 300, 200
        abs_left, abs_top = 120, 40
        parent_window, closable, closed = rig.window, False, False
        _edge_views, _row_views = {}, {}
    panel = View()
    divider = {"x": 150.0}
    panel._edge_views[("row", "p")] = (panel, [{"x": 0.0}, divider, {"x": 300.0}])
    column_core._ensure_window_state(panel)                      # what a layout does to its coordinate owner
    rig.frame(dx=10.0)                                           # first seen: baseline
    rig.frame(dx=10.0, edge=divider, move=10.0)
    rig.frame(dx=10.0, edge=divider, move=80.0)
    assert [v["edge"] for v in rig.reports[0]["violations"]] == ["panel ('row', 'p')[1]"]


def test_each_surface_keeps_its_own_gesture(rig, monkeypatch):
    class Child:
        title = "picker"
    child = Child()
    picker = FakeWindow(width=400, x=0.0)
    picker.name = "picker-body"
    left, right, divider = {"x": 0.0}, {"x": 400.0}, {"x": 200.0}
    picker._frame_edges = [left, right]
    picker._edge_views[("row", "p")] = (picker, [left, divider, right])
    active = [None]
    monkeypatch.setattr(guard, "_surface", lambda: active[0])
    monkeypatch.setattr(os_frame, "_all_windows", lambda: [picker] if active[0] is child else [rig.window])
    rig.frame(dx=30.0, edge=rig.divider, move=30.0)             # root surface gesture under way
    active[0] = child
    rig.frame(dx=30.0)                                           # child's first frame: baselines only
    assert rig.reports == [] and len(guard._STATE["gestures"]) == 2
    rig.frame(dx=30.0, edge=divider, move=200.0)
    assert [r["surface"] for r in rig.reports] == ["picker"]
    active[0] = None
    rig.frame(dx=30.0, edge=rig.divider, move=90.0)              # root: 90 within its 3-frame allowance
    rig.frame(dx=30.0, edge=rig.divider, move=30.0)
    assert len(rig.reports) == 1


def test_surface_bound_frame_pair_is_left_to_the_native_pair(rig):
    rig.window._frame_pinned = True                          # the app root: its frame is the native frame
    rig.frame(dx=30.0, edge=rig.divider, move=30.0)
    rig.frame(dx=30.0, edge=rig.right, move=200.0)           # acknowledged size landing late
    assert rig.reports == []
    rig.frame(dx=30.0, edge=rig.divider, move=200.0)         # a layout edge is still judged
    assert rig.kinds() == ["faster than the pointer"]


def test_lag_under_our_own_request_in_flight_is_not_judged(rig, monkeypatch):
    native = {"x": [{"x": 100.0}, {"x": 700.0}], "y": [{"y": 50.0}, {"y": 450.0}]}
    monkeypatch.setattr(os_frame, "_enabled", lambda: True)
    monkeypatch.setattr(os_frame, "edges", lambda axis: native[axis])
    monkeypatch.setattr(os_frame, "applied_origin", lambda axis: 0.0)
    monkeypatch.setattr(guard, "_unapplied", lambda: (-30.0, 0.0))     # a near-edge push in flight
    rig.frame(dx=-30.0)
    for _ in range(30):
        native["x"][0]["x"] -= 30.0
        rig.frame(dx=-30.0, edge=rig.left, move=-15.0)
    assert rig.reports == []


def test_compositor_resize_with_the_hand_on_a_button_is_still_judged(rig, monkeypatch):
    native = {"x": [{"x": 100.0}, {"x": 700.0}], "y": [{"y": 50.0}, {"y": 450.0}]}
    monkeypatch.setattr(os_frame, "_enabled", lambda: True)
    monkeypatch.setattr(os_frame, "edges", lambda axis: native[axis])
    monkeypatch.setattr(os_frame, "applied_origin", lambda axis: 0.0)
    rig.frame(dx=-30.0)
    for _ in range(30):
        native["x"][0]["x"] -= 30.0                                      # no request of ours: the compositor moves it
        rig.frame(dx=-30.0, edge=rig.left, move=-15.0)
    assert "slower than the pointer" in rig.kinds()
    assert all(v["edge"] != "native near" for r in rig.reports for v in r["violations"])


def test_trailing_by_one_chunk_under_acceleration_is_silent(rig):
    speeds = [30.0, 36.0, 42.0, 48.0, 54.0, 60.0, 66.0]
    previous = 30.0
    for dx in speeds:
        rig.frame(dx=dx, edge=rig.divider, move=previous)
        previous = dx
    assert rig.reports == []


def test_window_on_its_first_frames_is_not_judged(rig):
    rig.window.frame_count = 1                              # still fitting itself
    rig.frame(dx=30.0, edge=rig.divider, move=30.0)
    rig.frame(dx=30.0, edge=rig.divider, move=300.0)
    assert rig.reports == []


def test_moving_surface_without_native_motion_is_not_judged(rig, monkeypatch):
    monkeypatch.setattr(guard, "_unapplied", lambda: (-12.0, 0.0))
    for _ in range(12):
        rig.frame(dx=30.0, edge=rig.divider, move=22.0)
    assert rig.reports == []


class NativeRig(Rig):
    """A compositor-driven resize: no button, the native far edge moves."""

    def __init__(self, monkeypatch):
        super().__init__(monkeypatch)
        self.native = {"x": [{"x": 100.0}, {"x": 700.0}], "y": [{"y": 50.0}, {"y": 450.0}]}
        monkeypatch.setattr(os_frame, "_enabled", lambda: True)
        monkeypatch.setattr(os_frame, "edges", lambda axis: self.native[axis])
        monkeypatch.setattr(os_frame, "applied_origin", lambda axis: 0.0)
        self.down = False
        self.native_resize = True
        self.frame()                                            # arms the native-driven gesture

    def native_frame(self, far=0.0, edge=None, move=0.0, axis="x"):
        self.native["x"][1]["x"] += far
        self.frame(edge=edge, move=move, axis=axis)


@pytest.fixture
def native(monkeypatch):
    return NativeRig(monkeypatch)


def test_native_resize_arms_a_native_driven_gesture(native):
    assert guard._STATE["gestures"] and next(iter(guard._STATE["gestures"].values()))["driver"] == "native"


def test_edges_following_or_ignoring_the_native_edge_are_silent(native):
    for _ in range(6):
        native.native_frame(far=-30.0, edge=native.right, move=-30.0)   # the frame's far edge rides the native edge
    for _ in range(6):
        native.native_frame(far=-30.0)                                   # the divider stands still
    assert native.reports == []


def test_edge_outrunning_the_native_edge_is_reported(native):
    native.native_frame(far=-30.0, edge=native.right, move=-30.0)
    native.native_frame(far=-30.0, edge=native.divider, move=-150.0)
    assert native.kinds() == ["faster than the pointer"]
    assert native.reports[0]["driver"] == "native"


def test_edge_lagging_the_native_edge_is_reported(native):
    for _ in range(4):
        native.native_frame(far=-30.0, edge=native.divider, move=-21.0)  # 70% of the native edge, chunk after chunk
    assert native.kinds() == ["slower than the pointer"]


def test_native_edges_themselves_are_never_judged(native):
    for _ in range(4):
        native.native_frame(far=-40.0)
    assert native.reports == []


def test_native_gesture_survives_a_pause_between_configure_bursts(native):
    native.native_frame(far=-30.0, edge=native.divider, move=-21.0)
    native.native_resize = False                                 # the settle window closed
    native.clock[0] += 0.3
    native.native_frame(far=-30.0, edge=native.divider, move=-21.0)   # the next burst
    native.native_resize = True
    for _ in range(3):
        native.native_frame(far=-30.0, edge=native.divider, move=-21.0)
    assert len(guard._STATE["gestures"]) == 1
    assert "slower than the pointer" in native.kinds()


def test_native_gesture_ends_after_the_idle_time(native):
    native.native_resize = False
    native.clock[0] += guard.IDLE_S + 0.1
    native.frame(edge=native.divider, move=200.0)
    assert native.reports == [] and not guard._STATE["gestures"]


def test_button_flicker_during_a_native_resize_keeps_one_gesture(native):
    # An edge drag crossing the window edge drops and re-takes the press;
    # the native resize keeps landing throughout. Baselines must survive.
    for k in range(9):
        native.down = bool(k % 2)
        native.native_frame(far=-30.0, edge=native.divider, move=-21.0)
    assert len(guard._STATE["gestures"]) == 1
    assert native.kinds() and set(native.kinds()) == {"slower than the pointer"}


class PushRig(Rig):
    """A right-drag: the divider is latched, the right edge sits 240 px past
    it with a 60 px floor between them, the pointer is on the divider."""

    def __init__(self, monkeypatch):
        super().__init__(monkeypatch)
        self.right_button = True
        self.window._edge_cells = {("row", "r"): ([60.0, 60.0], [None, None])}
        self.pointer[0] = 100.0 + self.divider["x"]           # window x + divider: the pointer is on it
        self.window._resize_target_edge = self.divider
        self.frame()                                           # right button seen: the rule is armed


@pytest.fixture
def push(monkeypatch):
    return PushRig(monkeypatch)


def test_latched_edge_and_contact_pushes_are_silent(push):
    for _ in range(8):
        push.frame(dx=30.0, edge=push.divider, move=30.0)      # the latched divider follows the pointer
    push.right_at_floor = push.divider["x"] + 60.0
    push.right["x"] = push.right_at_floor                     # the cell reaches its floor
    for _ in range(4):
        push.divider["x"] += 30.0
        push.frame(dx=30.0, edge=push.right, move=30.0)        # pushed at the floor: contact
    assert push.reports == []


def test_edge_moving_with_the_pointer_without_contact_is_reported(push):
    for _ in range(4):
        push.divider["x"] += 30.0
        push.frame(dx=30.0, edge=push.right, move=30.0)        # 240 px of slack: nothing pushes it
    assert "pushed without contact" in push.kinds()
    violation = next(v for r in push.reports for v in r["violations"] if v["kind"] == "pushed without contact")
    assert violation["edge"] == "win frame[1]" and violation["pointer_distance"] > guard.NEAR_POINTER_PX


def test_opposite_motion_and_left_button_are_exempt(push):
    for _ in range(4):
        push.divider["x"] += 30.0
        push.frame(dx=30.0, edge=push.left, move=-30.0)        # the flip: opposite to the pointer
    push.right_button = False
    for _ in range(4):
        push.frame(dx=30.0, edge=push.right, move=30.0)        # a left-button drag: rule inactive
    assert push.reports == []


def test_edge_near_the_pointer_may_move(push):
    push.window._resize_target_edge = None
    push.pointer[0] = 100.0 + push.right["x"] - 20.0           # the hand is right by the right edge
    push.frame()
    for _ in range(4):
        push.frame(dx=30.0, edge=push.right, move=30.0)
    assert push.reports == []


def test_sticky_reversal_restoring_a_pushed_edge_is_exempt(push):
    push.down = False
    push.frame()                                               # released: arrange a cell at its floor
    push.right["x"] = push.divider["x"] + 60.0
    push.down = True
    push.frame()                                               # pressed again: fresh baselines
    for _ in range(4):
        push.divider["x"] += 30.0
        push.frame(dx=30.0, edge=push.right, move=30.0)        # pushed in contact
    for _ in range(4):
        push.divider["x"] -= 30.0
        push.frame(dx=-30.0, edge=push.right, move=-30.0)      # hand reverses: the push is restored
    assert push.reports == []


def test_pointer_gesture_ends_at_release_before_the_idle_time(push):
    push.frame(dx=30.0, edge=push.divider, move=30.0)
    push.down = push.right_button = False
    push.clock[0] += 0.2                                       # under IDLE_S: still ends at once
    push.frame()
    assert not guard._STATE["gestures"]


def test_near_edge_moving_at_the_declared_minimum_with_column_slack_is_reported(push):
    # The window's own frame cell sits at its declared minimum; the tiles
    # between the edges still have slack, so the near edge has no contact.
    push.window._edge_views[push.window.id] = (push.window, [push.left, push.right])
    push.window._edge_cells[push.window.id] = ([600.0], [None])
    push.window._resize_target_edge = push.right              # the far edge is the one dragged
    push.pointer[0] = 100.0 + push.right["x"]
    push.frame()
    for _ in range(4):
        push.frame(dx=-30.0, edge=push.right, move=-30.0)      # the far edge follows the hand
        push.left["x"] -= 30.0                                 # ... and the near edge slides with it
    assert "pushed without contact" in push.kinds()
    assert any(v["edge"] == "win frame[0]" for r in push.reports for v in r["violations"])
