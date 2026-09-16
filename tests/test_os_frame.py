"""gl_gui/os_frame.py — the OS window as a melty window: its frame pair is
two edges in the column edge system one level outside the root windows,
the screen's work area the wall outside it. Solved in SCREEN coordinates
inside each root's frame pass (columns._frame_pass ← os_frame.attach /
detach), with zero-floor gap cells linking a root's frame to the OS frame.

  - a root's far edge dragged into the OS edge pushes it out (surface grows);
    at the screen the OS edge stops and the drag FLIPS: the root grows on
    its other side
  - a root's near edge reaching the OS near edge pushes the studio left
    (surface grows + attach-offset move), other roots hold their screen
    position; the screen's left edge stops it
  - only the hand moves the OS window: a foreign size write overflows the
    display as before, the OS edge untouched
  - the OS window's own right-drag (queue_drag): grows, flips at the screen
  - foreign changes from the feed: a near RESIZE re-bases the roots and
    pushes the one it reaches; a MOVE carries them along
  - a requested move stays "in flight" until the feed shows it (or times out)
  - no position (no extension): the display edges are immovable walls

Run: .venv/bin/python -m pytest tests/test_os_frame.py -q
"""
import os
import sys

import pytest


from meltygui.core.windowing import os_frame, titlebar as tb
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from test_column_edge_solve import FakeWindow, C


def test_clamp_far_edge_pins_and_slides_the_remainder():
    assert os_frame.clamp_far_edge(500, 800, 1000, 0) == (800, 300)
    assert os_frame.clamp_far_edge(500, 1200, 1000, 0) == (1000, 500)
    assert os_frame.clamp_far_edge(200, 1000, 1000, 0) == (1000, 200)       # top lands on 0
    assert os_frame.clamp_far_edge(100, 1500, 1000, 0, cap_size=False) == (1500, 100)


class Studio:
    """A fake screen: the OS window's content rect on it, the work area,
    the requests the model files."""

    def __init__(self, x=400.0, y=300.0, w=3000.0, h=1500.0, area=(0.0, 0.0, 3840.0, 2160.0)):
        self.pos = [x, y]
        self.size = [w, h]
        self.feed_size = [w, h]                     # the compositor's own size: lands with the move (land())
        self.area = area
        self.requests = []
        self.mode = "feed"

    def observe(self):
        if self.mode is None:
            return None
        return (tuple(self.pos), self.area, self.mode,
                (self.pos[0] + self.feed_size[0], self.pos[1] + self.feed_size[1]), getattr(self, "window_id", 1))

    def request(self, window, w, h, offset=None):
        self.requests.append((w, h, tuple(offset) if offset else None))
        if getattr(self, "size_lag", 0):
            # the compositor answers `size_lag` frames later: sizes queue up
            self._queued = getattr(self, "_queued", []) + [[[float(w), float(h)], getattr(self, "_frame", 0)]]
            self._offset = offset
            return
        # what the next frame start does: the size lands, the move a
        # frame later (land())
        self.size = [float(w), float(h)]
        self._offset = offset

    def land(self):
        self._frame = getattr(self, "_frame", 0) + 1
        queued = getattr(self, "_queued", None)
        while queued and self._frame - queued[0][1] >= getattr(self, "size_lag", 0):
            self.size = queued.pop(0)[0]          # a request lands `size_lag` frames after it was made, oldest first
        self.feed_size = list(self.size)          # the compositor processed the request: its size shows
        offset = getattr(self, "_offset", None)
        if offset and getattr(self, "refuse_moves", False):
            self._offset = None                   # ... but it keeps the position (a clamp we don't know)
            return
        if offset:
            self.pos = [self.pos[0] + offset[0], self.pos[1] + offset[1]]
            self._offset = None


@pytest.fixture
def studio(monkeypatch):
    st = Studio()
    monkeypatch.setattr(Toggles.Melty, "push_os_window_edges", True)
    monkeypatch.setattr(Toggles.Melty, "push_os_window_edges_trace", False)
    monkeypatch.setattr(os_frame, "_observe", st.observe)
    monkeypatch.setattr(tb, "request_surface_size", st.request)
    monkeypatch.setattr(tb, "_studio_window", lambda: object())
    monkeypatch.setattr(tb, "window_inset", lambda: 0)
    monkeypatch.setattr(Melty, "frame_count", 10, raising=False)
    st.roots, st.nested = [], []
    monkeypatch.setattr(os_frame, "_movable_roots", lambda: list(st.roots))
    monkeypatch.setattr(os_frame, "_root_windows", lambda: list(st.roots))
    monkeypatch.setattr(os_frame, "_all_windows", lambda: list(st.roots) + list(st.nested))
    fresh = dict(mode="walls", expected=[None, None], inflight=[None, None],
                 size_expected=[None, None], pending={"x": [], "y": []},
                 consumed={"x": False, "y": False}, unapplied=[0.0, 0.0], os_seen=[None, None],
                 window_id=None, feed_far=[None, None], reset_frame=-10 ** 6, gestures={},
                 last_offset=[0, 0], learned={"x": [None, None], "y": [None, None]},
                 size_requests={"x": [], "y": []}, unapplied_far=[0.0, 0.0], pin_rebases={},
                 size_observed=[None, None], size_request_frame=[None, None])
    fresh["move_requests"] = {"x": [], "y": []}
    os_frame._STATE.update(fresh, generation=os_frame._STATE["generation"] + 1)
    st.frame = _frame_maker(st)
    yield st
    os_frame._STATE.update(fresh)


def _frame_maker(st):
    def frame(*windows):
        """One studio frame: land last frame's move, begin (feed + size),
        run every window's edge pass, flush the request."""
        Melty.frame_count += 1
        st.land()
        os_frame.apply_rebase()                    # apply_pending_surface_size's hook
        Melty.display_size = tuple(st.size)
        os_frame.begin_frame()
        tb.poll_os_window_drag()
        for w in windows:
            w.abs_left, w.abs_top = abs_of(w)         # the OS-level solve reads the wrapper's abs
        os_frame.solve()
        for w in windows:
            move = getattr(w, "_pending_move", None)  # a hand move this frame (the wrapper's move drag)
            if move:
                w.window_pos = (w.window_pos[0] + move[0], w.window_pos[1] + move[1])
                w._hand_move_frame = Melty.frame_count
                w._pending_move = None
            w.abs_left, w.abs_top = abs_of(w)         # the wrapper's per-frame read
            C.window_edge_pass(w)
            w.width = max(w.width, w.min_width)
            w.abs_left, w.abs_top = w.window_pos
        return os_frame.flush()
    return frame


def abs_of(w):
    """The wrapper's abs position: a nested window's window_pos is
    relative to its spawner's position in its parent (left_offset /
    top_offset, 0 unless a test sets them)."""
    parent = getattr(w, "parent_window", None)
    if parent is None:
        return w.window_pos
    px, py = abs_of(parent)
    anchor = str(getattr(w, "parent_anchor_pos", "") or "")
    if anchor.endswith("right"):                  # hangs off the parent's FAR edge
        px += parent.width
    if anchor.startswith("bottom"):
        py += parent.height
    return (px + getattr(w, "left_offset", 0) + w.window_pos[0],
            py + getattr(w, "top_offset", 0) + w.window_pos[1])


def nested(st, parent, x, width=300, min_width=200, name="n"):
    """A closable window nested in ``parent`` at parent-relative ``x``."""
    w = FakeWindow(width=width, min_width=min_width, x=x)
    w.id = w.name = name
    w.parent_window = parent
    w.closable = True
    st.nested.append(w)
    st.frame(parent, w)
    return w


def root(st, x=2000.0, width=800, min_width=200, name="w"):
    w = FakeWindow(width=width, min_width=min_width, x=x)
    w.id = w.name = name
    w.parent_window = None
    w.closable = True
    st.roots.append(w)
    st.frame(w)                      # seeds the frame edges, first sight of the OS edges
    return w


def os_x():
    near, far = os_frame.edges("x")
    return near["x"], far["x"]


def os_y():
    near, far = os_frame.edges("y")
    return near["y"], far["y"]


def test_far_edge_pushes_the_os_edge_out_then_the_screen_stops_it_and_the_drag_flips(studio):
    w = root(studio)                                  # right edge at content 2800 of 3000
    right = w._frame_edges[1]
    assert os_x() == (400.0, 3400.0) and studio.requests == []
    # +300: right edge to 3100 — 100 past the OS edge → the surface grows 100
    w._pending_drags.append((right, 1100.0, True))
    assert studio.frame(w) == (3100, 1500)
    assert (w.window_pos[0], w.width) == (2000.0, 1100) and os_x() == (400.0, 3500.0)
    assert studio.requests[-1] == (3100, 1500, None)
    # +500 more: the OS right edge can only reach the screen (3840 → content
    # 3440): the drag is blocked there and FLIPS — the window grows LEFT by
    # the remainder (160), the OS left edge untouched (nothing reached it)
    w._pending_drags.append((right, 1600.0, True))
    studio.frame(w)
    assert os_x() == (400.0, 3840.0)
    assert (w.window_pos[0], w.width) == (1840.0, 1600)
    assert w.window_pos[0] + w.width == 3440.0            # pinned on the OS edge = the screen
    assert studio.requests[-1] == (3440, 1500, None)


def test_near_edge_reaching_the_os_edge_pushes_the_studio_left_until_the_screen(studio):
    w = root(studio, x=0.0, width=3000)               # fills the content: both edges on the OS edges
    other = root(studio, x=1000.0, width=300, name="other")
    right = w._frame_edges[1]
    # blocked on the right (the OS edge is 440 short of the screen, the drag
    # asks 600): 440 grows the surface, the 160 remainder flips to the near
    # edge, which is ON the OS near edge → the studio moves left by 160:
    # surface +160 with an attach-offset move, in one request
    w._pending_drags.append((right, 3600.0, True))
    assert studio.frame(w, other) == (3600, 1500)
    assert os_x() == (240.0, 3840.0)
    assert studio.requests[-1] == (3600, 1500, (-160, 0))
    # THIS frame the surface has not moved: the roots' content coordinates
    # stay (w's left edge sits 160 past the surface's edge, clipped) — the
    # re-base lands with the move at the next frame's start, no jelly
    assert (w.window_pos[0], w.width) == (-160.0, 3600) and other.window_pos[0] == 1000.0
    studio.frame(w, other)
    assert (w.window_pos[0], w.width) == (0.0, 3600)      # the window rides the OS frame
    assert other.window_pos[0] == 1160.0                   # holds its SCREEN position
    # the screen's left edge: 240 of room, the drag asks 400 → 240 moves, then nothing
    w._pending_drags.append((right, 4000.0, True))
    studio.frame(w, other)
    assert os_x() == (0.0, 3840.0) and studio.requests[-1] == (3840, 1500, (-240, 0))
    studio.frame(w, other)
    assert (w.window_pos[0], w.width) == (0.0, 3840)
    w._pending_drags.append((right, 4200.0, True))
    before = len(studio.requests)
    studio.frame(w, other)
    assert os_x() == (0.0, 3840.0) and w.width == 3840 and len(studio.requests) == before


def test_only_the_hand_moves_the_os_window(studio):
    w = root(studio, x=2500.0, width=300)
    w.width = 900                                      # content grew: a foreign size write
    studio.frame(w)
    assert os_x() == (400.0, 3400.0) and studio.requests == []
    assert (w.window_pos[0], w.width) == (2500.0, 900) # overflows the display, as before


