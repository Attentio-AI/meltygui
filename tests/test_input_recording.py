"""Input recording: the observer funnel, the file, the compile step and the
replay driver (against a fake MCP desktop; the live path needs an agent seat)."""
import asyncio
import json

import meltygui.core.input.input_handler as input_handler
from meltygui.core.automation.input_replay import batch_steps
from meltygui.core.automation.input_replay import replay_steps
from meltygui.core.automation.input_replay import window_runs
from meltygui.model.input_recording_model import InputRecorder
from meltygui.model.input_recording_model import InputRecording
from meltygui.model.input_recording_model import compile_steps
from meltygui.model.input_recording_model import key_combo
from meltygui.model.input_recording_model import readable_text

WINDOW = [0.0, "window", "App", 800, 600]


def record(tmp_path, feed, **start):
    recorder = InputRecorder(folder=tmp_path, window_probe=lambda: ("App", 800, 600))
    path = recorder.start(**start)
    try:
        feed(recorder)
    finally:
        recorder.stop()
    return recorder, path


# --- the funnel ----------------------------------------------------------------------

def test_observers_see_what_the_app_receives(monkeypatch):
    seen = []
    observer = lambda kind, *args: seen.append((kind, *args))
    monkeypatch.setitem(input_handler._INPUT_TAP, "fn", None)
    input_handler.add_input_observer(observer)
    input_handler.add_input_observer(observer)          # registering twice listens once
    try:
        assert input_handler.input_tap("move", 1, 2) is False
        # A consuming tap (an Orchestrator replay muting the real mouse): the
        # app never receives the event, so neither does a recording.
        monkeypatch.setitem(input_handler._INPUT_TAP, "fn", lambda kind, *args: True)
        assert input_handler.input_tap("move", 3, 4) is True
    finally:
        input_handler.remove_input_observer(observer)
    input_handler.input_tap("move", 5, 6)
    assert seen == [("move", 1, 2)]


def test_effect_hook_fans_out():
    from meltygui.core.melty import Melty
    heard = []
    listener = lambda kind, name, draw_state=None, rect=None, text=None: heard.append((kind, name, text))
    Melty.effect_listeners.append(listener)
    try:
        Melty.effect_hook("button", "run_id", None, rect=(0, 0, 1, 1), text="Run")
    finally:
        Melty.effect_listeners.remove(listener)
    assert heard == [("button", "run_id", "Run")]


# --- the file ------------------------------------------------------------------------

def test_recorder_writes_through_the_input_funnel(tmp_path, monkeypatch):
    monkeypatch.setitem(input_handler._INPUT_TAP, "fn", None)

    def feed(recorder):
        input_handler.input_tap("move", 5.0, 5.0)
        input_handler.input_tap("move", 5.2, 5.3)               # jitter under move_min_px
        input_handler.input_tap("down", "left_mouse", 5.0, 5.0)
        input_handler.input_tap("down", "a_key", 5.0, 5.0)      # keys arrive as "key"/"char"
        input_handler.input_tap("axes", "space_mouse", (0,) * 6)
        recorder.observe_effect("button", "run_id", text="Run")
        input_handler.input_tap("up", "left_mouse", 5.0, 5.0)

    recorder, path = record(tmp_path, feed)
    assert not input_handler._INPUT_OBSERVERS and not recorder.recording
    recording = InputRecording.load(path)
    assert recording.header["version"] == 1
    assert [event[1:] for event in recording.events] == [
        ["window", "App", 800, 600],
        ["move", 5.0, 5.0],
        ["down", "left_mouse", 5.0, 5.0],
        ["effect", "button", "run_id", "Run"],
        ["up", "left_mouse", 5.0, 5.0]]
    assert recording.windows() == {"App": (800, 600)}
    assert recorder.recordings() == [path]


def test_typed_text_can_stay_out(tmp_path):
    def feed(recorder):
        recorder.observe_input("key", ord("A"), 0)
        recorder.observe_input("char", ord("a"))
        recorder.observe_input("key", ord("S"), 2)      # ctrl+s is a command, not text
        recorder.observe_input("key", 257, 0)           # so is Return

    _, path = record(tmp_path, feed, record_text=False)
    assert [event[1:] for event in InputRecording.load(path).events if event[1] != "window"] == [
        ["key", ord("S"), 2], ["key", 257, 0]]


