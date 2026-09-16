"""Injected state for account views."""
from meltygui.core.conversion.dict_conversion import DictConversion


class AccountsPanelState(DictConversion):
    """Which per-account panels are open — the usage limits (▾), Ollama's
    model list, the Edit fields — persisted with the window's draw_state
    (injected as `panel_state: AccountsPanelState = None`, the TabState
    pattern) so the window reopens the way it was left. The kinds keep
    toggling the account dicts' runtime keys (`_usage_open`, …); the window
    restores those from here on a fresh store (boot / restart) and mirrors
    them back after every frame. `open` maps "<account id><key>" → True.
    `usage` maps account id → the Claude plan numbers last fetched
    (`AnthropicKind.usage_cache_entry`: rows, summary, fetched_wall, email)
    — restored into a fresh account dict by `restore_usage`, so the panel
    shows last session's bars until the delayed fetch replaces them."""
    PANEL_KEYS = ("_usage_open", "_models_open", "_edit")

    def __init__(self):
        super().__init__()
        self.open = {}
        self.usage = {}
