"""The Hyprland feed serves its last frame through a transient poll miss.

A dropped reply mid-resize used to flip the feed unavailable at once; os_frame
then switched to walls mode, resetting every gesture and every root's view of
the OS edges. Only a persistent failure may drop the feed.

Run: .venv/bin/pytest tests/test_geometry_feed_grace.py -q
"""
import pytest

from meltygui.core.windowing import geometry_feed as feed


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def loop(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(feed.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(feed.time, "sleep", clock.sleep)
    monkeypatch.setattr(feed, "hyprland_socket_path", lambda: "/tmp/hypr.sock")
    monkeypatch.setattr(feed, "forget_socket", lambda: None)
    monkeypatch.setattr(feed, "_hypr_poll_monitors", lambda path: None)
    monkeypatch.setattr(feed, "_hypr_poll_interval", lambda: 0.01)
    monkeypatch.setitem(feed._STATE, "available", False)
    monkeypatch.setitem(feed._STATE, "error", None)
    monkeypatch.setitem(feed._STATE, "running", True)
    monkeypatch.setitem(feed._STATE, "gen", 7)
    return clock


def _drive(monkeypatch, outcomes):
    """Run the poll loop through ``outcomes`` (True = reply, False = miss),
    recording the feed's availability after each poll; a final None stops."""
    seen = []
    script = list(outcomes)

    def poll(pid, path):
        if not script:
            feed._STATE["running"] = False
            raise StopIteration
        ok = script.pop(0)
        seen.append(feed._STATE["available"])
        if not ok:
            raise RuntimeError("timed out")

    monkeypatch.setattr(feed, "_hypr_poll_windows", poll)
    try:
        feed._hyprland_thread_main(pid=1, gen=7)
    except StopIteration:
        pass
    return seen


def test_one_missed_reply_keeps_the_feed_available(monkeypatch, loop):
    seen = _drive(monkeypatch, [True, False, True, True])
    # availability observed at the start of each poll: after the miss it is still True
    assert seen == [False, True, True, True]
    assert max(loop.sleeps) < feed.RETRY_SECONDS


def test_persistent_failure_drops_the_feed_after_the_grace(monkeypatch, loop):
    misses = int(feed.HYPR_POLL_GRACE_S / feed.HYPR_POLL_RETRY_S) + 2
    seen = _drive(monkeypatch, [True] + [False] * misses + [True])
    assert seen[1] is True                      # the grace window starts available
    assert False in seen[2:]                    # ... and expires into unavailable
    assert feed.RETRY_SECONDS in loop.sleeps or any(s >= 0.25 for s in loop.sleeps)