def test_load_drops_the_stopping_click_and_a_torn_line(tmp_path):
    path = tmp_path / "crashed.input.jsonl"
    lines = [{"format": "meltygui-input-recording", "version": 1}, WINDOW,
             [1.0, "down", "left_mouse", 1, 1], [1.1, "up", "left_mouse", 1, 1],
             [2.0, "down", "left_mouse", 9, 9], [2.1, "up", "left_mouse", 9, 9],
             [2.1, "discard_last_press"]]
    path.write_text("\n".join(json.dumps(line) for line in lines) + '\n[3.0, "mo')
    events = InputRecording.load(path).events
    assert [event[0] for event in events] == [0.0, 1.0, 1.1]


def test_prune_keeps_the_newest(tmp_path):
    import os
    for index in range(4):
        path = tmp_path / f"{index}.input.jsonl"
        path.write_text("{}")
        os.utime(path, (index, index))
    recorder = InputRecorder(folder=tmp_path)
    recorder.prune(2)
    assert [path.name for path in recorder.recordings()] == ["3.input.jsonl", "2.input.jsonl"]


# --- compiling -----------------------------------------------------------------------

def test_click_is_anchored_by_its_button_text():
    steps = compile_steps([WINDOW,
                           [1.0, "down", "left_mouse", 100, 50], [1.1, "up", "left_mouse", 101, 50],
                           [1.12, "effect", "button", "run_id", "  Run"]])     # flat_button: after the click
    assert len(steps) == 1
    assert (steps[0].tool, steps[0].window, steps[0].gap, steps[0].text) == ("click", "App", 1.0, "Run")
    assert steps[0].args == {"x": 100, "y": 50, "button": "left", "count": 1}

    pressed = compile_steps([WINDOW, [1.0, "down", "left_mouse", 100, 50],
                             [1.01, "effect", "button", "Apply", "Apply"],          # draw_button: on the press
                             [1.1, "up", "left_mouse", 100, 50]])
    assert pressed[0].text == "Apply"


def test_double_click_drag_and_scroll():
    steps = compile_steps([WINDOW,
                           [1.0, "down", "right_mouse", 0, 0], [1.05, "up", "right_mouse", 0, 0],
                           [1.2, "down", "right_mouse", 1, 0], [1.25, "up", "right_mouse", 1, 0],
                           [2.0, "down", "left_mouse", 0, 0],
                           *[[2.0 + index / 100, "move", index * 10, 0] for index in range(1, 21)],
                           [2.5, "up", "left_mouse", 200, 0],
                           [3.0, "change", "scroll_y", -1.0], [3.05, "change", "scroll_y", -1.0],
                           [4.0, "change", "scroll_y", 1.0]])
    click, drag, down, up = steps
    assert click.args == {"x": 0, "y": 0, "button": "right", "count": 2}
    assert (drag.args["x1"], drag.args["x2"], drag.args["duration"]) == (0, 200, 0.5)
    assert 0 < len(drag.args["via"]) <= 8 and drag.args["via"][-1] == [200, 0]
    assert (down.args["dy"], up.args["dy"]) == (2.0, -1.0)  # GLFW up is positive, the MCP tool's down is
    assert batch_steps(down, "0x1")[-1] == {"tool": "scroll", "args": {"dy": 1, "dx": 0}}    # 1.5 units a notch
    assert (down.args["x"], down.args["y"]) == (200, 0)      # at the cursor


def test_keys_and_text():
    assert key_combo(ord("S"), 2 | 1) == "ctrl+shift+s"
    assert key_combo(257, 0) == "Return"
    assert key_combo(ord("A"), 1) is None       # typing: the "char" event carries it
    assert key_combo(341, 2) is None            # bare Control
    assert readable_text("") is None
    steps = compile_steps([[1.0, "key", ord("H"), 1], [1.0, "char", ord("H")], [1.1, "key", ord("I"), 0],
                           [1.1, "char", ord("i")], [1.3, "key", 257, 0], [1.5, "key", ord("S"), 2]])
    assert [(step.tool, step.args) for step in steps] == [
        ("type", {"text": "Hi"}), ("keys", {"keys": ["Return", "ctrl+s"]})]


