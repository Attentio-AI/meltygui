"""Input recorder window: start / stop a recording, and list the finished ones."""
import meltygui_imgui as imgui

from meltygui.core.core_render import render_func
from meltygui.core.runtime.toggles import Toggles
from meltygui.model.input_recording_model import InputRecorder
from meltygui.model.input_recording_model import SUFFIX
from meltygui.view.header_view import flat_button


@render_func(is_default_for=InputRecorder, tint=(0.85, 0.22, 0.25), use_cache=True,
             selectable=False, show_bg=False)
def draw_input_recorder(input_value: InputRecorder, draw_state=None):
    button_height = 26                  # the toolbar button; rows size off this
    corner_radius = 5.0
    # [tint=(0.85, 0.22, 0.25)]
    record_color = (0.62, 0.16, 0.18)   # Record, and lit while recording
    rows_shown = 12                     # newest recordings listed
    icon_record = f""             # fa circle
    icon_stop = f""               # fa stop

    changed = False
    if input_value.recording:
        if flat_button(f"{icon_stop}  Stop", draw_state, "input_recorder_stop", height=button_height,
                       color=record_color, corner_radius=corner_radius, tint_value=0.45,
                       max_bg_brightness=0.60):
            # The Stop click is input like any other: tell the file to drop it.
            input_value.stop(discard_last_press=True)
            changed = True
    elif flat_button(f"{icon_record}  Record", draw_state, "input_recorder_start", height=button_height,
                     color=record_color, corner_radius=corner_radius, tint_value=0.20):
        input_value.start(move_min_px=Toggles.InputRecording.move_sample_min_px,
                          record_text=Toggles.InputRecording.record_typed_text,
                          keep=Toggles.InputRecording.keep_recordings)
        changed = True

    if input_value.recording:
        status = f"recording to {input_value.path}"
    elif input_value.error:
        status = f"stopped: {input_value.error}"
    else:
        status = f"recordings: {input_value.folder}"
    imgui.text_disabled(status)

    # The newest finished recordings. Replaying one is melty-admin's job
    # (Integration Tests): it runs outside the app, through the desktop MCP server.
    for path in input_value.recordings()[:rows_shown]:
        if path != input_value.path:
            imgui.text(path.name[:-len(SUFFIX)])
    return changed, input_value
