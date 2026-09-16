"""Injected state for orchestration views."""
from meltygui.core.dict_conversion import DictConversion


class OrchestratorPanelState(DictConversion):
    """Per-window UI state (injected like TabState): which orchestrations are
    EXPANDED (several at once) and which detail tab each shows — "commands"
    (the generalized view, the default) or "events" (the raw grouped
    stream). Keyed by orchestration id; persisted with the window."""

    def __init__(self):
        super().__init__()
        self.expanded = {}          # orchestration id -> True
        self.tab = {}