def test_os_window_right_drag_grows_then_flips_at_the_screen(studio):
    # no root windows: the OS window's own drag solves against the screen alone
    os_frame.queue_drag("x", 1, 300)
    assert studio.frame() == (3300, 1500)
    assert os_x() == (400.0, 3700.0) and studio.requests[-1] == (3300, 1500, None)
    os_frame.queue_drag("x", 1, 300)                 # 140 of room left: the rest moves the studio
    studio.frame()
    assert os_x() == (240.0, 3840.0) and studio.requests[-1] == (3600, 1500, (-160, 0))
    # top-left mode drags the near edge; it can't collapse below MIN_SIZE
    os_frame.queue_drag("x", 0, 5000)
    studio.frame()
    near, far = os_x()
    assert far - near == os_frame.MIN_SIZE[0] and far == 3840.0
    # the y axis is the same machinery, independent
    assert os_frame.edges("y")[0]["y"] == 300.0


def test_foreign_near_resize_holds_the_roots_on_screen_and_pushes_the_one_it_reaches(studio):
    w = root(studio, x=100.0, width=300, min_width=200)
    far_root = root(studio, x=2000.0, width=300, name="far")
    # the compositor resizes from the LEFT: x +250, width -250 (far edge still)
    studio.pos[0] += 250.0
    studio.size[0] -= 250.0
    studio.frame(w, far_root)
    assert os_x() == (650.0, 3400.0)
    assert far_root.window_pos[0] == 1750.0             # same screen x (2400)
    # w sat at screen 500: the OS edge reached it — pushed to the OS edge,
    # compressed to its minimum, then slid
    assert w.window_pos[0] == 0.0 and w.width == 200
    assert studio.requests == []                        # nothing of ours to request


def test_foreign_move_carries_the_roots_along(studio):
    w = root(studio, x=100.0, width=300)
    studio.pos[0] += 500.0                              # a Super+drag: same size, new place
    studio.frame(w)
    assert os_x() == (900.0, 3900.0)
    assert (w.window_pos[0], w.width) == (100.0, 300)   # content coords unchanged
    assert w._os_seen["x"] == (900.0, 3900.0) and studio.requests == []


def test_a_requested_move_stays_in_flight_until_the_feed_shows_it(studio, monkeypatch):
    w = root(studio, x=0.0, width=3000)
    right = w._frame_edges[1]
    w._pending_drags.append((right, 3600.0, True))
    studio.frame(w)                                     # requests the -160 move
    assert os_frame._STATE["expected"][0] == 240.0 and os_frame._STATE["inflight"][0] is not None
    # the feed hasn't shown it yet (land() is skipped): the model keeps 240
    monkeypatch.setattr(studio, "land", lambda: None)
    studio.frame(w)
    assert os_x()[0] == 240.0 and os_frame._STATE["inflight"][0] is not None
    # ... for INFLIGHT_FRAMES; then the observed position is the truth again
    for _ in range(os_frame.INFLIGHT_FRAMES + 1):
        studio.frame(w)
    assert os_x()[0] == 400.0 and os_frame._STATE["inflight"][0] is None


def test_without_a_position_the_display_edges_are_walls(studio):
    studio.mode = None
    w = root(studio, x=2500.0, width=300)
    assert os_frame.mode() == "walls" and os_x() == (0.0, 3000.0)
    right = w._frame_edges[1]
    w._pending_drags.append((right, 900.0, True))       # right edge to 3400: 400 past the display
    studio.frame(w)
    assert os_x() == (0.0, 3000.0) and studio.requests == []
    assert (w.window_pos[0], w.width) == (2100.0, 900)  # pinned at the display, grown left
    w._pending_drags.append((right, 3500.0, True))      # far more than the display: left edge stops at 0
    studio.frame(w)
    assert (w.window_pos[0], w.width) == (0.0, 3000)


def test_os_window_motion_alone_invalidates_no_root(studio):
    """The OS window's own drag, and an OS move every root folds in, are not
    a change of a root whose edges stay put — no invalidation storm."""
    w = root(studio, x=1000.0, width=300)
    other = root(studio, x=2000.0, width=300, name="other")
    before = (w.invalidations, other.invalidations)
    os_frame.queue_drag("x", 1, 100)                    # OS right edge out: touches nobody
    studio.frame(w, other)
    assert os_x() == (400.0, 3500.0)
    assert (w.invalidations, other.invalidations) == before
    os_frame.queue_drag("x", 0, -100)                   # OS left edge out: the studio moves
    studio.frame(w, other)
    assert os_x() == (300.0, 3500.0)
    assert (w.window_pos[0], other.window_pos[0]) == (1000.0, 2000.0)   # not yet: the move lands next frame
    studio.frame(w, other)
    assert (w.window_pos[0], other.window_pos[0]) == (1100.0, 2100.0)   # screen positions held
    assert (w.invalidations, other.invalidations) == before
    # ...but a root the OS edge reaches IS changed (and invalidated)
    os_frame.queue_drag("x", 0, 1300)                   # OS left edge in to 1600, past w's left (screen 1400)
    studio.frame(w, other)
    assert w.width == 200 and w.invalidations > before[0]
    studio.frame(w, other)
    assert w.window_pos[0] == 0.0 and other.window_pos[0] == 800.0
    assert other.invalidations == before[1]


def test_the_rebase_lands_with_the_move_in_one_frame(studio):
    """A root touching nothing, while another root's drag moves the studio
    left by 160: its content coordinate changes exactly when the surface
    moves (apply_rebase at the frame start that applies the request), and
    a pass in between still sees its TRUE screen position."""
    w = root(studio, x=0.0, width=3000)
    still = root(studio, x=1000.0, width=300, name="still")
    right = w._frame_edges[1]
    w._pending_drags.append((right, 3600.0, True))
    studio.frame(w, still)
    assert os_frame._STATE["unapplied"][0] == -160.0 and still.window_pos[0] == 1000.0
    # a pass before the move lands: the OS near edge dragged INTO still
    # (from 240 to 1500 on screen: past still's left edge at 1400) must
    # find it where it really is. The overlapping root's near edge also
    # pushes it through the usual 60px inside margin (added after this test).
    os_frame.queue_drag("x", 0, 1260)
    studio.frame(w, still)          # apply_rebase runs first here: still → 1160, then the push
    assert still.window_pos[0] == 1260.0 + C.MIN_COLUMN_WIDTH and os_x()[0] == 1500.0
    studio.frame(w, still)
    assert still.window_pos[0] == C.MIN_COLUMN_WIDTH
    assert still.window_pos[0] + os_x()[0] == 1500.0 + C.MIN_COLUMN_WIDTH


