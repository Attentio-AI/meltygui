"""Input recordings: capture what the user did, and compile it for a replay.

Three pieces, none of which needs the desktop MCP server:

* `InputRecorder` writes every input event the app receives to a JSON-lines
  file. The input callback only queues a tuple; a writer thread encodes and
  flushes, so a recording survives a crash and costs the frame nothing.
* `InputRecording` reads such a file back (tolerating the torn last line a
  crash leaves).
* `compile_steps` turns raw events into `ReplayStep`s - click, drag, scroll,
  keys, type - which core/automation/input_replay.py sends to the MCP server.

File format: line 1 is the header object, every other line one event array
`[seconds_since_start, kind, *args]`:

    [t, "window", title, width, height]                       the OS window the following events happen in, content size
    [t, "move", x, y]                                         content coordinates, as the views see them
    [t, "down" | "up", "left_mouse", x, y]
    [t, "change", "scroll_y" | "scroll_x", value]
    [t, "key", glfw_key, glfw_mods]                           press and repeat
    [t, "char", codepoint]
    [t, "effect", kind, name, text]                           Melty.effect_hook: a fired button, a raised window
    [t, "discard_last_press"]                                 the press before this one stopped the recording
"""
import dataclasses
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path

FORMAT = "meltygui-input-recording"
VERSION = 1
SUFFIX = ".input.jsonl"

MOUSE_BUTTONS = {"left_mouse": "left", "right_mouse": "right", "middle_mouse": "middle"}

# Replay-side gesture thresholds; the first two mirror input_handler's
# CLICK_MAX_DISTANCE and DOUBLE_CLICK_WINDOW so a recorded double click
# compiles to one.
CLICK_MAX_DISTANCE = 5.0
DOUBLE_CLICK_WINDOW = 0.25
SCROLL_MERGE_WINDOW = 0.15      # wheel events closer than this become one scroll step
DRAG_VIA_POINTS = 8             # a drag's path is resampled to at most this many waypoints

# GLFW's key vocabulary, spelled out so a recording compiles without glfw.
# Printable keys (32..96) are their ASCII code; these are the named ones, as
# the X keysym names the MCP server's `key` tool takes.
GLFW_MOD_SHIFT, GLFW_MOD_CONTROL, GLFW_MOD_ALT, GLFW_MOD_SUPER = 1, 2, 4, 8
GLFW_MODIFIER_KEYS = range(340, 348)
GLFW_NAMED_KEYS = {
    256: "Escape", 257: "Return", 258: "Tab", 259: "BackSpace", 260: "Insert", 261: "Delete",
    262: "Right", 263: "Left", 264: "Down", 265: "Up", 266: "Prior", 267: "Next",
    268: "Home", 269: "End", 335: "KP_Enter",
    **{290 + index: f"F{index + 1}" for index in range(25)},
}
GLFW_PRINTABLE_NAMES = {
    32: "space", 39: "apostrophe", 44: "comma", 45: "minus", 46: "period", 47: "slash",
    59: "semicolon", 61: "equal", 91: "bracketleft", 92: "backslash", 93: "bracketright",
    96: "grave",
}


def default_folder():
    from meltygui.core.runtime.paths import cache_root
    return cache_root() / "input_recordings"


# --- recording ---------------------------------------------------------------------

