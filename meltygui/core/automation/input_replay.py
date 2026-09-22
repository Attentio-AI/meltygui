"""Send a compiled input recording to the hyprland-desktop MCP server.

Recording needs nothing but meltygui (model/input_recording_model.py). A replay
drives the app from outside, the way a user would, so the app under test runs
unmodified and at full frame rate. This module is the part of a replay that
knows the recording: which MCP `batch` steps a ReplayStep becomes, and how a run
of them is sent and recovered. It is transport-agnostic: `replay_steps` takes any
`desktop` with

    await desktop.find_window(title) -> address     (waits for the window to open)
    await desktop.call(tool, **arguments) -> the tool's decoded result; for
        `batch`, the report dict ({"ok", "steps_run", ...}) also when ok is false

The front end - the MCP connection, the reserved agent desktop, launching the app
in isolated state, video evidence, run history - is melty-admin's integration
runner (melty_integration/recorded.py), where a recording is one more test case.

A replay is: for each run of steps in one OS window, find that window by its
title, give it the recorded size the first time, and send the run as `batch`
calls. A click whose control published its text is sent as `click_text` (OCR,
searched around the recorded point, so it still lands when the layout shifted);
when OCR does not find the text the recorded coordinates are clicked instead and
the batch resumes.

    python -m meltygui.core.automation.input_replay RECORDING     prints the compiled steps
"""
import argparse
import dataclasses
import json
import math
import sys

from meltygui.model.input_recording_model import InputRecording
from meltygui.model.input_recording_model import ReplayStep
from meltygui.model.input_recording_model import compile_steps

ANCHOR_RADIUS = 120         # px around the recorded point in which OCR looks for a click's text
CURSOR_COVERS = 40          # a cursor this close to the point hides the text from OCR: click the point
MAX_GAP = 2.0               # a recorded pause replays no longer than this, seconds
FIRST_GAP = 30.0            # except the first: the user waited that long for the app to be ready
MIN_GAP = 0.03              # shorter pauses are not worth a batch step
SCROLL_UNITS_PER_NOTCH = 1.5 # one notch of the server's `scroll` arrives in the app as this much scroll_y (measured)
BATCH_LIMIT = 400           # steps per batch call (the server's own limit is 1000)


@dataclasses.dataclass
class ReplayResult:
    ok: bool
    steps: int                      # steps the recording compiled to
    completed: int                  # steps that ran
    anchored: int = 0               # clicks OCR placed
    fallbacks: int = 0              # anchored clicks that fell back to coordinates
    error: str = None
    failed_step: ReplayStep = None


def pointer_after(step, before=None):
    """Where a step leaves the cursor (keys and text leave it where it was)."""
    if step.tool == "drag":
        return step.args["x2"], step.args["y2"]
    if step.tool in ("click", "scroll"):
        return step.args["x"], step.args["y"]
    return before


def batch_steps(step, address, speed=1.0, cursor=None, max_gap=MAX_GAP):
    """One ReplayStep as the MCP `batch` tool's steps (its pause first).
    `cursor` is where the previous step left the pointer: a click right under
    it (a second press of the same button) has its text covered by the cursor
    and a layout that was right a moment ago, so it skips OCR."""
    out = []
    gap = min(step.gap / max(speed, 0.01), max_gap)
    if gap >= MIN_GAP:
        out.append({"sleep": round(gap, 3)})
    args = dict(step.args)
    covered = cursor is not None and step.tool == "click" and \
        math.dist(cursor, (args["x"], args["y"])) <= CURSOR_COVERS
    if step.tool == "click" and step.text and not covered:
        out.append({"tool": "click_text", "args": {
            "text": step.text, "window": address, "button": args["button"], "count": args["count"],
            "x": int(args["x"] - ANCHOR_RADIUS), "y": int(args["y"] - ANCHOR_RADIUS),
            "width": 2 * ANCHOR_RADIUS, "height": 2 * ANCHOR_RADIUS}})
    elif step.tool in ("click", "drag"):
        if step.tool == "drag" and not args["via"]:
            del args["via"]
        out.append({"tool": step.tool, "args": {**args, "window": address}})
    elif step.tool == "scroll":         # `scroll` acts at the cursor and takes no position
        out.append({"tool": "move", "args": {"x": args["x"], "y": args["y"], "window": address}})
        out.append({"tool": "scroll", "args": {axis: round(args[axis] / SCROLL_UNITS_PER_NOTCH) for axis in ("dy", "dx")}})
    elif step.tool == "keys":
        out.append({"keys": list(args["keys"])})
    elif step.tool == "type":
        out.append({"type": args["text"]})
    return out


