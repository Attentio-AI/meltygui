"""Input recording wiring: Melty owns the recorder, this module connects it.

The recorder itself is a model (model/input_recording_model.py) and its window
a plain view (view/input_recording_view.py). What is shared runtime lives here:
which OS window is receiving input, the Toggles a recording starts with, the
record-on-launch switch and the window registration. Replay is a separate
process: core/automation/input_replay.py.
"""
import os

from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.rendering.window_decoration import window
from meltygui.model.input_recording_model import InputRecorder

# Set to a file path to record this launch there, or to 1 for a timestamped
# file in the recordings folder. Same effect as Toggles.InputRecording.record_on_launch.
LAUNCH_VARIABLE = "MELTY_RECORD_INPUT"


def active_window():
    """The recorder's window_probe: the OS window whose input callback or
    frame is running, with its content size (the framebuffer less the
    frameless window's shadow margin). Surface swaps the per-window Melty
    state in for both, so these are plain reads. An app without surfaces
    (the studio) has one window."""
    from meltygui.core.windowing.surface import Surface
    surface = Surface.active
    width, height = Melty.framebuffer_size or (0, 0)
    margin = 2 * Melty.frame_inset
    return (surface.title if surface is not None else None, width - margin, height - margin)


def start_recording(path=None):
    return Melty.input_recorder.start(path, move_min_px=Toggles.InputRecording.move_sample_min_px,
                                      record_text=Toggles.InputRecording.record_typed_text,
                                      keep=Toggles.InputRecording.keep_recordings)


def start_on_launch():
    """runtime/app.py calls this once Melty is up; stop_on_exit when it ends."""
    requested = os.environ.get(LAUNCH_VARIABLE)
    if requested or Toggles.InputRecording.record_on_launch:
        start_recording(requested if requested not in (None, "", "1") else None)


def stop_on_exit():
    Melty.input_recorder.stop()


# Guarded: a hotswap re-exec of this module keeps the live recorder (and a
# recording in progress) instead of replacing it.
if Melty.input_recorder is None:
    Melty.input_recorder = InputRecorder(window_probe=active_window)
    Melty.effect_listeners.append(Melty.input_recorder.observe_effect)

from meltygui.view.input_recording_view import draw_input_recorder
draw_input_recorder = window(input_value=Melty.input_recorder, tint=(0.85, 0.22, 0.25), icon=f'',
                             display_name='Input Recorder',
                             initial={'width': 420, 'height': 320})(draw_input_recorder)
