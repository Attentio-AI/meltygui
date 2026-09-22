# Input recording and replay

Record what a user did in a MeltyGUI app; replay it against a fresh instance from
outside, through the `hyprland-desktop` MCP server. Recording needs only
MeltyGUI. Replay needs the MCP server and is run by melty-admin. This replaces new work on the Orchestrator, which
stays in place and untouched.

## Record

| How | When |
|---|---|
| `MELTY_RECORD_INPUT=1` (or `=/path/file.input.jsonl`) | one launch, from the first frame |
| `Toggles.InputRecording.record_on_launch = True` | every launch: crash reproduction, analytics |
| the **Input Recorder** window's Record / Stop | a span inside a session; the Stop click is left out |
| `draw_input_recorder(Melty.input_recorder)` | the same view inside your own app |

Files land in `~/.cache/meltygui/input_recordings/` (newest
`Toggles.InputRecording.keep_recordings` kept). A file is JSON lines, flushed as
it is written, so the session that crashed is on disk up to the crash; the
format is documented at the top of
[input_recording_model.py](../meltygui/model/input_recording_model.py).
`Toggles.InputRecording.record_typed_text = False` keeps typed text out.

## Replay

The front end is melty-admin's **Integration Tests** page and runner
(`melty-admin/docs/INTEGRATION_TESTS.md`): **Record** there opens the editor on
fresh state with `MELTY_RECORD_INPUT` set, and the session becomes the case
`recording.<name>`, replayed on a reserved agent desktop with the suite's
isolation, video and run history. MeltyGUI's part is knowing the recording:

```python
from meltygui.model.input_recording_model import InputRecording, compile_steps
from meltygui.core.automation.input_replay import replay_steps
recording = InputRecording.load(path)
result = await replay_steps(desktop, compile_steps(recording.events), recording.windows())
```

`desktop` is any transport with `find_window(title)` and `call(tool, **arguments)`
(the module docstring has the contract; melty-admin's is
`melty_integration/recorded.py`). `python -m meltygui.core.automation.input_replay
RECORDING` prints the compiled steps and needs no server.

A replay only reproduces a session that starts where the recording started:
launch both on the same document with empty `XDG_STATE_HOME`, `XDG_CACHE_HOME`,
`XDG_CONFIG_HOME` and `XDG_DATA_HOME`, which is what the page's Record does.

How a step is made reliable: each OS window is found by its title and resized to
the recorded content size; a click on a control that published its text
(`Melty.effect_hook("button", ..., text=label)`: `flat_button`, `draw_button`)
is placed by OCR within 120 px of the recorded point, and falls back to the
recorded point when OCR cannot read it. Everything else replays by coordinate.

## Where the code lives

| File | Owns |
|---|---|
| `core/input/input_handler.py` | `add_input_observer`: passive listeners on the one input funnel |
| `core/melty.py` | `Melty.input_recorder`; `Melty.effect_hook` fans out to `effect_listeners` |
| `model/input_recording_model.py` | `InputRecorder` (queue + writer thread), `InputRecording`, `compile_steps` (pure) |
| `view/input_recording_view.py` | `draw_input_recorder(input_value: InputRecorder)` |
| `core/automation/input_recording_core.py` | wiring: window probe, Toggles, record on launch, window registration |
| `core/automation/input_replay.py` | recording -> MCP `batch` steps, sent over any transport; never imported by the app |

## Frame cost

The observer list is empty unless a recording runs: idle cost is one truthiness
check per input event (0.09 µs). While recording, an event costs a tuple and a
`SimpleQueue.put` on the input callback (1.0 µs measured, with at most one cursor
sample per frame: about 0.01 % of the 8.3 ms budget); JSON encoding and file
writes happen on the writer thread. Replay and OCR run in another process.

## Known limits

- Bare cursor travel is not replayed, so hover-only UI (tooltips, hover menus)
  is not reproduced. Space-mouse axes are not recorded.
- Replay coordinates assume the compositor's window geometry is the content
  (true for the native Wayland backend, verified). The GLFW backend is unverified.
- One MCP wheel notch is 1.5 scroll units here (`SCROLL_UNITS_PER_NOTCH`).
- A replay reports only whether every step ran; the end state is for the
  test (or the video) to judge.
