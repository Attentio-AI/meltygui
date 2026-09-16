from typing import Dict
from meltygui.core.conversion.dict_conversion import DictConversion

class Orchestration(DictConversion):
    """One recorded input take: a stream of raw input events plus the
    undo-stack cues that anchor its replay. Events and cues are PLAIN
    primitive tuples so the lists serialize as data with the app state.

    events: (dt, kind, *args) — dt seconds from record start; kind one of
        "move" (x, y) · "down"/"up" (input_id, x, y) · "change" (input_id,
        value — scroll) · "key" (glfw key, mods — mirrors
        Melty.frame_key_events) · "char" (codepoint — imgui text input).
    cues: (event_index, stack_name, change_kind, display_name, new_repr,
        tile_repr) — recorded whenever an undo stack grew a new GROUP during
        recording: "after replaying event_index events, this change should
        exist." Replay pauses there until the live stack shows a matching
        change (see orchestrator.Orchestrator._cue_satisfied)."""

    def __init__(self):
        super().__init__()
        self.name = "Orchestration"
        # True once the user renamed it by hand - stop_recording's auto-name
        # (from the take's cues) never overwrites a hand-given name.
        self.custom_name = False
        self.tint = (0.719, 0.478, 0.208)
        # When checked, finishing a replay walks the undo stacks back to
        # where they were when the replay started - the app restores itself.
        self.restore_on_finish = False
        self.events = []
        self.cues = []
        # Argument overrides for the generalized commands: str(cue,) ->
        # value. Empty = play reproduces the recording exactly (raw replay);
        # any override routes replay through the command layer (new_value
        # per leaf-edit cue, recorded value unless overridden here).
        self.overrides = {}
        self.duration = 0.0


class Orchestrations(DictConversion):
    """The collection the Orchestrator window manages: id -> Orchestration.
    Selection is UI state and lives on the window's draw_state (the
    `selected_key` auto-state param), never here."""

    def __init__(self):
        super().__init__()
        self.orchestrations: Dict[str, Orchestration] = {}