def window_runs(steps):
    """Consecutive steps in one window, as [(title, [steps])]. Keys and typed
    text go wherever the keyboard focus is: they stay in the current run."""
    runs = []
    for step in steps:
        if runs and (step.window == runs[-1][0] or step.tool in ("keys", "type")):
            runs[-1][1].append(step)
        else:
            runs.append((step.window, [step]))
    return runs


async def replay_steps(desktop, steps, sizes=None, speed=1.0, log=None, cancelled=None, batch_limit=BATCH_LIMIT):
    """Send compiled `steps` through `desktop` (see the module docstring).
    `sizes` is InputRecording.windows(): each window is resized to it once,
    because the recorded coordinates are only right at the recorded size.
    `cancelled()` is asked before every batch (InterruptedError when true), so a
    smaller `batch_limit` cancels sooner and reports progress more often."""
    log = log or (lambda message: None)
    result = ReplayResult(ok=True, steps=len(steps), completed=0)
    sized = set()
    cursor = None               # where the last completed step left the pointer
    for title, run in window_runs(steps):
        try:
            address = await desktop.find_window(title)
            if sizes and title in sizes and address not in sized:
                sized.add(address)
                width, height = sizes[title]
                await desktop.call("place", window=address, width=int(width), height=int(height))
        except RuntimeError as error:
            result.ok, result.error, result.failed_step = False, str(error), run[0]
            return result
        log(f"{title or 'window'} ({address}): {len(run)} steps")
        pending = list(run)
        while pending:
            chunk, owners = [], []          # owners[i]: the ReplayStep batch step i came from
            if cancelled is not None and cancelled():
                raise InterruptedError("Replay cancelled")
            planned = cursor
            for step in pending[:batch_limit]:
                first = step is steps[0]
                for batch_step in batch_steps(step, address, speed, planned, FIRST_GAP if first else MAX_GAP):
                    chunk.append(batch_step)
                    owners.append(step)
                planned = pointer_after(step, planned)
            answer = await desktop.call("batch", steps=chunk, screenshot_after=False)
            report = answer[-1] if isinstance(answer, list) else answer
            # A failed batch stopped at its last run step; everything before it is done.
            failed = None if report.get("ok") else owners[max(report.get("steps_run", 1), 1) - 1]
            done = len(pending[:batch_limit]) if failed is None else \
                next(index for index, step in enumerate(pending) if step is failed)
            ran = len(chunk) if failed is None else next(index for index, owner in enumerate(owners) if owner is failed)
            result.completed += done
            result.anchored += sum(1 for batch_step in chunk[:ran] if batch_step.get("tool") == "click_text")
            for step in pending[:done]:
                cursor = pointer_after(step, cursor)
            if failed is None:
                pending = pending[done:]
                continue
            # An anchored click OCR could not place is clicked at its recorded
            # point instead; then the rest of the run resumes.
            if failed.tool == "click" and failed.text:
                log(f"  OCR did not find {failed.text!r}: clicking the recorded point")
                result.fallbacks += 1
                failed.text = None
                pending = pending[done:]
                continue
            result.ok, result.failed_step = False, failed
            result.error = str(report.get("error") or report)
            return result
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print the replay steps an input recording compiles to.")
    parser.add_argument("recording")
    options = parser.parse_args(argv)
    for step in compile_steps(InputRecording.load(options.recording).events):
        print(json.dumps(dataclasses.asdict(step)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