class InputRecorder:
    """Start / stop a recording; the value the recorder window renders.

    `window_probe()` returns the OS window receiving input right now as
    `(title, content_width, content_height)`; core/automation/input_recording_core.py
    supplies the real one, so the capture path holds no windowing code.
    """

    def __init__(self, folder=None, window_probe=None):
        self.folder = Path(folder) if folder else default_folder()
        self.window_probe = window_probe
        self.path = None            # the file being written; None while idle
        self.last_path = None       # the most recent finished recording
        self.error = None           # why the last recording stopped early, if it did
        self._queue = None
        self._writer = None
        self._started = 0.0
        self._window = None
        self._last_move = None
        self._move_min_px = 1.0
        self._record_text = True

    @property
    def recording(self):
        return self.path is not None

    def start(self, path=None, *, move_min_px=1.0, record_text=True, keep=None, header=None):
        """Begin writing to `path` (default: a timestamped file in `folder`).

        `move_min_px` drops cursor jitter; `record_text=False` leaves typed
        characters and unmodified keys out (analytics without keystrokes);
        `keep` deletes the oldest recordings so that many remain with this one.
        """
        if self.recording:
            return self.path
        if keep is not None:
            self.prune(keep - 1)
        from meltygui.core.input.input_handler import add_input_observer
        path = Path(path) if path else self.folder / (time.strftime("%Y%m%d-%H%M%S") + SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.error = path, None
        self._started = time.monotonic()
        self._window = self._last_move = None
        self._move_min_px, self._record_text = move_min_px, record_text
        self._queue = queue.SimpleQueue()
        self._queue.put({"format": FORMAT, "version": VERSION, "argv": list(sys.argv),
                         "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                         "record_text": record_text, **(header or {})})
        self._writer = threading.Thread(target=self._write, args=(path, self._queue),
                                        name="input-recorder", daemon=True)
        self._writer.start()
        add_input_observer(self.observe_input)
        self._changed()
        return path

    def stop(self, discard_last_press=False):
        """Finish the file. `discard_last_press=True` when a click inside the
        app stopped the recording: that click is not part of what was recorded."""
        if not self.recording:
            return self.last_path
        from meltygui.core.input.input_handler import remove_input_observer
        remove_input_observer(self.observe_input)
        if discard_last_press:
            self._put("discard_last_press")
        self._queue.put(None)
        self._writer.join(timeout=2.0)
        self.last_path, self.path = self.path, None
        self._queue = self._writer = None
        self._changed()
        return self.last_path

    def _changed(self):
        """Recording started or stopped: repaint the cached views of this
        recorder (the trace_report_model.reports_changed pattern)."""
        try:
            from meltygui.core.melty import Melty
            from meltygui.core.windowing.glfw_utils import request_render
            Melty.cache.invalidate_up_by_obj(self, force=True)
            request_render()
        except Exception:
            pass                                  # no app running (tests, the replay process)

    # --- the two listeners: they run inside input callbacks / a frame, so they only queue

    def observe_input(self, kind, *args):
        """input_handler observer: `kind` and `args` are the input tap's."""
        if kind == "move":
            x, y = args
            if self._last_move is not None:
                last_x, last_y = self._last_move
                if abs(x - last_x) < self._move_min_px and abs(y - last_y) < self._move_min_px:
                    return
            self._last_move = (x, y)
        elif kind in ("down", "up"):
            if args[0] not in MOUSE_BUTTONS:     # keys arrive as "key" / "char"
                return
        elif kind == "key":
            key, mods = args
            typed = not mods & (GLFW_MOD_CONTROL | GLFW_MOD_ALT | GLFW_MOD_SUPER) and key not in GLFW_NAMED_KEYS
            if typed and not self._record_text:
                return
        elif kind == "char":
            if not self._record_text:
                return
        elif kind != "change":                   # "axes": a space mouse cannot be replayed
            return
        self._note_window()
        self._put(kind, *args)

    def observe_effect(self, kind, name, draw_state=None, rect=None, text=None):
        """Melty.effect_listeners entry: the button text anchors a replayed click."""
        if self.recording:
            self._put("effect", str(kind), str(name), None if text is None else str(text))

    def _note_window(self):
        if self.window_probe is None:
            return
        window = self.window_probe()
        if window != self._window:
            self._window = window
            self._put("window", *window)

    def _put(self, kind, *args):
        self._queue.put((round(time.monotonic() - self._started, 4), kind, *args))

    def _write(self, path, events):
        """Writer thread: one line per event, flushed whenever the queue runs
        dry so a crash loses at most the burst in flight."""
        try:
            with open(path, "w", encoding="utf-8") as file:
                while True:
                    event = events.get()
                    if event is None:
                        return
                    file.write(json.dumps(event, separators=(",", ":"), default=float) + "\n")
                    if events.empty():
                        file.flush()
        except OSError as error:
            self.error = str(error)

    # --- the folder ------------------------------------------------------------------

    def recordings(self):
        """Finished and running recordings in `folder`, newest first."""
        try:
            paths = [path for path in self.folder.iterdir() if path.name.endswith(SUFFIX)]
        except OSError:
            return []
        return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)

    def prune(self, keep):
        """Delete all but the `keep` newest recordings (never the running one)."""
        for path in self.recordings()[max(int(keep), 0):]:
            if path != self.path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# --- reading -----------------------------------------------------------------------

class InputRecording:
    """A recording file as values: `header` (dict) and `events` (lists)."""

    def __init__(self, header, events):
        self.header = header
        self.events = events

    @classmethod
    def load(cls, path):
        header, events = {}, []
        with open(path, encoding="utf-8") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue                     # the torn last line of a crashed session
                if isinstance(record, dict):
                    header = record
                elif record[1] == "discard_last_press":
                    presses = [index for index, event in enumerate(events) if event[1] == "down"]
                    if presses:
                        del events[presses[-1]:]
                else:
                    events.append(record)
        if header.get("format") != FORMAT:
            raise ValueError(f"{path}: not a {FORMAT} file")
        return cls(header, events)

    @property
    def duration(self):
        return self.events[-1][0] if self.events else 0.0

    def windows(self):
        """{title: (width, height)} - the size each window had when first used."""
        sizes = {}
        for event in self.events:
            if event[1] == "window":
                sizes.setdefault(event[2], (event[3], event[4]))
        return sizes


# --- compiling for a replay ------------------------------------------------------------

@dataclasses.dataclass
class ReplayStep:
    """One action for the desktop MCP server. `args` are the tool's own
    arguments. Coordinates are the recorded content coordinates: the
    compositor's window geometry is the content (the frameless window's
    shadow margin lies outside it), so they are the server's window-local ones."""
    tool: str               # "click" | "drag" | "scroll" | "keys" | "type"
    args: dict
    window: str = None      # title of the OS window it happens in
    gap: float = 0.0        # recorded pause before it, seconds
    text: str = None        # click only: what the pressed control said (OCR anchor)


def readable_text(text):
    """A button label as OCR would read it: icon glyphs (private use area) out."""
    if not text:
        return None
    words = "".join(" " if 0xE000 <= ord(char) <= 0xF8FF else char for char in str(text)).split()
    cleaned = " ".join(words)
    return cleaned if len(cleaned) >= 2 else None


def key_combo(key, mods):
    """GLFW key + modifier mask as the MCP `key` tool's combo ("ctrl+shift+s"),
    or None for a key whose "char" event carries it (plain typing) and for
    bare modifiers."""
    if key in GLFW_MODIFIER_KEYS:
        return None
    command = mods & (GLFW_MOD_CONTROL | GLFW_MOD_ALT | GLFW_MOD_SUPER)
    name = GLFW_NAMED_KEYS.get(key)
    if name is None:
        if not command:
            return None
        name = GLFW_PRINTABLE_NAMES.get(key) or (chr(key).lower() if 32 < key < 127 else None)
        if name is None:
            return None
    held = [label for bit, label in ((GLFW_MOD_CONTROL, "ctrl"), (GLFW_MOD_SHIFT, "shift"),
                                     (GLFW_MOD_ALT, "alt"), (GLFW_MOD_SUPER, "super")) if mods & bit]
    return "+".join([*held, name])


def compile_steps(events):
    """Raw events -> [ReplayStep]. Pure; the order of events is the order of steps.

    Bare cursor travel is dropped (a click or drag carries its own position);
    a press and release within CLICK_MAX_DISTANCE is a click, otherwise a
    drag along the recorded path.
    """
    steps = []
    window = None
    cursor = (0.0, 0.0)
    press = None                # {"button", "t", "start", "path", "text"} while a button is held
    step_end = 0.0              # when the previous step finished, for `gap`

    def local(point):
        return round(point[0], 1), round(point[1], 1)

    def add(tool, args, began, ended, text=None):
        nonlocal step_end
        steps.append(ReplayStep(tool, args, window, round(max(began - step_end, 0.0), 3), text))
        step_end = ended

    for event in events:
        t, kind, *args = event
        if kind == "window":
            window = args[0]
        elif kind == "move":
            cursor = (args[0], args[1])
            if press is not None:
                press["path"].append(cursor)
        elif kind == "down":
            cursor = (args[1], args[2])
            if press is None:       # a second button during a drag is not replayable: ignored
                press = {"button": args[0], "t": t, "start": cursor, "path": [], "text": None}
        elif kind == "up":
            if press is None or args[0] != press["button"]:
                continue            # the release of the click that started the recording
            cursor = (args[1], args[2])
            button = MOUSE_BUTTONS[press["button"]]
            travelled = max((math.dist(press["start"], point) for point in [*press["path"], cursor]),
                            default=0.0)
            if travelled <= CLICK_MAX_DISTANCE:
                x, y = local(press["start"])
                previous = steps[-1] if steps else None
                if (previous is not None and previous.tool == "click" and previous.window == window
                        and previous.args["button"] == button and previous.args["count"] == 1
                        and press["t"] - step_end <= DOUBLE_CLICK_WINDOW
                        and math.dist((previous.args["x"], previous.args["y"]), (x, y)) <= CLICK_MAX_DISTANCE):
                    previous.args["count"] = 2
                    step_end = t
                else:
                    add("click", {"x": x, "y": y, "button": button, "count": 1}, press["t"], t,
                        text=press["text"])
            else:
                x1, y1 = local(press["start"])
                x2, y2 = local(cursor)
                path = press["path"]
                count = min(len(path), DRAG_VIA_POINTS)     # evenly spaced, ending on the last move
                via = [list(local(path[round((index + 1) * len(path) / count) - 1])) for index in range(count)]
                add("drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "button": button,
                             "duration": round(min(max(t - press["t"], 0.05), 30.0), 3), "via": via},
                    press["t"], t)
            press = None
        elif kind == "change":
            axis = "dy" if args[0] == "scroll_y" else "dx"
            units = -args[1]        # GLFW: positive is up / left. MCP scroll: positive is down / right.
            previous = steps[-1] if steps else None
            if previous is not None and previous.tool == "scroll" and t - step_end <= SCROLL_MERGE_WINDOW:
                previous.args[axis] += units
                step_end = t
            else:
                x, y = local(cursor)
                add("scroll", {"x": x, "y": y, "dx": 0, "dy": 0, axis: units}, t, t)
        elif kind == "key":
            combo = key_combo(*args)
            if combo is None:
                continue
            previous = steps[-1] if steps else None
            if previous is not None and previous.tool == "keys" and t - step_end < 1.0:
                previous.args["keys"].append(combo)
                step_end = t
            else:
                add("keys", {"keys": [combo]}, t, t)
        elif kind == "char":
            previous = steps[-1] if steps else None
            if previous is not None and previous.tool == "type" and t - step_end < 1.0:
                previous.args["text"] += chr(args[0])
                step_end = t
            else:
                add("type", {"text": chr(args[0])}, t, t)
        elif kind == "effect" and args[0] == "button":
            # draw_button fires on the press, flat_button on the click: the
            # text belongs to the press in flight, else to the click just made.
            text = readable_text(args[2])
            if press is not None:
                press["text"] = press["text"] or text
            elif steps and steps[-1].tool == "click" and steps[-1].text is None and t - step_end <= 0.5:
                steps[-1].text = text

    return steps