def test_background_right_drag_is_solved_in_the_same_frame(studio, monkeypatch):
    """poll_os_window_drag at frame start: the event's increment reaches the
    OS edge in THIS frame's pass, not the next."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    handler = MagicMock()
    handler.is_down.return_value = True
    monkeypatch.setattr(Melty, "event_handler", handler, raising=False)
    monkeypatch.setattr(Melty, "events", {tb._RESIZE_ID: {"non_blocking_right_mouse_dragged": SimpleNamespace(total_dx=80.0, total_dy=0.0)}}, raising=False)
    tb._rdrag = None
    try:
        w = root(studio, x=1000.0, width=300)           # root() runs a frame: the drag is polled + solved there
        assert os_x() == (400.0, 3480.0) and studio.requests[-1] == (3080, 1500, None)
    finally:
        tb._rdrag = None
        monkeypatch.setattr(Melty, "events", {}, raising=False)


def test_a_pushed_os_edge_advances_every_frame_with_fractional_steps(studio):
    """A request lands as an integer size; begin_frame snaps the model's
    far edge by the fraction, the next pass sees the OS edge "moved" and
    forces it — that force must not eat the push the window's own drag
    gives the same edge in that pass (before: the OS edge advanced every
    other frame by a double step, the window's edge overshooting it in
    between — the fight at the screen's right edge)."""
    w = root(studio, x=2000.0, width=800)               # right edge 2800, OS edge 3400 → contact after 600
    right = w._frame_edges[1]
    w._pending_drags.append((right, 1600.0, True))      # to 3600: pushes the OS edge 200
    studio.frame(w)
    for _ in range(6):
        w._pending_drags.append((right, right["x"] + 137.3, True))
        studio.frame(w)
        near, far = os_x()
        assert abs((near + w.window_pos[0] + w.width) - far) < 1.0      # in contact, every frame
        assert far > 3600.0
    assert studio.requests[-1][0] == int(round(os_x()[1] - os_x()[0]))


def test_a_hand_move_pushes_the_os_edge_out_and_is_never_clamped(studio):
    """Left-drag move: the window's frame pushes the OS edge it overlaps —
    the surface grows — until the screen, where the OS edge stops while
    the window keeps going off the display (a move is never blocked)."""
    w = root(studio, x=2500.0, width=800)               # right edge at content 3300 of 3000: already past → pushed on first move
    other = root(studio, x=100.0, width=200, name="other")
    w._pending_move = (200.0, 0.0)                     # → right edge 3500 (screen 3900): past the screen (3840)
    studio.frame(w, other)
    assert os_x() == (400.0, 3840.0)                    # pushed to the screen, not past it
    assert w.window_pos[0] == 2700.0 and w.width == 800 # the window itself: where the hand put it
    assert studio.requests[-1] == (3440, 1500, None)
    assert other.window_pos[0] == 100.0                 # untouched
    w._pending_move = (300.0, 0.0)                     # further off the display: nothing stops it
    studio.frame(w, other)
    assert w.window_pos[0] == 3000.0 and os_x() == (400.0, 3840.0)
    w._pending_move = (-1500.0, 0.0)                   # back inside: the OS edge stays (no pull)
    studio.frame(w, other)
    assert w.window_pos[0] == 1500.0 and os_x() == (400.0, 3840.0)


def test_a_hand_move_into_the_near_edge_drags_the_studio_along(studio):
    w = root(studio, x=50.0, width=400)
    w._initial_window_pos = (50.0, 0.0)                 # the move drag's press baseline
    w._pending_move = (-150.0, 0.0)                    # left edge to content -100: past the OS left edge
    studio.frame(w)
    assert os_x()[0] == 300.0 and studio.requests[-1] == (3100, 1500, (-100, 0))
    assert w.window_pos[0] == -100.0                    # this frame: past the surface's edge (clipped)
    studio.frame(w)                                     # the move lands: content re-based, baseline with it
    assert w.window_pos[0] == 0.0 and w._initial_window_pos == (150.0, 0.0)
    # at the screen's left the studio stops and the window goes on
    w._pending_move = (-600.0, 0.0)                    # (the fixture's screen starts at 0)
    studio.frame(w)
    assert os_x()[0] == 0.0 and studio.requests[-1] == (3400, 1500, (-300, 0))
    studio.frame(w)
    assert w.window_pos[0] == -300.0                    # 300 past the screen's edge, as dragged


def test_a_hand_move_is_hand_only_and_off_without_a_position(studio, monkeypatch):
    w = root(studio, x=2500.0, width=800)
    w.window_pos = (2700.0, w.window_pos[1])            # a programmatic move: no push
    studio.frame(w)
    assert os_x() == (400.0, 3400.0) and studio.requests == []
    monkeypatch.setattr(Toggles.Melty, "window_move_pushes_os_edges", False)
    w._pending_move = (100.0, 0.0)
    studio.frame(w)
    assert os_x() == (400.0, 3400.0) and studio.requests == []


def test_an_os_resize_pushes_windows_into_each_other_and_keeps_overlaps(studio):
    """During a GLFW window resize every root's edges are independent
    collidable objects: the OS edge pushes the window it reaches, that
    window pushes the next, each compresses to its minimum then slides;
    overlapping windows interleave and stay overlapping."""
    a = root(studio, x=100.0, width=400, min_width=200, name="a")           # screen 500..900
    b = root(studio, x=1200.0, width=400, min_width=200, name="b")          # screen 1600..2000
    c = root(studio, x=1400.0, width=400, min_width=200, name="c")          # screen 1800..2200, overlaps b by 200
    # the compositor resizes from the LEFT by 1500 (x +1500, width -1500)
    studio.pos[0] += 1500.0
    studio.size[0] -= 1500.0
    studio.frame(a, b, c)
    assert os_x() == (1900.0, 3400.0)
    # a: pushed from 500 to the OS edge, compressed to its minimum: screen 1900..2100
    assert (a.window_pos[0], a.width) == (0.0, 200)
    # b: a's far edge (2100) meets b's near edge on the OUTSIDE — they
    # touch, no margin — and b compresses: 2100..2300
    assert (b.window_pos[0], b.width) == (200.0, 200)
    # c overlapped b: its near edge rode b's (2100 → its own cell) and its far
    # edge was pushed by b's far edge: 2300.. → c at 2300..2500? no — c's near
    # edge sits at 2300 (pushed by b's far edge through the gap), width 200
    assert c.window_pos[0] + os_x()[0] >= b.window_pos[0] + os_x()[0]          # order kept
    assert c.window_pos[0] < b.window_pos[0] + b.width                         # still overlapping
    assert c.width == 200
    assert studio.requests == []


def test_melty_drags_still_pass_through_each_other(studio):
    a = root(studio, x=100.0, width=400, name="a")
    b = root(studio, x=600.0, width=400, name="b")
    right = a._frame_edges[1]
    a._pending_drags.append((right, 900.0, True))       # a's right edge to content 1000: through b
    studio.frame(a, b)
    assert a.width == 900 and (b.window_pos[0], b.width) == (600.0, 400)


def test_the_os_windows_own_inward_drag_pushes_the_roots_as_a_pile(studio):
    a = root(studio, x=100.0, width=300, min_width=200, name="a")           # screen 500..800
    b = root(studio, x=400.0, width=300, min_width=200, name="b")           # screen 800..1100 (touching a)
    os_frame.queue_drag("x", 0, 500)                    # OS left edge 400 → 900: into a, a into b
    studio.frame(a, b)
    assert os_x()[0] == 900.0
    assert (a.window_pos[0], a.width) == (500.0, 200)   # content coords still against the old origin this frame
    assert (b.window_pos[0], b.width) == (700.0, 200)
    studio.frame(a, b)                                  # the move lands: content re-based
    assert (a.window_pos[0], b.window_pos[0]) == (0.0, 200.0)


def test_a_new_window_never_meets_the_old_windows_numbers(studio):
    """A studio restart in the same process (or the feed switching
    windows): the model resets to first sight instead of reading the new
    window's position as a giant foreign change of the old one."""
    w = root(studio, x=100.0, width=300)
    right = w._frame_edges[1]
    w._pending_drags.append((right, 3600.0, True))     # leaves a booked move + expected position
    studio.frame(w)
    assert os_frame._STATE["unapplied"][0] == -160.0
    # the "new session": the studio's init resets the model before its first
    # frame (lsd_studio → os_frame.reset), then another window id at another
    # place shows up with the same persisted layout
    os_frame.reset("restart")
    assert os_frame._STATE["unapplied"] == [0.0, 0.0]   # the booked re-base died with the old window
    studio.window_id = 2
    studio.pos = [1500.0, 800.0]
    studio.size = [2000.0, 1000.0]
    studio.land = lambda: None
    w.window_pos = (100.0, 50.0)
    w.width = 300
    studio.frame(w)
    assert os_x() == (1500.0, 3500.0)
    assert (w.window_pos[0], w.width) == (100.0, 300)   # untouched
    assert os_frame._STATE["unapplied"] == [0.0, 0.0] and os_frame._STATE["expected"][0] == 1500.0
    assert studio.requests[-1][0] == 3600                # nothing new requested since
    # the feed switching windows mid-session (no init) is caught by the id too
    studio.window_id = 3
    studio.pos = [700.0, 800.0]
    studio.frame(w)
    assert os_x()[0] == 700.0 and (w.window_pos[0], w.width) == (100.0, 300)


def test_foreign_change_is_classified_from_the_feeds_own_far_edge(studio):
    """The feed's position and our size can be a frame apart: a move whose
    size change reaches us first must not read as a near resize."""
    w = root(studio, x=100.0, width=300)
    # the feed reports a move (+500) and, on the same rect, a smaller width;
    # our content size is still the old one this frame
    studio.pos[0] += 500.0
    studio.size[0] -= 500.0
    saved = studio.size[0]
    studio.size[0] += 500.0                             # display_size lags: still 3000
    studio.frame(w)
    assert os_x()[0] == 900.0
    assert w.window_pos[0] == 100.0                     # a MOVE: rode along, no re-base, no push
    studio.size[0] = saved
    studio.frame(w)
    assert os_x() == (900.0, 3400.0) and w.window_pos[0] == 100.0


def test_a_shrink_from_the_right_pushes_windows_it_touches_and_overlapping_ones_stay_a_pile(studio):
    """The far-edge mirror of the left push, over ties and sub-pixel
    overshoot: a window whose far edge sits ON (or a fraction past) the OS
    far edge is still pushed by it, and two windows pushed together keep
    pushing as one pile frame after frame (before: at a tie the OS far
    edge sorted after the window's edge and the window was ignored —
    left outside, "clipping through" its neighbour)."""
    a = root(studio, x=2000.0, width=400, min_width=200, name="a")   # screen 2400..2800
    b = root(studio, x=2300.0, width=400, min_width=200, name="b")   # screen 2700..3100, overlaps a
    margin = C.MIN_COLUMN_WIDTH
    studio.size[0] -= 800.0                              # OS far edge 3400 → 2600
    studio.frame(a, b)
    assert os_x() == (400.0, 2600.0)
    # b: 2400..2600 (compressed); a's far edge keeps the usual margin
    # below b's: 2340..2540
    assert (a.window_pos[0], a.width) == (2000.0 - margin, 200) and (b.window_pos[0], b.width) == (2000.0, 200)
    studio.size[0] -= 600.0                              # → 2000: b's far edge ON the OS edge, a's the margin below it
    studio.frame(a, b)
    assert os_x()[1] == 2000.0
    assert (a.window_pos[0], a.width) == (1400.0 - margin, 200) and (b.window_pos[0], b.width) == (1400.0, 200)
    assert b.window_pos[0] + b.width + os_x()[0] == 2000.0            # ON the edge, not past it
    # a pixel wider (a width re-stamp) and a fractional step: still pushed
    # through b's far edge each frame, never stretched
    a.width = 201
    studio.size[0] -= 100.3
    studio.frame(a, b)
    assert b.window_pos[0] + b.width + os_x()[0] <= os_x()[1] + 1.0
    assert a.window_pos[0] + a.width + os_x()[0] <= os_x()[1] - margin + 1.0
    assert a.width == 201 and b.width == 200       # a's cap is its size: never wider, never stretched


def test_outside_contact_touches_and_inside_contact_keeps_the_margin(studio):
    """A window's far edge meeting another's near edge from the OUTSIDE:
    0 px between them. An edge INSIDE the other window (an overlap): the
    usual margin. The collision rects never extend past the windows."""
    a = root(studio, x=100.0, width=400, min_width=200, name="a")           # screen 500..900
    b = root(studio, x=800.0, width=400, min_width=200, name="b")           # screen 1200..1600: outside a
    os_frame.queue_drag("x", 0, 600)                     # OS left edge 400 → 1000: a → 1000..1200 touching b
    studio.frame(a, b)
    studio.frame(a, b)                                   # the move lands
    assert abs_of(a)[0] + a.width + os_x()[0] == abs_of(b)[0] + os_x()[0]  # a_far == b_near: touching
    assert (b.window_pos[0], b.width) == (800.0 - 600.0, 400)               # b untouched (screen 1200..1600)
    os_frame.queue_drag("x", 0, 100)                     # 100 more: a (at its minimum) pushes b along, still touching
    studio.frame(a, b)
    studio.frame(a, b)
    assert abs_of(a)[0] + a.width == abs_of(b)[0] and a.width == 200
    assert abs_of(b)[0] + os_x()[0] == 1300.0


def test_the_flat_chain_orders_ties_by_role():
    near, far = {"x": 100.0}, {"x": 500.0}
    w_n, w_f = {"x": 100.0}, {"x": 500.0}                # a window exactly filling the OS window
    v_n, v_f = {"x": 500.4}, {"x": 700.0}                # a window starting a fraction past the OS edge
    roots = [(None, w_n, w_f, 0.0), (None, v_n, v_f, 0.0)]
    chain = os_frame._flat_chain([near, far], roots, "x")
    # Overhanging windows are still inside the OS boundary topologically,
    # with their own near/far order intact, so the next inward drag reaches them.
    assert [id(edge) for edge in chain] == [id(edge) for edge in (near, w_n, w_f, v_n, v_f, far)]


def test_a_nested_window_pushes_the_os_edge_and_flips_like_a_root(studio):
    parent = root(studio, x=1000.0, width=1500, name="parent")            # screen 1400..2900
    child = nested(studio, parent, x=1000.0, width=400, name="child")     # screen 2400..2800
    right = child._frame_edges[1]
    child._pending_drags.append((right, 1000.0, True))                    # right edge to screen 3400: the OS edge
    studio.frame(parent, child)
    assert os_x() == (400.0, 3400.0) and studio.requests == []
    child._pending_drags.append((right, 1600.0, True))                    # +600: 440 grows the surface, then the flip
    studio.frame(parent, child)
    assert os_x() == (400.0, 3840.0)
    assert (child.window_pos[0], child.width) == (840.0, 1600)            # grew left by 160, right edge on the screen
    assert abs_of(child)[0] + child.width + os_x()[0] == 3840.0
    assert (parent.window_pos[0], parent.width) == (1000.0, 1500)         # untouched


def test_an_os_shrink_compresses_a_parent_only_as_far_as_its_child_allows(studio):
    parent = root(studio, x=2000.0, width=800, min_width=200, name="parent")   # screen 2400..3200
    child = nested(studio, parent, x=100.0, width=300, min_width=200, name="child")   # screen 2500..2800
    studio.size[0] -= 1000.0                                                # OS far edge 3400 → 2400
    studio.frame(parent, child)
    assert os_x()[1] == 2400.0
    # the OS edge pushes the parent's right edge (free): the parent
    # compresses; its right edge reaches the child's (the margin) and
    # compresses the child from its free edge to its minimum, then the
    # child's right edge pushes its left edge — it slides to the parent's
    # left margin (60 in); the parent stops at margin + child + margin
    # (320); the rest slides the block — parent 2080..2400, child 2140..2340
    assert (child.window_pos[0], child.width) == (60.0, 200)
    assert (parent.window_pos[0], parent.width) == (1680.0, 320)
    # further: nothing more to give — the block slides as one
    studio.size[0] -= 300.0
    studio.frame(parent, child)
    assert (parent.window_pos[0], parent.width) == (1380.0, 320) and child.window_pos[0] == 60.0


def test_a_nested_hand_move_pushes_the_os_edge(studio):
    parent = root(studio, x=1000.0, width=1500, name="parent")
    child = nested(studio, parent, x=1600.0, width=400, name="child")     # screen 3000..3400: on the OS edge
    child._pending_move = (200.0, 0.0)
    studio.frame(parent, child)
    assert os_x() == (400.0, 3600.0) and studio.requests[-1] == (3200, 1500, None)
    assert child.window_pos[0] == 1800.0 and parent.window_pos[0] == 1000.0


def test_dragging_a_parent_makes_its_nested_windows_push_the_os_edge(studio):
    parent = root(studio, x=1000.0, width=800, name="parent")             # screen 1400..2200
    child = nested(studio, parent, x=1000.0, width=400, name="child")     # screen 2400..2800: past the parent, near the OS edge
    parent._pending_move = (800.0, 0.0)                                   # parent to 2200..3000; child rides to 3200..3600
    studio.frame(parent, child)
    assert os_x() == (400.0, 3600.0) and studio.requests[-1] == (3200, 1500, None)
    assert parent.window_pos[0] == 1800.0 and child.window_pos[0] == 1000.0   # child untouched relative to its parent


def test_resizing_a_parent_makes_its_nested_windows_push_the_os_edge(studio):
    """The parent's hand RESIZE moves the corner a child hangs from exactly
    as a hand move does, so the riding child collides with the OS edge the
    same way (it did not: the child's pass attached nothing)."""
    parent = root(studio, x=1000.0, width=800, name="parent")             # screen 1400..2200
    child = nested(studio, parent, x=200.0, width=400, name="child")
    child.parent_anchor_pos = "top_right"                                 # hangs off the parent's far edge: screen 2400..2800
    far = C._frame(parent, "x")[1]
    C._pending(parent, "x").append((far, far["x"] + 800.0, True))         # right-edge handle drag: parent 1400..3000
    studio.frame(parent, child)                                           # child rides to 3200..3600 (past the OS edge)
    assert parent.width == 1600 and parent.window_pos[0] == 1000.0
    assert child.window_pos[0] == 200.0                                   # rides, untouched relative to its parent
    assert os_x() == (400.0, 3600.0) and studio.requests[-1] == (3200, 1500, None)


def test_a_hand_move_never_passes_the_display_top(studio):
    """The display's TOP is a hard limit (Toggles.Melty.window_top_hard_limit):
    a hand move first pushes the OS top edge up to the screen as usual,
    then the remainder is clamped — the window's top lands exactly on the
    display top, in ONE frame; a further push up does nothing, dragging
    down lifts off at once (no ratchet)."""
    w = root(studio, x=100.0, width=800)                 # y=50 content → screen 350 (studio at 300)
    other = root(studio, x=2000.0, width=200, name="other")
    w._pending_move = (0.0, -500.0)                      # top → -450 content = screen -150: past the top
    studio.frame(w, other)
    assert os_y() == (0.0, 1800.0)                       # OS top edge pushed to the screen top, not past it
    assert os_frame.display_top() == -300.0             # the display top in (applied) content coords
    assert w.window_pos[1] == -300.0                     # the window's top ON the display top
    assert studio.requests[-1] == (3000, 1800, (0.0, -300.0))
    assert other.window_pos[1] == 50.0
    w._pending_move = (0.0, -100.0)                      # further up: nothing
    studio.frame(w, other)                               # (apply_rebase moved every root +300 first)
    assert os_frame.display_top() == 0.0 and w.window_pos[1] == 0.0 and other.window_pos[1] == 350.0
    w._pending_move = (0.0, 200.0)                       # down: lifts off, the OS edge stays
    studio.frame(w, other)
    assert w.window_pos[1] == 200.0 and os_y() == (0.0, 1800.0)


def test_the_top_limit_is_the_content_top_without_a_position(studio):
    studio.mode = None                                   # no feed: walls mode, the OS edges ARE the display edges
    w = root(studio, x=100.0, width=800)
    w._pending_move = (0.0, -200.0)
    studio.frame(w)
    assert os_frame.display_top() == 0.0 and w.window_pos[1] == 0.0


def test_a_nested_window_riding_a_parent_move_clamps_by_its_own_position(studio):
    """A child carried past the display top by its parent's move collides
    like any window: it slides down inside its parent (its own
    window_pos), the parent is never moved for it."""
    parent = root(studio, x=1000.0, width=800, name="parent")             # y=50: screen 350
    child = nested(studio, parent, x=100.0, width=300, name="child")      # y=50 parent-relative: screen 400
    child.window_pos = (100.0, -150.0)                                    # child above its parent: screen 200
    studio.frame(parent, child)
    child.pin_to_clip = object()                                          # pinned children clamp the same way (08-28)
    parent._pending_move = (0.0, -300.0)                                  # parent → screen 50; child → -100
    studio.frame(parent, child)
    assert os_y() == (0.0, 1800.0)
    assert parent.window_pos[1] == -250.0                                 # where the hand put it (screen 50)
    assert child.window_pos[1] == -50.0                                   # slid down to the top: screen 0
    child.window_pos = (100.0, child.window_pos[1] - 100.0)               # the child's OWN hand move: same limit
    child._hand_move_frame = Melty.frame_count + 1
    studio.frame(parent, child)
    assert child.window_pos[1] == -50.0 and parent.window_pos[1] == 50.0    # parent re-based +300 (the move landed)


def test_a_queued_top_edge_drag_stops_at_the_display_top_and_flips(studio):
    """The resize paths already stop at the screen wall through the solve:
    the top frame edge dragged past the display top pushes the OS edge to
    the screen, stops there, and the remainder grows the bottom (the flip)."""
    w = root(studio, x=100.0, width=800)                 # y=50 (screen 350), height 400
    top = C._frame(w, "y")[0]
    C._pending(w, "y").append((top, top["y"] - 500.0, True))   # → screen -150
    studio.frame(w)
    assert os_y() == (0.0, 1800.0)
    assert w.window_pos[1] == -300.0 and w.height == 400 + 350 + 150
    assert w._hand_resize_frame == Melty.frame_count


def test_overlapping_windows_keep_the_usual_margin_between_their_edges(studio):
    """Every edge is its own collision object. The overlapping order
    a_near → b_near → a_far → b_far: the OS far edge pushes b_far, b
    compresses, and b_far pushes a_far when they come within the usual
    margin (the columns' axis minimum) — the overhang survives as that
    margin instead of collapsing until the edges coincided and piled up on
    the OS edge (Lukas 08-27)."""
    a = root(studio, x=100.0, width=1400, min_width=600, name="a")     # screen 500..1900
    b = root(studio, x=1300.0, width=400, min_width=150, name="b")     # screen 1700..2100: a_n, b_n, a_f, b_f
    margin = C.MIN_COLUMN_WIDTH
    studio.size[0] -= 1400.0                        # OS far edge 3400 → 2000: b_f pushed 100; a_f (1900) is a margin+ away: untouched
    studio.frame(a, b)
    assert b.window_pos[0] + b.width + os_x()[0] == 2000.0
    assert (a.window_pos[0], a.width) == (100.0, 1400)
    assert b.window_pos[0] == 1300.0                # b compressed from its far edge, its near edge not reached
    studio.size[0] -= 300.0                         # → 1700: b at its minimum → its near edge moves, pushing nothing yet
    studio.frame(a, b)
    assert b.width == 150 and a.window_pos[0] + a.width + os_x()[0] == 1700.0 - margin
    assert b.window_pos[0] + os_x()[0] == 1550.0    # b_n: a_f (1640) keeps the margin above it
    assert a.width == 1140                          # a compressed from its far edge only


def test_a_child_collides_normally_and_the_parent_takes_over_when_the_push_reaches_it(studio):
    """A child hanging past its parent's far edge, the OS edge coming in:
    the child collides as its own object first — compresses to its
    minimum, slides inside its parent down to the usual margin — and the
    parent is untouched. Only what the push cannot do against the parent
    (the cascade) moves the parent, as a block with the child riding."""
    parent = root(studio, x=100.0, width=400, min_width=200, name="parent")     # screen 500..900
    child = nested(studio, parent, x=300.0, width=400, name="child")            # screen 800..1200: overhangs by 300
    studio.size[0] -= 2300.0                                                    # OS far edge 3400 → 1100
    studio.frame(parent, child)
    assert (parent.window_pos[0], parent.width) == (100.0, 400)                 # untouched
    assert (child.window_pos[0], child.width) == (300.0, 300)                   # compressed: 800..1100
    studio.size[0] -= 200.0                                                     # → 900: the child compresses to its minimum
    studio.frame(parent, child)                                                 # and slides 100 (its right edge pushing its
    assert (child.window_pos[0], child.width) == (200.0, 200)                   # left edge: 700..900); its far edge pushes the
    assert (parent.window_pos[0], parent.width) == (100.0, 340)                 # parent's far edge to the margin (840)
    assert abs_of(child)[0] + child.width + os_x()[0] == os_x()[1]             # the child's far edge on the OS edge
    # a child drawn at the display's sliver cap (scrolled off) is left out
    # of the parent's block in phase B
    child._capped_x = True
    child.window_pos = (2000.0, child.window_pos[1])                            # far outside: no contact with anything
    studio.size[0] -= 150.0                                                     # → 750: the parent's own far edge (840) pushed
    studio.frame(parent, child)
    assert (parent.window_pos[0], parent.width) == (100.0, 250)


def test_a_child_anchored_at_a_far_corner_is_driven_there_and_free_at_its_near_edge(studio):
    """A child placed by a right corner of its parent: its RIGHT edge is
    the driven one; its left edge collides normally — an OS near edge
    coming in compresses it from the left."""
    from meltygui.state.new_core_model import Anchor
    parent = root(studio, x=1000.0, width=800, min_width=200, name="parent")    # screen 1400..2200
    child = nested(studio, parent, x=-300.0, width=400, min_width=150, name="child")   # screen 1100..1500: overhangs the LEFT
    child.anchor_pos = Anchor.TOP_RIGHT
    os_frame.queue_drag("x", 0, 800)                    # OS left edge 400 → 1200: into the child's left edge
    studio.frame(parent, child)
    studio.frame(parent, child)                         # the move lands
    assert os_x()[0] == 1200.0
    assert (child.window_pos[0], child.width) == (-300.0 + 100.0, 300)        # left edge pushed 100, right edge (driven) held
    assert (parent.window_pos[0], parent.width) == (1000.0 - 800.0, 800)      # content re-based only: untouched on screen


def test_a_childs_free_edge_pushes_the_parents_free_edge_and_compresses_it(studio):
    """A child overhanging its parent's right edge by the usual margin, the
    OS edge coming in: the child's right edge (its own) pushes the
    parent's right edge (not a driving edge) — the parent compresses —
    while both keep the margin; the driving pair (the parent's left edge,
    the child's left edge) never moves until the block does."""
    parent = root(studio, x=100.0, width=400, min_width=200, name="parent")     # screen 500..900
    child = nested(studio, parent, x=200.0, width=260, min_width=200, name="child")   # screen 700..960: overhang = the margin
    studio.size[0] -= 2500.0                                                    # OS far edge 3400 → 900
    studio.frame(parent, child)
    assert (child.window_pos[0], child.width) == (200.0, 200)                   # compressed to its minimum: 700..900
    assert (parent.window_pos[0], parent.width) == (100.0, 340)                 # its right edge pushed to the margin: 500..840
    studio.size[0] -= 100.0                                                     # → 800: the child can give no more — it slides
    studio.frame(parent, child)                                                 # (its right edge pushing its left edge), its
    assert (child.window_pos[0], child.width) == (100.0, 200)                   # far edge still pushing the parent's to the margin
    assert (parent.window_pos[0], parent.width) == (100.0, 240)
    studio.size[0] -= 100.0                                                     # → 700: at the parent's left margin — the block
    studio.frame(parent, child)
    assert (child.window_pos[0], child.width) == (60.0, 200)
    assert (parent.window_pos[0], parent.width) == (60.0, 200)


def test_a_child_hung_from_the_parents_right_corner_compresses_slides_then_the_parent_collapses(studio):
    """A popover hung from its parent's top-RIGHT corner (parent_anchor_pos).
    Its own edges are ordinary: the OS edge compresses it to its minimum,
    then its right edge pushes its left edge — it slides, relative to its
    corner, back over its parent. Only the driving corner (the parent's
    right edge) and the parent's left edge (pushing it outward would move
    the parent and so the corner) are walls; when the child reaches the
    parent's left margin the parent collapses in the block phase, the
    child riding its right corner — never written for it."""
    from meltygui.state.new_core_model import Anchor
    parent = root(studio, x=100.0, width=800, min_width=200, name="parent")       # screen 500..1300
    child = nested(studio, parent, x=-100.0, width=260, min_width=200, name="child")
    child.parent_anchor_pos = Anchor.TOP_RIGHT
    child.left_offset = parent.width                                              # hung from the parent's right corner
    real_frame = studio.frame
    def frame(*ws):
        child.left_offset = parent.width                                          # the layout: the corner moves with the width
        return real_frame(*ws)
    studio.frame = frame
    assert abs_of(child)[0] + os_x()[0] == 1200.0                                 # child 1200..1460: overhangs by 160
    # 1. the OS far edge to 1400: the child compresses to its minimum
    studio.size[0] -= 2000.0
    studio.frame(parent, child)
    assert (child.window_pos[0], child.width) == (-100.0, 200) and parent.width == 800
    # 2. further: the child's far edge is within the margin of its
    #    DRIVING edge (the parent's right edge, 60 past it) — the one
    #    special case: the child slides the 40 to the margin, the parent
    #    collapses for the rest, and from then on collapses 100 a frame
    #    with the child riding its right corner (never written for it)
    for step in range(6):
        studio.size[0] -= 100.0
        studio.frame(parent, child)
        child.left_offset = parent.width                                          # the parent's draw answers the new width
        assert (child.window_pos[0], child.width) == (-140.0, 200)
        assert (parent.window_pos[0], parent.width) == (100.0, 800 - 60 - 100 * step)
        assert abs_of(child)[0] + child.width + os_x()[0] == os_x()[1]


def test_an_os_edge_already_past_the_screen_wall_is_the_wall(studio):
    """The studio dragged partly off the screen by the compositor (Hyprland:
    a hand move; the work area's reserved strip): its OS edge sits PAST
    the screen wall. The wall is then where the edge is — a drag further
    out is blocked and flips (the other side grows), a drag inward moves
    it — never a snap back onto the wall, which teleported the studio by
    the whole overhang and grew it by as much on the other side (09-09)."""
    studio.pos[0] = -375.0                                  # 475 past the work area's left edge (100)
    studio.area = (100.0, 0.0, 7580.0, 2160.0)
    w = root(studio, x=1000.0, width=600)
    assert os_x() == (-375.0, 2625.0)
    os_frame.queue_drag("x", 0, -80)                        # top-left drag outward: blocked, flips
    studio.frame(w)
    assert os_x() == (-375.0, 2705.0) and studio.requests[-1] == (3080, 1500, None)
    assert w.window_pos[0] == 1000.0                        # nothing moved
    os_frame.queue_drag("x", 0, 80)                         # inward: the edge moves, the studio with it
    studio.frame(w)
    assert os_x() == (-295.0, 2705.0) and studio.requests[-1] == (3000, 1500, (80, 0))
    studio.frame(w)                                         # the move lands: the root keeps its screen spot
    assert w.window_pos[0] == 920.0 and studio.pos[0] == -295.0
    # the far side the same way: the studio's bottom below the work area
    studio.pos[1], studio.size[1] = 700.0, 1500.0           # bottom at 2200, 40 past the wall (2160)
    studio.frame(w)
    os_frame.queue_drag("y", 1, 50)                         # bottom-right drag down: blocked, flips upward
    studio.frame(w)
    assert os_y() == (650.0, 2200.0) and studio.requests[-1] == (3000, 1550, (0, -50))


# --- an app's root: pinned to the surface --------------------------------------------

def app_root(st, name="app-root"):
    """An app's root (surface.root_view_kwargs): a closable window whose
    position and size are re-stamped from the surface every frame — it
    rides with the OS window (core_render stamps `_frame_pinned`)."""
    w = FakeWindow(width=st.size[0], min_width=200, x=0.0)
    w.id = w.name = name
    w.parent_window = None
    w.closable = True
    w.window_pos = (0.0, 0.0)
    w._frame_pinned = True
    st.roots.append(w)
    return w


def app_frame(st, root, *others):
    """The app's frame: like Studio.frame, but the wrapper re-stamps the
    root's window_pos / size from the surface AFTER the OS-level solve
    (root_view_kwargs), before the window passes."""
    Melty.frame_count += 1
    st.land()
    os_frame.apply_rebase()
    Melty.display_size = tuple(st.size)
    os_frame.begin_frame()
    tb.poll_os_window_drag()
    for w in (root, *others):
        w.abs_left, w.abs_top = abs_of(w)
    os_frame.solve()
    root.window_pos = (0.0, 0.0)
    root.width = os_frame.content_size(tuple(st.size))[0]     # the surface lays the root out at the MODEL's size
    root.height = os_frame.content_size(tuple(st.size))[1]
    for w in (root, *others):
        move = getattr(w, "_pending_move", None)  # a hand move this frame (the wrapper's move drag)
        if move:
            w.window_pos = (w.window_pos[0] + move[0], w.window_pos[1] + move[1])
            w._hand_move_frame = Melty.frame_count
            w._pending_move = None
        w.abs_left, w.abs_top = abs_of(w)
        C.window_edge_pass(w)
        w.width = max(w.width, w.min_width)
        w.abs_left, w.abs_top = abs_of(w)
    return os_frame.flush()


def test_a_pinned_apps_child_pushing_the_os_near_edge_stays_on_the_edge(studio):
    """The root rides with the surface; its nested window holds its screen
    position like a studio root would: after pushing the OS near edge out
    by 150 its left edge sits ON the OS edge (parent-relative 0), not 150
    px outside the surface, and idle frames move nothing."""
    root = app_root(studio)
    app_frame(studio, root)
    child = FakeWindow(width=300, min_width=200, x=100.0)
    child.id = child.name = "child"
    child.parent_window = root
    child.closable = True
    studio.nested.append(child)
    app_frame(studio, root, child)
    left = child._frame_edges[0]
    child._pending_drags.append((left, left["x"] - 250.0, True))     # 100 → -150: 150 into the OS near edge
    app_frame(studio, root, child)
    assert os_x() == (250.0, 3400.0) and studio.size[0] == 3150.0   # the surface grew 150 to the left
    app_frame(studio, root, child)                                    # the move lands, the re-base with it
    assert root.window_pos == (0.0, 0.0) and root.width == 3150.0    # the root fills the surface
    assert (child.window_pos[0], child.width) == (0.0, 550)           # the child's left edge ON the OS edge
    before = (os_x(), studio.pos[0], child.window_pos)
    for _ in range(3):
        app_frame(studio, root, child)
    assert (os_x(), studio.pos[0], child.window_pos) == before        # idle: nothing drifts


def test_a_pinned_apps_os_right_drag_grows_one_step_per_frame(studio):
    root = app_root(studio)
    app_frame(studio, root)
    for k in range(1, 5):
        os_frame.queue_drag("x", 1, 50)
        app_frame(studio, root)
        assert os_x() == (400.0, 3400.0 + 50 * k)
        assert studio.size[0] == 3000.0 + 50 * k
    app_frame(studio, root)
    assert root.width == 3200.0 and os_x() == (400.0, 3600.0)


def test_a_pinned_apps_column_divider_pushes_the_os_edge_one_to_one(studio):
    """A divider dragged past its pile pushes the root's far frame edge and,
    through the zero-floor gap cell, the OS edge — the hand-driven handle
    drags are queued CURSOR-DRIVEN (third slot True, columns._frame_pass
    and the layouts' handles), so they solve on the OS-level graph. Before
    09-13 a 2-tuple read as a foreign size write and solved locally: the
    push stopped at the root's frame and the pin re-stamped it."""
    studio.size = [1000.0, 1500.0]
    root = app_root(studio)
    app_frame(studio, root)
    left, right = root._frame_edges
    d0, d1 = {"x": 300.0}, {"x": 800.0}
    root._edge_views[("row", "r")] = (root, [left, d0, d1, right])      # 3 columns, 60 px floors
    app_frame(studio, root)
    for k in range(6):
        target = d1["x"] + 50.0                                           # contact once the last column hits 60
        root._pending_drags.append((d1, target, True))
        app_frame(studio, root)
        assert d1["x"] == target                                          # the divider stays under the hand
        assert os_x()[1] - os_x()[0] == max(1000.0, target + 60.0)        # the OS edge follows 1:1
    assert studio.size[0] == 1160.0 and root.width == 1160.0


def test_a_pinned_apps_frame_edge_pulled_inward_shrinks_the_os_window(studio):
    """The pinned root's link to the OS frame is RIGID (os_frame.gap_lists
    rigid=True): a right-drag latched on the root's own frame edge and
    dragged inward pulls the OS edge with it in the SAME pass — the window
    shrinks — with nothing special on the drag (it is an ordinary
    cursor-driven pending entry, like any studio window's frame drag)."""
    root = app_root(studio)
    app_frame(studio, root)
    left, right = root._frame_edges
    root._pending_drags.append((right, right["x"] - 200.0, True))
    app_frame(studio, root)
    assert os_x() == (400.0, 3200.0) and studio.size[0] == 2800.0
    app_frame(studio, root)
    assert root.width == 2800.0 and right["x"] == 2800.0


def test_a_pinned_apps_capped_column_pulls_the_os_edge_in_a_cascade(studio):
    """Two columns, the last one capped at 300: the divider dragged LEFT
    opens the last column to its cap, then PULLS the root's frame edge
    along — and through the rigid gap the OS edge: the surface shrinks
    with the divider, the cascade Lukas asked for (09-13)."""
    studio.size = [1000.0, 1500.0]
    root = app_root(studio)
    app_frame(studio, root)
    left, right = root._frame_edges
    divider = {"x": 700.0}
    root._edge_views[("row", "r")] = (root, [left, divider, right])
    root._edge_cells[("row", "r")] = ([60.0, 60.0], [None, 300.0])
    app_frame(studio, root)
    root._pending_drags.append((divider, 500.0, True))       # the last column would be 500 > cap 300
    app_frame(studio, root)
    assert divider["x"] == 500.0
    assert right["x"] == 800.0 and os_x() == (400.0, 1200.0) and studio.size[0] == 800.0
    app_frame(studio, root)
    assert root.width == 800.0 and right["x"] == 800.0


# --- sticky hand drags: replayed from the gesture's start ----------------------------

@pytest.fixture
def hand(monkeypatch):
    """A mouse button held for the whole test (the sticky gesture's lifetime)."""
    monkeypatch.setattr(os_frame, "_any_button_down", lambda: True)


def test_a_sticky_divider_push_returns_the_os_window_when_the_hand_comes_back(studio, hand):
    """Hyprland's right-drag feel (Lukas 09-13): the divider pushed past its
    pile into the OS edge grows the surface; dragged BACK the surface and
    the columns return to exactly where they started — the drag is
    replayed from the gesture's snapshot with the accumulated total."""
    studio.size = [1000.0, 1500.0]
    root = app_root(studio)
    app_frame(studio, root)
    left, right = root._frame_edges
    d0, d1 = {"x": 300.0}, {"x": 800.0}
    root._edge_views[("row", "r")] = (root, [left, d0, d1, right])
    app_frame(studio, root)
    start = (d0["x"], d1["x"], studio.size[0])
    for target in (900.0, 1000.0, 1100.0):              # out: the last column floors at 60, the OS edge follows
        root._pending_drags.append((d1, target, True))
        app_frame(studio, root)
    assert d1["x"] == 1100.0 and studio.size[0] == 1160.0 and os_x() == (400.0, 1560.0)
    for target in (1000.0, 900.0, 800.0):               # back: everything unwinds
        root._pending_drags.append((d1, target, True))
        app_frame(studio, root)
    app_frame(studio, root)
    assert (d0["x"], d1["x"], studio.size[0]) == start
    assert os_x() == (400.0, 1400.0) and right["x"] == 1000.0 and root.width == 1000.0


def test_a_sticky_drag_blocked_by_the_screen_waits_for_the_hand_to_come_back(studio, hand):
    """Pushed past the screen's right edge the OS edge stops at the wall;
    the swallowed travel is REMEMBERED: dragging back moves nothing until
    the hand is back where the wall stopped it, then the edge follows —
    exactly a compositor's sticky resize (Lukas 09-13)."""
    studio.size = [3000.0, 1500.0]                       # right edge at 3400, screen at 3840: 440 of room
    root = app_root(studio)
    app_frame(studio, root)
    left, right = root._frame_edges
    root._pending_drags.append((right, 3000.0 + 700.0, True))    # 260 past the screen: the FLIP grows the window left by it
    app_frame(studio, root)
    app_frame(studio, root)
    assert os_x() == (140.0, 3840.0) and studio.size[0] == 3700.0
    root._pending_drags.append((right, right["x"] - 200.0, True))  # back 200: still 60 past the wall — the flip unwinds to 60
    app_frame(studio, root)
    app_frame(studio, root)
    assert os_x() == (340.0, 3840.0) and studio.size[0] == 3500.0
    root._pending_drags.append((right, right["x"] - 160.0, True))  # back another 160: 100 inside now
    app_frame(studio, root)
    app_frame(studio, root)
    assert os_x()[1] == 3740.0 and studio.size[0] == 3340.0


def test_a_sticky_studio_window_returns_after_pushing_the_os_edge(studio, hand):
    """A free studio root: its divider pushed into the OS edge grows the
    surface; dragged back, root, columns and surface return."""
    w = root(studio, x=2000.0, width=800)               # right edge 2800, OS edge 3400
    left, right = w._frame_edges
    d0 = {"x": 400.0}
    w._edge_views[("row", "r")] = (w, [left, d0, right])
    studio.frame(w)
    start = (w.window_pos[0], w.width, d0["x"], studio.size[0])
    for target in (700.0, 1000.0, 1200.0):              # 60 floor: the frame is pushed from 460 on, the OS edge from 1060
        w._pending_drags.append((d0, target, True))
        studio.frame(w)
    assert studio.size[0] > 3000.0 and w.width > 800
    for target in (1000.0, 700.0, 400.0):
        w._pending_drags.append((d0, target, True))
        studio.frame(w)
    studio.frame(w)
    assert (w.window_pos[0], w.width, d0["x"], studio.size[0]) == start


def test_a_refused_move_teaches_the_wall_and_the_far_edge_never_ratchets_past_the_screen(studio, hand):
    """The studio at y=1 (Hyprland's floating top clamp; the work area says
    0) with its bottom on the display's bottom: the background right-drag
    pushed down is blocked, the flip asks to move up 1 and grow 1, the
    compositor grows it but keeps y=1 — the bottom is 1 px below the
    display. Before: read back as a foreign move with the bottom "where it
    is", the next frame flipped by 1 again — 1 px per frame below the
    display for as long as the hand pushed (09-13). Now the refused move
    teaches the wall, the bottom is pulled back onto the work area, and
    every further frame holds."""
    studio.pos = [400.0, 1.0]
    studio.size = [3000.0, 2159.0]                   # bottom on the work area's bottom (2160)
    studio.refuse_moves = True
    root = app_root(studio)
    app_frame(studio, root)
    for k in range(6):
        os_frame.queue_drag("y", 1, 20)
        app_frame(studio, root)
        assert studio.pos[1] == 1.0
        assert studio.pos[1] + studio.size[1] <= 2160.0 + 1.0, (k, studio.pos, studio.size)
    for _ in range(3):
        app_frame(studio, root)
    assert studio.pos[1] + studio.size[1] == 2160.0 and os_y() == (1.0, 2160.0)


@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('side', [0, 1])
def test_partial_compositor_move_does_not_expand_the_opposite_screen_wall(studio, hand, monkeypatch, axis, side):
    """Size lands, but a compositor inset allows only part of the flip's move.
    The old exact-refusal check missed this and promoted the overhang to a wall.
    """
    i = 0 if axis == 'x' else 1
    limit = studio.area[i + 2]
    studio.pos[i] = 100. if side == 0 else 0.
    studio.size[i] = limit - 100.
    land = studio.land
    def clamped_land():
        land()
        studio.pos[i] = (max(32., studio.pos[i]) if side == 0 else
                         min(limit - 32. - studio.feed_size[i], studio.pos[i]))
    monkeypatch.setattr(studio, 'land', clamped_land)
    root = app_root(studio)
    app_frame(studio, root)
    os_frame.queue_drag(axis, 1 - side, 200. if side == 0 else -200.)
    app_frame(studio, root)
    for _ in range(3):
        app_frame(studio, root)
        near, far = os_frame.edges(axis)
        assert near[axis] >= 0.
        assert far[axis] <= limit
    assert os_frame._STATE['learned'][axis][side] == (32. if side == 0 else limit - 32.)


def test_size_only_followup_preserves_unacknowledged_move(studio, hand):
    studio.pos[1], studio.size[1] = 100., 2060.
    root = app_root(studio)
    app_frame(studio, root)
    os_frame.queue_drag('y', 1, 50.)
    app_frame(studio, root)
    assert os_frame._STATE['last_offset'][1] == -50.
    # Before the compositor reports the move, another size is queued with no
    # new translation. Its zero offset must not hide a subsequent refusal.
    os_frame.edges('y')[1]['y'] -= 1.
    os_frame.flush()
    assert os_frame._STATE['last_offset'][1] == -50.


def test_a_pinned_apps_child_flipping_at_the_screen_pushes_the_os_near_edge_only_by_the_hand(studio, hand):
    """A nested window of an app root, its right edge right-dragged past
    the screen's right wall: the OS far edge stops there and the flip
    pushes the OS NEAR edge out — by exactly the hand's travel, frame
    after frame. The surface's move re-bases the child (it holds the
    screen); the sticky replay must restore it in SCREEN terms, or every
    re-base is undone, the solve pushes again and the push doubles: 48,
    204, 313 px per 108 px of hand — the app filled the display (09-13)."""
    root = app_root(studio)
    app_frame(studio, root)
    child = FakeWindow(width=300, min_width=200, x=2500.0)     # screen 2900..3200, the wall at 3840
    child.id = child.name = "child"
    child.parent_window = root
    child.closable = True
    studio.nested.append(child)
    app_frame(studio, root, child)
    right = child._frame_edges[1]
    total, last_near = 0.0, os_x()[0]
    for _ in range(12):
        total += 100.0
        child._pending_drags.append((right, right["x"] + 100.0, True))
        app_frame(studio, root, child)
        app_frame(studio, root, child)                         # the move lands, the re-base with it
        near, far = os_x()
        assert far <= 3840.0
        assert last_near - near <= 100.0 + 1e-6, (total, near, last_near)   # never more than the hand
        last_near = near
    # the wall at 3840: 440 of growth landed on the OS edge, the rest flipped
    # onto the child's LEFT edge — inside the root, whose 2500 px of room
    # the flip consumes before it could reach the OS near edge
    assert os_x() == (400.0, 3840.0) and studio.size[0] == 3440.0
    assert child.window_pos[0] < 2500.0 and abs_of(child)[0] + child.width == 3440.0


def test_the_os_windows_own_sticky_drag_flips_by_the_hand_not_the_residual(studio, hand):
    """The background right-drag pushed past the screen's right wall: the
    far edge stops, the FLIP moves the near edge out — by the hand's step
    each frame, never by the accumulated travel past the wall (the sticky
    replay restores BOTH OS edges before re-solving; restoring only the
    dragged one re-applied the whole residual to the other edge every
    frame: the app grew to the display in four frames, 09-13)."""
    studio.frame()
    last_near = os_x()[0]
    for k in range(10):
        os_frame.queue_drag("x", 1, 100)               # 440 of room: the wall from the 5th step
        studio.frame()
        near, far = os_x()
        assert far <= 3840.0
        assert last_near - near <= 100.0 + 1e-6, (k, near, last_near)
        last_near = near
    assert os_x() == (0.0, 3840.0)                      # 440 out, 400 flipped, 160 swallowed
    for _ in range(10):
        os_frame.queue_drag("x", 1, -100)
        studio.frame()
    assert os_x() == (400.0, 3400.0)                    # and all the way back


def test_a_nested_hand_move_pushing_the_os_edge_slides_it_back_on_return(studio, hand):
    """A nested window of an app root dragged by its header into the OS
    right edge pushes it out; dragged back, the edge slides back with it
    to where it began — the move's push is re-derived every frame against
    the OS edges as they were when the move began (Lukas 09-13: "edges
    don't slide back")."""
    root = app_root(studio)
    app_frame(studio, root)
    child = FakeWindow(width=300, min_width=200, x=2600.0)     # screen 3000..3300, the OS edge at 3400
    child.id = child.name = "child"
    child.parent_window = root
    child.closable = True
    studio.nested.append(child)
    app_frame(studio, root, child)
    for dx in (100.0, 100.0, 100.0):                          # out: contact after the first 100
        child._pending_move = (dx, 0.0)
        app_frame(studio, root, child)
    assert os_x() == (400.0, 3600.0) and studio.size[0] == 3200.0
    for dx in (-100.0, -100.0, -100.0):                       # back: the edge slides back with it
        child._pending_move = (dx, 0.0)
        app_frame(studio, root, child)
    app_frame(studio, root, child)
    assert os_x() == (400.0, 3400.0) and studio.size[0] == 3000.0
    assert child.window_pos[0] == 2600.0


def test_a_child_hung_from_the_app_roots_far_corner_pushes_the_os_edge_by_the_hand_only(studio, hand):
    """A nested window anchored at the pinned root's RIGHT corner (a
    context menu: parent_anchor top_right) dragged right into the OS
    right edge pushes it out by the hand's step, frame after frame — and
    HOLDS its screen position while the corner it hangs from moves away
    (compensate_far, the far-edge twin of apply_rebase). Uncompensated it
    hung further right with every push and pushed again: the app grew to
    the screen in a dozen frames (Lukas 09-13)."""
    root = app_root(studio)
    app_frame(studio, root)
    child = FakeWindow(width=300, min_width=200, x=-400.0)     # 400 left of the root's right corner
    child.id = child.name = "menu"
    child.parent_window = root
    child.closable = True
    child.parent_anchor_pos = "top_right"
    studio.nested.append(child)
    app_frame(studio, root, child)
    assert abs_of(child)[0] == 2600.0                          # screen 3000..3300, the OS edge at 3400
    last_far = os_x()[1]
    for k in range(4):                                         # 400: 100 of room, then 300 of push (the screen at 3840 stays clear)
        child._pending_move = (100.0, 0.0)
        app_frame(studio, root, child)
        near, far = os_x()
        assert far - last_far <= 100.0 + 1e-6, (k, far, last_far)   # never more than the hand
        assert near + abs_of(child)[0] + child.width <= far + 1e-6  # inside the OS window
        last_far = far
    app_frame(studio, root, child)                                    # the last request lands (the root's width)
    assert os_x() == (400.0, 3700.0) and studio.size[0] == 3300.0
    assert abs_of(child)[0] == 3000.0                                 # screen 3400..3700: exactly where the hand put it


def test_an_older_request_landing_late_is_ours_not_the_compositors(studio, hand):
    """A request goes out every drag frame and the compositor answers two
    frames later: the OLDER request lands while the newer is in flight.
    It is ours — the model keeps the newest, the drag keeps growing — not
    a foreign resize snapping the model back (the app crawled 16 px per
    200 of hand with a popover open, 09-13)."""
    studio.size_lag = 2
    root = app_root(studio)
    app_frame(studio, root)
    left, right = root._frame_edges
    for k in range(1, 9):
        root._pending_drags.append((right, right["x"] + 50.0, True))
        app_frame(studio, root)
        assert os_x()[1] == 3400.0 + 50.0 * k, (k, os_x(), studio.size)
    for _ in range(4):
        app_frame(studio, root)
    assert studio.size[0] == 3400.0 and os_x() == (400.0, 3800.0)


def test_a_child_hung_from_the_app_roots_far_corner_resized_into_the_os_edge_pushes_by_the_hand(studio, hand):
    """The same far-hung child (a context menu) RESIZED: its right frame
    edge right-dragged into the OS right edge pushes it out by the hand's
    step each frame and the window's left edge stays put on screen. The
    sticky replay converts its position through the OS FAR edge it hangs
    from; through the surface origin it undid compensate_far every frame
    and the push doubled (1180 → 2205 for 300 px of hand, 09-13)."""
    root = app_root(studio)
    app_frame(studio, root)
    child = FakeWindow(width=300, min_width=200, x=-400.0)     # screen 3000..3300, the OS edge at 3400
    child.id = child.name = "menu"
    child.parent_window = root
    child.closable = True
    child.parent_anchor_pos = "top_right"
    studio.nested.append(child)
    app_frame(studio, root, child)
    right = child._frame_edges[1]
    last_far = os_x()[1]
    for k in range(5):                                         # 500: 100 of room, then 400 of push
        child._pending_drags.append((right, right["x"] + 100.0, True))
        app_frame(studio, root, child)
        near, far = os_x()
        assert far - last_far <= 100.0 + 1e-6, (k, far, last_far)
        assert abs_of(child)[0] == 2600.0, (k, abs_of(child))          # its left edge never moves
        last_far = far
    app_frame(studio, root, child)
    assert os_x() == (400.0, 3800.0) and studio.size[0] == 3400.0
    assert child.width == 800 and abs_of(child)[0] + child.width == 3400.0   # its right edge ON the OS edge


def test_sticky_window_resize_keeps_newly_learned_compositor_wall(studio, hand):
    studio.pos = [1.0, 300.0]
    studio.size = [3839.0, 1500.0]
    studio.refuse_moves = True
    app = app_root(studio)
    app_frame(studio, app)
    right = app._frame_edges[1]
    for step in range(8):
        app._pending_drags.append((right, right['x'] + 20.0, True))
        app_frame(studio, app)
        if step:
            assert os_x() == (1.0, 3840.0)
            assert studio.size[0] == 3839.0
            assert os_frame._STATE['screen']['x'][0]['x'] == 1.0


def test_acknowledged_resize_does_not_mask_compositor_return_to_previous_size(studio):
    studio.frame()
    os_frame.queue_drag('x', 1, 100)
    studio.frame()
    studio.frame()
    assert studio.size[0] == 3100.0
    assert not os_frame._STATE['size_requests']['x']
    before = len(studio.requests)
    studio.size[0] = 3000.0
    studio.frame()
    assert os_x() == (400.0, 3400.0)
    assert studio.size[0] == 3000.0
    assert len(studio.requests) == before


def test_idle_flush_does_not_record_unsent_sizes(studio):
    for _ in range(4):
        studio.frame()
    assert not studio.requests
    assert os_frame._STATE['size_requests'] == {'x': [], 'y': []}


def test_unanswered_resize_eventually_accepts_compositor_size(studio):
    studio.size_lag = 1000
    studio.frame()
    os_frame.queue_drag('x', 1, 100)
    studio.frame()
    for _ in range(os_frame.INFLIGHT_FRAMES + 2):
        studio.frame()
    assert os_x() == (400.0, 3400.0)
    assert not os_frame._STATE['size_requests']['x']


def test_return_to_observed_size_cancels_pending_growth(studio, hand):
    studio.size_lag = 3
    studio.frame()
    os_frame.queue_drag('x', 1, 100)
    studio.frame()
    os_frame.queue_drag('x', 1, -100)
    studio.frame()
    assert studio.requests[-1][0] == 3000
    for _ in range(5):
        studio.frame()
    assert studio.size[0] == 3000.0
    assert os_x() == (400.0, 3400.0)


@pytest.mark.parametrize('anchor_fraction', [0.0, 0.5, 1.0])
@pytest.mark.parametrize('resize', [False, True])
def test_inspector_uses_its_text_pin_instead_of_the_os_corner(studio, hand, monkeypatch, anchor_fraction, resize):
    """A fixed text view or split column moves differently from the OS edge.
    Both a move and a resize must follow the hand without alternating pushes.
    """
    root = app_root(studio)
    app_frame(studio, root)

    class Inspector(FakeWindow):
        _pin_target = object()
        anchor_offset = (0, 0)

        @property
        def clip_anchor_base(self):
            return (2600.0 + anchor_fraction * (root.width - 3000.0), 0.0)

        def _pinned_base_y(self, base, anchor):
            return base

    child = Inspector(width=300, min_width=200, x=0.0)
    child.id = child.name = 'inspector'
    child.parent_window = root
    child.parent_anchor_pos = 'top_right'
    child.closable = True
    studio.nested.append(child)
    original_abs = abs_of
    monkeypatch.setattr(sys.modules[__name__], 'abs_of',
                        lambda ds: (child.clip_anchor_base[0] + child.window_pos[0], child.window_pos[1])
                        if ds is child else original_abs(ds))
    original_pass = C.window_edge_pass

    def pass_after_parent_layout(ds):
        os_frame.rebase_pin(ds)
        ds.abs_left, ds.abs_top = abs_of(ds)
        return original_pass(ds)

    monkeypatch.setattr(C, 'window_edge_pass', pass_after_parent_layout)
    app_frame(studio, root, child)
    for step in range(1, 5):
        if resize:
            edge = child._frame_edges[1]
            child._pending_drags.append((edge, edge['x'] + 100, True))
        else:
            child._pending_move = (100.0, 0.0)
        app_frame(studio, root, child)
        assert abs_of(child)[0] == 2600.0 + (0 if resize else 100 * step)
        assert os_x()[1] == 3400.0 + max(0, 100 * step - 100)
    app_frame(studio, root, child)
    assert abs_of(child)[0] == 2600.0 + (0 if resize else 400)
    assert child.width == (700 if resize else 300)
    assert os_x() == (400.0, 3700.0)
    for step in range(3, -1, -1):
        if resize:
            edge = child._frame_edges[1]
            child._pending_drags.append((edge, edge['x'] - 100, True))
        else:
            child._pending_move = (-100.0, 0.0)
        app_frame(studio, root, child)
        assert abs_of(child)[0] == 2600.0 + (0 if resize else 100 * step)
        assert os_x()[1] == 3400.0 + max(0, 100 * step - 100)
    app_frame(studio, root, child)
    assert abs_of(child)[0] == 2600.0
    assert child.width == 300


@pytest.mark.parametrize('driver', ['os', 'root', 'compositor'])
@pytest.mark.parametrize('axis', ['x', 'y'])
def test_os_shrink_compresses_inspector_without_reopening_parent(studio, hand, driver, axis):
    root = app_root(studio)
    app_frame(studio, root)
    child = FakeWindow(width=300, min_width=100, x=-400.0,
                       height=300, min_height=100, y=-400.0)
    child.id = child.name = 'inspector'
    child.parent_window = root
    child.parent_anchor_pos = 'bottom_right'
    child.closable = True
    studio.nested.append(child)
    app_frame(studio, root, child)
    i = 0 if axis == 'x' else 1
    initial = studio.pos[i], studio.pos[i] + studio.size[i]
    if driver == 'os':
        os_frame.queue_drag(axis, 1, -200.0)
    elif driver == 'compositor':
        studio.size[i] -= 200
    else:
        edge = C._frame(root, axis)[1]
        C._pending(root, axis).append((edge, edge[axis] - 200, True))
    app_frame(studio, root, child)
    assert tuple(e[axis] for e in os_frame.edges(axis)) == (initial[0], initial[1] - 200)
    assert (child.width if axis == 'x' else child.height) == 200
    app_frame(studio, root, child)
    assert tuple(e[axis] for e in os_frame.edges(axis)) == (initial[0], initial[1] - 200)
    assert (child.width if axis == 'x' else child.height) == 200


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_idle_release_forgets_os_sticky_snapshot(studio, monkeypatch, axis):
    down = [True]
    monkeypatch.setattr(os_frame, '_any_button_down', lambda: down[0])
    root = app_root(studio)
    app_frame(studio, root)
    os_frame.queue_drag(axis, 1, 100)
    app_frame(studio, root)
    app_frame(studio, root)  # request settled while the hand is still down
    down[0] = False
    app_frame(studio, root)  # release without any movement to solve
    assert axis not in os_frame._STATE['gestures']
    i = 0 if axis == 'x' else 1
    studio.pos[i] += 200  # user moves the GLFW window between drags
    app_frame(studio, root)
    before = tuple(edge[axis] for edge in os_frame.edges(axis))
    down[0] = True
    os_frame.queue_drag(axis, 1, 20)
    app_frame(studio, root)
    assert tuple(edge[axis] for edge in os_frame.edges(axis)) == (before[0], before[1] + 20)


def test_background_release_applies_only_its_remaining_motion(studio, monkeypatch):
    from types import SimpleNamespace
    handler = SimpleNamespace(is_down=lambda button: False)
    monkeypatch.setattr(Melty, 'event_handler', handler)
    monkeypatch.setattr(Melty, 'events', {tb._RESIZE_ID: {
        'non_blocking_right_mouse_dragged': SimpleNamespace(total_dx=100.0, total_dy=0.0)}})
    monkeypatch.setattr(tb, '_rdrag', {'top_left': False, 'x': 80.0, 'y': 0.0})
    tb.poll_os_window_drag()
    assert os_frame._STATE['pending']['x'] == [(1, 20.0)]
    assert tb._rdrag is None


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_failed_window_solve_restores_shared_screen_coordinates(studio, monkeypatch, axis):
    window = FakeWindow(x=120, y=90)
    studio.roots = [window]
    studio.frame(window)
    original = [(edge, edge[axis]) for edge in
                (*os_frame.edges(axis), *os_frame._STATE['screen'][axis])]
    pending = window._pending_drags if axis == 'x' else window._pending_row_drags
    pair = window._frame_edges if axis == 'x' else window._frame_rows
    pending.append((pair[1], pair[1][axis] + 10, True))

    def fail_solve(window, solve_axis, context):
        assert context is not None
        assert context.base != 0
        assert original[0][0][axis] != original[0][1]
        raise RuntimeError('injected collision failure')

    with monkeypatch.context() as patch:
        patch.setattr(C, '_solve_collisions', fail_solve)
        with pytest.raises(RuntimeError, match='injected collision failure'):
            C._frame_pass(window, axis)
    assert [edge[axis] for edge, _ in original] == pytest.approx([value for _, value in original])
    # A later frame must be able to solve in the original coordinate system.
    studio.frame(window)
    assert window.width > 0 and window.height > 0


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_sticky_resize_survives_foreign_surface_translation(studio, hand, axis):
    window = root(studio, x=100, width=400)
    index = 0 if axis == 'x' else 1
    pair = window._frame_edges if axis == 'x' else window._frame_rows
    pending = '_pending_drags' if axis == 'x' else '_pending_row_drags'
    initial_pos = window.window_pos
    initial_size = window.width if axis == 'x' else window.height
    getattr(window, pending).append((pair[1], pair[1][axis] + 20, True))
    studio.frame(window)
    # Moving to another desktop/monitor changes screen coordinates, not
    # the dragged view's position inside the surface.
    studio.pos[index] += 21982
    area = list(studio.area)
    area[index] += 21982
    studio.area = tuple(area)
    getattr(window, pending).append((pair[1], pair[1][axis] - 10, True))
    studio.frame(window)
    assert window.window_pos == initial_pos
    assert (window.width if axis == 'x' else window.height) == initial_size + 10
    getattr(window, pending).append((pair[1], pair[1][axis] - 10, True))
    studio.frame(window)
    assert window.window_pos == initial_pos
    assert (window.width if axis == 'x' else window.height) == initial_size


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_sticky_resize_restarts_snapshot_when_geometry_feed_returns(studio, hand, axis):
    studio.mode = None
    window = root(studio, x=100, width=400)
    pair = window._frame_edges if axis == 'x' else window._frame_rows
    pending = '_pending_drags' if axis == 'x' else '_pending_row_drags'
    initial_pos = window.window_pos
    initial_size = window.width if axis == 'x' else window.height
    getattr(window, pending).append((pair[1], pair[1][axis] + 20, True))
    studio.frame(window)
    studio.mode = 'feed'
    studio.pos = [21982.0, 341.0]
    studio.area = (21982.0, 341.0, 3840.0, 2160.0)
    getattr(window, pending).append((pair[1], pair[1][axis] - 10, True))
    studio.frame(window)
    assert window.window_pos == initial_pos
    assert (window.width if axis == 'x' else window.height) == initial_size + 10


def _classify_frame_pinned(window, kwargs):
    """Run the wrapper's actual classification without a GL rendering context."""
    import ast
    from pathlib import Path
    import meltygui.core.core_render as core_render
    path = Path(core_render.__file__)
    tree = ast.parse(path.read_text())
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Attribute) and target.attr == '_frame_pinned'
                              for target in node.targets))
    code = compile(ast.Expression(assignment.value), str(path), 'eval')
    return eval(code, dict(kwargs=kwargs, draw_state=window, closable=kwargs['closable'],
                           passed_width=kwargs['width'], passed_height=kwargs['height']))