# --- replaying -----------------------------------------------------------------------

class FakeDesktop:
    """DesktopSession's two methods. `unreadable` texts fail click_text the
    way the server does: the batch stops at that step."""

    def __init__(self, titles=("App",), unreadable=()):
        self.titles, self.unreadable, self.calls = titles, unreadable, []

    async def find_window(self, title, timeout=0):
        if title is not None and title not in self.titles:
            raise RuntimeError(f"no window titled {title!r}")
        return "0xabc"

    async def call(self, tool, **args):
        self.calls.append((tool, args))
        if tool != "batch":
            return {}
        for index, step in enumerate(args["steps"]):
            if step.get("tool") == "click_text" and step["args"]["text"] in self.unreadable:
                return [{"ok": False, "steps_run": index + 1, "error": "no text"}]
        return [{"ok": True, "steps_run": len(args["steps"])}]


CLICKS = [WINDOW,
          [1.0, "down", "left_mouse", 100, 50], [1.1, "up", "left_mouse", 100, 50],
          [1.12, "effect", "button", "run_id", "Run"],
          [2.0, "down", "left_mouse", 300, 50], [2.1, "up", "left_mouse", 300, 50],
          [2.5, "char", ord("x")]]


def test_replay_sends_one_batch_per_window_run():
    desktop = FakeDesktop()
    result = asyncio.run(replay_steps(desktop, compile_steps(CLICKS), {"App": (800, 600)}))
    assert (result.ok, result.completed, result.anchored, result.fallbacks) == (True, 3, 1, 0)
    assert [tool for tool, _ in desktop.calls] == ["place", "batch"]
    sent = desktop.calls[1][1]["steps"]
    assert sent[1]["tool"] == "click_text" and sent[1]["args"]["window"] == "0xabc"
    assert sent[1]["args"]["x"] < 100 < sent[1]["args"]["x"] + sent[1]["args"]["width"]
    assert sent[-1] == {"type": "x"}


def test_replay_falls_back_to_coordinates_when_ocr_misses():
    desktop = FakeDesktop(unreadable=("Run",))
    result = asyncio.run(replay_steps(desktop, compile_steps(CLICKS)))
    assert (result.ok, result.completed, result.anchored, result.fallbacks) == (True, 3, 0, 1)
    retry = desktop.calls[-1][1]["steps"]
    assert retry[1] == {"tool": "click", "args": {"x": 100, "y": 50, "button": "left", "count": 1,
                                                   "window": "0xabc"}}


def test_replay_reports_the_step_it_could_not_run():
    result = asyncio.run(replay_steps(FakeDesktop(titles=()), compile_steps(CLICKS)))
    assert not result.ok and result.completed == 0 and result.failed_step.tool == "click"


def test_pauses_scale_and_typing_stays_in_the_focused_window():
    step = compile_steps(CLICKS)[1]
    assert batch_steps(step, "0x1", speed=2.0)[0] == {"sleep": 0.45}
    other = compile_steps([WINDOW, [1.0, "down", "left_mouse", 1, 1], [1.1, "up", "left_mouse", 1, 1],
                           [1.5, "window", "Child", 300, 200], [1.5, "char", ord("x")],
                           [2.0, "down", "left_mouse", 1, 1], [2.1, "up", "left_mouse", 1, 1]])
    assert [(title, len(run)) for title, run in window_runs(other)] == [("App", 2), ("Child", 1)]


def test_a_repeat_click_under_the_cursor_skips_ocr():
    again = [WINDOW, *CLICKS[1:4],
             [1.8, "down", "left_mouse", 101, 50], [1.9, "up", "left_mouse", 101, 50],
             [1.92, "effect", "button", "run_id", "Run"]]
    desktop = FakeDesktop()
    result = asyncio.run(replay_steps(desktop, compile_steps(again)))
    assert (result.completed, result.anchored) == (2, 1)
    tools = [step.get("tool") for step in desktop.calls[-1][1]["steps"] if "tool" in step]
    assert tools == ["click_text", "click"]     # the cursor covers "Run" the second time