def test_fixed_picker_keeps_size_during_parent_resize(studio, monkeypatch):
    from meltygui.core.windowing.surface import root_view_kwargs
    parent = app_root(studio)
    app_frame(studio, parent)
    picker = nested(studio, parent, x=100, width=280, name='color_picker')
    picker.height = 430
    kwargs = dict(closable=True, draggable=False, width=280, height=430,
                  window_pos=(0, 10))
    picker._frame_pinned = _classify_frame_pinned(picker, kwargs)
    assert not picker._frame_pinned
    # A surface root remains coupled to the OS window.
    monkeypatch.setattr(Melty, "root_fill", (3000, 1500, 0))
    assert _classify_frame_pinned(parent, root_view_kwargs('root'))
    # Even if root kwargs flow to a child, that child is not the surface.
    assert not _classify_frame_pinned(picker, root_view_kwargs('nested'))
    for width, height in ((2500, 1300), (2900, 1400), (2400, 1200)):
        studio.size = [width, height]
        app_frame(studio, parent, picker)
        assert (picker.width, picker.height) == (280, 430)


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_off_surface_window_edges_never_form_a_cycle(axis):
    near, far = {axis: 100.0}, {axis: 500.0}
    outside_near, outside_far = {axis: 550.0}, {axis: 750.0}
    roots = [(None, outside_near, outside_far, 100.0)]
    ranked = os_frame._flat_chain_ranked([near, far], roots, axis)
    chain = [edge for edge, _ in ranked]
    positions = {id(edge): index for index, edge in enumerate(chain)}
    assert positions[id(outside_near)] < positions[id(outside_far)]
    floors = [os_frame._chain_floor(a, ra, b, rb, axis)
              for (a, ra), (b, rb) in zip(ranked, ranked[1:])]
    graph = C._EdgeGraph(C._cells_from_lists(
        [chain, [outside_near, outside_far]], axis,
        specs=[(floors, [None] * len(floors)), ([100.0], [200.0])]))
    C._solve_graph(graph, far, 400.0, axis=axis)
    assert outside_far[axis] == 400.0
    assert outside_near[axis] == 300.0


@pytest.mark.parametrize('axis', ['x', 'y'])
def test_os_sticky_return_restores_windows_and_their_layouts(studio, hand, axis):
    w = root(studio, x=100.0, width=400, min_width=120)
    w.window_pos = (100.0, 100.0)
    w.height, w.min_height = 400, 80
    studio.frame(w)
    near, far = C._frame(w, axis)
    divider = {axis: 200.0}
    C._views(w, axis)[('row' if axis == 'x' else 'rows', 'test')] = (w, [near, divider, far])
    studio.frame(w)
    before = w.window_pos, w.width, w.height, divider[axis], tuple(e[axis] for e in os_frame.edges(axis))
    os_frame.queue_drag(axis, 0, 500.0)
    studio.frame(w)
    os_frame.queue_drag(axis, 0, -500.0)
    studio.frame(w)
    studio.frame(w)  # compositor applies the final move, with its coordinate rebase
    assert (w.window_pos, w.width, w.height, divider[axis], tuple(e[axis] for e in os_frame.edges(axis))) == before


@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('driver', ['os', 'root'])
def test_glfw_sticky_return_restores_nested_tile_edges(studio, hand, axis, driver):
    studio.size = studio.feed_size = [1200.0, 850.0]
    root_window = app_root(studio)
    app_frame(studio, root_window)
    child = FakeWindow(width=700, min_width=180, x=150, height=500, min_height=120, y=100)
    child.id = child.name = 'tiles'
    child.closable = True
    child.parent_window = root_window
    studio.nested.append(child)
    app_frame(studio, root_window, child)
    near, far = C._frame(child, axis)
    dividers = [{axis: 200.0}, {axis: 350.0}]
    C._views(child, axis)[('row' if axis == 'x' else 'rows', 'tiles')] = (child, [near, *dividers, far])
    app_frame(studio, root_window, child)
    before = child.window_pos, child.width, child.height, [e[axis] for e in C._all_edges(child, axis)]
    for increment in (-500.0, -100.0, 600.0):
        if driver == 'os':
            os_frame.queue_drag(axis, 1, increment)
        else:
            edge = C._frame(root_window, axis)[1]
            C._pending(root_window, axis).append((edge, edge[axis] + increment, True))
        app_frame(studio, root_window, child)
    app_frame(studio, root_window, child)
    assert (child.window_pos, child.width, child.height, [e[axis] for e in C._all_edges(child, axis)]) == before


@pytest.mark.parametrize('axis,edge', [('x', 0), ('x', 1), ('y', 0), ('y', 1)])
def test_edge_push_at_size_floor_accepts_delayed_move_replies(studio, hand, monkeypatch, axis, edge):
    index = 0 if axis == 'x' else 1
    studio.size = studio.feed_size = [400., 400.]
    root = app_root(studio)
    root.min_width = root.min_height = 400.
    root.height = 400.
    app_frame(studio, root)
    initial = studio.pos[index]
    observations = [studio.observe()] * 2
    def delayed_observe():
        observations.append(studio.observe())
        return observations.pop(0)
    monkeypatch.setattr(os_frame, '_observe', delayed_observe)
    step = 20. if edge == 0 else -20.
    for number in range(10):
        os_frame.queue_drag(axis, edge, step)
        app_frame(studio, root)
        assert os_frame._STATE['expected'][index] == initial + step * (number + 1)
        assert os_frame._STATE['learned'][axis] == [None, None]
    for _ in range(4):
        app_frame(studio, root)
    assert studio.pos[index] == initial + step * 10
    assert studio.size[index] == 400.
    assert not os_frame._STATE['move_requests'][axis]


@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('resize', [False, True])
@pytest.mark.parametrize('anchor_fraction', [0.0, 0.5, 1.0])
def test_inline_panel_through_view_parent_tracks_hand(
        studio, hand, monkeypatch, axis, direction, resize, anchor_fraction):
    """The params panel's explicit parent is a text view inside the app root.

    Surface translation and text reflow must each be compensated once,
    including while stationary and on a full return of the held gesture.
    """
    from types import SimpleNamespace
    root = app_root(studio)
    app_frame(studio, root)
    index = 0 if axis == 'x' else 1
    original_size = (root.width, root.height)
    start = 100.0 if direction < 0 else original_size[index] - 400.0

    class TextParent(SimpleNamespace):
        @property
        def abs_left(self):
            return anchor_fraction * (root.width - original_size[0])

        @property
        def abs_top(self):
            return anchor_fraction * (root.height - original_size[1])

    # Two ordinary ancestors must not be mistaken for two nested windows.
    container = SimpleNamespace(parent_window=root, closable=False)
    view = TextParent(parent_window=container, closable=False)

    class InlinePanel(FakeWindow):
        left_offset = top_offset = 0.0
        parent_anchor_offset = (0.0, 0.0)

        def _ancestor_scroll(self):
            return 0.0, 0.0

    child = InlinePanel(width=300, height=300, x=100., y=100.)
    child.id = child.name = 'inline-params'
    child.parent_window = view
    child.closable = True
    position = list(child.window_pos)
    position[index] = start
    child.window_pos = tuple(position)
    studio.nested.append(child)
    original_abs = abs_of

    def actual_position(window):
        if window is child:
            return (view.abs_left + child.window_pos[0],
                    view.abs_top + child.window_pos[1])
        return original_abs(window)

    monkeypatch.setattr(sys.modules[__name__], 'abs_of', actual_position)
    original_pass = C.window_edge_pass
    travel = None

    def pass_after_layout(window):
        if window is child and travel is not None and not resize:
            # The real wrapper uses the press baseline plus total travel,
            # including held stationary frames. Rebase must update that latch.
            position = list(child._initial_window_pos)
            position[index] += travel
            child.window_pos = tuple(position)
            child._hand_move_frame = Melty.frame_count
        os_frame.rebase_pin(window)
        window.abs_left, window.abs_top = actual_position(window)
        return original_pass(window)

    monkeypatch.setattr(C, 'window_edge_pass', pass_after_layout)
    app_frame(studio, root, child)
    child._initial_window_pos = child.window_pos
    initial = os_frame.applied_origin(axis) + actual_position(child)[index]
    initial_frame = tuple(edge[axis] for edge in os_frame.edges(axis))
    previous = 0.0
    for distance in (0, 50, 100, 150, 200, 200, 200, 150, 100, 50, 0, 0):
        travel = direction * distance
        if resize:
            edge = C._frame(child, axis)[0 if direction < 0 else 1]
            C._pending(child, axis).append((edge, edge[axis] + travel - previous, True))
        app_frame(studio, root, child)
        expected = initial + (travel if not resize or direction < 0 else 0)
        assert os_frame.applied_origin(axis) + actual_position(child)[index] == pytest.approx(expected)
        size = child.width if axis == 'x' else child.height
        assert size == pytest.approx(300 + (distance if resize else 0))
        previous = travel
    assert tuple(edge[axis] for edge in os_frame.edges(axis)) == initial_frame
